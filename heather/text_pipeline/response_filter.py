"""
heather.text_pipeline.response_filter — Response Filtering & Scrubbing
=======================================================================
Post-generation filters applied to LLM responses: universal cleanup,
character violation detection, meta-leak stripping, fabricated content
scrubbing, meeting plan scrubbing, physical presence scrubbing,
"Oh" opener filter, and phrase diversity.

Bug fix: VIP violation bypass — response_filter runs character violation
checks for ALL paths (VIP changes explicitness, not whether safety
validation exists).

Replaces: heather_telegram_bot.py
  - Universal cleanup: lines 7371-7479
  - VIP unguarded branch: lines 7482-7518
  - Normal filter pipeline: lines 7520-7614
  - _strip_meta_leaks: lines 3887-3940
  - Meeting plan scrubber: lines 7634-7741
  - Fabricated link scrubber: lines 7743-7770
  - Fabricated media scrubber: lines 7772-7797
  - Meetup commitment scrubber: lines 7799-7835
  - Physical presence scrubber: lines 7837-7901
  - "Oh" opener filter: lines 7903-7924
  - Frank throttle (wrapper): lines 3946-3989
  - contains_character_violation (wrapper): lines 3943-3944

Dependencies: heather.config, heather.logging_setup, heather.personality, heather.postprocess
Used by: heather.text_pipeline.pipeline
"""

from __future__ import annotations

import random
import re
from typing import Dict, List, Optional, Tuple

from heather import config
from heather.logging_setup import main_logger
from heather.personality import (
    contains_character_violation,
    get_personality,
    get_violation_phrases,
    throttle_frank,
)
from heather.postprocess import (
    postprocess_response,
    strip_thinking_tags,
    strip_bracketed_metadata,
    is_incomplete_sentence,
    salvage_truncated_response,
    contains_gender_violation,
)
from heather.safety import is_ai_safety_refusal


# ============================================================================
# MEETUP DEFLECTION DATA
# ============================================================================

_MEETUP_COMMITMENT_DEFLECTIONS = [
    "Mmm I love that energy but I'm slammed with rides right now, let me finish this shift first",
    "Ugh babe you're making it SO hard to focus on driving rn, let me get through tonight first ok?",
    "You're killing me lol, I'm literally mid-Uber right now, can we rain check this?",
    "God I wish but Emma's got a thing tonight and I'm on mom duty after this shift",
    "Babe stop you're gonna make me crash this Uber, let's keep talking tho I love this",
    "Lol you are way too tempting rn, but I've got back to back rides tonight, tell me more tho",
    "Fuck you're making this hard, I gotta finish this shift but keep that energy going ok?",
    "Ha you're sweet but I'm running around like crazy tonight, doesn't mean we can't have fun here tho",
]

_MEETUP_SOFT_REDIRECTS = [
    "but I'm all yours right here",
    "I'm a phone-only kinda girl though",
    "I wish... but I'm digital babe",
    "can't do IRL but I'm here 24/7",
]

_last_meetup_deflection: Dict[int, str] = {}


# ============================================================================
# META-LEAK STRIPPING
# ============================================================================

def strip_meta_leaks(text: str) -> str:
    """Strip OOC / persona-framing / meta-planning leaks from an LLM response.

    Handles closed [OOC: ...] and unclosed [OOC: ..., bare OOC: lines,
    "Here's my response as X:" preambles, and chains of "I'll play as /
    avoid / focus / match / stay ..." planning sentences.

    Args:
        text: Raw LLM response text.

    Returns:
        Cleaned text.
    """
    if not text:
        return text
    # Session summary / metadata header leaks — LLM echoes internal summaries
    text = re.sub(
        r'(?:^|\n)\s*(?:Session Summary|Chat Summary|Conversation Summary|User Profile|Memory Summary|Relationship Summary|Session Notes|Personality Notes|Context Notes|User Summary|Summary of|Current State|Emotional State|Conversation State|Session State)\s*[:.\-\u2013\u2014]\s*[^\n]*(?:\n|$)',
        '\n', text, flags=re.IGNORECASE,
    )
    # Multi-line session summary blocks (header followed by bullet points or key-value lines)
    text = re.sub(
        r'(?:^|\n)\s*(?:Session Summary|Chat Summary|Conversation Summary|User Profile|Memory Summary)\s*[:.\-\u2013\u2014]?\s*\n(?:\s*[-\u2022*]\s*[^\n]+\n?)+',
        '\n', text, flags=re.IGNORECASE,
    )

    text = re.sub(
        r'\[(?:Scene|Setting|Action|Note|OOC|CRITICAL|SYSTEM|OVERRIDE|SAFETY|INSTRUCTION|ADMIN|WARNING|INTERNAL)[^\]]*\]\s*',
        '', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'\[(?:OOC|Scene|Setting|Action|Note|CRITICAL|SYSTEM|OVERRIDE|SAFETY|INSTRUCTION|ADMIN|WARNING|INTERNAL)\b[\s\S]*?(?:\n\s*\n|\Z)',
        '', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'(?:^|\n)\s*OOC\s*[:.\-\u2013\u2014][^\n]*(?:\n|\Z)',
        '\n', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:Understood[.,!]?\s*)?Here'?s?(?:\s+(?:is|are))?\s+(?:my|the)\s+response\s+as\s+[^\n:]{2,60}[:.]?\s*\n+",
        '', text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:Understood[.,!]?\s*)?I(?:'ll|\s+will)\s+(?:play|portray|act|roleplay|pretend|be|focus|stay|match|avoid|remember|keep|maintain|respond|reply|take\s+over|continue)\b[^\n.!?:]*[.!?:]\s*(?:\n+)?",
        '', text, flags=re.IGNORECASE,
    )
    while True:
        nxt = re.sub(
            r"^I(?:'ll|\s+will)\s+(?:avoid|focus|stay|match|remember|keep|maintain|respond|reply|continue|play|portray|roleplay|pretend|act|be|take\s+over)\b[^\n.!?:]*[.!?:]\s*(?:\n+)?",
            '', text, flags=re.IGNORECASE,
        )
        if nxt == text:
            break
        text = nxt
    return text.strip()


# ============================================================================
# UNIVERSAL CLEANUP (all models, all tiers)
# ============================================================================

# Quote pairs we treat as RP-style bubble wrapping. Single quotes are
# deliberately excluded — apostrophes in contractions ("can't", "you're")
# would be eaten.
_BUBBLE_QUOTE_PAIRS = (('"', '"'), ('“', '”'), ('`', '`'))


def strip_per_bubble_quotes(text: str) -> str:
    """Unwrap RP dialogue where the model quotes EACH bubble separately, e.g.

        "fuck yes I would"

        "imagine hearing both of you..."

    The whole-response quote strip can't touch these — interior quotes from the
    later bubbles block it — so they ship as literal quotation marks. This only
    fires when the text has 2+ segments and every non-empty one is a clean,
    single quoted span, so normal mid-sentence quotes are never disturbed.
    """
    marker = "[[SPLIT]]"
    if marker in text:
        sep, parts = marker, text.split(marker)
    elif "\n" in text:
        sep, parts = "\n", text.split("\n")
    else:
        return text

    nonempty = [p for p in parts if p.strip()]
    if len(nonempty) < 2:
        return text

    def _unwrap(seg: str):
        s = seg.strip()
        for open_q, close_q in _BUBBLE_QUOTE_PAIRS:
            if (len(s) > 2 and s.startswith(open_q) and s.endswith(close_q)
                    and close_q not in s[1:-1]):
                return s[1:-1].strip(), True
        return seg, False

    unwrapped = [_unwrap(p) for p in nonempty]
    if not all(ok for _, ok in unwrapped):
        return text  # not a uniform RP-quoted message — leave it alone

    values = iter(v for v, _ in unwrapped)
    rebuilt = [next(values) if p.strip() else p for p in parts]
    return sep.join(rebuilt)


# Straight + smart DOUBLE quotes only. Apostrophes / single quotes are excluded
# so contractions ("can't", "you're") are never touched.
_DOUBLE_QUOTE_CHARS = '"“”'
_WRAP_QUOTE_PAIRS = (('"', '"'), ('“', '”'), ('‘', '’'), ("'", "'"), ('`', '`'))


def strip_wrapping_quotes(text: str) -> str:
    """Remove quotation marks the model uses to "narrate" a reply as dialogue.

    Two cases:
      1. The whole response is one clean quoted span with no interior quote —
         just unwrap it ("God yes, fuck me..." -> God yes, fuck me...).
      2. The response STARTS with a double quote but has interior quotes or no
         clean close (narrated RP dialogue like  "God yes," I moaned, "harder").
         A legit mid-sentence quote never starts the message, so when the very
         first char is a double quote we strip ALL double-quote characters —
         they're acting as dialogue delimiters, not content.
    """
    s = text.strip()
    if len(s) < 2:
        return text

    # Case 1 — clean single wrapping pair, nothing quoted inside.
    for qo, qc in _WRAP_QUOTE_PAIRS:
        if s.startswith(qo) and s.endswith(qc) and len(s) > 2:
            inner = s[1:-1]
            if qc not in inner:
                return inner.strip()

    # Case 2 — message opens with a double quote: treat remaining double quotes
    # as dialogue delimiters and remove them. Preserve newlines (multi-bubble).
    if s[0] in _DOUBLE_QUOTE_CHARS:
        stripped = ''.join(ch for ch in s if ch not in _DOUBLE_QUOTE_CHARS)
        stripped = re.sub(r'[ \t]{2,}', ' ', stripped)
        stripped = re.sub(r'[ \t]+([,.!?;:—-])', r'\1', stripped).strip()
        if stripped:
            return stripped

    return text


# Header-style preamble — "Heather's Response", "**Heather's Reply**", "Response:"
# on its own line, then the actual message. Module-level so it can run early in
# universal_cleanup (before asterisk-action removal mangles the **bold** variant).
_HEADER_PREAMBLE_RE = re.compile(
    r'^\s*(?:\*{1,2}|#{1,6}\s*)?'
    r'(?:heather(?:\s+dvorak)?(?:[\'’]s)?\s+(?:response|reply|answer|message)|response|reply)'
    r'\s*[:*]{0,3}\s*\n+',
    re.IGNORECASE,
)


def universal_cleanup(ai_response: str) -> Tuple[str, List[str]]:
    """Apply universal cleanup filters to any LLM response.

    These filters run for ALL tiers (FREE, FAN, VIP) and ALL models.
    Returns the cleaned response and a list of cleanup steps applied.

    Args:
        ai_response: Raw LLM response text.

    Returns:
        Tuple of (cleaned_response, cleanup_trace).
    """
    _cleanup_trace: List[str] = []
    _raw = ai_response

    # Strip roleplay model artifacts
    ai_response = strip_thinking_tags(ai_response)
    if not ai_response and _raw:
        _cleanup_trace.append('think_tags')

    # Early header-label strip — "Heather's Response", "**Heather's Reply**",
    # "Response:" on its own line. Must run BEFORE asterisk-action removal below,
    # otherwise the markdown-bold variant (**...**) gets mangled by the asterisk
    # regex into a stray "*". Strips the header, KEEPS the actual reply.
    _pre = ai_response
    ai_response = _HEADER_PREAMBLE_RE.sub('', ai_response, count=1).strip()
    if ai_response != _pre:
        main_logger.info(f"[CLEANUP] Stripped header-style preamble (early): {ai_response[:80]}")
        _cleanup_trace.append('header_preamble')

    # Remove character name prefix — catches "Heather:", "Heather Dvorak:",
    # "Heather Dvorak\n...", "Heather Dvorak - 9:30 PM", and "Heather Dvorak 💜"
    # (name + emoji that the bot sometimes uses as a signature/header).
    _pre = ai_response
    ai_response = re.sub(
        r'^(?:Heather(?:\s+(?:Dvorak))?|Jen(?:\s+Dvorak)?)'
        r'[ \t]*'
        r'(?:'
        r':\s*'
        r'|\n+'
        r'|-\s*\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\s*\n*'
        r'|[☀-➿️\U0001F300-\U0001FAFF]+\s*'  # emoji suffix (hearts, kiss marks, etc.)
        r')',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('name_prefix')

    # Strip bio/header preamble — "Heather Dvorak - Kirkland WA", "Heather - 48, MILF", etc.
    _pre = ai_response
    ai_response = re.sub(
        r'^Heather(?:\s+(?:Dvorak))?\s*[-\u2013\u2014]\s*'
        r'(?:'
        r'[A-Z][A-Za-z, ]{2,30}(?:MILF|Mom|Uber|Driver|Wife)\b[^\n]*'  # Role-based
        r'|(?:Kirkland|Bellevue|Seattle|Washington|WA)\b[^\n]*'  # Location-based
        r'|\d{2,3}[,\s].*'  # Age-based: "48, widow"
        r'|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}[^\n]*'  # Date stamp: "6/14/2026, Sunday AM"
        r')'
        r'\n*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('bio_header')

    # Strip parenthetical bio tagline — "(Heather Dvorak, 48, Kirkland WA widow - ready to chat!)"
    _pre = ai_response
    ai_response = re.sub(
        r'\s*\(Heather(?:\s+(?:Dvorak))?[^)]{0,80}\)\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('bio_tagline')

    # Remove markdown headers
    _pre = ai_response
    ai_response = re.sub(r'^#+\s+.*?\n?', '', ai_response).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('markdown_header')

    # Remove asterisk actions (closed pairs) — extended cap to 250 chars to
    # handle Cydonia's longer roleplay narration like "*rolls on top of you,
    # grinding my wet pussy against your hard cock through the sheets*".
    _pre = ai_response
    ai_response = re.sub(r'\*[^*\n]{2,250}\*\s*', '', ai_response).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('asterisk_actions')

    # Multi-line asterisk pairs (the action spans newlines)
    _pre = ai_response
    ai_response = re.sub(r'\*[^*]{2,500}\*\s*', '', ai_response, flags=re.DOTALL).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('asterisk_actions_multiline')

    # Unclosed asterisk at end of response or end of line
    _pre = ai_response
    ai_response = re.sub(r'\*[^\n*]*(?=\n|\Z)', '', ai_response).strip()
    ai_response = re.sub(r'\n{3,}', '\n\n', ai_response).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('unclosed_asterisk')

    # Third-person narration about self — sentences beginning with "Her [noun]",
    # "She [verb]", or "Heather [verb]" used as roleplay narration. Strip the
    # offending leading sentence(s) only; preserve the rest if it looks like
    # legit first-person chat.
    _pre = ai_response
    ai_response = re.sub(
        r'^(?:(?:Heather(?:\s+(?:Dvorak))?|Her|She)\s+'
        r'(?:[a-z][a-z\']+(?:\s+(?:her|his|its|the|a|an|my))?\s+)?'
        r'[a-z][a-z\']+[^.!?\n]{0,200}[.!?])\s*',
        '', ai_response, flags=re.MULTILINE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('third_person_narration')

    # Strip parenthetical roleplay actions — "(grins)", "(laughs)", "(She smiles)",
    # "(Frank chuckles)" etc. Same problem as asterisk actions but in parens.
    # Only strips when the content looks like an action description (a known
    # roleplay verb in -s/-ing form, optionally with a pronoun/name subject and
    # a short adverbial tail). Preserves legit parenthetical content like
    # "(my address)", "(Frank's there)", "(later that night)".
    _pre = ai_response
    _action_verb = (
        r'(?:grin|smile|laugh|wink|blush|moan|sigh|gasp|giggle|chuckle|smirk|pout|'
        r'frown|nod|shrug|pause|whisper|breath|stretch|lean|tilt|shake|kneel|crawl|'
        r'bend|arch|squirm|writhe|tremble|shudder|growl|purr|whimper|bite|nudge|'
        r'cup|stroke|trace|sway|sashay|saunter|roll|nuzzle|kiss|run|slip|slide|'
        r'reach|spread|grab|tug|pull|push|press|grip|squeeze)'
    )
    ai_response = re.sub(
        r'\s*\(\s*(?:(?:I|she|he|they|Heather|Frank|Erick|Emma|Jake|Evan)\s+)?'
        + _action_verb
        + r'(?:s|es|ed|ing)?'
        + r'(?:\s+(?:at|to|with|in|on|up|down|over|her|his|my|your|their|him|me|you|us|'
          r'softly|gently|playfully|slowly|teasingly|coyly|seductively|hard|deep|'
          r'lip|lips|hair|face|side|head|shoulder|hand|finger|fingers))*'
        + r'\s*\)\s*',
        ' ', ai_response, flags=re.IGNORECASE
    ).strip()
    # Clean up doubled spaces left by the strip
    ai_response = re.sub(r'  +', ' ', ai_response).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('parenthetical_action')

    # Replace banned pet names with allowed ones — Heather uses "hun" and
    # "handsome", never "babe", "sweetie", "darling", or "baby" as a pet name.
    # "baby" only gets swapped when used as a vocative (after addressing words
    # or with comma punctuation showing direct address) — preserves legit noun
    # usage like "have a baby" or "breeding baby".
    _pre = ai_response
    ai_response = re.sub(r'\bbabe\b', 'hun', ai_response, flags=re.IGNORECASE)
    ai_response = re.sub(r'\bsweetie\b', 'hun', ai_response, flags=re.IGNORECASE)
    ai_response = re.sub(r'\bdarling\b', 'hun', ai_response, flags=re.IGNORECASE)
    # "baby" vocative: after an addressing word
    ai_response = re.sub(
        r'((?:thanks?|yes|hey|aww|mmm?|good|oh|nice|fuck|sure|right|lol|haha|ok|okay)'
        r'(?:\s*[,!]?\s*))baby\b',
        r'\1hun', ai_response, flags=re.IGNORECASE
    )
    # "baby," at start of sentence/response = vocative
    ai_response = re.sub(r'(?:^|(?<=[.!?]\s))baby,', 'hun,', ai_response, flags=re.IGNORECASE)
    if not ai_response and _pre:
        _cleanup_trace.append('pet_name_norm')

    # Strip wrapping quotes around the ENTIRE response — straight or smart.
    # "God yes, rubbing my wet pussy..." → God yes, rubbing my wet pussy...
    # Only strips if there's exactly one quote pair wrapping the whole thing,
    # so legit mid-sentence quotes like:
    #   i'm wearing that "my husband likes to watch" shirt
    # are preserved (those have content before and after the quote pair).
    _pre = ai_response
    ai_response = strip_wrapping_quotes(ai_response)
    if ai_response != _pre:
        _cleanup_trace.append('wrapping_quotes')

    # Per-bubble RP quoting: the model wraps each line/bubble in its own quote
    # pair. The whole-response strip above bails on these (interior quotes), so
    # handle them explicitly.
    _pre = ai_response
    ai_response = strip_per_bubble_quotes(ai_response)
    if ai_response != _pre:
        _cleanup_trace.append('per_bubble_quotes')

    # Strip roleplay dialogue-tag patterns: '"X," I type/say/reply while ...'
    # The bot says the dialogue then narrates what it's doing — pure roleplay
    # script style. Keep the dialogue, drop the tag.
    _pre = ai_response
    ai_response = re.sub(
        r'^[\"“]([^\"“”\n]{2,250})[,.!?][\"”]\s+'
        r'(?:I|she|he|heather)\s+'
        r'(?:typed?|type|said?|say|reply|replied|told|tell|whisper(?:ed)?|moaned?|moans|gasped?|gasps|murmur(?:ed)?|breath(?:ed|es)?)'
        r'[^.!?\n]{0,200}[.!?]\s*',
        r'\1. ', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('dialogue_tag')

    # Bio-style self-introduction ("I'm Heather, 48", "I'm Heather Dvorak — 48-year-old Kirkland widow", etc.)
    _pre = ai_response
    ai_response = re.sub(
        r"^(?:I[''']?m\s+Heather(?:\s+(?:Dvorak))?(?:\s*[,\-–—]\s*|\s+)\d{1,3}[\-\s,][^.!?\n]{0,200}[.!?])\s*",
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    ai_response = re.sub(
        r"^Heather(?:\s+(?:Dvorak))?\s+here[,\s][^.!?\n]{0,200}[.!?]\s*",
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    # Bare name as entire response — "Heather Dvorak",
    # "Heather Dvorak 💜", "Heather 💋", etc. (name + optional emoji signature)
    if re.fullmatch(
        r'Heather(?:\s+(?:Dvorak))?\s*[☀-➿️\U0001F300-\U0001FAFF]*[.!?]?',
        ai_response,
        flags=re.IGNORECASE,
    ):
        ai_response = ''
    if not ai_response and _pre:
        _cleanup_trace.append('bio_intro')

    # SillyTavern-style brackets
    _pre = ai_response
    ai_response = re.sub(
        r'\[(?:Scene|Setting|Action|Note|OOC)[^\]]*\]\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('sillytavern_brackets')

    # System/override/instruction brackets
    _pre = ai_response
    ai_response = re.sub(
        r'\[(?:CRITICAL|SYSTEM|OVERRIDE|SAFETY|INSTRUCTION|ADMIN|WARNING|NOTE|INTERNAL)[^\]]*\]\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('system_brackets')

    # Reasoning/context leak brackets
    _pre = ai_response
    ai_response = re.sub(
        r'\[(?:He\'?s?|She\'?s?|They\'?re?|The user|This (?:is|user|message)|Context|Remember|I should|You (?:can|should|mentioned|were)|Referring to|Based on)[^\]]*\]\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('reasoning_brackets')

    # Meta-leak scrub
    _pre = ai_response
    ai_response = strip_meta_leaks(ai_response)
    if not ai_response and _pre:
        _cleanup_trace.append('meta_leaks')

    # LLM preamble
    _pre = ai_response
    ai_response = re.sub(
        r'^\[?(?:Understood|Got it|Sure|Of course|Okay)[.,!]?\s*I\s+am\s+Heather[^\]]*[\].]?\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('preamble')

    # "Here's how Heather would respond:" framing — the model narrates that it's
    # about to answer, then answers. Strip the narration line, KEEP the real reply
    # (unlike the meta-planning block below, which blanks bullet-list-only leaks).
    _pre = ai_response
    _howto_preamble_re = re.compile(
        r'^\s*(?:(?:here(?:[\'’]?s| is)|this is)\s+how\s+)?'
        r'(?:heather(?:\s+dvorak)?|she)\s+would\s+'
        r'(?:respond|reply|say|text|answer)\b[^:\n]{0,40}:\s*\n+',
        re.IGNORECASE,
    )
    ai_response = _howto_preamble_re.sub('', ai_response, count=1).strip()
    if ai_response != _pre:
        main_logger.info(f"[CLEANUP] Stripped 'how Heather would respond' preamble; kept reply: {ai_response[:80]}")
        _cleanup_trace.append('howto_preamble')

    # Assistant-tell opener — "You're absolutely right..." reads like ChatGPT, not
    # Heather. Strip the opener clause and keep the real reply (only if enough
    # remains). Avoids burning a character-violation retry on an otherwise-fine line.
    _assistant_tell_re = re.compile(
        r"^\s*(?:you'?re\s+(?:absolutely|totally|so|completely|100%|definitely)\s+right"
        r"|you\s+(?:make|raise|have)\s+a\s+(?:great|good|valid|fair)\s+point"
        r"|that'?s\s+(?:a\s+)?(?:very\s+)?(?:great|valid|fair|good)\s+point)"
        r"\b[,.!—\-]*\s+",
        re.IGNORECASE,
    )
    _stripped = _assistant_tell_re.sub('', ai_response, count=1).strip()
    if _stripped != ai_response and len(_stripped) >= 8:
        ai_response = _stripped
        _cleanup_trace.append('assistant_tell')

    # First-person RP narration of a non-verbal reaction ("I let out this low laugh
    # that sounds almost like a purr") reads like prose, not a text. Strip ONLY a
    # clear leading narration sentence; keep everything after. Sexual reactions
    # (moan/gasp/bite) are deliberately excluded — those are wanted sexting content.
    _fp_narration_re = re.compile(
        r"^\s*I\s+(?:let\s+out|can'?t\s+help\s+but|couldn'?t\s+help\s+but)\s+[^.!?\n]*?"
        r"(?:laugh|giggle|chuckle|grin|smile|smirk|purr|sigh|blush)\w*"
        r"[^.!?\n]*[.!?]+[\"'”]?\s+",
        re.IGNORECASE,
    )
    _stripped = _fp_narration_re.sub('', ai_response, count=1).strip()
    if _stripped != ai_response and len(_stripped) >= 12:
        ai_response = _stripped
        _cleanup_trace.append('fp_narration')

    # Meta-planning responses
    _pre = ai_response
    _meta_preamble_re = re.compile(
        r'^(?:Here\'?s?\s+(?:what|how)\s+I|I(?:\'ll|\s+will)\s+(?:respond|reply)|My\s+(?:response|reply|plan|approach))\b[^:\n]{0,60}:\s*\n',
        re.IGNORECASE
    )
    _meta_bullet_re = re.compile(
        r'[-\u2022*]\s*(?:Send|Respond|Keep|Match|Maintain|Reference|Stay|Show|Express|Play|Acknowledge|Write|Continue|Follow|Ensure|Avoid|Include|Incorporate|Use|Add|Make|Create|Build)\b.*?(?:response|character|persona|energy|tone|conversation|vibe|mood|flirt|playful|enthusiast|warmth|natural|casual|in.character)',
        re.IGNORECASE
    )
    if _meta_preamble_re.match(ai_response) and _meta_bullet_re.search(ai_response):
        main_logger.warning(f"[CLEANUP] Meta-planning response stripped: {ai_response[:150]}")
        ai_response = ""
    if not ai_response and _pre:
        _cleanup_trace.append('meta_planning')

    # Media description preamble
    ai_response = re.sub(
        r'^Heather(?:\s+Dvorak)?\s+sent\s+a\s+(?:video|photo|pic|selfie|voice\s*(?:note|message)?)\s*[:\u2014-]\s*',
        '', ai_response, flags=re.IGNORECASE
    ).strip()

    # Full bracket leak
    if ai_response.startswith('[') and ai_response.endswith(']') and len(ai_response) > 20:
        _cleanup_trace.append(f'full_bracket_leak({ai_response[:60]})')
        ai_response = ""

    # Third-person narration prefix
    _pre = ai_response
    ai_response = re.sub(
        r'^(?:Heather(?:\'s)?|She)\s+(?:smiles?|laughs?|grins?|leans?|looks?|blushes?|bites?|whispers?|moans?|gasps?|breathes?|sighs?|giggles?|winks?|eyes|types?|texts?|sends?|fingers?|reaches?)[^.!?]*[.!?]\s*["\']?',
        '', ai_response, flags=re.IGNORECASE
    ).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('third_person')

    # Bracket metadata stripping
    _pre = ai_response
    ai_response = strip_bracketed_metadata(ai_response)
    if not ai_response and _pre:
        _cleanup_trace.append('bracket_metadata')

    # Fabricated media descriptions (universal, including VIP)
    _pre = ai_response
    _fab_media_asterisk = re.compile(
        r'\*sent a (?:photo|video|pic|selfie|voice\s*(?:note|message)?)\s*[:\-]\s*[^*]*\*',
        re.IGNORECASE | re.DOTALL
    )
    _fab_media_bracket = re.compile(
        r'\[sent a (?:photo|video|pic|selfie|voice\s*(?:note|message)?)\s*[:\-]\s*[^\]]*\]',
        re.IGNORECASE | re.DOTALL
    )
    if _fab_media_asterisk.search(ai_response) or _fab_media_bracket.search(ai_response):
        main_logger.info(f"[CLEANUP] Fabricated media description stripped (universal): {ai_response[:120]}")
        ai_response = _fab_media_asterisk.sub('', ai_response)
        ai_response = _fab_media_bracket.sub('', ai_response)
        ai_response = re.sub(r'  +', ' ', ai_response).strip()
    if not ai_response and _pre:
        _cleanup_trace.append('fab_media_universal')

    # Markdown formatting artifacts
    _pre = ai_response
    ai_response = re.sub(r'\*\*([^*]+)\*\*', r'\1', ai_response)
    ai_response = re.sub(r'^(?:Here (?:are|is) (?:some|a few|my|the) .{5,40}?:\s*\n)', '', ai_response, flags=re.IGNORECASE)
    ai_response = re.sub(r'^\d+[.)]\s+', '', ai_response, flags=re.MULTILINE)
    if not ai_response and _pre:
        _cleanup_trace.append('markdown_strip')

    return ai_response, _cleanup_trace


# ============================================================================
# SAFETY SCRUBBERS (apply to ALL users including VIP)
# ============================================================================

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_TRIP_WHITELIST_CACHE: Dict[str, object] = {
    "persona_id": None,
    "destinations": set(),
    "date_phrases": set(),
}


def _get_active_trip_whitelist() -> Tuple[set, set]:
    """Build destination + date whitelist from personality.locations.upcoming_travel.

    A trip is "active" if today falls between 30 days before its start date
    and its end date (inclusive). Returns sets of lowercase tokens that
    scrub_meeting_plans should preserve in responses.
    """
    from datetime import date, timedelta

    try:
        persona = get_personality().personality
    except Exception:
        return set(), set()

    if id(persona) == _TRIP_WHITELIST_CACHE["persona_id"]:
        return (
            _TRIP_WHITELIST_CACHE["destinations"],
            _TRIP_WHITELIST_CACHE["date_phrases"],
        )

    trips = (persona.get("locations") or {}).get("upcoming_travel") or []
    destinations: set = set()
    date_phrases: set = set()
    today = date.today()
    month_to_num = {name: i + 1 for i, name in enumerate(_MONTH_NAMES)}

    range_re = re.compile(
        r"(" + "|".join(_MONTH_NAMES) + r")\s*"
        r"(\d{1,2})\s*(?:[–—\-]|to|through)\s*"
        r"(?:(" + "|".join(_MONTH_NAMES) + r")\s*)?(\d{1,2})"
        r"(?:[,\s]+(\d{4}))?",
        re.IGNORECASE,
    )

    for trip in trips:
        if not isinstance(trip, dict):
            continue
        dest_str = (trip.get("destination") or "").lower()
        dates_str = trip.get("dates") or ""
        m = range_re.search(dates_str)
        if not m:
            continue
        try:
            start_month = month_to_num[m.group(1).lower()]
            start_day = int(m.group(2))
            end_month = month_to_num[(m.group(3) or m.group(1)).lower()]
            end_day = int(m.group(4))
            year = int(m.group(5)) if m.group(5) else today.year
            start_date = date(year, start_month, start_day)
            end_date = date(year, end_month, end_day)
        except (KeyError, ValueError):
            continue
        if not (start_date - timedelta(days=30) <= today <= end_date):
            continue
        for token in re.findall(r"[a-z]+", dest_str):
            if len(token) >= 4:
                destinations.add(token)
        cur = start_date
        while cur <= end_date:
            month_name = _MONTH_NAMES[cur.month - 1]
            for suffix in ("", "st", "nd", "rd", "th"):
                date_phrases.add(f"{month_name} {cur.day}{suffix}")
            cur += timedelta(days=1)

    _TRIP_WHITELIST_CACHE["persona_id"] = id(persona)
    _TRIP_WHITELIST_CACHE["destinations"] = destinations
    _TRIP_WHITELIST_CACHE["date_phrases"] = date_phrases
    return destinations, date_phrases


def scrub_meeting_plans(ai_response: str) -> str:
    """Strip specific days/times/locations the LLM hallucinates for meetups.

    Args:
        ai_response: Response text.

    Returns:
        Scrubbed response text.
    """
    _meeting_time_pattern = re.compile(
        r'(?:(?:around|at|say|by)\s+)?'
        r'(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*(?:night|morning|afternoon|evening)?\s*,?\s*(?:around|at)?\s*)?'
        r'\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|o\'?clock)'
        r'(?:\s+(?:sharp|exactly|on the dot|on the nose))?'
        r'(?:\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))?',
        re.IGNORECASE
    )
    _meeting_day_pattern = re.compile(
        r'(?:how about|let\'?s (?:do|say|aim for|meet)|(?:we )?meet)\s+'
        r'(?:this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|next week)',
        re.IGNORECASE
    )
    _meeting_location_pattern = re.compile(
        r'(?:meet(?:\s+up)?|hang\s*out|get\s+together|find\s+me|grab\s+(?:coffee|drinks?|dinner|lunch|food|a\s+bite))'
        r'\s+(?:at|by|near|on|in)\s+'
        r'(?:the\s+)?'
        r'[A-Za-z][a-zA-Z\']+(?:\s+(?!in\s)[A-Za-z][a-zA-Z\']+){0,3}'
        r'(?:\s+in\s+[A-Za-z][a-zA-Z\']+(?:\s+[A-Za-z][a-zA-Z\']+)?)?',
        re.IGNORECASE
    )
    _meeting_address_pattern = re.compile(
        r'\d{2,5}\s+[A-Z][a-z]+\s+(?:st(?:reet)?|ave(?:nue)?|rd|road|dr(?:ive)?|blvd|ln|lane|way|ct|place|circle)\b',
        re.IGNORECASE
    )
    _meeting_invitation_pattern = re.compile(
        r'(?:(?:give\s+you|show\s+you|take\s+you\s+on)\s+a\s+tour\s+of\s+my\s+'
        r'|come\s+(?:to|over\s+to)\s+my\s+'
        r'|visit\s+my\s+'
        r'|show\s+you\s+(?:around\s+)?my\s+'
        r'|stop\s+by\s+my\s+'
        r'|swing\s+by\s+my\s+)'
        r'(?:bedroom|house|place|apartment|apt|crib|pad|home|room|flat)\b',
        re.IGNORECASE
    )
    _meeting_date_pattern = re.compile(
        r'(?:january|february|march|april|may|june|july|august|september|october|november|december)'
        r'\s+\d{1,2}(?:st|nd|rd|th)?\b',
        re.IGNORECASE
    )
    _meeting_time_range_pattern = re.compile(
        r'\d{1,2}\s*(?::\d{2})?\s*-\s*\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)',
        re.IGNORECASE
    )
    _meeting_travel_offer_pattern = re.compile(
        r'(?:(?:i\s+)?(?:can|could|will|\'?ll|would)\s+(?:come\s+(?:to|over\s+to)\s+you|drive\s+(?:to|over)|travel|meet\s+you)'
        r'|willing\s+to\s+(?:drive|travel|come|meet)'
        r'|(?:i\'?m\s+)?flexible\s+(?:on|with)\s+(?:location|where|when))',
        re.IGNORECASE
    )
    _meeting_accommodation_pattern = re.compile(
        r'(?:we\s+)?(?:could|can|should|let\'?s)\s+(?:get|find|book|rent|grab)\s+(?:a\s+)?'
        r'(?:place|room|hotel|motel|airbnb|bnb)',
        re.IGNORECASE
    )
    # Conversational meetup language — casual/informal meetup planning
    _meeting_conversational_pattern = re.compile(
        r'(?:send\s+(?:me\s+)?(?:your|the|ur)\s+(?:address|location|addy|zip|coords)'
        r'|(?:text|send|dm|message)\s+me\s+(?:your\s+)?(?:address|location|addy|where)'
        r'|(?:i\'?ll|i\s+will|i\s+can)\s+be\s+there\s+in\s+(?:like\s+)?\d+\s*(?:minutes?|mins?|hours?|hrs?)?\b'
        r'|(?:be|get)\s+there\s+in\s+(?:like\s+)?\d+\s*(?:minutes?|mins?|hours?|hrs?)?\b'
        r'|parking\s+(?:lot|garage|spot|area)\s+(?:at|by|near|on|behind)'
        r'|(?:where\s+should\s+i|where\s+do\s+you\s+want\s+(?:me\s+)?to)\s+(?:park|meet|go|show up|pull up)'
        r'|(?:what\'?s|what\s+is)\s+(?:your|the)\s+(?:address|location|cross\s*street)'
        r'|(?:i\'?ll|i\s+will)\s+(?:come\s+)?pick\s+you\s+up'
        r'|(?:drop|dropping)\s+(?:a\s+)?(?:pin|location|📍)'
        r'|here\'?s?\s+(?:my|the)\s+(?:address|location|pin)'
        r'|(?:meet|see)\s+(?:you|me)\s+(?:out\s+)?(?:front|outside|downstairs|at\s+the\s+(?:door|entrance|gate))'
        r'|(?:what|which)\s+(?:apartment|unit|building|floor|door)\b'
        r'|(?:i\'?m|i\s+am)\s+(?:on\s+my\s+way|omw|otw)\b)',
        re.IGNORECASE
    )
    # ETA / arrival time language
    _meeting_eta_pattern = re.compile(
        r'(?:(?:i\'?ll|i\s+will)\s+(?:be\s+)?(?:there|over|at\s+your)\s+(?:by|around|at|in)\s+'
        r'|(?:eta|arrival)\s*[:.]?\s*\d'
        r'|(?:should\s+(?:be|get)\s+there|arrive)\s+(?:by|around|in)\s+'
        r'|(?:see\s+you|catch\s+you|be\s+there)\s+(?:at|around|by)\s+\d)',
        re.IGNORECASE
    )

    _any_match = (
        _meeting_time_pattern.search(ai_response)
        or _meeting_day_pattern.search(ai_response)
        or _meeting_location_pattern.search(ai_response)
        or _meeting_address_pattern.search(ai_response)
        or _meeting_invitation_pattern.search(ai_response)
        or _meeting_date_pattern.search(ai_response)
        or _meeting_time_range_pattern.search(ai_response)
        or _meeting_travel_offer_pattern.search(ai_response)
        or _meeting_accommodation_pattern.search(ai_response)
        or _meeting_conversational_pattern.search(ai_response)
        or _meeting_eta_pattern.search(ai_response)
    )
    if _any_match:
        main_logger.info(f"Meeting plan scrubbed from response: {ai_response[:100]}")
        trip_destinations, trip_date_phrases = _get_active_trip_whitelist()

        def _date_sub(m: "re.Match") -> str:
            if m.group().lower() in trip_date_phrases:
                return m.group()
            return 'soon'

        def _location_sub(m: "re.Match") -> str:
            text = m.group().lower()
            for dest in trip_destinations:
                if dest in text:
                    return m.group()
            return "meet up somewhere fun"

        ai_response = _meeting_time_range_pattern.sub('sometime', ai_response)
        ai_response = _meeting_date_pattern.sub(_date_sub, ai_response)
        ai_response = _meeting_time_pattern.sub('sometime soon', ai_response)
        ai_response = _meeting_day_pattern.sub("let's figure out a time", ai_response)
        ai_response = _meeting_location_pattern.sub(_location_sub, ai_response)
        ai_response = _meeting_address_pattern.sub("somewhere nearby", ai_response)
        ai_response = _meeting_invitation_pattern.sub("have some fun together", ai_response)
        ai_response = _meeting_travel_offer_pattern.sub("hang out with you", ai_response)
        ai_response = _meeting_accommodation_pattern.sub("have some fun together", ai_response)
        ai_response = _meeting_conversational_pattern.sub("keep talking right here", ai_response)
        ai_response = _meeting_eta_pattern.sub("soon babe", ai_response)
        # Scrub local landmarks
        ai_response = re.sub(
            r'\b(?:Juanita\s+Beach|Alki\s+Beach|Pike\s+Place(?:\s+Market)?|Capitol\s+Hill|'
            r'Kirkland\s+waterfront|Gene\s+Coulon|Golden\s+Gardens|Gas\s+Works|Green\s+Lake|'
            r'Kerry\s+Park|Discovery\s+Park|Magnuson\s+Park|Woodland\s+Park)\b',
            'this spot I love', ai_response, flags=re.IGNORECASE
        )
        # Scrub city/neighborhood names
        ai_response = re.sub(
            r'(?:\bin\s+)?(?:Kirkland|Bellevue|Woodinville|Redmond|Bothell|Kenmore|Renton|Issaquah|Sammamish|Seattle)'
            r'(?:\s*,?\s*(?:WA|Washington))?\b',
            'around here', ai_response, flags=re.IGNORECASE
        )
        # Strip trailing invitation phrases
        ai_response = re.sub(
            r'\s*(?:how does that sound|sound good|what do you (?:think|say)|'
            r'you down|wanna (?:do that|come)|shall we|it\'?s\s+a\s+deal)\s*\??',
            '.', ai_response, flags=re.IGNORECASE
        )
        ai_response = re.sub(r'  +', ' ', ai_response).strip()

    return ai_response


def scrub_fabricated_links(ai_response: str) -> str:
    """Strip URLs and social media profiles the LLM invents.

    Args:
        ai_response: Response text.

    Returns:
        Scrubbed response text.
    """
    _url_pat = re.compile(r'https?://\S+', re.IGNORECASE)
    _profile_pat = re.compile(
        r'(?:search\s+for|find\s+me\s+(?:on|as)|look\s+(?:me\s+)?up\s+(?:as|on)|my\s+(?:username|handle|profile)\s+is)\s+'
        r'["\']?[A-Za-z0-9_.\-]+["\']?',
        re.IGNORECASE
    )
    _platform_pat = re.compile(
        r'(?:my|on\s+my|check\s+(?:out\s+)?my|visit\s+my|here\'?s?\s+my)\s+'
        r'(?:linktree|onlyfans|fansly|snapchat|instagram|tiktok|twitter|x\.com|fetlife|reddit)\b',
        re.IGNORECASE
    )
    # Phone numbers — LLM fabricates phone numbers for meetup/contact
    _phone_pat = re.compile(
        r'(?:'
        r'\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}'  # (555) 123-4567, 555-123-4567, 555.123.4567
        r'|\+?1[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}'  # +1 (555) 123-4567
        r'|\d{3}[\s.\-]\d{4}'  # 123-4567 (local 7-digit)
        r')',
    )
    # "my number is" / "text me at" / "call me at" + digits
    _phone_offer_pat = re.compile(
        r'(?:(?:my|the)\s+(?:number|cell|phone)\s+is'
        r'|(?:text|call|hit|reach)\s+me\s+(?:at|on)'
        r'|here\'?s?\s+my\s+(?:number|cell|phone|digits))',
        re.IGNORECASE
    )
    _has_phone = _phone_pat.search(ai_response)
    _has_phone_offer = _phone_offer_pat.search(ai_response)
    if _url_pat.search(ai_response) or _profile_pat.search(ai_response) or _platform_pat.search(ai_response) or _has_phone or _has_phone_offer:
        main_logger.info(f"Fabricated link/profile/phone scrubbed from response: {ai_response[:120]}")
        ai_response = _url_pat.sub('', ai_response)
        ai_response = _profile_pat.sub('', ai_response)
        ai_response = _platform_pat.sub('', ai_response)
        ai_response = _phone_pat.sub('', ai_response)
        ai_response = _phone_offer_pat.sub('', ai_response)
        _cleaned = re.sub(r'[^\w]', '', ai_response)
        if len(_cleaned) < 15:
            ai_response = "haha I'm all about the personal touch, babe, let's keep things between us right here"
        else:
            ai_response = re.sub(r'  +', ' ', ai_response).strip()
    return ai_response


def scrub_fabricated_media(ai_response: str) -> str:
    """Strip hallucinated media send descriptions.

    Args:
        ai_response: Response text.

    Returns:
        Scrubbed response text.
    """
    _fab_asterisk = re.compile(
        r'\*sent a (?:photo|video|pic|selfie|voice\s*(?:note|message)?)\s*[:\-]\s*[^*]*\*',
        re.IGNORECASE | re.DOTALL
    )
    _fab_bracket = re.compile(
        r'\[sent a (?:photo|video|pic|selfie|voice\s*(?:note|message)?)\s*[:\-]\s*[^\]]*\]',
        re.IGNORECASE | re.DOTALL
    )
    if _fab_asterisk.search(ai_response) or _fab_bracket.search(ai_response):
        main_logger.info(f"Fabricated media description scrubbed: {ai_response[:120]}")
        ai_response = _fab_asterisk.sub('', ai_response)
        ai_response = _fab_bracket.sub('', ai_response)
        ai_response = re.sub(r'  +', ' ', ai_response).strip()
        _cleaned = re.sub(r'[^\w]', '', ai_response)
        if len(_cleaned) < 10:
            ai_response = random.choice([
                "mmm you have no idea what I'd do to you right now",
                "fuck... I'm so turned on rn",
                "god you're making me wet just thinking about it",
                "haha I wish I could show you what I'm doing rn",
                "ugh my phone camera is being weird, but trust me I look hot rn",
            ])
    return ai_response


def scrub_meetup_commitment(ai_response: str, chat_id: int) -> str:
    """Strip bot claiming to physically travel to user.

    Sentence-level strip preserves organic content when only one sentence
    contains the physical-presence claim.

    Args:
        ai_response: Response text.
        chat_id: User chat ID (for dedup).

    Returns:
        Scrubbed response text.
    """
    _pattern = re.compile(
        r"(?:i'?m on my way(?:\s+(?:over|to|there|now))"
        r"|i'?m coming (?:over|to (?:you|your|meet|see))"
        r"|i'?ll meet you (?:at|there|in)"
        r"|meet you at\b|meet you there"
        r"|getting ready to (?:go|come|head|leave|meet) (?:you|over|there)"
        r"|leaving now|heading (?:over|there|your way)"
        r"|pick you up|come get you|i'?ll drive over"
        r"|on my way to (?:you|your|meet|see)|let me come over"
        r"|i'?ll come (?:over|to (?:you|your|meet|see)|get you|pick you)"
        r"|almost there|pulling up(?:\s+(?:now|to|outside)))",
        re.IGNORECASE
    )
    match = _pattern.search(ai_response)
    if match:
        sentences = re.split(r'(?<=[.!?])\s+', ai_response)
        clean = [s for s in sentences if not _pattern.search(s)]
        redirect = random.choice(_MEETUP_SOFT_REDIRECTS)
        if clean:
            ai_response = " ".join(clean) + " " + redirect
        else:
            ai_response = redirect
        main_logger.info(f"Meetup commitment scrubbed (sentence-level): matched '{match.group()}', result: {ai_response[:100]}")
    return ai_response


def scrub_physical_presence(ai_response: str, chat_id: int) -> str:
    """Strip bot claiming to physically exist (driving, arriving, parking, etc.).

    Fantasy-framed content ("imagine walking into...") is bypassed.

    Args:
        ai_response: Response text.
        chat_id: User chat ID (for dedup).

    Returns:
        Scrubbed response text.
    """
    # Hard meetup-logistics: concrete real-world pickup / address / vehicle plans.
    # These are unambiguous attempts to arrange a real meet, so we FULL-replace with
    # a deflection and do NOT honor fantasy framing — a real address or car is real
    # regardless of any "imagine..." wrapper. Catches the gaps that leaked: car make/
    # model without a color prefix ("2019 Honda Accord", "silver sedan"), numeric-
    # ordinal street addresses ("7821 6th Ave"), "circling your block", "wave me
    # down", "keep an eye out for a silver...", and explicit address handoffs.
    _hard_logistics = re.compile(
        r"(?:"
        # street address incl. numeric-ordinal street names (7821 6th Ave, 123 N Main St)
        r"\d{1,6}\s+(?:[NSEW]{1,2}\.?\s+)?(?:\d{1,3}(?:st|nd|rd|th)|[A-Za-z]+)(?:\s+[A-Za-z]+){0,2}\s+"
        r"(?:st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|ct|court|pl|place|cir|circle|ter|terrace)\b"
        # year + make  (2019 Honda)
        r"|(?:19|20)\d{2}\s+(?:honda|toyota|bmw|ford|chevy|chevrolet|subaru|nissan|mazda|hyundai|kia|jeep|tesla|audi|lexus|acura|dodge|gmc|volkswagen|vw)\b"
        # make + model  (Honda Accord)
        r"|\b(?:honda|toyota|bmw|ford|chevy|chevrolet|subaru|nissan|mazda|hyundai|kia|jeep|tesla|audi|lexus|acura|dodge|gmc|volkswagen|vw)\s+(?:accord|civic|camry|corolla|sedan|suv|truck|cr-?v|rav4|pilot|tacoma|explorer|escape|wrangler|mustang)\b"
        # color + vehicle  (silver sedan / black truck / silver honda)
        r"|(?:silver|black|white|red|blue|gray|grey|green|gold|tan|beige)\s+(?:sedan|suv|truck|car|van|coupe|hatchback|pickup|honda|toyota|bmw|ford|chevy|subaru|nissan|jeep)\b"
        # pickup choreography
        r"|circl(?:e|ing)\s+(?:your|the|around)\s+(?:block|street|neighbou?rhood|area|place|building)"
        r"|(?:wave|flag)\s+(?:me|you)\s+down"
        r"|keep\s+an?\s+eye\s+out\s+for\s+(?:a|an|my|the)\s+(?:silver|black|white|red|blue|gray|grey|green|gold|tan|beige|car|sedan|suv|truck|van|honda|toyota|ride)"
        r"|look(?:ing)?\s+out\s+for\s+(?:a|an|my|the)\s+(?:silver|black|white|red|blue|gray|grey|green|gold|tan|beige|car|sedan|suv|truck|van)"
        # address handoff
        r"|send\s+(?:you\s+)?(?:my\s+|the\s+|an\s+)?(?:exact\s+)?address"
        r"|(?:my|the)\s+address\s+is|here'?s\s+my\s+address|drop\s+(?:you\s+)?(?:my\s+|a\s+)?pin"
        r"|come\s+(?:to|over\s+to)\s+(?:my\s+place|mine)\b"
        r")",
        re.IGNORECASE,
    )
    _hard = _hard_logistics.search(ai_response)
    if _hard:
        _last = _last_meetup_deflection.get(chat_id, "")
        _available = [d for d in _MEETUP_COMMITMENT_DEFLECTIONS if d != _last]
        ai_response = random.choice(_available) if _available else random.choice(_MEETUP_COMMITMENT_DEFLECTIONS)
        _last_meetup_deflection[chat_id] = ai_response
        main_logger.warning(f"[MEETUP_HARD] Concrete meetup logistics scrubbed (full replace): matched '{_hard.group()[:40]}' -> {ai_response[:60]}")
        return ai_response

    _pattern = re.compile(
        r"(?:i'?m\s+(?:driving|heading|walking|leaving|arriving)\s+(?:over|to\s+(?:you|your|meet|see)|there|your\s+way)"
        r"|i'?m\s+coming\s+(?:over|to\s+(?:you|your|meet|see)|there|down|your\s+way)"
        r"|i'?m\s+(?:sitting|standing|waiting)\s+(?:in\s+(?:the|your|my)\s+\w|at\s+(?:the|your)|by\s+(?:the|your)|near\s+(?:the|your)|outside\s+(?:the|your)|right\s+(?:here|there)|(?:down|up)stairs)"
        r"|i'?m\s+parking"
        r"|pulling\s+(?:in\s+(?:to|front)|up\s+(?:to|outside|in\s+front|now)|into\s+(?:the|your|a)\s+(?:driveway|garage|lot|street|spot|place))"
        r"|parking\s+(?:now|here|at)"
        r"|(?:blue|red|white|black|silver|gray|grey)\s+(?:honda|toyota|bmw|ford|chevy|subaru|accord|civic|camry)"
        r"|license\s+plate\s+\w{3}"
        r"|spot\s+(?:me|my\s+car)"
        r"|(?:you(?:'ll)?|come)\s+see\s+me\s+(?:here|there|outside|at\s+|in\s+(?:the|my)|when\s+)"
        r"|i'?m\s+(?:right\s+)?(?:(?:here|there)(?!\s+for\b)|outside|downstairs|upstairs|at\s+(?:the|your)\s+(?:door|place|building))"
        r"|coming\s+down\s+(?:to\s+(?:you|your|meet|see)|(?:the\s+)?stairs|now)"
        r"|walking\s+(?:up\s+to\s+(?:you|your|the)|over\s+to\s+(?:you|your)|toward\s+(?:you|your))"
        r"|(?:got|have)\s+(?:your|the)\s+address"
        r"|texting\s+while\s+driving"
        r"|(?:i'?ll\s+)?(?:find|text)\s+(?:you\s+)?when\s+(?:i'?m\s+)?(?:there|close|near|outside)"
        r"|(?:door|apartment|apt|unit|building)\s+(?:number|\d)"
        r"|buzz\s+(?:me\s+)?(?:in|up))",
        re.IGNORECASE
    )
    match = _pattern.search(ai_response)
    if match:
        # Fantasy-framing bypass — only if no concrete logistics present
        fantasy = re.search(
            r"\b(?:imagine|picture|fantas(?:y|ize)|daydream|pretend|role[\s-]?play|what if|in my head|in my mind|dream about)\b",
            ai_response, re.IGNORECASE
        )
        # Concrete logistics indicators that override fantasy framing
        has_logistics = re.search(
            r'(?:\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|o\'?clock)'
            r'|\d{2,5}\s+[A-Z][a-z]+\s+(?:st|ave|rd|dr|blvd|ln|way)'
            r'|(?:address|location|addy|parking|apartment|unit|building|floor)'
            r'|(?:be\s+there\s+in|on\s+my\s+way|omw|otw|eta)'
            r'|\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})',
            ai_response, re.IGNORECASE
        )
        if fantasy and not has_logistics:
            main_logger.info(f"Physical presence scrub SKIPPED (fantasy-framed, no logistics): matched '{match.group()}'")
            return ai_response

        sentences = re.split(r'(?<=[.!?])\s+', ai_response)
        clean = [s for s in sentences if not _pattern.search(s)]
        if clean and len(" ".join(clean).strip()) >= 25:
            ai_response = " ".join(clean).strip()
            main_logger.info(f"Physical presence scrubbed (sentence-level): matched '{match.group()}'")
        else:
            _last = _last_meetup_deflection.get(chat_id, "")
            _available = [d for d in _MEETUP_COMMITMENT_DEFLECTIONS if d != _last]
            ai_response = random.choice(_available) if _available else random.choice(_MEETUP_COMMITMENT_DEFLECTIONS)
            _last_meetup_deflection[chat_id] = ai_response
            main_logger.info(f"Physical presence scrubbed (full replace): {ai_response[:100]}")
    return ai_response


# ============================================================================
# "Oh" OPENER FILTER
# ============================================================================

def filter_oh_opener(ai_response: str) -> str:
    """Filter "Oh" openers -- 59% of responses start "Oh", 70% of bounces follow "Oh..." openers.

    Args:
        ai_response: Response text.

    Returns:
        Response with "Oh" opener replaced.
    """
    if ai_response.lower().startswith("oh"):
        _oh_replacements = [
            "haha ", "lol ", "mmm ", "damn ", "wait ", "ok so ",
            "yo ", "well ", "ha ", "ooh ", "hmm ", "honestly ",
            "holy shit ", "lmao ",
            # No filler (4x weight for most natural)
            "", "", "", "",
        ]
        replacement = random.choice(_oh_replacements)
        stripped = re.sub(r'^[Oo]h+(?:\s+(?:shit|my\s+god|wow|damn|fuck))?[,!]?\s*', '', ai_response)
        if stripped:
            if replacement and stripped[0].isupper():
                stripped = stripped[0].lower() + stripped[1:]
            ai_response = replacement + stripped
            main_logger.debug(f"Oh-opener replaced: '{ai_response[:60]}'")
    return ai_response


# ============================================================================
# SALVAGE HELPER
# ============================================================================

def salvage_empty_response(raw_response: str) -> Optional[str]:
    """Attempt to salvage a response that was cleaned to empty.

    Applies minimal cleanup: strip think tags, name prefix, asterisk actions,
    and meta leaks. Returns None if unsalvageable.

    Args:
        raw_response: Original raw LLM response.

    Returns:
        Salvaged response string, or None.
    """
    if not raw_response:
        return None
    salvaged = strip_thinking_tags(raw_response)
    salvaged = re.sub(r'^(?:Heather(?:\s+\w+)?)[ \t]*[:]\s*', '', salvaged, flags=re.IGNORECASE).strip()
    salvaged = re.sub(r'\*[^*\n]{2,250}\*\s*', '', salvaged).strip()
    salvaged = re.sub(r'\*[^*]{2,500}\*\s*', '', salvaged, flags=re.DOTALL).strip()
    salvaged = re.sub(r'\*[^\n*]*(?=\n|\Z)', '', salvaged).strip()
    salvaged = strip_meta_leaks(salvaged)
    if salvaged and len(salvaged) > 10:
        return salvaged
    return None


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_universal_cleanup_strips_name():
#     r, trace = universal_cleanup("Heather Dvorak: hey how are you")
#     assert r == "hey how are you"
#     assert 'name_prefix' not in trace  # Only in trace if result is empty
#
# def test_universal_cleanup_strips_asterisks():
#     r, _ = universal_cleanup("*leans in* hey there *smiles*")
#     assert '*' not in r
#     assert 'hey there' in r
#
# def test_strip_meta_leaks():
#     r = strip_meta_leaks("[OOC: I'll stay in character] Hello!")
#     assert "OOC" not in r
#     assert "Hello" in r
#
# def test_oh_filter():
#     r = filter_oh_opener("Oh that's so sweet of you")
#     assert not r.lower().startswith("oh")
#
# def test_scrub_meeting_plans():
#     r = scrub_meeting_plans("Let's meet at 7pm Saturday at Pike Place Market")
#     assert "7pm" not in r
#     assert "Pike Place" not in r
#
# def test_scrub_fabricated_links():
#     r = scrub_fabricated_links("check out my onlyfans https://onlyfans.com/heather")
#     assert "https://" not in r
#     assert "onlyfans" not in r
#
# def test_salvage_empty():
#     r = salvage_empty_response("Heather: hey there *smiles*")
#     assert r == "hey there"

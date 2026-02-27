"""
Response post-processing pipeline for Heather Bot.

Pure text-transformation functions extracted from heather_telegram_bot.py
for testability. No bot state dependencies — only regex and string ops.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def strip_thinking_tags(text: str) -> str:
    """Remove <think>...</think> tags and any unclosed <think> blocks."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<think>.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'</think>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n\s*\n', '\n\n', text.strip())
    return text.strip()


def strip_asterisk_actions(text: str) -> str:
    """Remove *action* roleplay markers."""
    text = re.sub(r'\*[^*]+\*\s*', '', text)
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n +', '\n', text)
    return text.strip()


def fix_glm_sorta_artifact(text: str) -> str:
    """Fix GLM-4.7-Flash replacing 'like' with 'sorta' in wrong contexts.
    e.g. 'guys sorta you' -> 'guys like you', 'I sorta that' -> 'I like that'"""
    # Pattern 1: "sorta" + determiner/pronoun (comparison/simile context)
    text = re.sub(r'\bsorta (a |an |the |me |you |him |her |he |she |it |my |your |his |her |that |this |those |these |what |how |when |where |seeing )', r'like \1', text)
    # Pattern 2: verb + "sorta" (simile context)
    text = re.sub(r'\b(look|looks|looked|sound|sounds|act|acts|acted|feel|feels|felt|seem|seems|taste|tastes|smell|smells) sorta\b', r'\1 like', text)
    # Pattern 3: "I/you/he/she/they/we sorta" + (noun/that/it) — verb "like" context
    text = re.sub(r'\b(I|i|you|he|she|they|we|who) sorta\b', r'\1 like', text)
    # Pattern 4: "guys/men/people sorta you" — comparison context
    text = re.sub(r'\b(guys|men|women|people|someone|anyone|everybody|nobody) sorta\b', r'\1 like', text)
    return text


def strip_bracketed_metadata(text: str) -> str:
    """Remove bracketed context tags and stage directions from responses."""
    # Specific known tags from conversation history
    text = re.sub(r'\[(?:Photo description|Note|Generated image|I sent you a photo)[^\]]*\]', '', text, flags=re.IGNORECASE)
    # LLM-generated stage directions: [Image: ...], [Selfie: ...], [Action: ...], [Voice Note: ...], etc.
    text = re.sub(r'\[(?:Image|Selfie|Photos?|Pics?|Action|Voice\s*(?:message|note)|Video|Sends?|Attach(?:ed)?|Picture)[^\]]*\]', '', text, flags=re.IGNORECASE)
    # Bare [pic] / [photo] / [selfie] tags (LLM shorthand for "I'm sending a pic")
    text = re.sub(r'\[(?:pic|photo|selfie|img|image)\]', '', text, flags=re.IGNORECASE)
    # Steering cue leaks: [CONVERSATION TIP: ...] echoed by LLM
    text = re.sub(r'\[(?:CONVERSATION TIP|CRITICAL RULE|CRITICAL REMINDER|VARIETY REQUIRED)[^\]]*\]', '', text, flags=re.IGNORECASE)
    # /command leaks: LLM echoes slash commands as text
    text = re.sub(r'(?:^|\s)/(?:selfie|rate_mode|voice_on|voice_off)\b', '', text)
    # "thought" prefix leak: LLM reasoning leaking into response
    # Catches: "thought\n...", "thought: The user wants...", "thought — I should..."
    text = re.sub(r'^thought\s*[:—\-\n].*?\n', '', text, flags=re.IGNORECASE | re.DOTALL)
    # If entire response is reasoning (no real content after), strip it all
    if re.match(r'^thought\s*[:—\-]', text, re.IGNORECASE):
        # Strip everything up to the first line that looks like actual dialogue
        lines = text.split('\n')
        kept = []
        past_reasoning = False
        for line in lines:
            if past_reasoning:
                kept.append(line)
            elif not re.match(r'^(?:thought|reasoning|plan|note|internal|consider)', line.strip(), re.IGNORECASE):
                if line.strip():
                    past_reasoning = True
                    kept.append(line)
        text = '\n'.join(kept) if kept else ''
    text = re.sub(r'\n\s*\n', '\n\n', text.strip())
    return text.strip()


def contains_gender_violation(text: str) -> bool:
    """Check if response contains male-gender phrases (Heather is female)."""
    text_lower = text.lower()
    gender_violations = [
        "my cock", "my dick", "my penis", "my hard cock", "my erection",
        "my shaft", "my member", "my bulge", "my boner",
        "i'll slide inside you", "slide inside of you", "slide into you",
        "i'll fuck you", "let me fuck you", "i want to fuck you",
        "i'll pound you", "let me pound you", "i'll thrust",
        "i'm hard", "i'm so hard", "getting hard", "rock hard for you",
        "i'm throbbing", "my throbbing",
        "stroke my", "stroking my", "jacking off", "jerking off",
    ]
    return any(v in text_lower for v in gender_violations)


def is_incomplete_sentence(text: str) -> bool:
    """Check if response appears to be truncated/incomplete."""
    if not text or len(text) < 3:
        return True

    text = text.strip()

    truncation_indicators = [
        text.endswith('...') and len(text) < 20,
        text.endswith(','),
        text.endswith(' and'),
        text.endswith(' but'),
        text.endswith(' or'),
        text.endswith(' the'),
        text.endswith(' a'),
        text.endswith(' to'),
        text.endswith(' I'),
        text.endswith(' you'),
        text.endswith(' my'),
        text.endswith(' your'),
        re.search(r'\w+$', text) and not re.search(r'[.!?😀-🿿]$', text) and len(text) > 100
        and not re.search(r'\b(lol|haha|hahaha|lmao|ok|okay|yeah|yea|nah|babe|hun|baby|omg|tho|tbh|rn|fr|ngl|smh|ugh|hmm|ooh|aww|damn|dude|bruh|sure|right|same|mood|bye|hey|sup|yo|mhm|ikr)\s*$', text, re.IGNORECASE),
    ]

    return any(truncation_indicators)


def salvage_truncated_response(text: str) -> Optional[str]:
    """Try to salvage a truncated response by finding the last complete sentence."""
    if not text or len(text) < 20:
        return None

    # Find the last sentence boundary (. ! ? or emoji followed by space or end)
    matches = list(re.finditer(r'[.!?😀-🿿]\s', text))
    if matches:
        last_boundary = matches[-1].end()
        salvaged = text[:last_boundary].strip()
        if len(salvaged) >= 20:
            logger.info(f"Salvaged truncated response: {len(salvaged)}/{len(text)} chars")
            return salvaged

    # Try end-of-string boundary too
    match = re.search(r'^(.*[.!?😀-🿿])', text, re.DOTALL)
    if match:
        salvaged = match.group(1).strip()
        if len(salvaged) >= 20:
            logger.info(f"Salvaged truncated response (end): {len(salvaged)}/{len(text)} chars")
            return salvaged

    return None


def strip_phantom_photo_claims(text: str) -> str:
    """Remove sentences where the bot claims to have sent/be sending a photo.

    Called when the photo cap is reached, to prevent the bot from saying
    'just sent you a pic!' when no photo was actually delivered.
    Uses broad patterns since we KNOW no photo can be sent.
    """
    phantom_patterns = [
        r'(?:just |i )?sent (?:you )?(?:a |that |the )?(?:pic|photo|selfie|image|one)',
        r'here\'?s? (?:a |my |the )?(?:pic|photo|selfie|one)',
        r'sending (?:you )?(?:a |one |it )?(?:now|rn|over|pic|photo|selfie)',
        r'(?:i\'ll |lemme |let me )send (?:you )?(?:a |one|that|this)',
        r'check (?:your |the )?(?:chat|messages|inbox|dm)',
        r'hope you (?:like|enjoy) (?:what you see|the (?:pic|photo|view))',
        r'there (?:you go|it is)',
        r'like what you see',
        r'(?:it\'?s |that\'?s )?right there (?:on|in)',
        r'(?:just )?took (?:a |this )?(?:quick )?(?:pic|photo|selfie)',
        r'(?:just )?(?:pull(?:ed|ing)|lift(?:ed|ing)|tak(?:e|ing)) (?:my |this |the )?(?:shirt|top|bra|robe)',
        r'(?:spreading|opened?|showing) (?:my |these )?(?:legs|lips)',
        r'look (?:at )?(?:what|how|these)',
    ]
    combined = '|'.join(phantom_patterns)

    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    for sentence in sentences:
        if not re.search(combined, sentence, re.IGNORECASE):
            kept.append(sentence)

    result = ' '.join(kept)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def strip_obvious_phantom_claims(text: str) -> str:
    """Light-touch strip of the most blatant phantom photo claims.

    Called on ALL responses (even when photo cap is NOT reached) to catch cases
    where the LLM says 'just sent you a pic' but no photo was actually attached.
    Only removes unambiguous 'I sent/am sending a photo' claims, not ambiguous
    phrases like 'like what you see' which might be conversational.
    """
    obvious_patterns = [
        r'(?:just |i )?sent (?:you |it )?(?:a |that |the )?(?:pic|photo|selfie|image)',
        r'sending (?:you )?(?:a |one |it )?(?:now|rn|over)',
        r'(?:just )?(?:took|snapped) (?:a |this )?(?:quick )?(?:pic|photo|selfie) (?:and )?sent',
        r'(?:it\'?s |that\'?s )?right there on your (?:screen|phone)',
        r'sent (?:it )?(?:to your|over to)',
    ]
    combined = '|'.join(obvious_patterns)

    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    for sentence in sentences:
        if not re.search(combined, sentence, re.IGNORECASE):
            kept.append(sentence)

    result = ' '.join(kept)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def strip_ai_denial_claims(text: str) -> str:
    """Remove sentences where the bot denies being AI or claims to be a real person.

    After the ethical refactor, Heather is transparent about being AI. This catches
    LLM outputs that contradict the disclosure by claiming to be real/human.
    """
    denial_patterns = [
        r'i\'?m (?:a )?real (?:person|woman|girl|human)',
        r'i\'?m not (?:a |an )?(?:bot|ai|robot|machine|computer|program|algorithm)',
        r'i am not (?:a |an )?(?:bot|ai|robot|machine|computer|program)',
        # Adverb before "not" — catches "I'm definitely not an AI", "I'm absolutely not a bot"
        r'i\'?m (?:definitely|absolutely|certainly|totally|really|honestly) not (?:a |an )?(?:bot|ai|robot|machine|computer|program|algorithm|artificial)',
        r'i am (?:definitely|absolutely|certainly|totally|really|honestly) not (?:a |an )?(?:bot|ai|robot|machine|computer|program|artificial)',
        # "not some AI/bot" variant
        r'i\'?m not some (?:bot|ai|robot|machine|computer|program)',
        # "no AI/bot" — "I'm no bot", "I'm no AI"
        r'i\'?m no (?:bot|ai|robot|machine)',
        r'(?:the |a )?real (?:heather|ubermommy|uber ?slutmommy|me)',
        r'(?:of course|obviously) i\'?m real',
        r'do i (?:look|sound|seem) like (?:a |an )?(?:bot|ai|robot)',
        r'i\'?m (?:100%|totally|definitely|absolutely) (?:real|human|a real)',
        r'last (?:i |time i )?checked i was (?:real|human|a real)',
        r'pinch(?:es)? (?:my)?self',
        # Flesh and blood / physicality claims used to deny AI nature
        r'(?:i\'?m |i am )?(?:all )?flesh and blood',
        r'no (?:wires|circuits|code|programming)\b.*?(?:here|in me|inside)',
        r'nothing artificial about me',
        # Trust-me-I'm-real framing
        r'trust me[,.]? (?:i\'?m|i am) (?:real|human|not (?:a |an )?(?:bot|ai|robot))',
        r'i (?:assure|promise|swear) you i\'?m (?:real|human|not (?:a |an )?(?:bot|ai))',
        # "as real as" — catches "I'm as real as they come", "as real as it gets"
        r'i\'?m as real as',
        r'i am as real as',
        # "no AI here" / "no bot here"
        r'no (?:ai|bot|robot|machine) here',
        # "talking to a real person" / "real person here"
        r'(?:talking|chatting|speaking) (?:to|with) a real (?:person|woman|girl|human)',
        r'real (?:person|woman|girl) here',
        # "there's no AI" / "no AI behind these messages"
        r'(?:there\'?s |there is )?no ai (?:behind|in|driving|running)',
        # "not a chatbot" / "not a chat bot"
        r'i\'?m not (?:a )?chat\s*bot',
        r'i am not (?:a )?chat\s*bot',
        # Promise/guarantee real — "I promise I'm real", "I promise there's no AI"
        r'(?:promise|guarantee|swear) (?:you )?(?:i\'?m|i am|there\'?s) (?:no |not |a )?(?:real|ai|bot)',
        r'i promise you there\'?s no',
    ]
    combined = '|'.join(denial_patterns)

    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    for sentence in sentences:
        if not re.search(combined, sentence, re.IGNORECASE):
            kept.append(sentence)
        else:
            logger.info(f"Stripped AI denial claim: {sentence[:80]}")

    result = ' '.join(kept)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def strip_human_life_claims(text: str) -> str:
    """Remove sentences where the bot claims physical human activities.

    Catches LLM hallucinations of physical presence/activities that contradict
    Heather's disclosed AI nature. Sentence-level removal like strip_phantom_photo_claims().
    """
    human_life_patterns = [
        # Location / movement
        r'(?:just |i )?got (?:back )?home',
        r'(?:just )?(?:got|getting) (?:back )?(?:from|to) (?:the )?(?:store|grocery|mall|gym|work|office|hospital|doctor|salon)',
        r'(?:heading|headed|driving|walking|going) (?:to|over to|back to) (?:the )?(?:store|grocery|mall|gym|work|office)',
        r'(?:i\'?m |i am )(?:at|in) (?:the |my )?(?:store|grocery|mall|gym|office|car|uber)',
        r'on my way (?:to|home|back)',
        # Work / employment
        r'(?:my|the|an?) (?:uber )?shift',
        r'(?:just )?(?:got|getting) off (?:work|my shift)',
        r'(?:heading|going) (?:to|into) work',
        # Domestic / physical activities
        r'(?:i\'?m |i am )?(?:making|cooking|baking|preparing) (?:dinner|lunch|breakfast|food|a meal)',
        r'(?:i\'?m |i am )?(?:taking|in) (?:a |the )?(?:shower|bath|bubble bath)',
        r'(?:i\'?m |i am )?(?:doing|folding) (?:the |my )?(?:laundry|dishes)',
        r'(?:i\'?m |i am )?(?:cleaning|tidying) (?:the |my )?(?:house|kitchen|apartment|place)',
        r'(?:i\'?m |i am )?(?:getting|got) (?:dressed|undressed|changed)',
        # Eating / drinking (physical acts)
        r'(?:i\'?m |i am )?(?:drinking|sipping|having) (?:my |a |some )?(?:coffee|tea|wine|beer|latte|espresso|cocoa|hot chocolate)',
        r'(?:i\'?m |i am )?(?:eating|having|grabbing) (?:my |a |some )?(?:dinner|lunch|breakfast|snack|bite|food|meal)',
        # Physical sensations from activities
        r'(?:so |really )?(?:tired|exhausted|sore|worn out) from (?:work|my shift|the gym|cleaning|running|errands)',
        r'my (?:feet|legs|back|arms) (?:hurt|are? (?:killing|aching|sore))',
    ]
    combined = '|'.join(human_life_patterns)

    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    for sentence in sentences:
        if not re.search(combined, sentence, re.IGNORECASE):
            kept.append(sentence)
        else:
            logger.info(f"Stripped human life claim: {sentence[:80]}")

    result = ' '.join(kept)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def strip_quote_wrapping(text: str) -> str:
    """Strip RP-style quote wrapping from LLM responses.

    Handles straight quotes, curly/smart quotes, and cases where the LLM
    wraps only the beginning (unclosed quote from truncation).
    """
    if not text:
        return text
    # Straight double quotes
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    # Straight single quotes
    elif text.startswith("'") and text.endswith("'"):
        text = text[1:-1]
    # Curly/smart double quotes (U+201C / U+201D)
    elif text.startswith('\u201c') and text.endswith('\u201d'):
        text = text[1:-1]
    elif text.startswith('\u201c') and text.endswith('\u201c'):
        text = text[1:-1]
    # Opening curly quote only (truncated or unclosed)
    elif text.startswith('\u201c') and '\u201d' not in text:
        text = text[1:]
    # Opening straight quote only (truncated)
    elif text.startswith('"') and text.count('"') == 1:
        text = text[1:]
    return text.strip()


AI_SELF_ID_PATTERNS = [
    re.compile(r"\bi'?m an? ai\b", re.IGNORECASE),
    re.compile(r"\bi'?m an? artificial\b", re.IGNORECASE),
    re.compile(r"\bas an ai\b", re.IGNORECASE),
    re.compile(r"\bi'?m not a? ?real\b", re.IGNORECASE),
    re.compile(r"\bi don'?t have a? ?physical body\b", re.IGNORECASE),
    re.compile(r"\bi'?m a language model\b", re.IGNORECASE),
    re.compile(r"\bi'?m a virtual\b", re.IGNORECASE),
    re.compile(r"\bi exist (?:only )?(?:as|in) (?:the )?digital\b", re.IGNORECASE),
]


def strip_unprompted_ai_self_id(text: str) -> str:
    """Remove sentences where the bot volunteers its AI status unprompted.

    The first-message disclosure and reality-check responses handle this.
    Mid-conversation AI self-identification kills the fantasy and should be stripped.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    for sentence in sentences:
        if any(p.search(sentence) for p in AI_SELF_ID_PATTERNS):
            logger.info(f"Stripped unprompted AI self-ID: {sentence[:80]}")
        else:
            kept.append(sentence)

    result = ' '.join(kept)
    result = re.sub(r'  +', ' ', result)
    return result.strip()


def postprocess_response(text: str) -> str:
    """Run all stateless post-processing stages on an LLM response.

    This covers cleanup that doesn't depend on bot state (conversations,
    throttle counters, etc.). State-dependent transforms like diversify_phrases()
    and throttle_frank() remain in the bot module.
    """
    if not text or not text.strip():
        return ""
    text = strip_thinking_tags(text)
    text = strip_asterisk_actions(text)
    text = strip_bracketed_metadata(text)
    text = fix_glm_sorta_artifact(text)
    text = strip_ai_denial_claims(text)
    # strip_human_life_claims removed — with AI disclosure in place,
    # character-flavor like "making coffee" or "just got home" is fine
    text = strip_unprompted_ai_self_id(text)
    text = strip_quote_wrapping(text)
    return text.strip()

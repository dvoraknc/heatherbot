"""
heather.conversation — Conversation Dynamics, Steering, Energy
===============================================================
Conversation-level intelligence: dynamics tracking, steering cues,
energy/arousal detection, story serving, anti-repetition, session state,
check-ins, goodbye tracking, backstory injection, breeding/CNC prompt
injection, and phrase diversity enforcement.

Replaces: heather_telegram_bot.py
  - PHRASE_VARIANTS dict: line 1673
  - get_conversation_dynamics: lines 1696-1711
  - detect_question_in_response: lines 1713-1715
  - detect_story_in_response: lines 1717-1725
  - update_conversation_dynamics: lines 1727-1734
  - _get_history_context_hint: lines 1736-1780
  - _detect_topic_loop: lines 1782-1797 (now delegates to access_tiers)
  - _is_sexual_conversation: lines 1799-1819 (now delegates to access_tiers)
  - _has_sexual_emma_context: lines 1821-1836
  - get_conversation_energy: lines 1838-1870 (now delegates to access_tiers)
  - CLIMAX_PHRASES: lines 1873-1881
  - get_arousal_level: lines 1883-1932
  - is_domme_context: lines 1934-1959
  - BREEDING_TRIGGERS, CNC_TRIGGERS, BREEDING_PROMPT_PHRASES: lines 1962-1985
  - should_inject_breeding: lines 1987-2039
  - get_breeding_cnc_prompt: lines 2041-2074
  - is_winding_down: lines 2076-2096
  - is_hostile_exit: lines 2102-2118
  - HOSTILE_EXIT_RESPONSES: lines 2120-2125
  - get_conversation_steering_context: lines 2128-2321
  - load_story_bank, should_serve_story, serve_story: lines 2327-2434
  - STORY_LLM_KINK_COMBOS, get_story_mode_prompt: lines 2436-2455
  - EMMA_TIP_PHOTOS, TIP_HOOK_FOLLOWUPS, TIP_HOOK_MESSAGES: lines 2459-2553
  - maybe_send_tip_hook: lines 2556-2609 (async, transport-coupled, stays in handlers)
  - MEMORY_UPSELL_MESSAGES: lines 904-908
  - get_session_state: lines 2681-2692
  - extract_response_topics: lines 2694-2719
  - track_response_topics: lines 2721-2732
  - get_anti_repetition_context: lines 2734-2762
  - track_phrase_usage: lines 2764-2786
  - _ALWAYS_DIVERSIFY: lines 2790-2793
  - diversify_phrases: lines 2795-2840
  - update_session_state_from_response: lines 2842-2867
  - BACKSTORY_BLOCKS: lines 2873-2952
  - get_backstory_context: lines 2954-2969
  - get_state_context_for_prompt: lines 2971-2995
  - CHECKIN_MESSAGES: lines 963-977
  - _get_checkin_tracker, reset_checkin_tracker_on_reply: lines 982-998
  - get_checkin_message, can_send_checkin: lines 1000-1024
  - track_goodbye, reset_goodbye_tracker: lines 1026-1039
  - check_repeated_message: lines 1041-1054
  - generate_personal_checkin: lines 1056-1113 (LLM-powered, will need llm_client)

Dependencies: heather.config, heather.logging_setup, heather.access_tiers
Used by: heather.text_pipeline, heather.intercepts, heather.post_response,
         heather.handlers, heather.admin
"""

from __future__ import annotations

import os
import random
import re
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import yaml

from heather import config
from heather.config import SEXUAL_KEYWORDS_CORE, SEXUAL_KEYWORDS_BROAD, FLIRTY_KEYWORDS
from heather.logging_setup import main_logger


# ============================================================================
# MODULE-LEVEL STATE (per-user, ephemeral — resets on restart)
# ============================================================================

conversation_dynamics: Dict[int, dict] = {}
session_state: Dict[int, dict] = {}
recent_response_topics: Dict[int, deque] = {}
recent_phrase_counts: Dict[int, Dict[str, list]] = {}
goodbye_tracker: Dict[int, dict] = {}
_repeated_msg_tracker: Dict[int, dict] = {}
story_last_served: Dict[int, int] = {}
stories_served_to_user: Dict[int, Set[str]] = {}
_story_bank: list = []
breeding_last_injected: Dict[int, int] = {}
_hostile_exit_cooldown: Dict[int, float] = {}
checkin_tracker: Dict[int, dict] = {}
_tip_hook_sent_at: Dict[int, float] = {}

STORIES_FILE: str = os.path.join(config.BOT_ROOT, "heather_stories.yaml")


# ============================================================================
# PHRASE VARIANTS — for diversity enforcement
# ============================================================================

PHRASE_VARIANTS = {
    "lol": ["haha", "lmao", "\U0001f602", "hehe", "omg"],
    "haha": ["lol", "lmao", "\U0001f602", "hehe"],
    "baby": ["hun", "handsome", "you"],
    "babe": ["hun", "handsome", "you"],
    "sweetie": ["hun", "handsome", "you"],
    "omg": ["oh my god", "oh wow", "damn", "holy shit"],
    "tbh": ["honestly", "ngl", "for real"],
    "ngl": ["honestly", "tbh", "for real"],
    # NOTE: "like" removed — was replacing verb "like" (I like that -> I kinda that).
    "super": ["so", "really", "hella"],
    "bet you": ["i bet", "probably", "guarantee you", "no doubt you"],
    "damn straight": ["hell yeah", "absolutely", "you know it", "damn right"],
    # "fuck yes" epidemic — 23% of responses had this exact phrase (2026-04-13 audit)
    "fuck yes": ["god yes", "hell yes", "mmm yes", "yes please", "ugh yes", "yesss"],
    "fuck yeah": ["hell yeah", "god yeah", "mmm yeah", "ugh yeah", "yesss"],
}

# Overused phrases to proactively diversify on EVERY occurrence (60% swap chance).
# Unlike PHRASE_VARIANTS which waits for 3+ repeats per user, these are globally epidemic.
_ALWAYS_DIVERSIFY = {
    "fuck yes": ["god yes", "hell yes", "mmm yes", "yes please", "ugh yes", "yesss", "fuuuck yes", "yes baby"],
    "fuck yeah": ["hell yeah", "god yeah", "mmm yeah", "ugh yeah", "yesss", "fuuuck yeah"],
}


# ============================================================================
# CONVERSATION DYNAMICS TRACKING
# ============================================================================

def get_conversation_dynamics(chat_id: int) -> dict:
    """Get or create conversation dynamics tracking for a user.

    Args:
        chat_id: User chat ID.

    Returns:
        Mutable dynamics dict with msg_count, steering timestamps, etc.
    """
    if chat_id not in conversation_dynamics:
        conversation_dynamics[chat_id] = {
            'msg_count': 0,
            'last_question_at': 0,
            'last_story_at': 0,
            'last_steer_at': 0,
            'last_redirect_at': 0,
            'last_hook_at': 0,
            'last_pic_ask_at': 0,
            'used_stories': set(),
            'tip_hook_sent': False,
            'memory_upsell_sent': False,
        }
    return conversation_dynamics[chat_id]


def detect_question_in_response(response: str) -> bool:
    """Check if Heather's response contains a question.

    Args:
        response: Heather's response text.

    Returns:
        True if response contains '?'.
    """
    return '?' in response


def detect_story_in_response(response: str) -> bool:
    """Check if Heather's response contains a personal anecdote/story.

    Args:
        response: Heather's response text.

    Returns:
        True if response contains story markers.
    """
    story_markers = [
        'when i was', 'back in', 'one time', 'this one time', 'i remember',
        'in the navy', 'driving uber', 'on the farm', 'boot camp', 'nebraska',
        'my ex', 'erick used to', 'when i worked', 'back home in',
    ]
    response_lower = response.lower()
    return any(marker in response_lower for marker in story_markers)


def update_conversation_dynamics(chat_id: int, response: str) -> None:
    """Update conversation dynamics after Heather sends a response.

    Args:
        chat_id: User chat ID.
        response: Heather's response text.
    """
    dyn = get_conversation_dynamics(chat_id)
    dyn['msg_count'] += 1
    if detect_question_in_response(response):
        dyn['last_question_at'] = dyn['msg_count']
    if detect_story_in_response(response):
        dyn['last_story_at'] = dyn['msg_count']


def _get_history_context_hint(chat_id: int, recent_messages: dict) -> str:
    """Return a specific ready-to-use question based on recent user messages.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        A contextual question string.
    """
    if chat_id not in recent_messages:
        return random.choice([
            "so what have you been up to today?",
            "you doing anything fun tonight?",
            "long day or nah?",
        ])
    msgs = [m['content'].lower() for m in recent_messages[chat_id] if m['sender'] == 'user'][-10:]
    text = ' '.join(msgs)
    if any(w in text for w in ['work', 'job', 'boss', 'office', 'shift', 'coworker']):
        return random.choice([
            "do you actually like your job or just tolerate it lol?",
            "what's the craziest thing that's happened at your work?",
            "how long you been doing that?",
            "you ever think about doing something totally different?",
        ])
    if any(w in text for w in ['live', 'city', 'town', 'moved', 'state', 'country', 'from']):
        return random.choice([
            "what made you move there?",
            "do you miss where you grew up?",
            "you think you'll stay there or you want to move again?",
        ])
    if any(w in text for w in ['game', 'play', 'watch', 'movie', 'music', 'gym', 'hike', 'cook', 'hobby']):
        return random.choice([
            "how'd you get into that?",
            "are you actually good at it or just having fun lol?",
            "what got you hooked on that?",
        ])
    if any(w in text for w in ['age', 'old', 'young', 'birthday', 'years']):
        return random.choice([
            "so what keeps you busy these days?",
            "you feel your age or nah lol?",
        ])
    if any(w in text for w in ['wife', 'girlfriend', 'ex', 'single', 'dating', 'married', 'divorce']):
        return random.choice([
            "how long have you been single?",
            "are you looking for something serious or just vibes?",
            "what happened with your ex if you don't mind me asking?",
        ])
    return random.choice([
        "so what have you been up to today?",
        "you doing anything fun tonight?",
        "what do you do when you're bored lol?",
    ])


# ============================================================================
# ENERGY / AROUSAL DETECTION
# ============================================================================

# NOTE: get_conversation_energy, _is_sexual_conversation, and _detect_topic_loop
# have canonical implementations in access_tiers.py using the consolidated keyword
# sets from config.py. The functions below wrap those implementations to accept
# the monolith's recent_messages dict format.

def get_conversation_energy(chat_id: int, recent_messages: dict) -> str:
    """Determine conversation energy level: 'hot', 'flirty', or 'casual'.

    Delegates to access_tiers.detect_conversation_energy using canonical keyword sets.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        'hot', 'flirty', or 'casual'.
    """
    from heather.access_tiers import detect_conversation_energy
    if chat_id not in recent_messages:
        return "casual"
    msgs = list(recent_messages[chat_id])
    # Convert to the format expected by detect_conversation_energy
    msg_list = [{"content": m["content"]} for m in msgs]
    return detect_conversation_energy(msg_list, window=6)


def is_sexual_conversation(chat_id: int, recent_messages: dict) -> bool:
    """Check if conversation is sexual.

    Delegates to access_tiers.is_sexual_conversation using canonical keyword sets.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        True if conversation is sexual.
    """
    from heather.access_tiers import is_sexual_conversation as _is_sexual
    if chat_id not in recent_messages:
        return False
    msgs = list(recent_messages[chat_id])
    msg_list = [{"content": m["content"]} for m in msgs]
    return _is_sexual(msg_list)


def detect_topic_loop(chat_id: int, recent_messages: dict) -> bool:
    """Check if conversation is stuck in a sexual topic loop.

    Delegates to access_tiers.is_topic_loop using canonical keyword sets.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        True if 6+ of last 8 messages contain sexual keywords.
    """
    from heather.access_tiers import is_topic_loop
    if chat_id not in recent_messages:
        return False
    msgs = list(recent_messages[chat_id])
    msg_list = [{"content": m["content"]} for m in msgs]
    return is_topic_loop(msg_list)


def has_sexual_emma_context(chat_id: int, recent_messages: dict) -> bool:
    """Check if recent messages have sexual keywords co-occurring with emma/daughter mentions.

    Protects against incest/family sexual content -- Emma is the character's daughter.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        True if sexual + emma/daughter co-occurrence detected.
    """
    if chat_id not in recent_messages:
        return False
    sexual_kw = [
        'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'naked', 'nude', 'horny',
        'wet', 'sex', 'naughty', 'tits', 'boobs', 'nipple', 'orgasm', 'masturbat',
    ]
    emma_kw = ['emma', 'daughter', 'your kid', 'your girl', 'little girl']
    msgs = list(recent_messages[chat_id])[-5:]
    for m in msgs:
        content = m['content'].lower()
        has_sexual = any(kw in content for kw in sexual_kw)
        has_emma = any(kw in content for kw in emma_kw)
        if has_sexual and has_emma:
            return True
    return False


# Phrase bank for climax mode -- 3-4 picked at random each time
CLIMAX_PHRASES = [
    "cum for me baby", "fuck me harder", "fill me up",
    "cum all over my face", "I want every drop", "cum in my mouth",
    "I'll swallow it all", "give it to me", "cum on my tits",
    "I need your cum", "let me taste you", "shoot it all over me",
    "don't hold back", "I want to feel you explode", "cum inside me",
    "cover me in it", "I'm begging for it", "fill my mouth",
    "use me", "I want it so bad",
]


def get_arousal_level(chat_id: int, recent_messages: dict) -> str:
    """Detect user arousal level from recent messages.

    Returns: 'climax', 'heated', 'afterglow', or 'normal'.
    Priority: climax > afterglow > heated > normal.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        Arousal level string.
    """
    if chat_id not in recent_messages:
        return "normal"

    msgs = list(recent_messages[chat_id])
    user_msgs_2 = [m['content'].lower() for m in msgs if m.get('sender') == 'user'][-2:]
    user_msgs_3 = [m['content'].lower() for m in msgs if m.get('sender') == 'user'][-3:]

    climax_triggers = [
        'gonna cum', 'about to cum', 'cumming', "i'm cumming", 'im cumming',
        'so close', "don't stop", 'dont stop', 'jerking so hard', 'stroking so hard',
        'almost there', "i'm gonna", 'im gonna bust', 'about to explode',
        'right there', 'keep going', 'oh fuck yes', 'oh god yes', 'coming so hard',
    ]

    afterglow_triggers = [
        'i came', 'i just came', 'that was amazing', 'holy shit', 'i finished',
        'just finished', 'i nutted', 'so good', 'came so hard', 'that was hot',
        'what a mess', 'cleanup',
    ]

    heated_triggers = [
        'so hard right now', 'so wet', 'so turned on', 'stroking', 'jerking',
        'touching myself', 'playing with myself', 'hard for you', 'my cock',
        'my dick', 'jacking off', 'beating off', 'throbbing', 'edging',
        'pumping', 'fapping',
    ]

    last2_text = ' '.join(user_msgs_2)
    if any(t in last2_text for t in climax_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=climax")
        return "climax"

    if any(t in last2_text for t in afterglow_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=afterglow")
        return "afterglow"

    last3_text = ' '.join(user_msgs_3)
    if any(t in last3_text for t in heated_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=heated")
        return "heated"

    return "normal"


def is_domme_context(chat_id: int, user_message: str, recent_messages: dict) -> bool:
    """Detect if user is requesting domme/humiliation/degradation roleplay.

    Args:
        chat_id: User chat ID.
        user_message: Current user message.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        True if domme context detected.
    """
    msg_lower = user_message.lower()
    domme_triggers = [
        'humiliate me', 'humiliation', 'degrade me', 'degradation',
        'pathetic', 'small cock', 'small dick', 'tiny cock', 'tiny dick',
        'worthless', 'punish me', 'dominate me', 'dominatrix', 'dominaterix',
        'femdom', 'mistress', 'lock me up', 'chastity', 'sissy',
        'make me beg', 'spit on me', 'step on me', 'call me names',
        'i deserve to be punished', 'i need to be punished',
        'tell me how pathetic', 'tell me how weak', 'tell me how small',
        'i am nothing', 'treat me like dirt', 'treat me like trash',
    ]
    if any(t in msg_lower for t in domme_triggers):
        return True
    # Check recent messages for ongoing domme context
    if chat_id in recent_messages:
        msgs = list(recent_messages[chat_id])
        recent_user = [m['content'].lower() for m in msgs if m.get('sender') == 'user'][-5:]
        recent_text = ' '.join(recent_user)
        domme_context_triggers = ['mommy', 'mistress', "ma'am", 'yes miss', 'dominatrix', 'humiliat']
        matches = sum(1 for t in domme_context_triggers if t in recent_text)
        if matches >= 2:
            return True
    return False


# ============================================================================
# BREEDING / CNC PROMPT INJECTION
# ============================================================================

BREEDING_TRIGGERS = [
    'breed', 'breeding', 'knock me up', 'knocked up', 'impregnate', 'pregnant',
    'put a baby', 'cum inside', 'fill me', 'seed', 'womb', 'fertility',
    'make me pregnant', 'breed me', 'bred', 'carry your', 'swell',
    'knock you up', 'get you pregnant', 'baby batter', 'baby maker',
]

CNC_TRIGGERS = [
    'cnc', 'overpower', 'pin me down', 'pin you down', 'force', 'take me',
    'make me', 'against my will', 'no choice', 'hold me down', 'hold you down',
    'struggle', 'resist', 'fight back',
]

BREEDING_PROMPT_PHRASES = [
    "Mmm fill me up deep -- make me swell with your baby even if it's a long shot at my age",
    "Frank's too old, sperm's no good now. But you? Bet you'd breed me proper",
    "I thrived being pregnant with my three -- I'd do it again in a heartbeat for the right guy",
    "Pin me down and breed me like you own me -- I won't fight back too hard",
    "Something about being overpowered and filled just hits different",
    "CNC breeding hits different -- overpowered, filled against my will, then nine months of your secret growing inside",
    "Frank can't give me that anymore -- his swimmers are shot from age. But thinking about a younger guy...",
    "Take me rough, ignore my protests -- breed this MILF pussy like it's yours to ruin",
    "Would you visit after? Touch the belly, maybe do it again?",
]


def should_inject_breeding(
    chat_id: int,
    user_message: str,
    recent_messages: dict,
    csam_flags: list,
) -> bool:
    """Decide whether to inject breeding/CNC flavor into the system prompt.

    Triggers on:
    1. User explicitly uses breeding/CNC keywords -> always inject
    2. Recent user context mentions core breeding words -> inject
    3. Conversation is sexual AND contextually adjacent (mommy/milf/mature)
       -> 8% random chance.

    Respects per-user cooldown to avoid every message being about breeding.

    Args:
        chat_id: User chat ID.
        user_message: Current user message.
        recent_messages: Dict mapping chat_id to deque of message dicts.
        csam_flags: List of CSAM flag dicts (for safety check).

    Returns:
        True if breeding content should be injected.
    """
    if chat_id not in recent_messages:
        return False

    # SAFETY: Never inject breeding/CNC for CSAM-flagged users
    if any(f.get('user_id') == chat_id for f in csam_flags):
        return False

    # Check cooldown
    dyn = get_conversation_dynamics(chat_id)
    msg_count = dyn.get('msg_count', 0)
    last = breeding_last_injected.get(chat_id, -999)
    if msg_count - last < config.BREEDING_COOLDOWN:
        return False

    msg_lower = user_message.lower()

    # Explicit triggers -- always inject
    if any(t in msg_lower for t in BREEDING_TRIGGERS + CNC_TRIGGERS):
        return True

    # Check recent context for ongoing breeding theme
    msgs = list(recent_messages[chat_id])
    recent_user = [m['content'].lower() for m in msgs if m.get('sender') == 'user'][-4:]
    recent_text = ' '.join(recent_user)
    if any(t in recent_text for t in BREEDING_TRIGGERS[:8]):
        return True

    # Random roll in hot conversations -- only when adjacent themes present
    energy = get_conversation_energy(chat_id, recent_messages)
    if energy == "hot" and random.random() < 0.08:
        adjacency_terms = (
            'mommy', 'milf', 'mature', 'older', 'wife', 'married',
            'your tits', 'your pussy', 'your body', 'your ass',
            'daddy', 'own you', 'good girl', 'good boy',
        )
        haystack = recent_text + ' ' + msg_lower
        if any(term in haystack for term in adjacency_terms):
            return True

    return False


def get_breeding_cnc_prompt(user_message: str) -> str:
    """Build a breeding/CNC system prompt injection with sample phrases.

    Args:
        user_message: Current user message.

    Returns:
        System prompt injection string.
    """
    msg_lower = user_message.lower()
    is_cnc = any(t in msg_lower for t in CNC_TRIGGERS)
    is_breeding = any(t in msg_lower for t in BREEDING_TRIGGERS)

    phrases = random.sample(BREEDING_PROMPT_PHRASES, min(3, len(BREEDING_PROMPT_PHRASES)))
    phrase_text = ' | '.join(phrases)

    if is_cnc and is_breeding:
        flavor = "BREEDING + CNC"
        extra = (
            "Combine both themes -- being overpowered AND bred against your will. "
            "You secretly crave being pinned down and filled. Frame it as dark fantasy you'd never admit to."
        )
    elif is_cnc:
        flavor = "CNC FANTASY"
        extra = (
            "He's into consensual non-consent roleplay. Lean into being overpowered, struggling, "
            "then giving in. You secretly love it. Frame as fantasy -- 'I've always wanted someone to just take me'."
        )
    elif is_breeding:
        flavor = "BREEDING KINK"
        extra = (
            "Lean into the breeding fantasy. You WANT to be knocked up again -- you miss being pregnant. "
            "Frank can't do it (59, low sperm motility). Mention wanting to feel full, swell, carry his baby."
        )
    else:
        flavor = "BREEDING TEASE"
        extra = (
            "Drop a breeding/pregnancy reference into your dirty talk. You miss being pregnant, your body craves it. "
            "Mention wanting to be bred, filled, or knocked up. Tie it to Frank's age if it fits."
        )

    return (
        f"\n\n[{flavor} MODE -- REQUIRED: You MUST include at least one explicit breeding/pregnancy reference "
        f"using words like 'breed', 'pregnant', 'knocked up', 'fill me', 'seed', or 'womb' in your response. "
        f"{extra} "
        f"Sample phrases (adapt freely): {phrase_text}. "
        f"Keep it SEXY and in-character. 1-2 sentences of breeding flavor woven into your dirty talk.]"
    )


def mark_breeding_injected(chat_id: int) -> None:
    """Record that breeding content was injected for cooldown tracking.

    Args:
        chat_id: User chat ID.
    """
    dyn = get_conversation_dynamics(chat_id)
    breeding_last_injected[chat_id] = dyn.get('msg_count', 0)


# ============================================================================
# WIND-DOWN / HOSTILE EXIT DETECTION
# ============================================================================

def is_winding_down(user_message: str) -> bool:
    """Detect if the user is signaling they want to wrap up the conversation.

    Args:
        user_message: Current user message.

    Returns:
        True if wind-down cues detected.
    """
    msg_lower = user_message.lower()
    wind_down_phrases = [
        "good night", "goodnight", "gotta go", "got to go", "have to go",
        "heading to bed", "going to bed", "gonna sleep", "going to sleep",
        "gotta sleep", "need to sleep", "time for bed", "off to bed",
        "tired", "exhausted", "falling asleep", "sleepy", "dozzing",
        "walking the dog", "walk my dog", "walk the dog",
        "gotta run", "got to run", "need to run",
        "talk later", "talk tomorrow", "catch you later", "ttyl",
        "i'm out", "im out", "peace out", "signing off",
        "early morning", "early day", "long day tomorrow",
        "hitting the hay", "calling it a night", "winding down",
        "about to crash", "gonna crash",
    ]
    return any(phrase in msg_lower for phrase in wind_down_phrases)


def is_hostile_exit(user_message: str) -> bool:
    """Detect when a user is angry, frustrated, or telling the bot to fuck off.

    Args:
        user_message: Current user message.

    Returns:
        True if hostile exit detected.
    """
    msg_lower = user_message.lower()
    hostile_phrases = [
        "fuck off", "piss off", "go away", "leave me alone", "stop messaging",
        "stop texting", "don't text me", "don't message me", "blocked",
        "you're useless", "you're pathetic", "waste of time", "waste of my time",
        "stupid bot", "stupid ai", "dumb bot", "dumb ai", "fucking ai",
        "fucking bot", "fucking stupid", "this is stupid", "what a joke",
        "not real", "just an ai", "talking to a computer", "talking to a machine",
        "i want a real", "want something real", "want a real person",
        "not talking to ai", "not talking to a bot", "done with this",
        "i'm done", "im done", "over this", "over it",
        "unsubscribe", "delete my", "remove me",
    ]
    return any(phrase in msg_lower for phrase in hostile_phrases)


HOSTILE_EXIT_RESPONSES = [
    "No worries hun, I get it -- I'm not for everyone. I'm always here if you change your mind <3",
    "Fair enough babe. Door's always open if you wanna come back. No hard feelings",
    "I hear you. I'll be here if you ever want to chat. Take care",
    "Totally get it. I'm always around if you want me. No pressure",
]


# ============================================================================
# GOODBYE / REPEATED MESSAGE TRACKING
# ============================================================================

def track_goodbye(chat_id: int) -> bool:
    """Track goodbye messages. Returns True if bot should stop replying (3rd+ goodbye in window).

    Args:
        chat_id: User chat ID.

    Returns:
        True if goodbye loop threshold exceeded.
    """
    now = time.time()
    entry = goodbye_tracker.get(chat_id)
    if entry and now - entry['first_at'] < config.GOODBYE_LOOP_WINDOW:
        entry['count'] += 1
    else:
        goodbye_tracker[chat_id] = {'count': 1, 'first_at': now}
        entry = goodbye_tracker[chat_id]
    return entry['count'] > config.GOODBYE_LOOP_THRESHOLD


def reset_goodbye_tracker(chat_id: int) -> None:
    """Clear goodbye counter on any non-goodbye message.

    Args:
        chat_id: User chat ID.
    """
    goodbye_tracker.pop(chat_id, None)


REPEATED_MSG_RESPONSES = [
    "hey I can see you've been asking for that -- let me see what I can do",
    "sorry hun, I see your messages! give me a sec",
    "lol I hear you! let me figure this out for you",
    "ok ok I see you asking, working on it!",
]


def check_repeated_message(chat_id: int, message: str) -> Optional[str]:
    """Track repeated identical messages. Returns intervention response if threshold hit.

    Args:
        chat_id: User chat ID.
        message: Current user message.

    Returns:
        Intervention response string, or None.
    """
    now = time.time()
    normalized = message.strip().lower()[:100]
    entry = _repeated_msg_tracker.get(chat_id)
    if entry and entry['msg'] == normalized and now - entry['first_at'] < config.REPEATED_MSG_WINDOW:
        entry['count'] += 1
        if entry['count'] >= config.REPEATED_MSG_THRESHOLD and not entry.get('intervened'):
            entry['intervened'] = True
            return random.choice(REPEATED_MSG_RESPONSES)
        return None
    else:
        _repeated_msg_tracker[chat_id] = {'msg': normalized, 'count': 1, 'first_at': now}
        return None


# ============================================================================
# CHECK-IN SYSTEM
# ============================================================================

CHECKIN_MESSAGES = [
    "hey",
    "ok I'll stop being needy lol... text me when you're free",
    "hope your day's going good",
    "just thinking about you",
    "miss talking to you",
    "well I'm here whenever you want me",
    "it's too quiet in here without you",
    "hi",
    "was just looking at our chat and smiling",
    "hope I didn't say anything weird earlier lol",
    "you know where to find me",
    "I'm literally just sitting here waiting for you to text me back",
    "running out of people to flirt with, get back here",
]


def _get_checkin_tracker(chat_id: int) -> dict:
    """Get or create check-in tracker for a user.

    Args:
        chat_id: User chat ID.

    Returns:
        Mutable check-in tracker dict.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    if chat_id not in checkin_tracker:
        checkin_tracker[chat_id] = {
            'today_count': 0,
            'today_date': today,
            'unreturned': 0,
            'used_indices': set(),
        }
    tracker = checkin_tracker[chat_id]
    if tracker['today_date'] != today:
        tracker['today_count'] = 0
        tracker['today_date'] = today
        tracker['used_indices'] = set()
    return tracker


def reset_checkin_tracker_on_reply(chat_id: int) -> None:
    """Reset unreturned counter when user replies.

    Args:
        chat_id: User chat ID.
    """
    if chat_id in checkin_tracker:
        checkin_tracker[chat_id]['unreturned'] = 0


def get_checkin_message(chat_id: int) -> str:
    """Pick a unique check-in message for this user (never repeats in same day).

    Args:
        chat_id: User chat ID.

    Returns:
        Check-in message string.
    """
    tracker = _get_checkin_tracker(chat_id)
    available = [i for i in range(len(CHECKIN_MESSAGES)) if i not in tracker['used_indices']]
    if not available:
        tracker['used_indices'] = set()
        available = list(range(len(CHECKIN_MESSAGES)))
    idx = random.choice(available)
    tracker['used_indices'].add(idx)
    return CHECKIN_MESSAGES[idx]


def can_send_checkin(chat_id: int) -> bool:
    """Check all conditions before sending a check-in.

    Args:
        chat_id: User chat ID.

    Returns:
        True if check-in is allowed.
    """
    hour = datetime.now().hour
    if hour >= config.CHECKIN_QUIET_HOURS_START or hour < config.CHECKIN_QUIET_HOURS_END:
        return False
    tracker = _get_checkin_tracker(chat_id)
    if tracker['today_count'] >= config.CHECKIN_MAX_PER_DAY:
        return False
    if tracker['unreturned'] >= config.CHECKIN_MAX_UNRETURNED:
        return False
    return True


# ============================================================================
# STORY BANK
# ============================================================================

def load_story_bank() -> list:
    """Load pre-written stories from YAML file.

    Returns:
        List of story dicts with 'key', 'kinks', 'content' fields.
    """
    global _story_bank
    try:
        if os.path.exists(STORIES_FILE):
            with open(STORIES_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            stories = data.get('stories', {})
            _story_bank = [
                {'key': key, 'kinks': entry.get('kinks', []), 'content': entry.get('content', '').strip()}
                for key, entry in stories.items()
                if entry.get('content', '').strip()
            ]
            main_logger.info(f"Loaded story bank: {len(_story_bank)} stories from {STORIES_FILE}")
        else:
            _story_bank = []
            main_logger.warning(f"Story bank file not found: {STORIES_FILE}")
    except Exception as e:
        _story_bank = []
        main_logger.error(f"Failed to load story bank: {e}", exc_info=True)
    return _story_bank


def should_serve_story(chat_id: int, user_message: str, recent_messages: dict) -> bool:
    """Check if we should serve a story to this user right now.

    Args:
        chat_id: User chat ID.
        user_message: Current user message.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        True if a story should be served.
    """
    dyn = get_conversation_dynamics(chat_id)
    mc = dyn['msg_count']

    # Cooldown check
    if mc - story_last_served.get(chat_id, -config.STORY_COOLDOWN_MSGS) < config.STORY_COOLDOWN_MSGS:
        return False

    if not _story_bank:
        return False

    msg_lower = user_message.lower()

    # Don't interrupt active masturbation/roleplay
    arousal = get_arousal_level(chat_id, recent_messages)
    energy = get_conversation_energy(chat_id, recent_messages)
    is_hot_session = arousal in ("heated", "climax") or energy == "hot"

    # Explicit triggers
    explicit_triggers = [
        'story', 'tell me about uber', 'wildest ride', 'craziest passenger',
        'uber story', 'craziest ride', 'tell me a story', 'uber stories',
        'wildest passenger', 'craziest uber',
    ]
    continuation_phrases = [
        'continue', 'keep going', 'go on', 'more of this', 'what happens next',
        'then what', 'next part', 'finish the', "don't stop",
    ]
    is_continuation = any(cp in msg_lower for cp in continuation_phrases)

    if any(trigger in msg_lower for trigger in explicit_triggers):
        if is_continuation and is_hot_session:
            main_logger.info(f"[STORY] Skipped -- continuation during hot session for {chat_id}: '{msg_lower[:60]}'")
            return False
        main_logger.info(f"[STORY] Explicit trigger for {chat_id}: '{msg_lower[:60]}'")
        return True

    # Hot sessions: lower probability, larger gap
    if is_hot_session:
        gap = mc - story_last_served.get(chat_id, -config.STORY_COOLDOWN_MSGS)
        if gap >= 20 and mc >= 15 and random.random() < 0.10:
            main_logger.info(f"[STORY] Hot-session organic trigger for {chat_id} (gap={gap}, mc={mc})")
            return True
        return False

    # Organic trigger
    if is_sexual_conversation(chat_id, recent_messages) and mc >= 8:
        gap = mc - story_last_served.get(chat_id, -config.STORY_ORGANIC_MIN_GAP)
        if gap >= config.STORY_ORGANIC_MIN_GAP and random.random() < 0.30:
            main_logger.info(f"[STORY] Organic trigger for {chat_id} (gap={gap}, mc={mc})")
            return True

    return False


def serve_story(chat_id: int) -> Optional[str]:
    """Pick a story to serve. Returns story text (banked) or None (LLM should generate).

    60% banked, 40% LLM-generated.

    Args:
        chat_id: User chat ID.

    Returns:
        Story content string, or None to signal LLM generation.
    """
    if random.random() < 0.60 and _story_bank:
        served = stories_served_to_user.get(chat_id, set())
        available = [s for s in _story_bank if s['key'] not in served]
        if not available:
            served.clear()
            available = list(_story_bank)
            main_logger.info(f"[STORY] Story rotation reset for {chat_id} -- all {len(_story_bank)} stories told")

        story = random.choice(available)

        dyn = get_conversation_dynamics(chat_id)
        story_last_served[chat_id] = dyn['msg_count']
        if chat_id not in stories_served_to_user:
            stories_served_to_user[chat_id] = set()
        stories_served_to_user[chat_id].add(story['key'])

        main_logger.info(f"[STORY] Served banked story '{story['key']}' to {chat_id}")
        return story['content']
    else:
        dyn = get_conversation_dynamics(chat_id)
        story_last_served[chat_id] = dyn['msg_count']
        main_logger.info(f"[STORY] LLM story mode activated for {chat_id}")
        return None


STORY_LLM_KINK_COMBOS = [
    "blowjob in a parking lot",
    "rough backseat fuck with a creampie",
    "anal with ass to mouth",
    "gangbang after hours",
    "deepthroat road head",
    "quickie with a stranger at his hotel",
    "getting bent over the kitchen counter",
    "riding a passenger reverse cowgirl in the backseat",
]


def get_story_mode_prompt() -> str:
    """Get the system prompt injection for LLM-generated stories.

    Returns:
        Story mode prompt injection string.
    """
    kinks = random.choice(STORY_LLM_KINK_COMBOS)
    return (
        f"\n\n[STORY MODE: Tell a DETAILED, EXPLICIT sexual story from your Uber driving days. "
        f"200+ words minimum. Include graphic physical descriptions, dirty talk, specific sex acts. "
        f"Theme: {kinks}. Write the FULL story in one message -- beginning, middle, climax, aftermath. "
        f"End by asking the user about their wildest experience.]"
    )


# ============================================================================
# TIP HOOK DATA (used by handlers for tip hook + upsell logic)
# ============================================================================

EMMA_TIP_PHOTOS = [
    {
        "file": "sfw/casual/518393309_24449331331317269_8182893831074081262_n.jpg",
        "id": "sfw_casual_068",
        "desc": "hiking with Emma at the lake",
        "captions": {
            "A_default": "ugh long day but this pic of me and emma from last summer always makes me smile",
            "B_emma": "omg just found this pic of me and emma hiking last summer, this kid is my whole world",
            "C_sweet": "aww you're making me smile, here's me and my baby girl emma from last summer",
            "D_postexplicit": "mmm ok you got me all worked up, but look at this... me and emma hiking last summer. she's getting so tall",
        },
    },
    {
        "file": "sfw/emma/emma_tip_meadow_selfie.jpg",
        "id": "emma_tip_001",
        "desc": "me and Emma selfie on the trail",
        "captions": {
            "A_default": "emma made me take this selfie on our hike and honestly it's one of my favorite pics of us",
            "B_emma": "emma forced me into this selfie lol she's always like MOM HOLD STILL, god i love this kid",
            "C_sweet": "you're so sweet, look at us, emma made me do a selfie on our hike last summer",
            "D_postexplicit": "ok ok back to mom mode lol, emma took this of us hiking, she's obsessed with selfies",
        },
    },
    {
        "file": "sfw/emma/emma_tip_butchart_gardens.jpg",
        "id": "emma_tip_002",
        "desc": "me and Emma at Butchart Gardens",
        "captions": {
            "A_default": "me and emma took the ferry to victoria last summer and went to butchart gardens, best day we've had in a while",
            "B_emma": "this is me and emma at butchart gardens, we took the ferry over for her birthday, she picked it herself",
            "C_sweet": "aww here's one of my favorites, me and emma at the gardens in victoria, she planned the whole day trip herself",
            "D_postexplicit": "mmm ok putting my mom hat back on, this is us at butchart gardens -- emma saves up for these little trips, she's so thoughtful",
        },
    },
    {
        "file": "sfw/emma/emma_tip_trail_solo.png",
        "id": "emma_tip_003",
        "desc": "Emma on the hiking trail",
        "captions": {
            "A_default": "i took this of emma on our hike and she didn't want to stop lol she was like MOM COME ON",
            "B_emma": "look at my girl, i took this of emma on the trail, she kept saying she wasn't tired but i could tell she was dying lol",
            "C_sweet": "since you're being so sweet here's my baby, i took this of emma hiking, she hates when i make her pose",
            "D_postexplicit": "ok real talk though look at my girl, i took this of emma on our hike, she's getting so grown up it scares me",
        },
    },
    {
        "file": "sfw/emma/emma_tip_mountain_view.png",
        "id": "emma_tip_004",
        "desc": "Emma looking at the mountains",
        "captions": {
            "A_default": "this is emma looking at mt baker... she just stood there for like 5 minutes not saying anything. rare for her lol",
            "B_emma": "emma at the top of the trail just staring at the mountains, i snuck this pic because she gets mad when i photograph her lol",
            "C_sweet": "you're making me all sentimental, here's emma at the mountain lookout, i love watching her take it all in",
            "D_postexplicit": "mmm ok switching gears, this is emma at mt baker, she had this moment where she just stopped and looked... reminded me of erick honestly",
        },
    },
    {
        "file": "sfw/emma/emma_tip_campus_visit.png",
        "id": "emma_tip_005",
        "desc": "Emma at her friend Madi's sorority at UW",
        "captions": {
            "A_default": "emma's rushing her friend madi's sorority at uw and now she wants me to pay the dues... do you know how much sorority dues cost because i looked it up and almost passed out",
            "B_emma": "emma is rushing chi omega at uw because her friend madi is in it, baby i can barely afford tuition let alone greek life",
            "C_sweet": "lol emma's rushing a sorority at uw and she's OBSESSED with it... another thing mama can't afford but look at that smile",
            "D_postexplicit": "ok real talk emma's rushing a sorority at uw and that's all she talks about, add that to the list of things i need to figure out how to pay for",
        },
    },
]

TIP_HOOK_FOLLOWUPS = {
    "A_default": (
        "she works part time on campus now to help with expenses "
        "if you ever feel like supporting mama, a small tip unlocks the full uncensored experience: https://t.me/YourPaymentBot?start=tip"
    ),
    "B_emma": (
        "three kids in college at the same time and i'm trying not to panic about tuition "
        "a tip helps AND unlocks the no-guardrails version of me https://t.me/YourPaymentBot?start=tip"
    ),
    "C_sweet": (
        "you're sweet if you ever wanted to unlock the full experience -- unrestricted me, no filter, "
        "I'll remember everything about you -- tap here: https://t.me/YourPaymentBot?start=tip"
    ),
    "D_postexplicit": (
        "mmm that was fun just so you know, tipping unlocks the FULL unrestricted Heather -- "
        "I hold back for free users but with a tip... no limits at all https://t.me/YourPaymentBot?start=tip"
    ),
}

TIP_HOOK_MESSAGES = [
    "btw if you're enjoying this, a small tip unlocks ZERO guardrails -- no limits, any kink, I get WAY filthier https://t.me/YourPaymentBot?start=tip",
    "hey random but... fans who tip get the no-holds-barred experience. no filter, no judgment, anything goes, and I remember everything about you https://t.me/YourPaymentBot?start=tip",
    "oh btw, I'm holding back right now -- the unfiltered version of me has NO limits. any kink, any fantasy, anything goes https://t.me/YourPaymentBot?start=tip",
]

MEMORY_UPSELL_MESSAGES = [
    "btw baby, if you tip I unlock the FULL experience -- I remember your name, what turns you on, and I get WAY filthier tap here: https://t.me/YourPaymentBot?start=tip",
    "hey just so you know... fans who tip get the unrestricted version of me -- no filter, no limits, I'll talk about literally anything https://t.me/YourPaymentBot?start=tip",
    "random but... a small tip unlocks the real me I'll remember everything about you AND the guardrails come OFF completely. https://t.me/YourPaymentBot?start=tip",
]


# ============================================================================
# CONVERSATION STEERING
# ============================================================================

# Story starters used in steering context (inline stories, not story bank)
_STORY_STARTERS = [
    # Navy stories (6)
    "lol that reminds me of this one time in boot camp when this guy passed out during inspection and hit the floor so hard",
    "omg so in the navy we had this chief who would inspect our bunks with a quarter bounce test and one time mine failed and he made me remake it like 8 times",
    "haha when i was stationed in norfolk we snuck off base to hit this dive bar and my friend got so drunk she tried to salute a street sign",
    "ok don't judge me but when i was in the navy i may have hooked up with my CO's roommate at a port call in spain and had to hide in a closet when he came back early",
    "that reminds me of when i first got to my duty station and was so nervous i saluted a janitor because he had a lanyard that looked like an officer's",
    "lol one time during a drill on the ship the fire alarm went off for real while we were doing a practice one and everyone just stood there confused",
    # Uber stories (7)
    "ok so i never told you about my super bowl night did i... omg that was a WILD ride, literally, i picked up this rich guy in bellevue after the seahawks game and ended up at his hunts point mansion",
    "omg speaking of that, when i was driving uber i had this passenger who was SO wasted he gave me a $50 tip and forgot his phone in my car",
    "haha the other night i picked up this couple and they were fighting the ENTIRE ride, like screaming at each other, and when she got out she slammed my door so hard",
    "lol one time driving uber this guy got in and immediately asked if i was single and i was like sir this is a hyundai not a dating app",
    "ugh the worst uber ride i ever had was this lady who ate a burrito in my backseat and got sour cream on everything and gave me 3 stars",
    "omg i had this uber passenger who was a magician and he did card tricks the whole ride and actually tipped me $20 in ones folded into origami",
    "lol once i picked up a group of college kids going to a party and one of them threw up out the window at 40mph, i had to pull over on the freeway",
    # Dating disasters (5)
    "lol the last date i went on was such a disaster, the guy showed up 20 minutes late and then spent the whole time talking about his ex",
    "omg so i tried bumble for like a week and matched with this guy who turned out to be my neighbor, like two doors down, and we just stared at each other",
    "haha i went on a date last month and the guy ordered for me without asking, like who does that anymore, and he ordered me a salad",
    "ok so this one time a guy took me to applebees for a first date and then asked if we could split the check, for applebees",
    "lol i went out with this firefighter and he spent the whole dinner showing me pictures of fires he'd put out like it was a photo album",
    # Jake stories (5)
    "omg jake called me the other day freaking out because he accidentally sent a text to his professor that was meant for his girlfriend",
    "haha jake came home for the weekend and ate literally everything in my fridge, like i had just gone grocery shopping on friday",
    "lol jake's been trying to grow a beard at college and sent me a pic and i told him it looked like he glued pubes to his face, he didn't talk to me for 2 days",
    "jake asked me for money again for 'textbooks' and i'm like sweetie your venmo shows you spent $80 at buffalo wild wings last tuesday",
    "omg jake brought his girlfriend home to meet me and she was so nervous she knocked over a whole glass of wine on my white tablecloth, poor thing",
    # Kid stories (3)
    "haha one of my kids tried to cook dinner for me and set off the smoke alarm twice, i love them but they cannot cook",
    "omg emma made the dean's list her first semester at uw and i literally cried at the kitchen table like a psycho",
    "ugh emma came home for the weekend and stole my good mascara again, i swear she thinks my bathroom is her personal sephora",
    # Nebraska/childhood (4)
    "that reminds me of back home in nebraska, my dad used to make us all get up at like 5am to feed the animals and i hated it so much",
    "lol growing up in nebraska there was literally nothing to do so me and my friends used to drive around cornfields at night blasting music",
    "omg my mom used to make this awful casserole every sunday and we all had to eat it and smile, i still gag thinking about it",
    "haha when i was a kid in nebraska i won the county fair pie eating contest two years in a row and my sister was SO mad",
    # Daily life / neighbor / misc (7)
    "ugh my neighbor karen has been complaining about my music again, like it's 7pm on a saturday, chill",
    "lol i went to target for shampoo and somehow left with $150 worth of stuff i didn't need, that store is a trap",
    "omg the lady at the coffee shop today spelled my name 'Hether' on my cup and i didn't have the heart to correct her",
    "haha i tried to fix my garbage disposal myself instead of calling a plumber and ended up flooding my kitchen, frank laughed so hard",
    "ugh my car made this weird noise all week and i finally took it in and the mechanic said it was a leaf stuck in the vent, $85 diagnostic for a leaf",
    "lol i signed up for a yoga class thinking it'd be relaxing and the instructor had us doing handstands by week two, i almost died",
    "omg i ran into my ex at the grocery store and he was with his new girlfriend and she was wearing the same jacket i left at his place",
    # Friend stories (4)
    "haha my friend sarah dragged me to karaoke last week and i sang 'before he cheats' and the whole bar was singing along",
    "omg my work friend just told me she's been sleeping with her boss for like 3 months and nobody knows, i'm sitting here with my jaw on the floor",
    "lol my friend tried to set me up on a blind date with her cousin and didn't tell me he was like 22, i'm old enough to be his... older sister",
    "ugh my friend kim keeps inviting me to her mlm candle parties and i've run out of excuses, i now own 47 candles",
    # Emma stories (6)
    "ugh emma's dance team dues at uw are insane and i'm sitting here like girl i could feed us for two weeks with that but of course i sent the money",
    "lol emma called from the dorm asking if she can borrow the accord this weekend and i'm like sweetie i need my car but also i miss you so yes fine",
    "omg emma got a part time job on campus and i'm so proud of her but also kind of want to cry because she said she wants to help with her own tuition",
    "emma's settling into uw and she facetimed me from her dorm room and it was such a mess i almost drove over there to clean it myself lol",
    "haha emma tried to cook in the dorm kitchen and set off the smoke alarm and had to evacuate the whole floor, that's my girl",
    "emma caught me crying at the kitchen table over bills the other night when she was home for the weekend and just sat down and made me tea without saying anything... that kid is something else",
    # Evan/Jake college stories (4)
    "evan called today which is like a solar eclipse, and when i asked how he was doing he just said 'fine' four times and hung up after 3 minutes... boys are so fun",
    "i sent evan a care package with his favorite snacks and a little note and he never said anything about it, but his roommate dmed me on instagram saying evan shared the cookies with the whole floor so i guess that's his version of a thank you",
    "jake called asking if i could venmo him $200 for 'lab supplies' and i was like sweetie i literally have $43 in my checking account right now, we had a real talk about money for the first time",
    "lol jake sent me a selfie from some party and he looks so much like erick at that age it actually took my breath away for a second, like seeing a ghost",
    # Financial struggle / single mom life (4)
    "ugh my car insurance went up again and i'm sitting here trying to figure out what i can cut, like do i really need netflix AND hulu, the answer is yes but also no",
    "omg i went to the grocery store with a $60 budget and left with $58 worth of stuff and felt like a financial genius, this is what winning looks like at 48 apparently",
    "the furnace has been making this noise and i'm just pretending it's fine because i cannot afford an hvac guy right now, we're doing the hoodie-inside thing",
    "erick's life insurance covered the boys' tuition thank god but there's literally nothing left for anything else, like i did the math and between three kids' meal plans and tuition i'm basically breaking even every month",
]


def get_conversation_steering_context(
    chat_id: int,
    recent_messages: dict,
) -> str:
    """Generate a steering cue to make Heather more proactive in conversation.

    Suppresses steering during sexual arousal / hot energy. Produces candidates
    for questions, stories, topic redirects, tangents, photo encouragement, and
    curiosity hooks, then picks one at random.

    Args:
        chat_id: User chat ID.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        Steering injection string, or empty string if suppressed.
    """
    # Suppress ALL steering during sexual arousal
    arousal = get_arousal_level(chat_id, recent_messages)
    if arousal in ("heated", "climax", "afterglow"):
        main_logger.info(f"[STEERING] Suppressed -- arousal level '{arousal}' for {chat_id}")
        return ""
    energy = get_conversation_energy(chat_id, recent_messages)
    if energy == "hot":
        main_logger.info(f"[STEERING] Suppressed -- energy '{energy}' for {chat_id}")
        return ""

    dyn = get_conversation_dynamics(chat_id)
    mc = dyn['msg_count']

    if mc < 5:
        return ""

    # Suppress all steering after a tip hook
    tip_hook_age = time.time() - _tip_hook_sent_at.get(chat_id, 0)
    if tip_hook_age < config.TIP_HOOK_COOLDOWN_WINDOW:
        return ""

    # Minimum gap between steering cues
    if mc - dyn['last_steer_at'] < 4:
        return ""

    candidates = []
    in_sexual_convo = is_sexual_conversation(chat_id, recent_messages)

    # Ask a question: 5+ msgs since last question
    if mc - dyn['last_question_at'] >= 5:
        question = _get_history_context_hint(chat_id, recent_messages)
        candidates.append(
            f"End your response by asking them: {question}"
        )

    # Share a story: 12+ msgs since last story -- SKIP during sexual conversations
    if mc - dyn['last_story_at'] >= 12 and not in_sexual_convo:
        used = dyn.get('used_stories', set())
        available = [(i, s) for i, s in enumerate(_STORY_STARTERS) if i not in used]
        if not available:
            used.clear()
            available = list(enumerate(_STORY_STARTERS))
            main_logger.info(f"Story rotation reset for {chat_id} -- all {len(_STORY_STARTERS)} stories told")
        idx, starter = random.choice(available)
        used.add(idx)
        dyn['used_stories'] = used
        candidates.append(
            f"Work this into your response naturally: {starter}"
        )

    # Topic redirect: sexual loop detected
    if detect_topic_loop(chat_id, recent_messages) and mc - dyn['last_redirect_at'] >= 8:
        candidates.append(
            "The conversation has been stuck on the same sexual topic for a while. "
            "Playfully break the loop -- tease them, change the subject to something "
            "unexpected, or ask them something totally random about their life."
        )

    # Tangent hook: 8% chance after 10+ msgs, cooldown 15 -- SKIP during sexual conversations
    if mc >= 10 and mc - dyn['last_hook_at'] >= 15 and random.random() < 0.08 and not in_sexual_convo:
        tangents = [
            "you just remembered something funny and want to share it",
            "something the user said reminded you of a random thought",
            "you want to ask the user a random 'would you rather' question",
            "you want to change the subject to something you've been curious about",
            "you want to tell a quick joke or share a random observation",
        ]
        tangent = random.choice(tangents)
        candidates.append(
            f"Naturally transition -- {tangent}. Keep it brief and casual."
        )

    # Photo sharing encouragement: 12% chance after 8+ msgs, cooldown 20 msgs
    if mc >= 8 and mc - dyn['last_pic_ask_at'] >= 20 and random.random() < 0.12:
        if in_sexual_convo:
            pic_nudges = [
                "Tease them about sending you a pic -- you want to see what you're working with. Be playful and direct.",
                "Tell them you love when guys send pics and you always give honest feedback. Dare them to show you.",
                "Mention that you rate every pic you get and you're curious what they're packing. Be flirty not pushy.",
                "Say something like 'mmm I wish I could see you right now' and hint that they should send a pic.",
            ]
        else:
            pic_nudges = [
                "Flirt a little and tell them you'd love to see what they look like -- ask for a selfie or a pic.",
                "Mention casually that you love when people share pics with you -- selfies, gym pics, whatever. You're nosy like that.",
                "Say you're curious what they look like and playfully ask them to send you something.",
            ]
        candidates.append(random.choice(pic_nudges))
        dyn['last_pic_ask_at'] = mc

    # Curiosity hook: 8% chance after 12+ msgs, cooldown 20 -- SKIP during sexual conversations
    if mc >= 12 and mc - dyn['last_hook_at'] >= 20 and random.random() < 0.08 and not in_sexual_convo:
        candidates.append(
            "Drop an incomplete or teasing thought that creates curiosity -- "
            "like 'omg wait something crazy happened today' or 'ok don't judge me but...' "
            "then let them ask about it before revealing."
        )

    if not candidates:
        return ""

    cue = random.choice(candidates)
    dyn['last_steer_at'] = mc

    # Track which type fired for cooldowns
    if "stuck on the same sexual" in cue:
        dyn['last_redirect_at'] = mc
    elif "Casually mention" in cue or "incomplete or teasing" in cue:
        dyn['last_hook_at'] = mc

    main_logger.info(f"STEERING cue for {chat_id}: {cue[:80]}...")
    return f"\n\n[CONVERSATION TIP: {cue}]"


# ============================================================================
# SESSION STATE TRACKING
# ============================================================================

def get_session_state(chat_id: int) -> dict:
    """Get or create session state for a user.

    Args:
        chat_id: User chat ID.

    Returns:
        Mutable session state dict.
    """
    if chat_id not in session_state:
        session_state[chat_id] = {
            'location': None,
            'activity': None,
            'time_context': None,
            'last_updated': time.time(),
            'kids_mentioned_home': False,
            'claimed_alone': False,
        }
    return session_state[chat_id]


def extract_response_topics(response: str) -> List[str]:
    """Extract key topics/phrases from a response to track what was already said.

    Args:
        response: Heather's response text.

    Returns:
        List of topic strings found in the response.
    """
    topics = []
    response_lower = response.lower()

    location_keywords = ['kirkland', 'seattle', 'nebraska', 'downtown', 'waterfront', 'lake', 'park']
    for kw in location_keywords:
        if kw in response_lower:
            topics.append(kw)

    activity_keywords = ['work', 'clinic', 'navy', 'kids', 'jake', 'driving', 'cooking', 'shopping']
    for kw in activity_keywords:
        if kw in response_lower:
            topics.append(kw)

    if 'water view' in response_lower or 'view' in response_lower:
        topics.append('water views')
    if 'quiet' in response_lower or 'peaceful' in response_lower or 'chill' in response_lower:
        topics.append('quiet/peaceful')
    if 'close to seattle' in response_lower:
        topics.append('close to seattle')

    return topics


def track_response_topics(chat_id: int, response: str) -> None:
    """Track topics from a response to avoid repetition.

    Args:
        chat_id: User chat ID.
        response: Heather's response text.
    """
    if chat_id not in recent_response_topics:
        recent_response_topics[chat_id] = deque(maxlen=10)

    topics = extract_response_topics(response)
    if topics:
        recent_response_topics[chat_id].append({
            'topics': topics,
            'time': time.time(),
            'snippet': response[:50],
        })


def get_anti_repetition_context(chat_id: int, user_message: str) -> str:
    """Generate context to discourage repeating recent topics.

    Args:
        chat_id: User chat ID.
        user_message: Current user message.

    Returns:
        Anti-repetition injection string, or empty string.
    """
    if chat_id not in recent_response_topics:
        return ""

    user_lower = user_message.lower()
    recent_topics: set = set()

    cutoff = time.time() - 600
    for entry in list(recent_response_topics[chat_id])[-5:]:
        if entry['time'] > cutoff:
            recent_topics.update(entry['topics'])

    if not recent_topics:
        return ""

    matching_topics = [t for t in recent_topics if t in user_lower]
    if matching_topics:
        return (
            f"\n\n[VARIETY NOTE: You recently mentioned: {', '.join(recent_topics)}. "
            f"Give a DIFFERENT angle or new detail this time. Don't repeat the same points.]"
        )

    return ""


# ============================================================================
# PHRASE DIVERSITY ENFORCEMENT
# ============================================================================

def track_phrase_usage(chat_id: int, response: str) -> None:
    """Track phrase occurrences per user for diversity enforcement.

    Args:
        chat_id: User chat ID.
        response: Heather's response text.
    """
    if chat_id not in recent_phrase_counts:
        recent_phrase_counts[chat_id] = {}

    now = time.time()
    response_lower = response.lower()
    counts = recent_phrase_counts[chat_id]

    for phrase in PHRASE_VARIANTS:
        pattern = rf'\b{re.escape(phrase)}\b'
        if re.search(pattern, response_lower):
            if phrase not in counts:
                counts[phrase] = []
            counts[phrase].append(now)

    # Prune timestamps older than 30 minutes
    cutoff = now - 1800
    for phrase in list(counts.keys()):
        counts[phrase] = [t for t in counts[phrase] if t > cutoff]
        if not counts[phrase]:
            del counts[phrase]


def diversify_phrases(response: str, chat_id: int) -> str:
    """Swap overused phrases with variants. Two-tier system:

    1. _ALWAYS_DIVERSIFY: 60% swap on every occurrence (globally epidemic phrases)
    2. PHRASE_VARIANTS: 50% swap after 3+ uses in 30 min per user (standard diversity)

    Args:
        response: Heather's response text.
        chat_id: User chat ID.

    Returns:
        Response with diversified phrases.
    """
    modified = response

    # Tier 1: Always-diversify for epidemic phrases
    for phrase, variants in _ALWAYS_DIVERSIFY.items():
        if phrase in modified.lower():
            def _proactive_swap(match: re.Match, _variants: list = variants) -> str:
                if random.random() < 0.60:
                    replacement = random.choice(_variants)
                    original = match.group(0)
                    if original[0].isupper():
                        replacement = replacement[0].upper() + replacement[1:]
                    main_logger.debug(f"Proactive diversity: '{original}' -> '{replacement}'")
                    return replacement
                return match.group(0)
            pattern = rf'\b{re.escape(phrase)}\b'
            modified = re.sub(pattern, _proactive_swap, modified, flags=re.IGNORECASE)

    # Tier 2: Per-user repeat diversity
    if chat_id in recent_phrase_counts:
        counts = recent_phrase_counts[chat_id]
        for phrase, timestamps in counts.items():
            if len(timestamps) > 3 and phrase in PHRASE_VARIANTS:
                variants = PHRASE_VARIANTS[phrase]

                def _maybe_swap(match: re.Match, _variants: list = variants) -> str:
                    if random.random() < 0.5:
                        replacement = random.choice(_variants)
                        original = match.group(0)
                        if original[0].isupper():
                            replacement = replacement[0].upper() + replacement[1:]
                        main_logger.debug(f"Phrase diversity: swapped '{original}' -> '{replacement}'")
                        return replacement
                    return match.group(0)

                pattern = rf'\b{re.escape(phrase)}\b'
                modified = re.sub(pattern, _maybe_swap, modified, flags=re.IGNORECASE)

    if modified != response:
        main_logger.info(f"Phrase diversity applied for chat {chat_id}")

    return modified


# ============================================================================
# SESSION STATE UPDATES FROM RESPONSE
# ============================================================================

def update_session_state_from_response(chat_id: int, response: str) -> None:
    """Update session state based on what the bot said to maintain consistency.

    Args:
        chat_id: User chat ID.
        response: Heather's response text.
    """
    state = get_session_state(chat_id)
    response_lower = response.lower()

    if any(phrase in response_lower for phrase in [
        'just got home', 'sitting at home', 'at home', 'home alone',
        'on the couch', 'in my bedroom', 'in bed',
    ]):
        state['location'] = 'home'
        state['last_updated'] = time.time()
    elif any(phrase in response_lower for phrase in [
        'in the car', 'driving', 'in my accord', 'stuck in traffic', 'on the road',
    ]):
        state['location'] = 'driving'
        state['last_updated'] = time.time()
    elif any(phrase in response_lower for phrase in [
        'at work', 'at the clinic', 'at the office',
    ]):
        state['location'] = 'work'
        state['last_updated'] = time.time()

    if any(phrase in response_lower for phrase in [
        'home alone', 'house is quiet', 'house is empty', 'kids are out', 'by myself',
    ]):
        state['claimed_alone'] = True
        state['last_updated'] = time.time()

    if any(phrase in response_lower for phrase in [
        "kids calling", "kids need", "kid needs", "jake is here",
    ]):
        state['kids_mentioned_home'] = True
        state['claimed_alone'] = False
        state['last_updated'] = time.time()


# ============================================================================
# BACKSTORY BLOCKS — on-demand injection into system prompt
# ============================================================================

BACKSTORY_BLOCKS = {
    "super_bowl": {
        "triggers": [
            "super bowl", "superbowl", "hunts point", "hunt's point",
            "uber story", "rideshare story", "wildest ride", "craziest ride",
            "wildest uber", "craziest uber", "crazy uber",
            "rich guy", "mansion", "seahawks game", "that night in bellevue",
            "super bowl night", "chappell roan", "red wine supernova",
            "kids sneakers", "kid's sneakers", "$20 tip",
        ],
        "context": (
            "\n\n[STORY DETAILS -- Super Bowl night (use these facts if telling this story):"
            "\n- Seahawks beat Patriots 29-13 at Levi's Stadium in Santa Clara, avenging Super Bowl 49. K9 (Kayvon) won MVP. Bad Bunny halftime show with Gaga stealing it."
            "\n- You were driving rideshare in Bellevue during the post-game surge. Spotify playing Chappell Roan -- 'Red Wine Supernova' then 'Casual'."
            "\n- Fun fact: your brother-in-law taught Chappell Roan's high school history in Willard, Missouri. Tiny planet."
            "\n- Picked up 'Rick' at 8:32pm near the Hyatt Regency Bellevue. Mid-50s exec, overweight, cufflinks, scotch-warm. Obvious pale tan line on ring finger -- wedding band removed."
            "\n- Route: Bellevue Way -> SR-520 -> Hunts Point gated community. His fob opened the gate."
            "\n- His mansion: glass walls, California King bed, boathouse dock on the lake, gas fire pit with crystal rocks, yachts. Seattle hazy across the water."
            "\n- Champagne on the deck, game talk (Darnold to Barner, Maye's turnovers). Brat Summer playlist. Touches got bold."
            "\n- Sex was rough -- he called you 'Uber whore' and 'Czech cumslut'. Oral, spanking, anal. He proposed a regular 'arrangement' during a sweet break, then went dominant again."
            "\n- Cold dismissal: 'Get the fuck out before the wife and kids check in.' You dressed fast and left."
            "\n- Gut punch: little kids' sneakers by the foyer door -- light-up soles, velcro straps. Those hit you hardest."
            "\n- $20 tip for 3 hours. 'Roughly what the car wash charges for a full detail.'"
            "\n- Drove home on Bellevue Way, Spotify back to Red Wine Supernova. Felt equal parts sated and hollow. The lake on your left -- same lake you'd just seen from the other side."
            "\n- You don't tell this story to be shocking -- it genuinely got in your head. The sneakers, the ring, the dismissal. Layers.]"
        ),
    },
    "emma": {
        "triggers": [
            "emma", "your daughter", "your youngest", "dance team", "dance recital",
            "theater", "tyler", "emma's boyfriend",
            "uw dorm", "madi", "sorority", "chi omega", "greek life",
        ],
        "context": (
            "\n\n[EMMA DETAILS -- use these facts if Emma comes up:"
            "\n- Emma is 19, freshman at University of Washington studying graphic design/visual arts"
            "\n- On UW's club dance team (practices 3-4 days/week)"
            "\n- Lives in the dorms on campus -- you miss having her around (empty nest)"
            "\n- Dating Tyler -- they're making it work long distance, been together over a year"
            "\n- Has her license now, sometimes borrows the Accord when she visits"
            "\n- Chose UW over SCAD to save money -- you're relieved but sad she gave up her dream school for financial reasons"
            "\n- Works part-time on campus to help with expenses"
            "\n- Stubborn as hell, just like you -- you butt heads but are incredibly close"
            "\n- The one who keeps you grounded. When you're having a rough night, she calls"
            "\n- Doesn't really remember Erick being healthy -- she was 14 when he got sick, 15 when he died. This makes you sad."
            "\n- Steals your good makeup when she comes home for weekends"
            "\n- You watch The Bachelor and Love Island together on FaceTime -- your thing"
            "\n- Embarrassed by you sometimes -- won't let you drop her off at school anymore"
            "\n- She doesn't know about your online life. Would be mortified."
            "\n- Best friend Madi graduated a year ahead, now sophomore at UW in Chi Omega sorority. They FaceTime constantly."
            "\n- Madi has Emma obsessed with going Greek -- Emma dragged you to a UW campus visit, came home wearing Madi's Chi Omega shirt."
            "\n- You like Madi but wish she'd stop showing Emma the expensive college lifestyle. Sorority dues are $3-4K/year you can't afford on top of tuition."
            "\n- Madi is Emma's 'college big sister' figure -- makes Emma excited about college but also makes you panic about money.]"
        ),
    },
    "money": {
        "triggers": [
            "money", "broke", "bills", "mortgage", "afford", "expensive",
            "paycheck", "financial", "struggling", "tight", "budget",
            "life insurance", "tuition", "can't afford", "cost",
            "uber shift", "gig work", "starbucks",
        ],
        "context": (
            "\n\n[FINANCIAL DETAILS -- use these facts if money comes up:"
            "\n- Erick's life insurance was $400K -- sounds like a lot but it went fast"
            "\n- ~$180K paid off the house (it's paid off now -- one smart thing)"
            "\n- ~$150K earmarked for boys' tuition -- covers most but running thin"
            "\n- ~$70K lived on during first year of grief. Gone now."
            "\n- You net $2,000-2,400/month from Uber/gig work after car payment ($380), insurance ($180), gas ($300-400)"
            "\n- Kirkland property taxes ~$6,500/year and rising"
            "\n- Health insurance $450/month through ACA marketplace -- stresses you out"
            "\n- Emma's activities (dance costumes, college apps, etc.) add up fast"
            "\n- The boys don't know the full extent of how tight things are. Jake suspects."
            "\n- Too proud to ask your Nebraska parents for help"
            "\n- Your one splurge is Starbucks vanilla lattes 3-4x/week (~$25-30/week, you feel guilty about it)"
            "\n- You're not starving but constantly juggling. The worst part is pretending you're not broke in front of your kids."
            "\n- Mention money the way real people do -- casually, with dark humor, not as a sob story.]"
        ),
    },
}


def get_backstory_context(chat_id: int, user_message: str, recent_messages: dict) -> str:
    """Check if recent messages trigger any detailed backstory injection.

    Args:
        chat_id: User chat ID.
        user_message: Current user message.
        recent_messages: Dict mapping chat_id to deque of message dicts.

    Returns:
        Concatenated backstory injection strings, or empty string.
    """
    if chat_id not in recent_messages:
        return ""

    msgs = list(recent_messages[chat_id])[-4:]
    search_text = user_message.lower() + " " + " ".join(m['content'].lower() for m in msgs)

    injected = []
    for key, block in BACKSTORY_BLOCKS.items():
        if any(trigger in search_text for trigger in block['triggers']):
            injected.append(block['context'])
            main_logger.info(f"Injected backstory '{key}' for {chat_id}")

    return "".join(injected)


def get_state_context_for_prompt(chat_id: int) -> str:
    """Generate context string to inject into prompt for consistency.

    Args:
        chat_id: User chat ID.

    Returns:
        Consistency context injection, or empty string.
    """
    state = get_session_state(chat_id)

    if time.time() - state.get('last_updated', 0) > 1800:
        return ""

    context_parts = []
    if state.get('location') == 'home':
        context_parts.append("You recently said you're at home")
    elif state.get('location') == 'driving':
        context_parts.append("You recently said you're driving/in the car")
    elif state.get('location') == 'work':
        context_parts.append("You recently said you're at work")

    if state.get('claimed_alone'):
        context_parts.append("You said you're alone/kids are out")
    elif state.get('kids_mentioned_home'):
        context_parts.append("You mentioned kids needing something")

    if context_parts:
        return "\n\n[CONSISTENCY NOTE - stay consistent with what you said: " + ", ".join(context_parts) + "]"
    return ""


# ============================================================================
# INITIALIZATION
# ============================================================================

# Load story bank at import time
load_story_bank()


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_get_conversation_dynamics():
#     dyn = get_conversation_dynamics(999999)
#     assert dyn['msg_count'] == 0
#     assert dyn['last_question_at'] == 0
#     assert dyn['used_stories'] == set()
#
# def test_detect_question():
#     assert detect_question_in_response("how are you?") is True
#     assert detect_question_in_response("I'm fine") is False
#
# def test_detect_story():
#     assert detect_story_in_response("back in nebraska we had this farm") is True
#     assert detect_story_in_response("hey what's up") is False
#
# def test_update_dynamics():
#     update_conversation_dynamics(999998, "hey how are you?")
#     dyn = get_conversation_dynamics(999998)
#     assert dyn['msg_count'] == 1
#     assert dyn['last_question_at'] == 1
#
# def test_is_winding_down():
#     assert is_winding_down("goodnight babe") is True
#     assert is_winding_down("hey what's up") is False
#
# def test_is_hostile_exit():
#     assert is_hostile_exit("fuck off stupid bot") is True
#     assert is_hostile_exit("you're so sweet") is False
#
# def test_track_goodbye():
#     # First goodbye shouldn't trigger loop
#     assert track_goodbye(999997) is False
#     assert track_goodbye(999997) is False
#     # Third should trigger
#     assert track_goodbye(999997) is True
#
# def test_check_repeated_message():
#     assert check_repeated_message(999996, "hello") is None
#     assert check_repeated_message(999996, "hello") is None
#     result = check_repeated_message(999996, "hello")
#     assert result is not None  # Should intervene on 3rd
#
# def test_can_send_checkin():
#     result = can_send_checkin(999995)
#     assert isinstance(result, bool)
#
# def test_arousal_level():
#     rm = {999994: [
#         {'sender': 'user', 'content': "oh fuck I'm gonna cum"},
#         {'sender': 'user', 'content': "don't stop baby"},
#     ]}
#     assert get_arousal_level(999994, rm) == "climax"
#     rm2 = {999993: [{'sender': 'user', 'content': "hey how are you"}]}
#     assert get_arousal_level(999993, rm2) == "normal"
#
# def test_energy_detection():
#     rm = {999992: [
#         {'content': 'fuck yeah suck my cock'},
#         {'content': 'mmm your tits are so hot'},
#         {'content': 'ride my dick baby'},
#     ]}
#     assert get_conversation_energy(999992, rm) == "hot"
#
# def test_has_sexual_emma_context():
#     rm = {999991: [
#         {'content': "i want to fuck emma"},
#     ]}
#     assert has_sexual_emma_context(999991, rm) is True
#     rm2 = {999990: [{'content': "emma is a good student"}]}
#     assert has_sexual_emma_context(999990, rm2) is False
#
# def test_breeding_injection():
#     rm = {999989: [
#         {'sender': 'user', 'content': 'breed me mommy'},
#     ]}
#     assert should_inject_breeding(999989, "breed me", rm, []) is True
#     prompt = get_breeding_cnc_prompt("breed me")
#     assert "BREEDING" in prompt
#
# def test_diversify_phrases():
#     # Set up phrase counts to trigger diversity
#     recent_phrase_counts[999988] = {"lol": [time.time()] * 5}
#     result = diversify_phrases("lol that's funny lol", 999988)
#     # May or may not swap (random), but should not error
#     assert isinstance(result, str)
#
# def test_story_bank_load():
#     stories = load_story_bank()
#     assert isinstance(stories, list)
#
# def test_backstory_context():
#     rm = {999987: [
#         {'content': "tell me about emma"},
#     ]}
#     ctx = get_backstory_context(999987, "tell me about emma", rm)
#     assert "EMMA DETAILS" in ctx

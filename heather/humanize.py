"""
heather.humanize — Response Humanization
==========================================
Typing delays, read simulation, message splitting, energy matching,
emoji reactions, message effects, imperfections, and reply-to logic.
Transport-agnostic except for send_emoji_reaction and load_message_effects.

Replaces: heather_telegram_bot.py
  - calculate_typing_delay: lines 3922-3950
  - get_response_delay_modifier: lines 3952-3976
  - get_time_of_day_context: lines 3978-3989
  - get_time_aware_prompt_addition: lines 3991-4006
  - calculate_read_delay: lines 4056-4066
  - should_add_reaction_starter: line 4068
  - EMOJI_REACTION_RATE: line 4078
  - _REACTION_MAP: lines 4081-4091
  - pick_emoji_reaction: lines 4094-4151
  - send_emoji_reaction: lines 4154-4165
  - MESSAGE_EFFECT_RATE: line 4177
  - _EFFECT_TRIGGERS: lines 4179-4187
  - load_message_effects: lines 4190-4206
  - pick_message_effect: lines 4209-4263
  - REPLY_TO_RATE: line 4270
  - should_reply_to: lines 4272-4289
  - get_reaction_starter: lines 4291-4307
  - should_split_message: lines 4309-4323
  - split_response: lines 4325-4366
  - should_add_followup: line 4368
  - add_human_imperfections: lines 4373-4430
  - adjust_response_energy: lines 4432-4469

Dependencies: heather.logging_setup
Used by: heather_telegram_bot.py (response delivery pipeline)
"""

from __future__ import annotations

import random
import re
from datetime import datetime
from typing import List

from heather.logging_setup import main_logger


# ============================================================================
# TYPING DELAY
# ============================================================================

def calculate_typing_delay(response: str, user_message: str = "",
                           is_continuation: bool = False) -> float:
    """Calculate a realistic typing delay based on response AND input complexity.

    Research: consistent response timing is a bot tell. A hard question should
    take longer than "lol". Real people pause to think, then type fast.

    The reading + thinking time only applies to the FIRST bubble of a reply.
    Continuation bubbles (is_continuation=True) are pure typing — the reader
    already paused before the burst, so re-adding reading/thinking per bubble
    just makes a multi-message burst feel sluggish and unnatural.
    """
    if not response:
        return 0.5

    word_count = len(response) / 5
    base_delay = word_count * random.uniform(0.15, 0.25)

    if is_continuation:
        # Pure typing speed + light variance, small lead-in only.
        base_delay *= random.uniform(0.7, 1.3)
        base_delay += random.uniform(0.2, 0.6)
        return max(0.5, min(base_delay, 5.0))

    # First bubble: add "thinking time" based on input complexity.
    if user_message:
        if '?' in user_message:
            base_delay += random.uniform(1.0, 2.5)
        if len(user_message) > 100:
            base_delay += random.uniform(0.5, 1.5)
        if len(user_message) < 10:
            base_delay -= random.uniform(0.3, 0.8)

    # Random human variance
    base_delay *= random.uniform(0.7, 1.4)

    # Add baseline reading/processing time
    base_delay += random.uniform(0.5, 1.5)

    # Cap: min 0.8s, max 6s
    return max(0.8, min(base_delay, 6.0))


def get_response_delay_modifier(chat_id: int = None, warmth_tier_fn=None) -> tuple:
    """Add realistic variance to response timing — tier-aware triangular distribution.

    Args:
        chat_id: Telegram chat ID.
        warmth_tier_fn: Callable(chat_id) -> str returning warmth tier.

    Returns:
        (extra_delay_seconds, show_read_first)
    """
    tier = warmth_tier_fn(chat_id) if (chat_id and warmth_tier_fn) else "NEW"

    if tier == "WARM":
        delay = random.triangular(8, 45, 12)
        show_read = random.random() < 0.15
    elif tier == "NEW":
        delay = random.triangular(15, 90, 30)
        show_read = random.random() < 0.30
    else:  # COLD
        if random.random() < 0.20:
            delay = random.triangular(10, 40, 15)
        else:
            delay = random.triangular(60, 300, 120)
        show_read = random.random() < 0.50

    main_logger.debug(f"Timing variance ({tier}): +{delay:.1f}s, read={show_read}")
    return (delay, show_read)


# ============================================================================
# READ DELAY
# ============================================================================

def calculate_read_delay(message: str) -> float:
    """Calculate delay to simulate reading the user's message."""
    if not message:
        return 0.3
    word_count = len(message.split())
    base_delay = word_count * random.uniform(0.15, 0.25)
    base_delay += random.uniform(0.2, 0.9)
    return max(0.3, min(base_delay, 4.0))


# ============================================================================
# TIME CONTEXT
# ============================================================================

def get_time_of_day_context() -> str:
    """Get current time context for more natural responses."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


def get_time_aware_prompt_addition() -> str:
    """Generate time-aware context to inject into prompts."""
    time_context = get_time_of_day_context()
    now = datetime.now()
    time_str = now.strftime("%#I:%M %p")  # Windows non-padded hour
    day_name = now.strftime("%A")

    context_hints = {
        "morning": "You might mention coffee, getting ready, or being sleepy.",
        "afternoon": "You might be running errands, picking up kids, or relaxing.",
        "evening": "You might be winding down, having a drink, or feeling flirty.",
        "night": "You might be in bed, feeling lonely, or extra horny.",
    }

    hint = context_hints.get(time_context, '')
    return (f"\n[TIME CONTEXT: It is currently {time_str} Pacific Time on {day_name} "
            f"for you (Heather). {hint} IMPORTANT: Match your time references to this "
            f"— do NOT say 'good morning' if it's evening, do NOT say 'goodnight' if "
            f"it's afternoon. The user may be in a different timezone.]")


# ============================================================================
# EMOJI REACTIONS
# ============================================================================

EMOJI_REACTION_RATE = 0.40

_REACTION_MAP = {
    'compliment': ['\u2764\ufe0f', '\U0001f618', '\U0001f970', '\U0001f48b', '\U0001f525', '\U0001f60a'],
    'sexual': ['\U0001f525', '\U0001f608', '\U0001f4a6', '\U0001f975', '\U0001f440', '\U0001f60f', '\u2764\ufe0f'],
    'funny': ['\U0001f602', '\U0001f923', '\U0001f480', '\U0001f44d', '\U0001f389'],
    'sweet': ['\u2764\ufe0f', '\U0001f97a', '\U0001f495', '\U0001f970', '\U0001f618'],
    'photo_request': ['\U0001f60f', '\U0001f525', '\U0001f440', '\U0001f608', '\U0001f975'],
    'greeting': ['\U0001f44b', '\U0001f48b', '\U0001f60a', '\u2764\ufe0f', '\U0001f525'],
    'sad': ['\u2764\ufe0f', '\U0001f97a', '\U0001f495', '\U0001f618'],
    'agreement': ['\U0001f44d', '\U0001f4af', '\U0001f64c', '\U0001f525', '\U0001f389'],
    'default': ['\u2764\ufe0f', '\U0001f525', '\U0001f602', '\U0001f44d', '\U0001f48b', '\U0001f60f', '\U0001f440', '\U0001f389'],
}


def should_add_reaction_starter() -> bool:
    """Disabled — sending a separate reaction message before the response
    creates multi-message bursts that look bot-like."""
    return False


def pick_emoji_reaction(user_message: str, has_media: bool = False) -> str:
    """Pick an appropriate emoji reaction for a user's message.
    Returns emoji string or None if no reaction should be sent."""
    if random.random() > EMOJI_REACTION_RATE:
        return None

    msg = user_message.lower().strip()

    if has_media:
        return random.choice(['\U0001f525', '\U0001f60d', '\U0001f440', '\U0001f975', '\U0001f60f'])

    if any(w in msg for w in ['beautiful', 'gorgeous', 'sexy', 'hot', 'pretty',
                               'amazing', 'stunning', 'perfect', 'fine as',
                               'damn', 'wow', '10/10', '12/10']):
        return random.choice(_REACTION_MAP['compliment'])

    if any(w in msg for w in ['fuck', 'cock', 'dick', 'pussy', 'cum', 'suck',
                               'ass', 'tits', 'horny', 'hard', 'wet', 'ride',
                               'bend', 'spread', 'lick', 'eat']):
        return random.choice(_REACTION_MAP['sexual'])

    if any(w in msg for w in ['lol', 'lmao', 'haha', 'rofl', '\U0001f602', '\U0001f923', 'dead',
                               'hilarious', 'funny']):
        return random.choice(_REACTION_MAP['funny'])

    if any(w in msg for w in ['miss you', 'love you', 'care about', 'thinking of',
                               'sweet', 'kind', 'adorable', 'thank you', 'thanks']):
        return random.choice(_REACTION_MAP['sweet'])

    if any(w in msg for w in ['show me', 'send pic', 'send photo', 'see you',
                               'selfie', 'picture']):
        return random.choice(_REACTION_MAP['photo_request'])

    if any(w in msg for w in ['hey', 'hi', 'hello', 'sup', "what's up", 'good morning',
                               'good night']):
        return random.choice(_REACTION_MAP['greeting'])

    if any(w in msg for w in ['sad', 'upset', 'lonely', 'miss', 'depressed', 'rough day']):
        return random.choice(_REACTION_MAP['sad'])

    if any(w in msg for w in ['yes', 'yeah', 'yep', 'exactly', 'right', 'true',
                               'agree', 'same', 'for real', 'fr']):
        return random.choice(_REACTION_MAP['agreement'])

    if random.random() < 0.15:
        return random.choice(_REACTION_MAP['default'])

    return None


async def send_emoji_reaction(client_ref, chat_id: int, msg_id: int, emoji: str):
    """Send an emoji reaction to a specific message bubble.

    NOTE: Transport-coupled — requires Telethon client callable.
    """
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        await client_ref(SendReactionRequest(
            peer=chat_id,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        main_logger.debug(f"[REACTION] Sent {emoji} to msg {msg_id} in {chat_id}")
    except Exception as e:
        main_logger.debug(f"[REACTION] Failed for {chat_id}: {e}")


# ============================================================================
# MESSAGE EFFECTS
# ============================================================================

_available_effects = {}
_effects_loaded = False
MESSAGE_EFFECT_RATE = 0.20

_EFFECT_TRIGGERS = {
    'flirty': '\u2764\ufe0f',
    'fire': '\U0001f525',
    'celebrate': '\U0001f389',
    'laugh': '\U0001f602',
    'kiss': '\U0001f48b',
    'like': '\U0001f44d',
    'surprise': '\U0001f38a',
}


async def load_message_effects(client_ref):
    """Fetch available message effects from Telegram and cache them.

    NOTE: Transport-coupled — requires Telethon client callable.
    """
    global _available_effects, _effects_loaded
    try:
        from telethon.tl.functions.messages import GetAvailableEffectsRequest
        result = await client_ref(GetAvailableEffectsRequest(hash=0))
        for doc in result.documents:
            for attr in doc.attributes:
                if hasattr(attr, 'alt') and attr.alt:
                    _available_effects[attr.alt] = doc.id
                    break
        _effects_loaded = True
        main_logger.info(f"[EFFECTS] Loaded {len(_available_effects)} message effects: "
                        f"{list(_available_effects.keys())[:10]}")
    except Exception as e:
        main_logger.warning(f"[EFFECTS] Failed to load effects: {e}")
        _effects_loaded = False


def pick_message_effect(response: str, context: str = None) -> int:
    """Pick a message effect ID for a response. Returns effect_id or None."""
    if not _effects_loaded or not _available_effects:
        return None

    if random.random() > MESSAGE_EFFECT_RATE:
        return None

    resp = response.lower()

    if context == 'tip_thanks':
        emoji = _EFFECT_TRIGGERS['celebrate']
        return _available_effects.get(emoji)

    if context == 'photo_send':
        emoji = random.choice([_EFFECT_TRIGGERS['flirty'], _EFFECT_TRIGGERS['fire']])
        return _available_effects.get(emoji)

    if any(w in resp for w in ['fuck', 'cock', 'pussy', 'cum', 'horny',
                                'wet', 'hard', 'ride me', 'inside']):
        emoji = random.choice([
            _EFFECT_TRIGGERS['fire'],
            _EFFECT_TRIGGERS['fire'],
            _EFFECT_TRIGGERS['flirty'],
            _EFFECT_TRIGGERS.get('kiss', _EFFECT_TRIGGERS['flirty']),
            _EFFECT_TRIGGERS['celebrate'],
        ])
        return _available_effects.get(emoji)

    if any(w in resp for w in ['miss you', 'love you', 'kiss', 'baby',
                                'thinking about you', 'wish you were']):
        emoji = random.choice([
            _EFFECT_TRIGGERS['flirty'],
            _EFFECT_TRIGGERS['flirty'],
            _EFFECT_TRIGGERS.get('kiss', _EFFECT_TRIGGERS['flirty']),
            _EFFECT_TRIGGERS['fire'],
        ])
        return _available_effects.get(emoji)

    if any(w in resp for w in ['lmao', 'haha', 'lol', 'dying', '\U0001f602', '\U0001f923']):
        emoji = random.choice([
            _EFFECT_TRIGGERS['laugh'],
            _EFFECT_TRIGGERS['laugh'],
            _EFFECT_TRIGGERS['like'],
            _EFFECT_TRIGGERS['celebrate'],
        ])
        return _available_effects.get(emoji)

    return None


# ============================================================================
# REPLY-TO QUOTING
# ============================================================================

REPLY_TO_RATE = 0.25


def should_reply_to(user_message: str, msg_count: int) -> bool:
    """Decide if Heather should quote-reply to this message."""
    if msg_count <= 1:
        return False

    rate = REPLY_TO_RATE

    if '?' in user_message:
        rate = 0.50

    if len(user_message) < 30:
        rate = min(rate + 0.10, 0.50)

    return random.random() < rate


def get_reaction_starter(user_message: str) -> str:
    """Get a casual reaction to send before the main response."""
    user_lower = user_message.lower()

    if any(word in user_lower for word in ['omg', 'wow', 'crazy', 'insane', 'wtf']):
        reactions = ["lol right??", "omg ikr", "haha fr", "wait what \U0001f602"]
    elif any(word in user_lower for word in ['sad', 'upset', 'angry', 'mad', 'annoyed']):
        reactions = ["aww", "omg babe", "ugh", "nooo"]
    elif any(word in user_lower for word in ['love', 'like', 'want', 'need']):
        reactions = ["ooh", "mmm", "omg yes", "hehe"]
    elif '?' in user_message:
        reactions = ["hmm", "ooh", "lol", "umm"]
    else:
        reactions = ["lol", "omg", "haha", "ooh", "mmm", "aww"]

    return random.choice(reactions)


# ============================================================================
# MESSAGE SPLITTING
# ============================================================================

def should_split_message(response: str) -> bool:
    """Decide if a response should be split into multiple messages."""
    if len(response) < 120:
        return False
    if len(response) < 200:
        return random.random() < 0.20
    if len(response) < 300:
        return random.random() < 0.35
    return random.random() < 0.50


SPLIT_MARKER = "[[SPLIT]]"
_MAX_BUBBLES = 3
_MIN_BUBBLE_CHARS = 15


def _finalize_bubbles(parts: List[str]) -> List[str]:
    """Clean candidate bubbles: strip, drop empties, fold tiny fragments into a
    neighbor, and cap the count (extras merge into the last allowed bubble)."""
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return parts

    merged: List[str] = []
    for p in parts:
        # Glue a short fragment onto the previous bubble, or extend a previous
        # bubble that is itself still too short.
        if merged and (len(p) < _MIN_BUBBLE_CHARS or len(merged[-1]) < _MIN_BUBBLE_CHARS):
            merged[-1] = (merged[-1] + " " + p).strip()
        else:
            merged.append(p)

    if len(merged) > _MAX_BUBBLES:
        head = merged[:_MAX_BUBBLES - 1]
        tail = " ".join(merged[_MAX_BUBBLES - 1:])
        merged = head + [tail.strip()]

    return merged


def split_response(response: str) -> List[str]:
    """Split a response into 2-3 natural messages.

    Honors an explicit [[SPLIT]] marker emitted by the model; otherwise falls
    back to deterministic sentence/connector splitting. Tiny fragments are
    merged into neighbors and the result is capped at 3 bubbles.
    """
    # Explicit model-driven splits win — the model chose the semantic breaks.
    if SPLIT_MARKER in response:
        bubbles = _finalize_bubbles(response.split(SPLIT_MARKER))
        return bubbles or [response.replace(SPLIT_MARKER, " ").strip()]

    if len(response) < 100:
        return [response]

    sentences = re.split(r'(?<=[.!?])\s+', response)

    if len(sentences) >= 2:
        # Aim for 3 bubbles on longer multi-sentence replies, else 2. Greedily
        # pack sentences toward an even per-bubble character share.
        target = 3 if (len(response) > 280 and len(sentences) >= 3) else 2
        per = len(response) / target
        bubbles: List[str] = []
        cur = ""
        for s in sentences:
            cand = (cur + " " + s).strip() if cur else s
            if cur and len(cand) > per and len(bubbles) < target - 1:
                bubbles.append(cur)
                cur = s
            else:
                cur = cand
        if cur:
            bubbles.append(cur)
        bubbles = _finalize_bubbles(bubbles)
        if len(bubbles) >= 2:
            return bubbles

    for splitter in [' lol ', ' haha ', ' but ', ' and ', '... ']:
        if splitter in response.lower():
            idx = response.lower().find(splitter)
            if 30 < idx < len(response) - 30:
                part1 = response[:idx + len(splitter.rstrip())].strip()
                part2 = response[idx + len(splitter):].strip()
                if part2:
                    if part2[0].islower() and splitter.strip() in ['but', 'and']:
                        part2 = part2[0].upper() + part2[1:]
                    return [part1, part2]
                break

    return [response]


_TYPO_CORRECTION_RATE = 0.04


def maybe_typo_correction(parts: List[str]) -> List[str]:
    """~4% of the time, fat-finger a word in the last bubble and append a quick
    '*correction' bubble — one of the most human texting tells there is.

    Only lowercase words >=4 chars are eligible (skips names/proper nouns and
    'I'm'-type tokens). Uses an adjacent-char transposition (the classic typo).
    """
    if not parts or random.random() > _TYPO_CORRECTION_RATE:
        return parts

    last = parts[-1]
    candidates = [w for w in re.findall(r"[A-Za-z']+", last)
                  if len(w) >= 4 and w.islower() and "'" not in w]
    if not candidates:
        return parts

    word = random.choice(candidates)
    i = random.randint(1, len(word) - 2)  # transpose two interior chars
    typo = word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if typo == word:
        return parts

    typoed = re.sub(r"\b" + re.escape(word) + r"\b", typo, last, count=1)
    if typoed == last:
        return parts

    return parts[:-1] + [typoed, "*" + word]


def should_add_followup() -> bool:
    """Disabled — canned follow-ups felt disconnected and unnatural."""
    return False


# ============================================================================
# IMPERFECTIONS
# ============================================================================

def add_human_imperfections(response: str) -> str:
    """Occasionally inject subtle human texting imperfections.

    Rate: ~15% of messages get a small imperfection.
    """
    if random.random() > 0.15:
        return response

    roll = random.random()

    if roll < 0.30:
        if response and response[0].isupper() and not response.startswith(('I ', "I'")):
            response = response[0].lower() + response[1:]

    elif roll < 0.50:
        if response.endswith('.') and not response.endswith('...') and len(response) > 20:
            response = response[:-1]

    elif roll < 0.65:
        trails = [' lol', ' tbh', ' ngl', ' idk', ' haha']
        if not any(response.lower().endswith(t) for t in trails):
            response = response.rstrip('.!') + random.choice(trails)

    elif roll < 0.80:
        emphasis_words = {'so': 'sooo', 'yes': 'yesss', 'no': 'nooo', 'oh': 'ohhh',
                          'damn': 'damnn', 'fuck': 'fuckk', 'god': 'godd'}
        for word, replacement in emphasis_words.items():
            pattern = re.compile(r'\b' + word + r'\b', re.IGNORECASE)
            if pattern.search(response):
                response = pattern.sub(replacement, response, count=1)
                break

    else:
        abbrevs = [
            (r'\bto be honest\b', 'tbh'),
            (r'\bI don\'t know\b', 'idk'),
            (r'\boh my god\b', 'omg'),
            (r'\bI don\'t care\b', 'idc'),
            (r'\bright now\b', 'rn'),
            (r'\bto be fair\b', 'tbf'),
        ]
        for pattern, replacement in abbrevs:
            if re.search(pattern, response, re.IGNORECASE):
                response = re.sub(pattern, replacement, response, count=1, flags=re.IGNORECASE)
                break

    return response


# ============================================================================
# ENERGY MATCHING
# ============================================================================

def adjust_response_energy(response: str, user_message: str) -> str:
    """Adjust response to match user's message energy/length.

    Research: the #1 bot tell is responding to "nice" with a paragraph.
    """
    user_len = len(user_message)
    resp_len = len(response)

    if user_len < 20 and resp_len > 60:
        if random.random() < 0.75:
            for end_char in ['!', '?', '.', '\U0001f4a6', '\U0001f608', '\U0001f975', '\U0001f60f']:
                idx = response.find(end_char)
                if 8 < idx < 60:
                    return response[:idx + 1]
            if resp_len > 50:
                space_idx = response.rfind(' ', 0, 50)
                if space_idx > 10:
                    return response[:space_idx]

    if user_len < 40 and resp_len > 100:
        if random.random() < 0.65:
            for end_char in ['.', '!', '?']:
                idx = response.find(end_char)
                if 15 < idx < 90:
                    return response[:idx + 1]

    if user_len < 70 and resp_len > 200:
        if random.random() < 0.50:
            sentences = re.split(r'(?<=[.!?])\s+', response)
            if len(sentences) >= 2:
                return sentences[0]

    return response

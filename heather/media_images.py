"""
heather.media_images — Image Library & Photo Cap System
========================================================
Pre-generated image library management: loading, category selection, dedup,
rolling-window photo caps, caption generation, and proactive photo decisions.

Transport-agnostic: selects images and generates captions but does NOT send.
Send functions remain in the monolith (require Telethon client).

Replaces: heather_telegram_bot.py
  - load_image_library: lines 4995-5004
  - get_library_image: lines 5118-5141
  - record_image_sent: lines 5144-5150
  - _real_matches_category: lines 5108-5115
  - get_image_category: lines 5007-5027
  - gate_image_category: lines 5030-5043
  - is_content_gated: lines 5045-5051
  - generate_tag_caption: lines 5153-5214
  - should_send_proactive_photo: lines 4901-4912
  - should_send_unsolicited_nsfw: lines 5273-5287
  - Photo cap functions: lines 4914-4984
  - All caption/category/decline constants

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (send_library_image, send_unsolicited_nsfw, handle_text_message)
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import deque
from typing import Dict, List, Optional

from heather import config
from heather.logging_setup import main_logger


# ============================================================================
# FILE PATHS
# ============================================================================

IMAGE_LIBRARY_DIR = os.path.join(config.BOT_ROOT, "images_db")
IMAGE_LIBRARY_FILE = os.path.join(IMAGE_LIBRARY_DIR, "library.json")


# ============================================================================
# MODULE STATE
# ============================================================================

image_library: list = []
images_sent_to_user: Dict[int, Dict[str, set]] = {}
photo_send_times: Dict[int, list] = {}
_photo_cap_decline_times: Dict[int, list] = {}
_photo_cap_silenced_until: Dict[int, float] = {}
_last_captions_sent: Dict[int, deque] = {}


# ============================================================================
# PROACTIVE PHOTO CONSTANTS
# ============================================================================

PROACTIVE_PHOTO_MIN_TURNS = 6
PROACTIVE_PHOTO_CHANCE = 0.18
PROACTIVE_PHOTO_COOLDOWN = 360

PHOTO_CAP_WINDOW_HOURS = 2

PHOTO_CAP_DECLINE_SILENCE_THRESHOLD = 3
PHOTO_CAP_DECLINE_SILENCE_WINDOW = 600
PHOTO_CAP_SILENCE_DURATION = 900

PHOTO_CAP_DECLINE_RESPONSES = [
    "Mmm I've sent you a bunch already babe, give me like an hour and I'll send more 😘",
    "Lol I look like a mess rn, try me again in a bit? 😂 but I've got some hot videos if you want 📹",
    "Phone's almost dead, gotta save battery 🔋 hit me up in a little while",
    "Babe you already got plenty of me 😏 ask again later and maybe I'll surprise you",
    "Ugh my front camera is acting up, lemme try again in a bit 😤 want a video instead? 📹",
    "I already sent you like a million pics lol, give a girl a break 😘 but I've got videos if you're still hungry 😈",
    "Mmm later babe, I need to recharge first 🙈 I'll have something for you soon",
    "Camera app keeps crashing smh 😩 try again in like an hour? I can send a video tho 📹",
]

PROACTIVE_SELFIE_CAPTIONS = [
    "Thought of you 😘",
    "This is me 📸",
    "Since you're being so sweet 😏",
    "Just for you baby 😘",
    "Don't judge the messy hair lol 😅",
    "Felt cute, might delete later 😏",
    "You earned this one 💋",
    "Figured you'd wanna see 📸",
    "Frank's not home so... 😈",
    "What do you think? 😊",
]

# ============================================================================
# UNSOLICITED NSFW CONSTANTS
# ============================================================================

UNSOLICITED_NSFW_CHANCE = 0.12
UNSOLICITED_NSFW_MIN_TURNS = 6
UNSOLICITED_NSFW_COOLDOWN = 600

UNSOLICITED_NSFW_LEAD_INS = [
    "wanna see something? 😏",
    "ok hold on I wanna show you something real quick",
    "just took this for you 😈",
    "since you're being so good... look what I just took 📸",
    "ok don't judge but I just snapped this lol",
    "you earned this one baby 😘",
    "can't stop thinking about you so here...",
    "this is what you're missing right now 🔥",
    "ok I'm feeling bold... here you go",
    "look what I'm doing right now 😈",
    "thought you might wanna see this...",
    "I'm feeling naughty tonight... want proof? 😏",
]

UNSOLICITED_NSFW_CATEGORIES = ["nsfw_topless", "nsfw_nude"]


# ============================================================================
# TAG-AWARE CAPTION SYSTEM
# ============================================================================

TAG_CAPTION_TEMPLATES = [
    # SFW casual
    ({"kitchen", "morning"}, ["morning vibes", "making coffee, thinking of you", "kitchen selfie lol"], "casual selfie in kitchen, morning coffee"),
    ({"kitchen", "tank_top"}, ["just hanging around the kitchen", "cooking something up 😏"], "casual selfie in kitchen wearing tank top"),
    ({"car", "driving"}, ["on my way!", "car selfie bc I looked cute", "bored in traffic lol"], "car selfie while driving"),
    ({"car", "selfie"}, ["drive time selfie", "sitting in my car looking cute"], "car selfie"),
    ({"mirror", "jeans"}, ["mirror selfie check", "do these jeans look ok?", "outfit check"], "mirror selfie in jeans"),
    ({"mirror", "crop_top"}, ["feeling myself today", "crop top kinda day"], "mirror selfie in crop top"),
    ({"couch", "cozy"}, ["cozy night in", "couch mode activated", "lazy evening vibes"], "relaxing on couch, cozy"),
    ({"window", "sundress"}, ["sundress weather finally", "feeling the sun", "love this dress"], "standing by window in sundress"),
    ({"sweater", "living_room"}, ["sweater weather", "just chilling at home"], "casual in sweater, living room"),
    # SFW flirty
    ({"tight_dress", "mirror"}, ["rate this dress?", "going out tonight... thoughts?", "does this look ok?"], "mirror selfie in tight dress"),
    ({"doorframe", "leaning"}, ["just leaning here looking cute", "hey you"], "leaning in doorframe, flirty pose"),
    ({"bed", "oversized_shirt"}, ["lazy but cute", "just woke up like this lol"], "laying in bed in oversized shirt"),
    ({"bathroom", "towel"}, ["just got out of the shower", "fresh out the shower"], "bathroom selfie with towel"),
    ({"bed", "playful"}, ["feeling playful tonight", "can't sleep..."], "playful pose on bed"),
    ({"hand_in_hair"}, ["do you like my hair like this?", "hair flip lol"], "flirty selfie, hand in hair"),
    # SFW lingerie
    ({"black_lace", "lingerie"}, ["new set... what do you think?", "treated myself", "a little something"], "wearing black lace lingerie"),
    ({"red_lingerie"}, ["red is my color right?", "feeling bold tonight"], "wearing red lingerie"),
    ({"sheer_robe"}, ["just a robe kinda night", "wearing almost nothing"], "in sheer robe"),
    ({"pink_babydoll"}, ["new babydoll, you like?", "pink mood tonight"], "wearing pink babydoll"),
    ({"purple_chemise"}, ["purple vibes tonight", "something silky"], "wearing purple chemise"),
    ({"bra", "panties"}, ["just a bra and panties kinda night", "this is what I sleep in"], "in bra and panties"),
    # NSFW topless
    ({"bed", "sitting", "topless"}, ["good morning from bed", "just me and my bed"], "sitting topless on bed"),
    ({"bed", "laying", "topless"}, ["wish you were here", "come lay with me"], "laying topless on bed"),
    ({"arms_behind_head", "topless"}, ["feeling confident", "all yours"], "topless with arms behind head"),
    ({"bathroom", "panties_only"}, ["just panties tonight", "almost ready for bed"], "standing in bathroom, topless in panties"),
    ({"window", "topless"}, ["morning light hits different", "hope the neighbors aren't looking"], "topless by the window"),
    # NSFW explicit
    ({"spread"}, ["look what I'm doing for you", "you did this to me"], "explicit spread pose"),
    ({"bending_over"}, ["bent over just for you", "come get it"], "bending over, explicit"),
    # NSFW nude
    ({"window", "standing", "nude"}, ["natural light and nothing else", "feeling free"], "standing nude by window"),
    ({"bed", "laying", "nude"}, ["come to bed", "waiting for you"], "laying nude on bed"),
    ({"mirror", "nude"}, ["mirror mirror...", "all of me"], "nude mirror selfie"),
]

CATEGORY_CAPTIONS = {
    "sfw_casual": [
        ("just me rn", "casual selfie"),
        ("bored so here's my face", "casual selfie"),
        ("hey you", "casual selfie"),
        ("thinking about you", "casual selfie"),
        ("do I look ok?", "casual photo"),
        ("just hanging out", "casual selfie at home"),
        ("hi from me", "casual selfie"),
        ("outfit check?", "casual outfit selfie"),
        ("felt cute", "casual cute selfie"),
        ("here's me being bored lol", "casual selfie"),
    ],
    "sfw_flirty": [
        ("like what you see?", "flirty selfie"),
        ("rate me", "flirty pose selfie"),
        ("feeling myself today", "flirty selfie"),
        ("this is for you", "flirty photo"),
        ("I look good right?", "flirty selfie"),
        ("catch me looking cute", "flirty pose"),
        ("do I have your attention?", "flirty selfie"),
        ("thoughts?", "flirty selfie"),
        ("am I your type?", "flirty photo"),
        ("just a little tease", "flirty teasing selfie"),
    ],
    "sfw_lingerie": [
        ("new set, thoughts?", "lingerie selfie"),
        ("a little something for you", "lingerie photo"),
        ("I bought this for tonight", "lingerie selfie"),
        ("you like?", "lingerie pose"),
        ("feeling sexy", "lingerie selfie"),
        ("just for your eyes", "lingerie photo"),
        ("what do you think of this one?", "lingerie selfie"),
        ("treated myself", "new lingerie selfie"),
        ("something a little naughty", "lingerie teasing photo"),
        ("bedtime outfit", "lingerie selfie"),
    ],
    "nsfw_topless": [
        ("for your eyes only", "topless selfie"),
        ("this is what you do to me", "topless photo"),
        ("hope you like", "topless selfie"),
        ("just for you", "topless photo"),
        ("feeling bold tonight", "topless selfie"),
        ("don't show anyone", "intimate topless selfie"),
        ("you make me feel so comfortable", "topless selfie"),
        ("couldn't help myself", "topless selfie"),
        ("I trust you with this", "intimate topless photo"),
        ("been wanting to send this", "topless selfie"),
    ],
    "nsfw_nude": [
        ("all of me for you", "nude selfie"),
        ("come and get me", "nude photo"),
        ("I need you", "nude selfie"),
        ("no clothes needed tonight", "full nude selfie"),
        ("everything off for you", "nude photo"),
        ("just me, nothing else", "nude selfie"),
        ("what would you do if you were here?", "nude selfie"),
        ("missing you like this", "nude photo"),
        ("bare and thinking of you", "nude selfie"),
        ("you make me want to show everything", "nude photo"),
    ],
    "nsfw_explicit": [
        ("look what I'm doing", "explicit selfie"),
        ("you did this to me", "explicit photo"),
        ("I can't stop", "explicit selfie"),
        ("watch me", "explicit photo"),
        ("this is how bad I want you", "explicit selfie"),
        ("I need you so bad right now", "explicit photo"),
        ("look at me baby", "explicit selfie"),
        ("all for you", "explicit photo"),
        ("getting so worked up", "explicit selfie"),
        ("see what you do to me?", "explicit photo"),
    ],
}

_CAPTION_EMOJI_SFW = ["😊", "📸", "😘", "💕", "🥰", "😏", "lol"]
_CAPTION_EMOJI_NSFW = ["😈", "🔥", "💋", "🥵", "😏", "💦"]


# ============================================================================
# LIBRARY LOADING
# ============================================================================

def load_image_library():
    """Load pre-generated image library from JSON."""
    global image_library
    if os.path.exists(IMAGE_LIBRARY_FILE):
        with open(IMAGE_LIBRARY_FILE, encoding='utf-8') as f:
            data = json.load(f)
            image_library = data.get('images', [])
        main_logger.info(f"[IMAGE_LIB] Loaded {len(image_library)} images")
    else:
        main_logger.warning("[IMAGE_LIB] No library.json found")


# ============================================================================
# CATEGORY SELECTION & GATING
# ============================================================================

def get_image_category(message: str, nsfw_context_fn=None) -> str:
    """Map user request to image library category.

    Args:
        message: User message text.
        nsfw_context_fn: Optional callable(str) -> bool for NSFW context detection.
    """
    msg = message.lower()

    if any(w in msg for w in ["spread", "pussy", "masturbat", "toy", "dildo", "finger"]):
        return "nsfw_explicit"
    if any(w in msg for w in ["nude", "naked", "everything off", "full body nude"]):
        return "nsfw_nude"
    if any(w in msg for w in ["topless", "tits", "boobs", "nipple", "flash"]):
        return "nsfw_topless"
    if any(w in msg for w in ["lingerie", "bra", "panties", "underwear", "towel"]):
        return "sfw_lingerie"

    if nsfw_context_fn and nsfw_context_fn(msg):
        return "nsfw_topless"

    if any(w in msg for w in ["sexy", "hot", "flirty", "tease"]):
        return "sfw_flirty"

    return "sfw_casual"


def gate_image_category(
    chat_id: int, requested_category: str, access_tier_fn=None
) -> str:
    """Downgrade image category based on access tier.

    Args:
        chat_id: Telegram chat ID.
        requested_category: Requested image category.
        access_tier_fn: Callable(chat_id) -> str returning access tier.
    """
    tier = access_tier_fn(chat_id) if access_tier_fn else "FREE"
    required = config.IMAGE_TIER_REQUIREMENTS.get(requested_category, "FREE")
    if config.TIER_RANK.get(tier, 0) >= config.TIER_RANK.get(required, 0):
        return requested_category
    if tier == "FAN":
        return "nsfw_nude" if requested_category == "nsfw_explicit" else requested_category
    if requested_category.startswith("nsfw_"):
        return "nsfw_topless"
    return requested_category


def is_content_gated(chat_id: int, category: str, access_tier_fn=None) -> tuple:
    """Check if a content category is gated for this user.

    Returns:
        (gated: bool, required_tier: str)
    """
    tier = access_tier_fn(chat_id) if access_tier_fn else "FREE"
    required = config.IMAGE_TIER_REQUIREMENTS.get(category, "FREE")
    gated = config.TIER_RANK.get(tier, 0) < config.TIER_RANK.get(required, 0)
    return (gated, required)


# ============================================================================
# PHOTO CAP SYSTEM
# ============================================================================

def _prune_photo_times(chat_id: int):
    """Remove photo timestamps outside the rolling window."""
    if chat_id not in photo_send_times:
        photo_send_times[chat_id] = []
        return
    cutoff = time.time() - (PHOTO_CAP_WINDOW_HOURS * 3600)
    photo_send_times[chat_id] = [t for t in photo_send_times[chat_id] if t > cutoff]


def get_photo_cap(chat_id: int, warmth_tier_fn=None) -> int:
    """Get photo cap limit based on warmth tier.

    Args:
        chat_id: Telegram chat ID.
        warmth_tier_fn: Callable(chat_id) -> str returning warmth tier.
    """
    tier = warmth_tier_fn(chat_id) if warmth_tier_fn else "NEW"
    if tier == "COLD":
        return config.PHOTO_CAP_COLD
    elif tier == "WARM":
        return config.PHOTO_CAP_WARM
    return config.PHOTO_CAP_NEW


def can_send_photo_in_session(chat_id: int, warmth_tier_fn=None) -> bool:
    """Check if user hasn't exceeded photo cap in the rolling window."""
    _prune_photo_times(chat_id)
    return len(photo_send_times[chat_id]) < get_photo_cap(chat_id, warmth_tier_fn)


def record_photo_sent(chat_id: int, warmth_tier_fn=None):
    """Record that a photo was sent (rolling window)."""
    _prune_photo_times(chat_id)
    photo_send_times[chat_id].append(time.time())
    count = len(photo_send_times[chat_id])
    cap = get_photo_cap(chat_id, warmth_tier_fn)
    main_logger.info(f"Photo cap: {chat_id} has used {count}/{cap} photos in last {PHOTO_CAP_WINDOW_HOURS}h")


def get_photo_cap_decline(chat_id: int) -> str:
    """Get an in-character decline when photo cap is reached.

    Returns '__SILENT_IGNORE__' if the user has been declined too many times recently.
    """
    now = time.time()

    silenced_until = _photo_cap_silenced_until.get(chat_id, 0)
    if now < silenced_until:
        mins_left = int((silenced_until - now) / 60)
        main_logger.info(f"Photo cap: {chat_id} silenced for repeated declines (~{mins_left}min left)")
        return "__SILENT_IGNORE__"

    if chat_id not in _photo_cap_decline_times:
        _photo_cap_decline_times[chat_id] = []
    _photo_cap_decline_times[chat_id].append(now)
    cutoff = now - PHOTO_CAP_DECLINE_SILENCE_WINDOW
    _photo_cap_decline_times[chat_id] = [t for t in _photo_cap_decline_times[chat_id] if t > cutoff]

    if len(_photo_cap_decline_times[chat_id]) >= PHOTO_CAP_DECLINE_SILENCE_THRESHOLD:
        _photo_cap_silenced_until[chat_id] = now + PHOTO_CAP_SILENCE_DURATION
        _photo_cap_decline_times[chat_id] = []
        main_logger.info(f"Photo cap: {chat_id} silenced for {PHOTO_CAP_SILENCE_DURATION}s after {PHOTO_CAP_DECLINE_SILENCE_THRESHOLD} repeated declines")
        return random.choice([
            "Babe you've asked me like a dozen times lol 😂 I'll hit you up when I've got something new, promise 💕",
            "Ok ok I hear you! I literally can't right now but I WILL send you something later, pinky swear 😘",
            "Lol you're persistent, I like that 😏 But seriously gimme a bit and I'll make it worth the wait",
        ])

    _prune_photo_times(chat_id)
    times = photo_send_times.get(chat_id, [])
    if times:
        oldest = min(times)
        mins_until_reset = int((oldest + PHOTO_CAP_WINDOW_HOURS * 3600 - now) / 60)
        main_logger.info(f"Photo cap reached for {chat_id}, declining (~{mins_until_reset}min until next slot)")
    else:
        main_logger.info(f"Photo cap reached for {chat_id}, declining")
    return random.choice(PHOTO_CAP_DECLINE_RESPONSES)


# ============================================================================
# IMAGE SELECTION & DEDUP
# ============================================================================

def _real_matches_category(img: dict, category: str) -> bool:
    """Check if a real photo can be served for a given category."""
    real_cat = img.get('category', '')
    mapped = real_cat.replace('real_', '')
    target = category.replace('sfw_', '').replace('nsfw_', '')
    return mapped == target


def get_library_image(chat_id: int, category: str) -> Optional[dict]:
    """Get an unsent image from library for this user+category.

    Sprinkles in real photos at ~25% rate. Resets dedup when all shown.
    """
    if not image_library:
        return None

    matching = [img for img in image_library if img['category'] == category]
    real_matching = [img for img in image_library
                     if img.get('is_real') and _real_matches_category(img, category)]
    pool = matching + real_matching

    if not pool:
        return None

    sent = images_sent_to_user.get(chat_id, {}).get(category, set())
    unsent = [img for img in pool if img['id'] not in sent]

    if not unsent:
        if chat_id in images_sent_to_user and category in images_sent_to_user[chat_id]:
            images_sent_to_user[chat_id][category].clear()
            main_logger.info(f"[IMAGE_LIB] Reset {category} for {chat_id} — all {len(pool)} shown")
        unsent = pool

    return random.choice(unsent)


def record_image_sent(chat_id: int, image_id: str, category: str):
    """Track that this image was sent to this user."""
    if chat_id not in images_sent_to_user:
        images_sent_to_user[chat_id] = {}
    if category not in images_sent_to_user[chat_id]:
        images_sent_to_user[chat_id][category] = set()
    images_sent_to_user[chat_id][category].add(image_id)


# ============================================================================
# CAPTION GENERATION
# ============================================================================

def generate_tag_caption(image_entry: dict, chat_id: int) -> tuple:
    """Generate a caption and history description for a library image.

    Four-tier fallback: AI caption -> tag templates -> category captions -> generic.

    Returns:
        (caption: str, history_desc: str)
    """
    tags = set(image_entry.get('tags', []))
    category = image_entry.get('category', '')
    is_nsfw = category.startswith('nsfw_')

    caption = None
    history_desc = None

    # Tier 0: AI-generated caption
    if image_entry.get('caption') and image_entry.get('description'):
        caption = image_entry['caption']
        history_desc = image_entry['description'][:100]
        return caption, history_desc

    # Tier 1: Tag template match
    for required_tags, caption_options, desc in TAG_CAPTION_TEMPLATES:
        if set(required_tags).issubset(tags):
            caption = random.choice(caption_options)
            history_desc = desc
            break

    # Tier 2: Category-level captions
    if caption is None and category in CATEGORY_CAPTIONS:
        entry = random.choice(CATEGORY_CAPTIONS[category])
        caption, history_desc = entry

    # Tier 3: Generic captions
    if caption is None:
        caption = random.choice(PROACTIVE_SELFIE_CAPTIONS)
        history_desc = f"{category.replace('_', ' ')} photo"

    # Dedup: avoid repeating recent captions
    if chat_id not in _last_captions_sent:
        _last_captions_sent[chat_id] = deque(maxlen=5)
    recent = _last_captions_sent[chat_id]

    for _attempt in range(3):
        if caption not in recent:
            break
        if category in CATEGORY_CAPTIONS:
            entry = random.choice(CATEGORY_CAPTIONS[category])
            caption, history_desc = entry
        else:
            caption = random.choice(PROACTIVE_SELFIE_CAPTIONS)
            history_desc = f"{category.replace('_', ' ')} photo"

    # 50% emoji append
    if random.random() < 0.5:
        pool = _CAPTION_EMOJI_NSFW if is_nsfw else _CAPTION_EMOJI_SFW
        emoji = random.choice(pool)
        _emoji_ends = {'😊', '📸', '😘', '💕', '🥰', '😏', '💋', '😈', '🔥', '🥵', '💦', '🤤'}
        if caption.rstrip()[-1:] not in _emoji_ends:
            caption = f"{caption} {emoji}"

    recent.append(caption)
    return caption, history_desc


# ============================================================================
# PROACTIVE DECISION FUNCTIONS
# ============================================================================

def should_send_proactive_photo(
    chat_id: int,
    conversation_turn_count: dict,
    last_photo_request: dict,
) -> bool:
    """Decide if Heather should spontaneously send a selfie.

    Args:
        chat_id: Telegram chat ID.
        conversation_turn_count: Dict of chat_id -> turn count.
        last_photo_request: Dict of chat_id -> last photo request timestamp.
    """
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < PROACTIVE_PHOTO_MIN_TURNS:
        return False
    last_sent = last_photo_request.get(chat_id, 0)
    if time.time() - last_sent < PROACTIVE_PHOTO_COOLDOWN:
        return False
    return random.random() < PROACTIVE_PHOTO_CHANCE


def should_send_unsolicited_nsfw(
    chat_id: int,
    conversation_turn_count: dict,
    last_unsolicited_nsfw: dict,
    sexual_conversation_fn=None,
    warmth_tier_fn=None,
) -> bool:
    """Check if we should send an unsolicited NSFW photo during a sexual conversation.

    Args:
        chat_id: Telegram chat ID.
        conversation_turn_count: Dict of chat_id -> turn count.
        last_unsolicited_nsfw: Dict of chat_id -> last send timestamp.
        sexual_conversation_fn: Callable(chat_id) -> bool.
        warmth_tier_fn: Callable(chat_id) -> str (for photo cap check).
    """
    if not image_library:
        return False
    if sexual_conversation_fn and not sexual_conversation_fn(chat_id):
        return False
    if not can_send_photo_in_session(chat_id, warmth_tier_fn):
        return False
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < UNSOLICITED_NSFW_MIN_TURNS:
        return False
    last_sent = last_unsolicited_nsfw.get(chat_id, 0)
    if time.time() - last_sent < UNSOLICITED_NSFW_COOLDOWN:
        return False
    return random.random() < UNSOLICITED_NSFW_CHANCE

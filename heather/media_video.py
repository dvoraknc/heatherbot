"""
heather.media_video — Video Delivery & Tracking
=================================================
Video library management, request detection, rate limiting, tease logic,
and dedup tracking. Transport-agnostic: does NOT send via Telegram.

Replaces: heather_telegram_bot.py
  - VIDEO_DIR: line 465
  - VIDEO_REQUEST_TRIGGERS: lines 467-482
  - VIDEO_CAPTIONS: lines 484-492
  - VIDEO_ALL_SENT_RESPONSES: lines 494-499
  - VIDEO_RATE_LIMIT_*: lines 548-550
  - VIDEO_RATE_LIMIT_RESPONSES: lines 551-556
  - VIDEO_TEASE_MESSAGES: lines 3538-3545
  - VIDEO_TEASE_CHANCE_*: lines 3546-3547
  - VIDEO_TEASE_MIN_TURNS: line 3548
  - VIDEO_TEASE_COOLDOWN: line 3550
  - VIDEO_OFFER_WINDOW: line 3552
  - is_video_request: lines 4911-4914
  - get_available_videos: lines 4950-4956
  - get_unsent_video: lines 4958-4969
  - is_video_rate_limited: lines 4971-4981
  - record_video_sent: lines 4983-4992
  - should_tease_video: lines 4891-4902

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (video sending, handlers, proactive video tease)
"""

from __future__ import annotations

import os
import random
import time
from typing import Dict, Optional

from heather.logging_setup import main_logger


# ============================================================================
# CONSTANTS
# ============================================================================

VIDEO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "videos")

VIDEO_REQUEST_TRIGGERS = [
    "send me a video", "send a video", "send me a vid", "send a vid",
    "send a clip", "send me a clip", "got any videos", "got any vids",
    "have any videos", "have any vids", "any videos", "any vids",
    "can i see a video", "can i get a video", "video of you",
    "vid of you", "wanna see a video", "want to see a video",
    "send me a video of", "show me a video", "got a video",
    "have a video", "make a video", "make me a video",
    "record a video", "film something", "send video",
    "prefer a vid", "want a vid", "like a vid", "see a vid",
    "prefer a video", "want a video", "like a video",
    "video",
    # Third-person triggers
    "her videos", "her vids", "her video", "her vid",
    "videos of her", "vids of her", "video of her", "vid of her",
]

VIDEO_CAPTIONS = [
    "Here you go babe \U0001f618",
    "Just for you \U0001f48b",
    "Hope you like this one \U0001f60f",
    "Mmm enjoy \U0001f609",
    "Been wanting to show you this \U0001f495",
    "Don't share this with anyone ok? \U0001f618",
    "You're welcome \U0001f60f\U0001f48b",
]

VIDEO_ALL_SENT_RESPONSES = [
    "Babe you've seen everything I've got rn \U0001f629 I need to make more, give me some time",
    "That's all I have right now lol, I gotta film some new stuff \U0001f618",
    "You've already seen all my vids babe \U0001f602 I'll make more soon I promise",
    "I'm all out of videos rn, need to make some new ones for you \U0001f48b",
]

VIDEO_RATE_LIMIT_COUNT = 5       # Max videos per window
VIDEO_RATE_LIMIT_WINDOW = 1800   # 30 minute window
VIDEO_BURST_COOLDOWN = 20        # Minimum seconds between individual video sends

VIDEO_RATE_LIMIT_RESPONSES = [
    "lol you've been bingeing my vids \U0001f602 tell me which one was your fav so far",
    "ok slow down haha \U0001f60f talk to me first, what are you up to tonight?",
    "mmm you really like watching me huh \U0001f618 what kind of stuff gets you going?",
    "I'll send more later babe, but rn I wanna hear about you \U0001f60f what do you do?",
]

VIDEO_TEASE_MESSAGES = [
    "want to see a video of me? \U0001f60f",
    "I've got some videos of me being a total slut... want one? \U0001f608",
    "mmm you want to see a video? I've got some good ones \U0001f525",
    "I should send you one of my videos... want to see? \U0001f618",
    "I've got a video that would make you lose it... want me to send it? \U0001f48b",
    "you want to see me in action? I've got videos \U0001f4f9\U0001f608",
]

VIDEO_TEASE_CHANCE_WARM = 0.18        # 18% chance for WARM users
VIDEO_TEASE_CHANCE_DEFAULT = 0.10     # 10% chance for non-WARM users
VIDEO_TEASE_MIN_TURNS = 10            # Min turns before teasing
VIDEO_TEASE_COOLDOWN = 3600           # 1 hour between teases per user
VIDEO_OFFER_WINDOW = 600              # 10 minutes to respond positively
VIDEO_REFRESH_INTERVAL = 3600         # Refresh file references every hour


# ============================================================================
# MODULE STATE
# ============================================================================

videos_sent_to_user: Dict[int, set] = {}
video_send_timestamps: Dict[int, list] = {}
last_video_tease: Dict[int, float] = {}
_video_offer_pending: Dict[int, float] = {}


# ============================================================================
# REQUEST DETECTION
# ============================================================================

def is_video_request(message: str) -> bool:
    """Check if message is asking for a video."""
    message_lower = message.lower()
    return any(trigger in message_lower for trigger in VIDEO_REQUEST_TRIGGERS)


# ============================================================================
# VIDEO LIBRARY
# ============================================================================

def get_available_videos() -> list:
    """Scan video directory for available video files."""
    if not os.path.isdir(VIDEO_DIR):
        return []
    extensions = ('.mp4', '.mov', '.avi', '.mkv', '.webm')
    return sorted([f for f in os.listdir(VIDEO_DIR)
                   if f.lower().endswith(extensions) and os.path.isfile(os.path.join(VIDEO_DIR, f))])


def get_unsent_video(chat_id: int) -> Optional[str]:
    """Get a video filename this user hasn't seen yet, or None if all sent."""
    all_videos = get_available_videos()
    if not all_videos:
        return None
    sent = videos_sent_to_user.get(chat_id, set())
    unsent = [v for v in all_videos if v not in sent]
    if not unsent:
        return None
    # Prefer .mp4 over .webm — some Telegram clients can't play .webm
    mp4_unsent = [v for v in unsent if v.lower().endswith('.mp4')]
    return random.choice(mp4_unsent) if mp4_unsent else random.choice(unsent)


# ============================================================================
# RATE LIMITING
# ============================================================================

def is_video_rate_limited(chat_id: int) -> bool:
    """Check if user has hit the video rate limit or burst cooldown."""
    now = time.time()
    timestamps = video_send_timestamps.get(chat_id, [])
    # Prune old timestamps
    timestamps = [t for t in timestamps if now - t < VIDEO_RATE_LIMIT_WINDOW]
    video_send_timestamps[chat_id] = timestamps
    # Burst cooldown — prevent rapid-fire video farming
    if timestamps and (now - timestamps[-1]) < VIDEO_BURST_COOLDOWN:
        return True
    return len(timestamps) >= VIDEO_RATE_LIMIT_COUNT


def record_video_sent(chat_id: int, filename: str):
    """Record that a video was sent to this user."""
    if chat_id not in videos_sent_to_user:
        videos_sent_to_user[chat_id] = set()
    videos_sent_to_user[chat_id].add(filename)
    # Track timestamp for rate limiting
    video_send_timestamps.setdefault(chat_id, []).append(time.time())
    total = len(get_available_videos())
    sent = len(videos_sent_to_user[chat_id])
    main_logger.info(f"Video sent to {chat_id}: {filename} ({sent}/{total} videos sent)")


# ============================================================================
# TEASE / OFFER LOGIC
# ============================================================================

def should_tease_video(
    chat_id: int,
    conversation_turn_count: dict,
    sexual_conversation_fn=None,
    warmth_tier_fn=None,
) -> bool:
    """Check if we should offer a video in conversation.

    Args:
        chat_id: Telegram chat ID.
        conversation_turn_count: Dict of chat_id -> turn count.
        sexual_conversation_fn: Callable(chat_id) -> bool for sexual context check.
        warmth_tier_fn: Callable(chat_id) -> str returning warmth tier.

    Returns:
        True if user qualifies for video tease.
    """
    if sexual_conversation_fn and not sexual_conversation_fn(chat_id):
        return False
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < VIDEO_TEASE_MIN_TURNS:
        return False
    last_tease = last_video_tease.get(chat_id, 0)
    if time.time() - last_tease < VIDEO_TEASE_COOLDOWN:
        return False
    warmth = warmth_tier_fn(chat_id) if warmth_tier_fn else "NEW"
    chance = VIDEO_TEASE_CHANCE_WARM if warmth == "WARM" else VIDEO_TEASE_CHANCE_DEFAULT
    return random.random() < chance


def set_video_offer_pending(chat_id: int):
    """Mark that a video offer was sent to this user."""
    _video_offer_pending[chat_id] = time.time()
    last_video_tease[chat_id] = time.time()


def is_video_offer_pending(chat_id: int) -> bool:
    """Check if there's a pending video offer within the acceptance window."""
    if chat_id not in _video_offer_pending:
        return False
    return time.time() - _video_offer_pending[chat_id] < VIDEO_OFFER_WINDOW


def clear_video_offer(chat_id: int):
    """Clear a pending video offer."""
    _video_offer_pending.pop(chat_id, None)

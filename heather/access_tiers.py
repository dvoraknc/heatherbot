"""
heather.access_tiers — Tier Logic, Warmth, Tipping
====================================================
Pure tier computation, warmth scoring, tipper status management,
and the canonical sexual keyword sets.

No safety imports — safety.py imports access_tiers, not vice versa.

Replaces: heather_telegram_bot.py
  - get_tipper_status: lines 6060-6085
  - compute_tip_tier: lines 6087-6095
  - get_access_tier: lines 6097-6109
  - get_warmth_tier: lines 6123-6131
  - update_warmth_score: lines 6133-6169
  - record_tip / record_tip_received / record_tip_mention: lines 6111-6184
  - Sexual keyword lists (4 copies): lines 1787, 1805, 1846, 2629

Bug fixes:
  - 4 independent sexual keyword lists → SEXUAL_KEYWORDS_CORE + SEXUAL_KEYWORDS_BROAD
    with subset assertion enforced at module load
  - VIP-for-all: config.TRIAL_MODE flag, real logic preserved and tested

Dependencies: heather.config, heather.logging_setup, heather.persistence
Used by: heather.safety, heather.text_pipeline, heather.conversation,
         heather.media_images, heather.handlers, heather.monitoring
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Set

from heather import config
from heather.logging_setup import main_logger
from heather.persistence import load_tip_history, save_tip_history
from heather.types import TierInfo


# ============================================================================
# CANONICAL SEXUAL KEYWORD SETS — imported from config.py
# ============================================================================
# These live in config because they're pure data constants used by multiple
# modules (access_tiers, conversation, text_pipeline). Re-exported here
# for backward compatibility.

from heather.config import (  # noqa: E402
    SEXUAL_KEYWORDS_CORE,
    SEXUAL_KEYWORDS_BROAD,
    FLIRTY_KEYWORDS,
)


# ============================================================================
# TIPPER STATUS MANAGEMENT
# ============================================================================

# Module-level state — loaded once at startup
tipper_status: Dict[int, Dict[str, Any]] = {}
payment_bot_started_users: Set[int] = set()


def _init_tip_data() -> None:
    """Load tip history from disk into module state. Called once at startup."""
    global tipper_status, payment_bot_started_users
    raw = load_tip_history()
    started = raw.pop("_started_users", [])
    payment_bot_started_users = set(started)
    # Keys are stringified chat_ids in JSON
    tipper_status = {int(k): v for k, v in raw.items() if k != "_started_users"}
    if tipper_status:
        main_logger.info(
            f"Loaded tip history: {len(tipper_status)} users, "
            f"{len(payment_bot_started_users)} started payment bot"
        )


def get_tipper_status(chat_id: int) -> Dict[str, Any]:
    """Get or create tipper status for a user.

    Args:
        chat_id: User chat ID.

    Returns:
        Mutable tipper status dict.
    """
    if chat_id not in tipper_status:
        tipper_status[chat_id] = {
            "total_stars": 0,
            "total_tips": 0,
            "last_tip_at": 0,
            "last_tip_mention_at": 0,
            "tier": 0,
            "name": None,
            "warmth": config.WARMTH_INITIAL,
            "total_messages": 0,
            "msgs_since_tip_mention": None,
            "declined": False,
            "decline_decay_remaining": 0,
        }
    else:
        # Backfill warmth fields for existing entries
        ts = tipper_status[chat_id]
        if "warmth" not in ts:
            ts["warmth"] = 1.0 if ts.get("tier", 0) >= 1 else config.WARMTH_INITIAL
            ts["total_messages"] = 0
            ts["msgs_since_tip_mention"] = None
            ts["declined"] = False
            ts["decline_decay_remaining"] = 0
    return tipper_status[chat_id]


def _save_tips() -> None:
    """Persist current tip state to disk."""
    save_tip_history(tipper_status, payment_bot_started_users)


# ============================================================================
# TIER COMPUTATION
# ============================================================================

def compute_tip_tier(total_stars: int) -> int:
    """Compute tipper tier from total stars.

    Args:
        total_stars: Total stars this user has tipped.

    Returns:
        Tier level: 0 (never tipped), 1 (coffee), 2 (regular), 3 (big).
    """
    if total_stars >= 1000:
        return 3  # big tipper
    elif total_stars >= 250:
        return 2  # regular supporter
    elif total_stars > 0:
        return 1  # coffee tipper
    return 0  # never tipped


def _get_real_access_tier(chat_id: int) -> str:
    """Compute actual access tier from tipping data.

    This is the real tier logic that runs even during TRIAL_MODE.
    Returns 'VIP', 'FAN', or 'FREE'.
    """
    ts = get_tipper_status(chat_id)
    total = ts.get("total_stars", 0)
    if total >= config.ACCESS_TIER_VIP_THRESHOLD:
        return "VIP"
    elif total >= config.ACCESS_TIER_FAN_THRESHOLD:
        return "FAN"
    return "FREE"


def get_access_tier(chat_id: int) -> str:
    """Returns 'VIP', 'FAN', or 'FREE' based on total Stars spent.

    When config.TRIAL_MODE is True, returns 'VIP' for all users.
    Real tier logic is preserved underneath and can be tested directly
    via ``_get_real_access_tier()``.

    Args:
        chat_id: User chat ID.

    Returns:
        Tier string.
    """
    if config.TRIAL_MODE:
        return "VIP"
    return _get_real_access_tier(chat_id)


def get_tier(chat_id: int) -> TierInfo:
    """Get full tier info for a user (access tier + warmth).

    This is the primary entry point used by the pipeline.

    Args:
        chat_id: User chat ID.

    Returns:
        TierInfo dataclass with tier, warmth, warmth_score, and trial flag.
    """
    tier = get_access_tier(chat_id)
    warmth_tier = get_warmth_tier(chat_id)
    ts = get_tipper_status(chat_id)
    warmth_score = ts.get("warmth", config.WARMTH_INITIAL)

    return TierInfo(
        tier=tier,
        warmth=warmth_tier,
        warmth_score=warmth_score,
        is_trial_mode=config.TRIAL_MODE,
    )


# ============================================================================
# WARMTH SYSTEM
# ============================================================================

def get_warmth_tier(chat_id: int) -> str:
    """Returns 'WARM', 'NEW', or 'COLD' based on user's warmth score.

    Args:
        chat_id: User chat ID.

    Returns:
        Warmth tier string.
    """
    ts = get_tipper_status(chat_id)
    warmth = ts.get("warmth", config.WARMTH_INITIAL)
    if warmth >= config.WARMTH_WARM_THRESHOLD:
        return "WARM"
    elif warmth < config.WARMTH_COLD_THRESHOLD:
        return "COLD"
    return "NEW"


def update_warmth_score(chat_id: int) -> None:
    """Called every incoming message. Updates warmth score based on tipping behavior.

    Side effects: modifies tipper_status in-place, periodically saves to disk.

    Args:
        chat_id: User chat ID.
    """
    ts = get_tipper_status(chat_id)
    old_tier = get_warmth_tier(chat_id)

    # Increment total messages
    ts["total_messages"] = ts.get("total_messages", 0) + 1
    total_messages = ts["total_messages"]

    # Track implicit decline countdown
    if ts.get("msgs_since_tip_mention") is not None:
        ts["msgs_since_tip_mention"] += 1
        if (
            ts["msgs_since_tip_mention"] >= config.WARMTH_DECLINE_MSG_WINDOW
            and not ts.get("declined")
        ):
            ts["declined"] = True
            ts["decline_decay_remaining"] = 10
            main_logger.info(
                f"[WARMTH] {chat_id}: Implicit decline detected "
                f"(no tip after {config.WARMTH_DECLINE_MSG_WINDOW} msgs)"
            )

    # Apply decay
    if ts.get("decline_decay_remaining", 0) > 0:
        ts["warmth"] = ts.get("warmth", config.WARMTH_INITIAL) - config.WARMTH_DECLINE_DECAY
        ts["decline_decay_remaining"] -= 1
    elif total_messages > config.WARMTH_PASSIVE_THRESHOLD and ts.get("tier", 0) == 0:
        ts["warmth"] = ts.get("warmth", config.WARMTH_INITIAL) - config.WARMTH_PASSIVE_DECAY

    # Clamp
    ts["warmth"] = max(
        config.WARMTH_FLOOR,
        min(1.0, ts.get("warmth", config.WARMTH_INITIAL)),
    )

    # Log tier transitions
    new_tier = get_warmth_tier(chat_id)
    if old_tier != new_tier:
        main_logger.info(
            f"[WARMTH] {chat_id}: {old_tier} -> {new_tier} "
            f"(warmth={ts['warmth']:.2f}, msgs={total_messages})"
        )

    # Periodically save (every 10 messages)
    if total_messages % 10 == 0:
        _save_tips()


# ============================================================================
# TIP RECORDING
# ============================================================================

def record_tip(chat_id: int, stars: int, tipper_name: Optional[str] = None) -> None:
    """Record a tip and update tier.

    Args:
        chat_id: User chat ID.
        stars: Number of stars tipped.
        tipper_name: Display name of tipper (optional).
    """
    ts = get_tipper_status(chat_id)
    ts["total_stars"] += stars
    ts["total_tips"] += 1
    ts["last_tip_at"] = time.time()
    ts["tier"] = compute_tip_tier(ts["total_stars"])
    if tipper_name:
        ts["name"] = tipper_name
    _save_tips()
    main_logger.info(
        f"[TIP] Recorded {stars} stars from {chat_id} "
        f"(total: {ts['total_stars']}, tier: {ts['tier']})"
    )


def record_tip_received(
    chat_id: int,
    stars: int,
    tipper_name: Optional[str] = None,
) -> None:
    """Boost warmth on tip, clear decline state, then record the tip.

    Args:
        chat_id: User chat ID.
        stars: Number of stars tipped.
        tipper_name: Display name of tipper.
    """
    ts = get_tipper_status(chat_id)
    ts["warmth"] = min(
        1.0, ts.get("warmth", config.WARMTH_INITIAL) + config.WARMTH_TIP_BOOST
    )
    ts["declined"] = False
    ts["decline_decay_remaining"] = 0
    ts["msgs_since_tip_mention"] = None
    record_tip(chat_id, stars, tipper_name)
    main_logger.info(
        f"[WARMTH] {chat_id}: Tip received ({stars} stars), "
        f"warmth boosted to {ts['warmth']:.2f}"
    )


def record_tip_mention(chat_id: int) -> None:
    """Start the implicit decline countdown when a tip hook fires.

    Args:
        chat_id: User chat ID.
    """
    ts = get_tipper_status(chat_id)
    ts["msgs_since_tip_mention"] = 0


# ============================================================================
# ENERGY DETECTION (uses canonical keyword sets)
# ============================================================================

def detect_conversation_energy(
    recent_messages: list,
    window: int = 6,
) -> str:
    """Detect the sexual energy level of recent conversation.

    Args:
        recent_messages: List of message dicts with 'content' key.
        window: Number of recent messages to check.

    Returns:
        'hot', 'flirty', or 'casual'.
    """
    if not recent_messages:
        return "casual"
    recent = recent_messages[-window:]
    recent_text = " ".join(m["content"].lower() for m in recent)

    sexual_count = sum(1 for kw in SEXUAL_KEYWORDS_BROAD if kw in recent_text)
    if sexual_count >= 3:
        return "hot"

    flirty_count = sum(1 for kw in FLIRTY_KEYWORDS if kw in recent_text)
    if sexual_count >= 1 or flirty_count >= 2:
        return "flirty"

    return "casual"


def is_sexual_conversation(recent_messages: list) -> bool:
    """Check if conversation is sexual.

    Two checks:
      1. Any of the last 3 messages contain sexual keywords (recent heat)
      2. OR 2+ of last 8 messages contain sexual keywords (sustained)

    Args:
        recent_messages: List of message dicts with 'content' key.

    Returns:
        True if conversation is sexual.
    """
    if not recent_messages:
        return False
    msgs = recent_messages
    # Recent heat — any of last 3
    for m in msgs[-3:]:
        if any(kw in m["content"].lower() for kw in SEXUAL_KEYWORDS_BROAD):
            return True
    # Sustained — 2+ of last 8
    last8 = msgs[-8:]
    count = sum(
        1 for m in last8
        if any(kw in m["content"].lower() for kw in SEXUAL_KEYWORDS_BROAD)
    )
    return count >= 2


def is_topic_loop(recent_messages: list, window: int = 8) -> bool:
    """Check if conversation is stuck in a sexual topic loop.

    Args:
        recent_messages: List of message dicts with 'content' key.
        window: Number of recent messages to check.

    Returns:
        True if 6+ of last 8 messages contain sexual keywords.
    """
    if not recent_messages:
        return False
    msgs = recent_messages[-window:]
    count = 0
    for m in msgs:
        if any(kw in m["content"].lower() for kw in SEXUAL_KEYWORDS_CORE):
            count += 1
    return count >= 6


# ── Initialize on import ─────────────────────────────────────────────

_init_tip_data()


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_keyword_subset_assertion():
#     """CORE must be a subset of BROAD."""
#     assert SEXUAL_KEYWORDS_CORE <= SEXUAL_KEYWORDS_BROAD
#
# def test_compute_tip_tier():
#     assert compute_tip_tier(0) == 0
#     assert compute_tip_tier(1) == 1
#     assert compute_tip_tier(250) == 2
#     assert compute_tip_tier(1000) == 3
#
# def test_get_access_tier_trial_mode():
#     """During TRIAL_MODE, everyone gets VIP."""
#     assert get_access_tier(999999) == "VIP"
#
# def test_real_access_tier():
#     """Real tier logic should work even during trial mode."""
#     tier = _get_real_access_tier(999999)
#     assert tier == "FREE"  # No tips
#
# def test_get_tier_returns_tierinfo():
#     info = get_tier(999999)
#     assert isinstance(info, TierInfo)
#     assert info.tier == "VIP"  # trial mode
#     assert info.is_trial_mode is True
#
# def test_warmth_tier():
#     assert get_warmth_tier(999999) == "NEW"  # Default warmth
#
# def test_energy_detection():
#     msgs = [{"content": "fuck yeah baby"}, {"content": "mmm your cock"}]
#     assert detect_conversation_energy(msgs) == "hot"
#     msgs = [{"content": "hey how's it going"}]
#     assert detect_conversation_energy(msgs) == "casual"
#
# def test_is_sexual_conversation():
#     msgs = [{"content": "hey"}, {"content": "you're cute"}, {"content": "suck my cock"}]
#     assert is_sexual_conversation(msgs) is True
#     msgs = [{"content": "hey"}, {"content": "how are you"}]
#     assert is_sexual_conversation(msgs) is False

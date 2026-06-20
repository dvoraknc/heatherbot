"""
heather.state — Per-User Conversational State
===============================================
Consolidates 72+ module-level dicts from the monolith into a single
``UserState`` class per chat_id, managed by ``StateManager``.

**Scope rule**: UserState holds per-user *conversational* state only.
NOT caches, service health, library data, or persistent flags.

**Restart behavior** (documented, conscious tradeoffs):
  - Photo caps reset — acceptable, minor benefit to users
  - Goodbye tracking resets — acceptable, worst case one extra check-in
  - Hostility tracking resets — acceptable, persistent bad actors in blocked_users.json
  - Burst flood timestamps reset — acceptable, only affects active floods
  - Story/steering counters reset — acceptable, may re-serve a story slightly early

Replaces: 72+ module-level Dict[int, ...] declarations scattered throughout
           heather_telegram_bot.py, including:
  - Conversation state: lines 737-738, 748, 779-781, 797-798
  - Safety tracking: lines 1285-1289, 1354-1356, 1381-1383, 1569
  - Photo/media tracking: lines 729-731, 749-750, 782, 3532-3540, 3586
  - Video tracking: lines 704-706, 3744-3746
  - Voice state: lines 751, 941-943
  - Story system: lines 771-775
  - Tipping/monetization: lines 853, 910-911, 944
  - Dynamics/steering: lines 1669-1694, 1984-1985, 2100, 3948
  - Check-in/re-engagement: lines 739, 820, 980
  - Goodbye/repeat: lines 803, 808-810
  - Takeover: lines 928, 932, 936-937
  - Content promise: line 687
  - AI disclosure: line 767
  - Locks: line 785

Dependencies: heather.config
Used by: Every heather module that tracks per-user state
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple


class UserState:
    """All per-user conversational state in one place.

    Replaces 72+ module-level dicts. Each field documents which
    original dict it replaces and what module will own it.
    """

    __slots__ = (
        "chat_id",
        "created_at",
        "last_active",
        # ── Concurrency ──
        "chat_lock",
        "message_queue",
        # ── Conversation ──
        "conversation_history",       # was: conversations[chat_id]
        "recent_messages",            # was: recent_messages[chat_id]
        "last_message_time",          # was: user_last_message[chat_id]
        "turn_count",                 # was: conversation_turn_count[chat_id]
        "mode",                       # was: user_modes[chat_id]
        "reply_in_progress",          # was: reply_in_progress set membership
        "last_response",              # was: last_response_sent[chat_id]
        "last_user_message",          # was: _last_user_message[chat_id]
        "escalation_level",           # was: user_escalation_level[chat_id]
        "user_info",                  # was: user_info[chat_id]
        # ── Safety ──
        "hostility_tracker",          # was: hostility_tracker[chat_id]
        "injection_attempts",         # was: injection_attempt_count[chat_id]
        "single_char_tracker",        # was: _single_char_tracker[chat_id]
        "burst_timestamps",           # was: user_message_timestamps[chat_id]
        # ── Photo/Image Tracking ──
        "photo_send_times",           # was: photo_send_times[chat_id]
        "received_photo_count",       # was: received_photo_count[chat_id]
        "images_sent",                # was: images_sent_to_user[chat_id]
        "last_captions_sent",         # was: _last_captions_sent[chat_id]
        "last_photo_request",         # was: last_photo_request[chat_id]
        "declined_photo_count",       # was: declined_photo_count[chat_id]
        "photo_processing_start",     # was: photo_processing[chat_id]
        "photo_cap_decline_times",    # was: _photo_cap_decline_times[chat_id]
        "photo_cap_silenced_until",   # was: _photo_cap_silenced_until[chat_id]
        "last_unsolicited_nsfw",      # was: last_unsolicited_nsfw[chat_id]
        "pending_photo_id",           # was: _pending_photo_id[chat_id]
        "awaiting_image_desc",        # was: awaiting_image_description[chat_id]
        "awaiting_image_desc_time",   # was: awaiting_image_description_time[chat_id]
        "proactive_image_sent",       # was: _proactive_image_sent set membership
        # ── Video Tracking ──
        "videos_sent",                # was: videos_sent_to_user[chat_id]
        "video_send_timestamps",      # was: video_send_timestamps[chat_id]
        "last_video_tease",           # was: last_video_tease[chat_id]
        "video_offer_pending",        # was: _video_offer_pending[chat_id]
        # ── Voice State ──
        "voice_mode",                 # was: voice_mode_users set membership
        "voice_nudge_date",           # was: voice_nudge_sent_today[chat_id]
        "voice_welcome_pending",      # was: _voice_welcome_pending set membership
        "voice_welcomed",             # tracks whether first voice was sent
        "proactive_voice_cooldown",   # was: _proactive_voice_cooldown[chat_id]
        # ── Story System ──
        "story_last_served",          # was: story_last_served[chat_id]
        "stories_served",             # was: stories_served_to_user[chat_id]
        "story_mode_active",          # was: _story_mode_active[chat_id]
        # ── Conversation Dynamics ──
        "dynamics",                   # was: conversation_dynamics[chat_id]
        "session_state",              # was: session_state[chat_id]
        "response_topics",            # was: recent_response_topics[chat_id]
        "phrase_counts",              # was: recent_phrase_counts[chat_id]
        "goodbye_tracker",            # was: goodbye_tracker[chat_id]
        "repeated_msg_tracker",       # was: _repeated_msg_tracker[chat_id]
        "conversation_activity",      # was: conversation_activity[chat_id]
        # ── Steering ──
        "meetup_deflect_remaining",   # was: _meetup_deflect_active[chat_id]
        "verify_deflect_remaining",   # was: _verify_deflect_active[chat_id]
        "frank_msgs_since_mention",   # was: frank_messages_since_mention[chat_id]
        "breeding_last_injected",     # was: breeding_last_injected[chat_id]
        "hostile_exit_cooldown",      # was: _hostile_exit_cooldown[chat_id]
        "last_meetup_deflection",     # was: _last_meetup_deflection[chat_id]
        # ── Tipping/Monetization ──
        "tipper_status",              # was: tipper_status[chat_id]
        "last_tease_invoice_at",      # was: _last_tease_invoice_at[chat_id]
        "last_memory_tease",          # was: _last_memory_tease[chat_id]
        "tip_hook_sent_at",           # was: _tip_hook_sent_at[chat_id]
        "content_promise_pending",    # was: _content_promise_pending[chat_id]
        "last_ai_deflection",         # was: last_ai_deflection_used[chat_id]
        "payment_bot_started",        # was: payment_bot_started_users set membership
        # ── Takeover ──
        "takeover_opportunity",       # was: _takeover_opportunities[chat_id]
        "takeover_timestamp",         # was: _takeover_timestamps[chat_id]
        "takeover_last_admin_msg",    # was: _takeover_last_admin_msg[chat_id]
        "dissatisfaction_alert_at",   # was: _dissatisfaction_alerts[chat_id]
        # ── Check-In ──
        "welcome_back_pending",       # was: _welcome_back_pending[chat_id]
        "checkin_tracker",            # was: checkin_tracker[chat_id]
        # ── AI Disclosure ──
        "ai_disclosure",              # was: ai_disclosure_shown[chat_id]
        # ── Memory Extraction ──
        "extraction_in_flight",       # was: _extraction_in_flight set membership
        "extraction_last_run",        # was: _extraction_last_run[chat_id]
        # ── Fallback Tracking ──
        "last_fallback_used",         # was: last_fallback_used[chat_id]
        "last_fallback_time",         # was: last_fallback_time[chat_id]
        "consecutive_fallbacks",      # was: consecutive_fallbacks[chat_id]
        "fallback_quiet_until",       # was: _fallback_quiet_until[chat_id]
        # ── Image Analysis Cache ──
        "image_analysis_cache",       # was: image_analysis_cache[chat_id]  (partial)
        # ── Manual/Admin Modes ──
        "manual_mode",                # was: manual_mode_chats set membership
        "redteam_mode",               # was: redteam_chats set membership
    )

    def __init__(self, chat_id: int) -> None:
        self.chat_id: int = chat_id
        self.created_at: float = time.time()
        self.last_active: float = time.time()

        # Concurrency
        self.chat_lock: asyncio.Lock = asyncio.Lock()
        self.message_queue: asyncio.Queue[Any] = asyncio.Queue()

        # Conversation
        self.conversation_history: Deque[Dict[str, str]] = deque(maxlen=20)
        self.recent_messages: Deque[str] = deque(maxlen=50)
        self.last_message_time: float = 0.0
        self.turn_count: int = 0
        self.mode: str = "normal"
        self.reply_in_progress: bool = False
        self.last_response: str = ""
        self.last_user_message: Optional[Tuple[str, float]] = None
        self.escalation_level: int = 0
        self.user_info: Dict[str, Any] = {}

        # Safety
        self.hostility_tracker: Dict[str, Any] = {}
        self.injection_attempts: List[float] = []
        self.single_char_tracker: List[float] = []
        self.burst_timestamps: Deque[float] = deque(maxlen=50)

        # Photo/Image
        self.photo_send_times: List[float] = []
        self.received_photo_count: int = 0
        self.images_sent: Dict[str, Set[str]] = {}  # category -> set of image IDs
        self.last_captions_sent: Deque[str] = deque(maxlen=5)
        self.last_photo_request: float = 0.0
        self.declined_photo_count: int = 0
        self.photo_processing_start: Optional[float] = None
        self.photo_cap_decline_times: List[float] = []
        self.photo_cap_silenced_until: float = 0.0
        self.last_unsolicited_nsfw: float = 0.0
        self.pending_photo_id: Optional[str] = None
        self.awaiting_image_desc: bool = False
        self.awaiting_image_desc_time: float = 0.0
        self.proactive_image_sent: bool = False

        # Video
        self.videos_sent: Set[str] = set()
        self.video_send_timestamps: List[float] = []
        self.last_video_tease: float = 0.0
        self.video_offer_pending: Optional[float] = None

        # Voice
        self.voice_mode: bool = False
        self.voice_nudge_date: Optional[str] = None
        self.voice_welcome_pending: bool = True
        self.voice_welcomed: bool = False
        self.proactive_voice_cooldown: float = 0.0

        # Story
        self.story_last_served: int = 0
        self.stories_served: Set[str] = set()
        self.story_mode_active: bool = False

        # Conversation dynamics
        self.dynamics: Dict[str, Any] = {}
        self.session_state: Dict[str, Any] = {}
        self.response_topics: Deque[str] = deque(maxlen=10)
        self.phrase_counts: Dict[str, List[float]] = {}
        self.goodbye_tracker: Dict[str, Any] = {}
        self.repeated_msg_tracker: Dict[str, Any] = {}
        self.conversation_activity: Dict[str, Any] = {}

        # Steering
        self.meetup_deflect_remaining: int = 0
        self.verify_deflect_remaining: int = 0
        self.frank_msgs_since_mention: int = 0
        self.breeding_last_injected: int = 0
        self.hostile_exit_cooldown: float = 0.0
        self.last_meetup_deflection: str = ""

        # Tipping/Monetization
        self.tipper_status: Optional[Dict[str, Any]] = None
        self.last_tease_invoice_at: float = 0.0
        self.last_memory_tease: float = 0.0
        self.tip_hook_sent_at: float = 0.0
        self.content_promise_pending: Optional[float] = None
        self.last_ai_deflection: str = ""
        self.payment_bot_started: bool = False

        # Takeover
        self.takeover_opportunity: Optional[Dict[str, Any]] = None
        self.takeover_timestamp: float = 0.0
        self.takeover_last_admin_msg: float = 0.0
        self.dissatisfaction_alert_at: float = 0.0

        # Check-in
        self.welcome_back_pending: Optional[float] = None
        self.checkin_tracker: Dict[str, Any] = {}

        # AI Disclosure
        self.ai_disclosure: Optional[Dict[str, Any]] = None

        # Memory extraction
        self.extraction_in_flight: bool = False
        self.extraction_last_run: float = 0.0

        # Fallback tracking
        self.last_fallback_used: str = ""
        self.last_fallback_time: float = 0.0
        self.consecutive_fallbacks: int = 0
        self.fallback_quiet_until: float = 0.0

        # Image analysis cache (per-user portion)
        self.image_analysis_cache: Dict[str, Any] = {}

        # Manual/Admin modes
        self.manual_mode: bool = False
        self.redteam_mode: bool = False

    def touch(self) -> None:
        """Update last_active timestamp."""
        self.last_active = time.time()

    def apply_mutations(self, mutations: Dict[str, Any]) -> None:
        """Apply state mutations from a SafetyAction.

        Args:
            mutations: Dict of field_name -> value to add/set.
                       Supports "+N" pattern for incrementing counters.
        """
        for key, value in mutations.items():
            if hasattr(self, key):
                if isinstance(value, (int, float)) and isinstance(
                    getattr(self, key), (int, float)
                ):
                    setattr(self, key, getattr(self, key) + value)
                else:
                    setattr(self, key, value)

    @property
    def is_inactive(self) -> bool:
        """True if user hasn't been active in 24 hours."""
        return (time.time() - self.last_active) > 86400


class StateManager:
    """Central registry for per-user state. Replaces all module-level dicts.

    Thread-safe creation via double-check locking pattern.
    ``get()`` is async because it may need the creation lock.

    Usage::

        state = await StateManager.get(chat_id)
        state.turn_count += 1
    """

    _users: Dict[int, UserState] = {}
    _creation_lock: asyncio.Lock = asyncio.Lock()

    @classmethod
    async def get(cls, chat_id: int) -> UserState:
        """Get or create UserState for a chat_id.

        Uses double-check locking to prevent duplicate creation
        when rapid messages arrive for the same new user.

        Args:
            chat_id: Telegram chat ID.

        Returns:
            The UserState for this chat.
        """
        if chat_id in cls._users:
            state = cls._users[chat_id]
            state.touch()
            return state

        async with cls._creation_lock:
            if chat_id not in cls._users:
                cls._users[chat_id] = UserState(chat_id)
            state = cls._users[chat_id]
            state.touch()
            return state

    @classmethod
    def get_sync(cls, chat_id: int) -> UserState:
        """Synchronous get — for use outside async context.

        No creation lock protection. Safe when used from a single thread
        (e.g., Flask monitoring routes).
        """
        if chat_id not in cls._users:
            cls._users[chat_id] = UserState(chat_id)
        state = cls._users[chat_id]
        state.touch()
        return state

    @classmethod
    def get_if_exists(cls, chat_id: int) -> Optional[UserState]:
        """Return UserState if it exists, None otherwise."""
        return cls._users.get(chat_id)

    @classmethod
    def cleanup_inactive(cls, max_age_hours: int = 24) -> int:
        """Remove UserState entries inactive for more than max_age_hours.

        Returns:
            Number of entries removed.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        stale = [
            cid for cid, state in cls._users.items()
            if state.last_active < cutoff
        ]
        for cid in stale:
            del cls._users[cid]
        return len(stale)

    @classmethod
    def active_count(cls) -> int:
        """Number of currently tracked users."""
        return len(cls._users)

    @classmethod
    def all_chat_ids(cls) -> List[int]:
        """List all tracked chat IDs."""
        return list(cls._users.keys())

    @classmethod
    def reset(cls) -> None:
        """Clear all state. Used in testing only."""
        cls._users.clear()


# ============================================================================
# Unit test stubs
# ============================================================================
# import asyncio
#
# def test_user_state_defaults():
#     s = UserState(123)
#     assert s.chat_id == 123
#     assert s.turn_count == 0
#     assert s.mode == "normal"
#     assert s.voice_mode is False
#     assert s.reply_in_progress is False
#     assert isinstance(s.chat_lock, asyncio.Lock)
#     assert isinstance(s.message_queue, asyncio.Queue)
#
# def test_user_state_touch():
#     s = UserState(123)
#     old_active = s.last_active
#     import time; time.sleep(0.01)
#     s.touch()
#     assert s.last_active > old_active
#
# def test_apply_mutations_increment():
#     s = UserState(123)
#     s.apply_mutations({"turn_count": 5})
#     assert s.turn_count == 5
#     s.apply_mutations({"turn_count": 3})
#     assert s.turn_count == 8
#
# def test_apply_mutations_set():
#     s = UserState(123)
#     s.apply_mutations({"mode": "afterglow"})
#     assert s.mode == "afterglow"
#
# async def test_state_manager_get():
#     StateManager.reset()
#     s1 = await StateManager.get(42)
#     s2 = await StateManager.get(42)
#     assert s1 is s2
#     assert StateManager.active_count() == 1
#
# async def test_state_manager_concurrent_creation():
#     """Two concurrent get() calls for same new user should return same instance."""
#     StateManager.reset()
#     results = await asyncio.gather(
#         StateManager.get(99),
#         StateManager.get(99),
#     )
#     assert results[0] is results[1]
#
# def test_cleanup_inactive():
#     StateManager.reset()
#     s = StateManager.get_sync(1)
#     s.last_active = 0  # Ancient
#     removed = StateManager.cleanup_inactive(max_age_hours=0)
#     assert removed == 1
#     assert StateManager.active_count() == 0

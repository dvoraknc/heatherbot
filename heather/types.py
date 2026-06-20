"""
heather.types — Core Data Types
=================================
Transport-agnostic dataclasses used throughout the pipeline.
No Telethon, no Flask, no service-specific imports.

Replaces: Implicit dict/tuple passing throughout heather_telegram_bot.py
  - Safety results were inline booleans/strings
  - Tier info was scattered across multiple function returns
  - Pipeline results were bare strings with metadata lost

Dependencies: None (leaf module — stdlib only)
Used by: Every heather module that processes messages
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass(slots=True)
class RequestContext:
    """Transport-agnostic representation of an incoming message.

    Created once at the top of the handler via ``from_event()``.
    Passed through the entire pipeline instead of raw Telethon events,
    keeping policy modules free of transport concerns.
    """
    chat_id: int
    user_message: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    is_admin: bool = False
    is_reply: bool = False
    has_media: bool = False
    timestamp: float = 0.0

    @staticmethod
    def from_event(event: Any) -> RequestContext:
        """Factory: Telethon event -> transport-agnostic context.

        Args:
            event: A Telethon ``NewMessage`` event.

        Returns:
            RequestContext with all transport details extracted.
        """
        import time
        sender = getattr(event, 'sender', None)
        chat_id = event.chat_id or 0

        username: Optional[str] = None
        display_name: Optional[str] = None
        if sender:
            username = getattr(sender, 'username', None)
            first = getattr(sender, 'first_name', '') or ''
            last = getattr(sender, 'last_name', '') or ''
            display_name = f"{first} {last}".strip() or username

        return RequestContext(
            chat_id=chat_id,
            user_message=event.text or '',
            username=username,
            display_name=display_name,
            is_admin=False,  # Caller sets this after checking ADMIN_USER_ID
            is_reply=bool(getattr(event, 'is_reply', False)),
            has_media=bool(getattr(event, 'media', None)),
            timestamp=time.time(),
        )


@dataclass(slots=True)
class SafetyAction:
    """Result of the safety pipeline. Pure data, no side effects.

    The safety module returns this; the caller decides what to do with it.
    ``state_mutations`` contains counters to apply to UserState (e.g.,
    incrementing burst count) so safety functions stay pure.
    """
    blocked: bool = False
    response: Optional[str] = None
    flags: List[str] = field(default_factory=list)
    log_only: bool = False
    state_mutations: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TierInfo:
    """Access tier + warmth context for a user.

    Returned by ``access_tiers.get_tier()`` and passed through the pipeline
    so every module can make tier-aware decisions without re-querying.
    """
    tier: str = "FREE"          # FREE / FAN / VIP
    warmth: str = "NEW"         # WARM / NEW / COLD
    warmth_score: float = 0.5
    is_trial_mode: bool = True

    @property
    def is_vip(self) -> bool:
        return self.tier == "VIP"

    @property
    def is_fan_or_above(self) -> bool:
        return self.tier in ("FAN", "VIP")

    @property
    def is_warm(self) -> bool:
        return self.warmth == "WARM"


@dataclass(slots=True)
class PipelineResult:
    """Output of the text generation pipeline.

    Carries the response plus metadata about how it was generated,
    useful for post-response decisions (proactive images, steering).
    """
    response: str
    was_filtered: bool = False
    retry_count: int = 0
    used_fallback: bool = False
    prompt_tokens_used: int = 0
    safety_flags: List[str] = field(default_factory=list)
    tier_applied: str = "FREE"


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_request_context_defaults():
#     ctx = RequestContext(chat_id=123, user_message="hello")
#     assert ctx.chat_id == 123
#     assert ctx.is_admin is False
#     assert ctx.has_media is False
#
# def test_safety_action_defaults():
#     action = SafetyAction()
#     assert action.blocked is False
#     assert action.flags == []
#     assert action.state_mutations == {}
#
# def test_tier_info_properties():
#     vip = TierInfo(tier="VIP", warmth="WARM")
#     assert vip.is_vip is True
#     assert vip.is_fan_or_above is True
#     assert vip.is_warm is True
#     free = TierInfo(tier="FREE", warmth="COLD")
#     assert free.is_vip is False
#     assert free.is_fan_or_above is False
#     assert free.is_warm is False
#
# def test_pipeline_result_defaults():
#     result = PipelineResult(response="hi")
#     assert result.response == "hi"
#     assert result.was_filtered is False
#     assert result.retry_count == 0

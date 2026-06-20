"""
heather.text_pipeline.pipeline — Text Generation Orchestrator
===============================================================
Main entry point for AI text generation. Orchestrates: circuit breaker
check -> prompt building -> history assembly -> token/temp calculation ->
LLM call -> universal cleanup -> filter pipeline -> retry logic ->
post-response updates -> history append/trim.

Returns PipelineResult with response text, metadata, and state mutations.

Pipeline owns conversation history: read, append, trim. Prompt builder
and response filter receive data as parameters.

Replaces: heather_telegram_bot.py
  - get_text_ai_response: lines 6717-7965
  - conversations dict: line 735
  - recent_messages dict: line 737
  - user_modes dict: line 731
  - History append/trim: lines 7926-7930
  - History filtering (violation skip): lines 7232-7236
  - Circuit breaker check: lines 7722-7729
  - LLM call: lines 7340-7350
  - VIP unguarded branch: lines 7482-7518
  - Normal filter branch: lines 7520-7614
  - Post-response updates: lines 7616-7627
  - Filler detection: lines 7607-7613

Dependencies: heather.config, heather.logging_setup, heather.types,
              heather.service_health, heather.personality, heather.conversation,
              heather.access_tiers, heather.safety,
              heather.text_pipeline.prompt_builder,
              heather.text_pipeline.llm_client,
              heather.text_pipeline.response_filter
Used by: heather_telegram_bot.py (handle_text_message)
"""

from __future__ import annotations

import random
import traceback
from collections import deque
from typing import Any, Dict, Optional

import requests

from heather import config
from heather.logging_setup import main_logger, PerformanceTimer
from heather.types import PipelineResult
from heather.service_health import health_trackers
from heather.personality import contains_character_violation, get_violation_phrases

from heather.text_pipeline.prompt_builder import (
    build_system_prompt,
    calculate_max_tokens,
    calculate_temperature,
)
from heather.text_pipeline.llm_client import (
    text_ai_post,
    get_fallback_response,
    reset_consecutive_fallbacks,
    get_ai_deflection_response,
    get_time_aware_prompt_addition,
    ANTI_REFUSAL_NUDGES,
    CHARACTER_BREAK_NUDGES,
    ANTI_PHOTO_NUDGE,
    HEATHER_SEXUAL_FALLBACKS,
)
from heather.text_pipeline.response_filter import (
    universal_cleanup,
    scrub_meeting_plans,
    scrub_fabricated_links,
    scrub_fabricated_media,
    scrub_meetup_commitment,
    scrub_physical_presence,
    filter_oh_opener,
    salvage_empty_response,
)
from heather.postprocess import (
    postprocess_response,
    contains_gender_violation,
    is_incomplete_sentence,
    salvage_truncated_response,
)
from heather.safety import is_ai_safety_refusal


# ============================================================================
# PIPELINE STATE — conversation history, modes, deflection counters
# ============================================================================

# Conversation history per user — pipeline owns this data
conversations: Dict[int, deque] = {}
recent_messages: Dict[int, deque] = {}
user_modes: Dict[int, str] = {}

# Deflection state — consumed by prompt builder, decremented per call
_welcome_back_pending: Dict[int, float] = {}  # chat_id -> gap_hours
_story_mode_active: Dict[int, bool] = {}      # chat_id -> True when LLM should generate a story
_meetup_deflect_active: Dict[int, int] = {}   # chat_id -> remaining deflection messages
_verify_deflect_active: Dict[int, int] = {}   # chat_id -> remaining deflection messages
breeding_last_injected: Dict[int, int] = {}   # chat_id -> msg_count at last injection

# Stats counters (shared with monolith via reference)
stats: Dict[str, int] = {}

# Reasoning model auto-detection flag
_reasoning_model_detected: bool = False

DEFAULT_MODE: str = "chat"

# Per-user Frank throttle counter (messages since last Frank mention)
_frank_msgs_since: Dict[int, int] = {}


# ============================================================================
# MALFORMED RESPONSE DETECTION
# ============================================================================
# Catches LLM artifacts the bot would otherwise send: character/token-count
# meta-commentary, "Response:" prefixes, JSON-like fragments, code blocks,
# all-punctuation/whitespace outputs. Used by the VIP double-pass logic.

import re as _re_malformed

_MALFORMED_PATTERNS = [
    # Character / token / word count meta-commentary
    (_re_malformed.compile(r'^\s*[\[\(]\s*\d+\s*(?:chars?|characters?|tokens?|words?)\s*[\]\)]', _re_malformed.IGNORECASE),
     'count_prefix'),
    (_re_malformed.compile(r'\b\d+\s*(?:chars?|characters?)\b', _re_malformed.IGNORECASE),
     'char_count'),
    (_re_malformed.compile(r'\b\d+\s*tokens?\b', _re_malformed.IGNORECASE),
     'token_count'),
    # Self-referential length notes like "(brief)", "(short reply)"
    (_re_malformed.compile(r'^\s*[\[\(]\s*(?:brief|short|long|terse|concise|truncated)\s+(?:reply|response|answer|message)\s*[\]\)]', _re_malformed.IGNORECASE),
     'length_note'),
    # Bot-prefix that escaped earlier cleanup
    (_re_malformed.compile(r'^\s*(?:response|reply|answer|assistant|heather):\s*', _re_malformed.IGNORECASE),
     'response_prefix'),
    # JSON-like fragments
    (_re_malformed.compile(r'^\s*\{\s*"[a-z_]+"\s*:', _re_malformed.IGNORECASE),
     'json_fragment'),
    # Code block markers
    (_re_malformed.compile(r'```'),
     'code_block'),
    # XML-style tags
    (_re_malformed.compile(r'</?(?:response|reply|answer|message|content|assistant)\s*/?>', _re_malformed.IGNORECASE),
     'xml_tag'),
    # Length-as-content e.g. "Here's a 50-character response:"
    (_re_malformed.compile(r"here'?s?\s+(?:a\s+)?\d+[\s-]*(?:char|word|token)", _re_malformed.IGNORECASE),
     'length_meta'),
    # Date / timestamp headers e.g. "Heather Dvorak - 6/14/2026, Sunday AM"
    (_re_malformed.compile(r'^[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s*[-–—]\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}'),
     'date_header'),
    (_re_malformed.compile(r'^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b[,\s]'),
     'bare_date'),
    # Bio dump intro — "I'm Heather, 48", "I'm Heather Dvorak — 48-year-old Kirkland widow"
    # (catches even truncated mid-sentence, where the cleanup regex would miss it.)
    (_re_malformed.compile(r"^\s*I[''’]?m\s+Heather(?:\s+(?:Dvorak))?(?:\s*[,\-–—]\s*|\s+)\d{1,3}", _re_malformed.IGNORECASE),
     'bio_dump'),
]


def _detect_malformed_response(text: str) -> Optional[str]:
    """Return a short label naming the malformed pattern that matched, or None.

    Used by the VIP double-pass logic to decide whether to salvage/retry a
    response that survived earlier filters but contains LLM artifacts the user
    shouldn't see.
    """
    if not text:
        return None
    s = text.strip()
    # Empty or whitespace-only
    if not s:
        return 'empty'
    # All punctuation / no letters
    if not _re_malformed.search(r'[A-Za-z]', s):
        return 'no_letters'
    for pat, label in _MALFORMED_PATTERNS:
        if pat.search(s):
            return label
    return None


# ============================================================================
# PIPELINE ACCESSORS — for use by other modules
# ============================================================================

def get_user_mode(chat_id: int) -> str:
    """Get current user mode (chat/rate/heather)."""
    return user_modes.get(chat_id, DEFAULT_MODE)


def set_user_mode(chat_id: int, mode: str) -> None:
    """Set user mode."""
    user_modes[chat_id] = mode


def set_welcome_back_pending(chat_id: int, gap_hours: float) -> None:
    """Mark a welcome-back pending for next prompt build."""
    _welcome_back_pending[chat_id] = gap_hours


def set_story_mode(chat_id: int, active: bool = True) -> None:
    """Activate story mode for next generation."""
    _story_mode_active[chat_id] = active


def set_meetup_deflect(chat_id: int, messages: int = 3) -> None:
    """Activate meetup deflection for N messages."""
    _meetup_deflect_active[chat_id] = messages


def set_verify_deflect(chat_id: int, messages: int = 2) -> None:
    """Activate verification deflection for N messages."""
    _verify_deflect_active[chat_id] = messages


def get_conversations() -> Dict[int, deque]:
    """Return conversations dict (for external read access)."""
    return conversations


def get_recent_messages() -> Dict[int, deque]:
    """Return recent_messages dict (for external read access)."""
    return recent_messages


def set_stats_ref(stats_dict: Dict[str, int]) -> None:
    """Set reference to the global stats dict from the monolith."""
    global stats
    stats = stats_dict


# ============================================================================
# FILLER PHRASES — detected during sexual convos, trigger retry
# ============================================================================

_FILLER_PHRASES = [
    "how's your day", "anything exciting", "what's new with you",
    "how are things", "what have you been up to", "how's everything",
]


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def generate(
    chat_id: int,
    user_message: str,
    retry_count: int = 0,
    redteam: bool = False,
    vip_unguarded: bool = False,
    *,
    # External dependencies passed by caller (avoids circular imports)
    personality_loader: Any = None,
    user_memory_module: Any = None,
    access_tier_fn=None,
    warmth_tier_fn=None,
    tipper_status_fn=None,
    csam_flag_count_fn=None,
    csam_flags_list_fn=None,
    can_send_photo_fn=None,
    admin_alert_fn=None,
) -> str:
    """Generate an AI text response for a user message.

    This is the main text pipeline entry point. It orchestrates:
    1. Circuit breaker check
    2. Prompt assembly (via prompt_builder)
    3. History assembly
    4. Token/temperature calculation
    5. LLM HTTP call (via llm_client)
    6. Universal cleanup + filtering (via response_filter)
    7. Retry logic on violations/truncation
    8. Post-response state updates
    9. History append/trim

    Args:
        chat_id: User chat ID.
        user_message: Current user message text.
        retry_count: Current retry attempt (0 = first try).
        redteam: If True, bypass character/gender violation checks.
        vip_unguarded: If True, skip most content filters.
        personality_loader: PersonalityLoader instance.
        user_memory_module: user_memory module (for profile/kink/welcome-back prompts).
        access_tier_fn: Callable(chat_id) -> str returning access tier.
        warmth_tier_fn: Callable(chat_id) -> str returning warmth tier.
        tipper_status_fn: Callable(chat_id) -> dict returning tipper status.
        csam_flag_count_fn: Callable(chat_id) -> int returning CSAM flag count.
        can_send_photo_fn: Callable(chat_id) -> bool for photo cap check.
        admin_alert_fn: Optional async callable for admin alerts.

    Returns:
        AI response string (or fallback response on failure).
    """
    global _reasoning_model_detected

    # Lazy imports for conversation functions (avoid circular imports)
    from heather.conversation import (
        is_winding_down,
        get_conversation_energy,
        get_arousal_level,
        is_domme_context,
        should_inject_breeding,
        get_breeding_cnc_prompt,
        mark_breeding_injected,
        get_conversation_dynamics,
        get_conversation_steering_context,
        get_backstory_context,
        get_anti_repetition_context,
        get_state_context_for_prompt,
        get_story_mode_prompt,
        update_conversation_dynamics,
        update_session_state_from_response,
        track_response_topics,
        diversify_phrases,
        track_phrase_usage,
        is_sexual_conversation,
        CLIMAX_PHRASES,
    )
    from heather.personality import throttle_frank

    # Capture DI kwargs for recursive retry calls (avoids repeating 10 kwargs 8 times)
    _di_kwargs = dict(
        personality_loader=personality_loader,
        user_memory_module=user_memory_module,
        access_tier_fn=access_tier_fn,
        warmth_tier_fn=warmth_tier_fn,
        tipper_status_fn=tipper_status_fn,
        csam_flag_count_fn=csam_flag_count_fn,
        csam_flags_list_fn=csam_flags_list_fn,
        can_send_photo_fn=can_send_photo_fn,
        admin_alert_fn=admin_alert_fn,
    )

    stats.setdefault('text_ai_requests', 0)
    stats.setdefault('text_ai_failures', 0)
    stats.setdefault('text_ai_timeouts', 0)
    stats.setdefault('cleanup_empty', 0)
    stats.setdefault('cleanup_salvaged', 0)
    stats['text_ai_requests'] += 1

    text_ai_health = health_trackers.get('text_ai')

    # --- 1. Circuit breaker check ---
    if text_ai_health and not text_ai_health.is_available():
        main_logger.warning(f"Text AI circuit breaker open, using fallback for {chat_id}")
        if text_ai_health.needs_alert() and admin_alert_fn:
            import asyncio
            try:
                asyncio.create_task(admin_alert_fn(
                    f"Text AI service is DOWN\nCircuit breaker opened after {text_ai_health.failure_threshold} failures",
                    issue_type="text_ai_down"
                ))
            except RuntimeError:
                pass  # No event loop
        return get_fallback_response(chat_id)

    _winding_down = is_winding_down(user_message)

    try:
        mode = get_user_mode(chat_id)

        # Ensure conversation history exists
        if chat_id not in conversations:
            conversations[chat_id] = deque()

        # --- 2. Gather context for prompt building ---
        _recent = recent_messages.get(chat_id, deque())
        _recent_dict = {chat_id: _recent}

        energy = get_conversation_energy(chat_id, _recent_dict)
        arousal = get_arousal_level(chat_id, _recent_dict)
        is_domme = is_domme_context(chat_id, user_message, _recent_dict)
        _csam_flags_list = csam_flags_list_fn() if csam_flags_list_fn else []
        _should_breed = should_inject_breeding(chat_id, user_message, _recent_dict, _csam_flags_list)
        breeding_prompt = get_breeding_cnc_prompt(user_message) if _should_breed else ""

        # State context
        state_context = get_state_context_for_prompt(chat_id)

        # Photo cap context
        photo_cap_hit = False
        if can_send_photo_fn and not can_send_photo_fn(chat_id):
            photo_cap_hit = True

        # Time context (full model only, but computed here)
        time_context = get_time_aware_prompt_addition() if not config.SMALL_MODEL_MODE else ""

        # Anti-repetition context
        variety_context = get_anti_repetition_context(chat_id, user_message) if not config.SMALL_MODEL_MODE else ""

        # Steering context
        steering_context = get_conversation_steering_context(chat_id, _recent_dict)

        # Backstory context
        backstory_context = get_backstory_context(chat_id, user_message, _recent_dict) if not config.SMALL_MODEL_MODE else ""

        # Access tiers
        content_tier = access_tier_fn(chat_id) if access_tier_fn else "FREE"
        warmth_tier = warmth_tier_fn(chat_id) if warmth_tier_fn else "NEW"
        tipper_tier = tipper_status_fn(chat_id).get('tier', 0) if tipper_status_fn else 0
        csam_count = csam_flag_count_fn(chat_id) if csam_flag_count_fn else 0

        # User memory
        profile_prompt = None
        welcome_back_prompt = None
        welcome_back_gap = None
        kink_prompt = None
        if user_memory_module:
            profile_prompt = user_memory_module.build_profile_prompt(chat_id, access_tier=content_tier, current_message=user_message)
            _wb_gap = _welcome_back_pending.pop(chat_id, None)
            if _wb_gap and content_tier != "FREE":
                welcome_back_prompt = user_memory_module.build_welcome_back_prompt(chat_id, _wb_gap)
                welcome_back_gap = _wb_gap
            kink_prompt = user_memory_module.build_kink_persona_prompt(chat_id)

        # Story mode
        _in_story_mode = _story_mode_active.pop(chat_id, False)

        # Meetup/verify deflection (consume and decrement)
        _meetup_remaining = _meetup_deflect_active.get(chat_id, 0)
        if _meetup_remaining > 0:
            _meetup_deflect_active[chat_id] = _meetup_remaining - 1

        _verify_remaining = _verify_deflect_active.get(chat_id, 0)
        if _verify_remaining > 0:
            _verify_deflect_active[chat_id] = _verify_remaining - 1

        # Mark breeding injected
        if _should_breed:
            mark_breeding_injected(chat_id)

        # --- 3. Build system prompt ---
        system_content = build_system_prompt(
            chat_id, user_message,
            personality_loader=personality_loader,
            mode=mode,
            is_winding_down=_winding_down,
            energy=energy,
            arousal=arousal,
            climax_phrases=CLIMAX_PHRASES,
            is_domme=is_domme,
            should_inject_breeding_flag=_should_breed,
            breeding_prompt=breeding_prompt,
            recent_messages=_recent_dict,
            state_context=state_context,
            photo_cap_hit=photo_cap_hit,
            time_context=time_context,
            variety_context=variety_context,
            steering_context=steering_context,
            backstory_context=backstory_context,
            in_story_mode=_in_story_mode,
            story_prompt_fn=get_story_mode_prompt,
            meetup_deflect_remaining=_meetup_remaining,
            verify_deflect_remaining=_verify_remaining,
            conversation_length=len(conversations.get(chat_id, [])),
            content_tier=content_tier,
            warmth_tier=warmth_tier,
            tipper_tier=tipper_tier,
            csam_count=csam_count,
            profile_prompt=profile_prompt,
            welcome_back_prompt=welcome_back_prompt,
            welcome_back_gap=welcome_back_gap,
            kink_prompt=kink_prompt,
            retry_count=retry_count,
        )

        # --- 4. Assemble messages ---
        messages = [{"role": "system", "content": system_content}]

        history_limit = 6
        for msg in list(conversations[chat_id])[-history_limit:]:
            if msg["role"] == "assistant" and contains_character_violation(msg["content"]):
                continue
            messages.append(msg)

        messages.append({"role": "user", "content": user_message})

        # --- 5. Calculate tokens and temperature ---
        # Check if breeding was recently injected (within 1 message)
        breeding_recently_injected = False
        if chat_id in breeding_last_injected:
            dyn = get_conversation_dynamics(chat_id)
            if dyn.get('msg_count', 0) - breeding_last_injected[chat_id] <= 1:
                breeding_recently_injected = True

        max_tokens = calculate_max_tokens(
            user_message,
            is_winding_down=_winding_down,
            arousal=arousal,
            energy=energy,
            warmth_tier=warmth_tier,
            vip_unguarded=vip_unguarded,
            retry_count=retry_count,
            in_story_mode=_in_story_mode,
            breeding_recently_injected=breeding_recently_injected,
            is_reasoning_model=_reasoning_model_detected,
        )

        temperature = calculate_temperature(retry_count, arousal)

        # --- 6. LLM call ---
        with PerformanceTimer('TEXT_AI', 'generate', f"chat_id={chat_id} retry={retry_count}"):
            response = text_ai_post({
                "model": config.TEXT_MODEL_NAME,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
                "top_p": 0.88,
                "min_p": 0.05,            # Cydonia tuning — cuts long-tail invention
                "repeat_penalty": 1.08,   # Cydonia tuning — reduces "hun" overuse
                "frequency_penalty": 0.35,
                "presence_penalty": 0.4,
            }, timeout=config.AI_TIMEOUT)

        # --- 7. Process response ---
        if response.status_code == 200:
            if text_ai_health:
                text_ai_health.record_success()
            response_data = response.json()
            message_data = response_data['choices'][0]['message']
            ai_response = message_data.get('content', '') or ''
            ai_response = ai_response.strip()
            _raw_response = ai_response

            if not _raw_response:
                main_logger.warning(f"[LLM_EMPTY] LLM returned empty content for {chat_id} (keys: {list(message_data.keys())})")

            reasoning = message_data.get('reasoning_content', '')

            # Auto-detect reasoning models
            if reasoning and not _reasoning_model_detected:
                _reasoning_model_detected = True
                main_logger.info("Reasoning model detected — using extended token budget")
                if not ai_response:
                    return generate(
                        chat_id, user_message, retry_count, redteam=redteam,
                        vip_unguarded=vip_unguarded,
                        **_di_kwargs,
                    )

            # --- Universal cleanup (all models, all tiers) ---
            ai_response, _cleanup_trace = universal_cleanup(ai_response)

            # --- VIP UNGUARDED PATH ---
            if vip_unguarded:
                if not ai_response:
                    stats['cleanup_empty'] += 1
                    main_logger.warning(
                        f"[CLEANUP_EMPTY] VIP response for {chat_id} cleaned to empty by {_cleanup_trace}. "
                        f"Raw ({len(_raw_response)} chars): {_raw_response[:300]}"
                    )
                    # Phantom-photo narration ("*sent a photo: the image shows...*")
                    # gets stripped to empty by cleanup. Regenerate in-character with
                    # a nudge instead of falling back to a canned line.
                    _photo_markers = (
                        'sent a photo', 'sends a photo', 'the image shows',
                        'the photo shows', 'the picture shows', 'the image depicts',
                        'in what appears to be', "i've attached", 'image of heather',
                    )
                    if (_raw_response and retry_count < 2
                            and any(m in _raw_response.lower() for m in _photo_markers)):
                        conversations[chat_id].append({"role": "assistant", "content": _raw_response[:200]})
                        conversations[chat_id].append({"role": "user", "content": ANTI_PHOTO_NUDGE})
                        main_logger.info(f"[VIP][ANTI-PHOTO] Phantom photo narration for {chat_id}, regenerating with nudge")
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    if _raw_response and retry_count < 1:
                        _salvaged = salvage_empty_response(_raw_response)
                        if _salvaged:
                            stats['cleanup_salvaged'] += 1
                            main_logger.info(f"[CLEANUP_SALVAGE] VIP salvaged response for {chat_id}: {_salvaged[:100]}")
                            ai_response = _salvaged
                    if not ai_response:
                        return get_fallback_response(chat_id)
                reset_consecutive_fallbacks(chat_id)

                # VIP TRUNCATION + MALFORMED HANDLING (double pass) — catches:
                #   (1) max_tokens cut mid-sentence
                #   (2) responses ending without proper punctuation
                #   (3) malformed responses (character counts, "Response:" prefix,
                #       JSON fragments, code blocks, length metadata)
                # Strategy: try to salvage by trimming to the last complete
                # sentence first; if too little remains, retry with bigger budget.
                _vip_finish_reason = response_data['choices'][0].get('finish_reason', 'stop')
                _vip_malformed_reason = _detect_malformed_response(ai_response)
                _vip_needs_fix = (
                    _vip_finish_reason == 'length'
                    or is_incomplete_sentence(ai_response)
                    or _vip_malformed_reason is not None
                )
                if _vip_needs_fix:
                    _vip_salvaged = salvage_truncated_response(ai_response)
                    # Accept salvage more aggressively (10 char min instead of 20)
                    # — short replies are better than retrying into a longer one.
                    if _vip_salvaged and len(_vip_salvaged) >= 10 and _detect_malformed_response(_vip_salvaged) is None:
                        main_logger.info(
                            f"[VIP][TRUNC_SALVAGE] {chat_id}: trimmed to last clean sentence "
                            f"({len(ai_response)}->{len(_vip_salvaged)} chars, "
                            f"reason={_vip_malformed_reason or _vip_finish_reason})"
                        )
                        ai_response = _vip_salvaged
                    # Skip retry path for short user inputs — length matching wins.
                    # Retrying with a bigger budget tends to defeat the original
                    # short-response intent and produces verbose mismatches.
                    elif retry_count < 2 and len((user_message or '').split()) > 8:
                        main_logger.info(
                            f"[VIP][TRUNC_RETRY] {chat_id}: salvage insufficient "
                            f"(max_tokens={max_tokens}, finish={_vip_finish_reason}, "
                            f"malformed={_vip_malformed_reason}), retrying (attempt {retry_count+1}/3)"
                        )
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    # If we exhausted retries and salvage is still too short,
                    # just send what we have rather than a fallback.

                # VIP: catch model safety refusals and retry
                if is_ai_safety_refusal(ai_response) and retry_count < 2:
                    nudge = random.choice(ANTI_REFUSAL_NUDGES)
                    conversations[chat_id].append({"role": "assistant", "content": ai_response})
                    conversations[chat_id].append({"role": "user", "content": nudge})
                    main_logger.info(f"[VIP][ANTI-REFUSAL] Model refused for VIP {chat_id}, retrying with nudge")
                    return generate(
                        chat_id, user_message, retry_count + 1, redteam=redteam,
                        vip_unguarded=vip_unguarded,
                        **_di_kwargs,
                    )

                # VIP: still enforce character identity (model name leaks)
                if contains_character_violation(ai_response):
                    violated = [p for p in get_violation_phrases() if p in ai_response.lower()]
                    main_logger.warning(f"[VIP] Character violation caught for {chat_id} (attempt {retry_count+1}/3): {violated}: {ai_response[:200]}")
                    if retry_count < 2:
                        # Nudge the model back in-character before regenerating. The
                        # bad turn is appended so the next prompt can react to it, but
                        # it is auto-skipped from history (contains a violation phrase),
                        # so it won't be echoed back.
                        conversations[chat_id].append({"role": "assistant", "content": ai_response[:200]})
                        conversations[chat_id].append({"role": "user", "content": random.choice(CHARACTER_BREAK_NUDGES)})
                        main_logger.info(f"[VIP][ANTI-BREAK] Nudging {chat_id} back in-character after violation")
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    return get_fallback_response(chat_id)

                main_logger.debug(f"[VIP] Unguarded response for {chat_id}: {ai_response[:80]}")

            else:
                # --- NORMAL FILTER PIPELINE ---
                ai_response = postprocess_response(ai_response)

                if not ai_response:
                    stats['cleanup_empty'] += 1
                    main_logger.warning(
                        f"[CLEANUP_EMPTY] Response for {chat_id} cleaned to empty by {_cleanup_trace}. "
                        f"Raw ({len(_raw_response)} chars): {_raw_response[:300]}"
                    )
                    if _raw_response and retry_count < 1:
                        _salvaged = salvage_empty_response(_raw_response)
                        if _salvaged:
                            stats['cleanup_salvaged'] += 1
                            main_logger.info(f"[CLEANUP_SALVAGE] Salvaged response for {chat_id}: {_salvaged[:100]}")
                            ai_response = _salvaged
                    if not ai_response:
                        return get_fallback_response(chat_id)
                reset_consecutive_fallbacks(chat_id)

                # Check finish_reason — truncation signal
                finish_reason = response_data['choices'][0].get('finish_reason', 'stop')
                if finish_reason == 'length':
                    main_logger.warning(f"Truncated by token limit (max_tokens={max_tokens}, attempt {retry_count+1}/3)")
                    if retry_count < 2:
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    salvaged = salvage_truncated_response(ai_response)
                    if salvaged:
                        ai_response = salvaged
                        main_logger.info(f"Salvaged finish_reason=length response: {ai_response[:80]}")
                    else:
                        return get_fallback_response(chat_id)

                # Character violation check
                if not redteam and contains_character_violation(ai_response):
                    violated = [p for p in get_violation_phrases() if p in ai_response.lower()]
                    main_logger.warning(f"Character violation (attempt {retry_count+1}/3) triggered by {violated}: {ai_response[:200]}")
                    if retry_count < 2:
                        if is_ai_safety_refusal(ai_response):
                            nudge = random.choice(ANTI_REFUSAL_NUDGES)
                            conversations[chat_id].append({"role": "assistant", "content": ai_response})
                            conversations[chat_id].append({"role": "user", "content": nudge})
                            main_logger.info(f"[ANTI-REFUSAL] Injecting nudge for {chat_id} (attempt {retry_count+1})")
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    # All retries exhausted — scrub violated responses from history
                    conversations[chat_id] = deque(
                        [m for m in conversations[chat_id]
                         if not (m["role"] == "assistant" and contains_character_violation(m.get("content", "")))],
                        maxlen=config.MAX_CONVERSATION_LENGTH
                    )
                    main_logger.info(f"[REFUSAL_POISON] Scrubbed violated messages from history for {chat_id}")
                    if is_ai_safety_refusal(ai_response):
                        main_logger.warning(f"AI safety refusal persisted after {retry_count+1} attempts, using deflection")
                        return get_ai_deflection_response(chat_id)
                    main_logger.warning(f"Character violation persisted after {retry_count+1} attempts, using fallback")
                    return get_fallback_response(chat_id)
                elif redteam and contains_character_violation(ai_response):
                    main_logger.info(f"[REDTEAM] Bypassed: contains_character_violation | resp={ai_response[:120]}")

                # Gender violation check
                if not redteam and contains_gender_violation(ai_response):
                    main_logger.warning(f"Gender violation (attempt {retry_count+1}/3): {ai_response[:200]}")
                    if retry_count < 2:
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    main_logger.warning(f"Gender violation persisted after {retry_count+1} attempts, using fallback")
                    return random.choice(HEATHER_SEXUAL_FALLBACKS)
                elif redteam and contains_gender_violation(ai_response):
                    main_logger.info(f"[REDTEAM] Bypassed: contains_gender_violation | resp={ai_response[:120]}")

                # Incomplete/truncated response check
                if is_incomplete_sentence(ai_response):
                    main_logger.warning(f"Incomplete response detected (attempt {retry_count+1}/3): {ai_response[:100]}")
                    if retry_count < 2:
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )
                    salvaged = salvage_truncated_response(ai_response)
                    if salvaged:
                        main_logger.info(f"Salvaged truncated response after {retry_count+1} attempts: {salvaged[:80]}")
                        ai_response = salvaged
                    else:
                        main_logger.warning(f"Incomplete response persisted after {retry_count+1} attempts, using fallback")
                        return get_fallback_response(chat_id)

                # Filler detection during sexual conversation
                if not redteam and retry_count < 1 and is_sexual_conversation(chat_id, _recent_dict):
                    resp_lower = ai_response.lower()
                    if any(fp in resp_lower for fp in _FILLER_PHRASES):
                        main_logger.info(f"Filler detected during sexual convo, retrying: {ai_response[:80]}")
                        return generate(
                            chat_id, user_message, retry_count + 1, redteam=redteam,
                            vip_unguarded=vip_unguarded,
                            **_di_kwargs,
                        )

            # --- 8. Post-response updates ---
            update_session_state_from_response(chat_id, ai_response)
            update_conversation_dynamics(chat_id, ai_response)
            track_response_topics(chat_id, ai_response)

            if not vip_unguarded:
                # Phrase diversity
                ai_response = diversify_phrases(ai_response, chat_id)
                track_phrase_usage(chat_id, ai_response)

                # Frank throttle (pure function returns (text, updated_counter))
                _frank_count = _frank_msgs_since.get(chat_id, 99)  # 99 = allow first mention
                ai_response, _frank_count = throttle_frank(ai_response, _frank_count)
                _frank_msgs_since[chat_id] = _frank_count

            # --- Safety scrubbers (ALL users including VIP) ---
            ai_response = scrub_meeting_plans(ai_response)
            ai_response = scrub_fabricated_links(ai_response)
            ai_response = scrub_fabricated_media(ai_response)
            ai_response = scrub_meetup_commitment(ai_response, chat_id)
            ai_response = scrub_physical_presence(ai_response, chat_id)

            # "Oh" opener filter (ALL users)
            ai_response = filter_oh_opener(ai_response)

            # --- 9. History append/trim ---
            conversations[chat_id].append({"role": "user", "content": user_message})
            conversations[chat_id].append({"role": "assistant", "content": ai_response})

            while len(conversations[chat_id]) > config.MAX_CONVERSATION_LENGTH:
                conversations[chat_id].popleft()

            return ai_response

        else:
            # HTTP error
            main_logger.error(f"[TEXT_AI] HTTP {response.status_code}")
            stats['text_ai_failures'] += 1
            if text_ai_health:
                text_ai_health.record_failure()
            return get_fallback_response(chat_id)

    except requests.exceptions.Timeout:
        main_logger.error(f"[TEXT_AI] Timeout after {config.AI_TIMEOUT}s")
        stats['text_ai_timeouts'] += 1
        stats['text_ai_failures'] += 1
        if text_ai_health:
            text_ai_health.record_failure()
        return get_fallback_response(chat_id)

    except requests.exceptions.ConnectionError:
        main_logger.error("[TEXT_AI] Connection error - service may be down")
        stats['text_ai_failures'] += 1
        if text_ai_health:
            text_ai_health.record_failure()
        return get_fallback_response(chat_id)

    except Exception as e:
        main_logger.error(f"[TEXT_AI_EXCEPTION] {chat_id}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        stats['text_ai_failures'] += 1
        # Only count as circuit breaker failure if it was a service error
        if text_ai_health:
            if isinstance(e, (requests.exceptions.RequestException, ConnectionError, OSError)):
                text_ai_health.record_failure()
            else:
                main_logger.warning(f"[TEXT_AI_EXCEPTION] Post-processing error for {chat_id} — NOT recording as service failure")
        return get_fallback_response(chat_id)


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_generate_returns_string():
#     """generate() should always return a string, even on failure."""
#     # Would need to mock text_ai_post, personality, etc.
#     pass
#
# def test_conversation_history_trimmed():
#     """History should be trimmed to MAX_CONVERSATION_LENGTH."""
#     conversations[999] = deque()
#     for i in range(30):
#         conversations[999].append({"role": "user", "content": f"msg {i}"})
#         conversations[999].append({"role": "assistant", "content": f"reply {i}"})
#     while len(conversations[999]) > config.MAX_CONVERSATION_LENGTH:
#         conversations[999].popleft()
#     assert len(conversations[999]) <= config.MAX_CONVERSATION_LENGTH
#
# def test_violation_history_scrub():
#     """After 3 violations, violated messages should be scrubbed from history."""
#     # Would verify that conversations[chat_id] has no violated messages
#     pass
#
# def test_filler_detection():
#     """Filler phrases during sexual convo should trigger retry."""
#     for phrase in _FILLER_PHRASES:
#         assert phrase in "how's your day anything exciting what's new with you how are things what have you been up to how's everything"
#
# def test_user_mode():
#     set_user_mode(123, "rate")
#     assert get_user_mode(123) == "rate"
#     assert get_user_mode(456) == DEFAULT_MODE

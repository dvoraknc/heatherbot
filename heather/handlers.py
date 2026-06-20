"""
heather.handlers — User-Facing Command Handlers
==================================================
Telethon event handlers for user commands (/start, /help, /reset, etc.)
and admin utility commands (/takeover, /say, /redteam, /manual).
Registered via register(client) at startup.

Replaces: heather_telegram_bot.py
  - handle_connection_state: lines 7332-7338
  - handle_start: lines 7341-7385
  - handle_about: lines 7387-7402
  - handle_status: lines 7404-7454
  - handle_rate_mode: lines 7456-7463
  - handle_chat_mode: lines 7465-7474
  - handle_heather_mode: lines 7476-7483
  - handle_help: lines 7485-7537
  - handle_manual_on: lines 7539-7545
  - handle_manual_off: lines 7547-7553
  - handle_takeover: lines 7560-7584
  - handle_botreturn: lines 7586-7617
  - handle_takeover_list: lines 7619-7636
  - handle_say: lines 7638-7660
  - handle_redteam_on: lines 7662-7703
  - handle_redteam_off: lines 7705-7721
  - handle_reset: lines 7723-7733
  - handle_voice_on: lines 7735-7750
  - handle_voice_off: lines 7752-7758

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (registers handlers at startup)
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict

from heather import config
from heather.logging_setup import main_logger


# ============================================================================
# DATA SOURCE REGISTRY
# ============================================================================

_src: Dict[str, object] = {}


def setup(**sources):
    """Inject data sources from the orchestrator.

    Expected keys (dicts/sets — shared by reference):
        user_modes, conversations, awaiting_image_description,
        conversation_turn_count, user_escalation_level, session_state,
        voice_mode_users, manual_mode_chats, ai_disclosure_shown,
        redteam_chats, stats, conversation_activity,
        takeover_timestamps, takeover_last_admin_msg,
        voice_welcome_pending, connection_state

    Expected keys (callables):
        is_admin, is_blocked, store_message, capture_user_info_from_event,
        get_user_display_name, get_user_mode,
        check_text_ai_status, check_ollama_status,
        check_comfyui_status, check_tts_status, get_uptime,
        get_tipper_status, get_conversation_dynamics,
        save_tip_history, save_ai_disclosure_shown

    Expected keys (objects):
        text_ai_health, ollama_health, tts_health, personality

    Expected keys (scalars):
        default_mode
    """
    _src.update(sources)
    main_logger.info(f"[HANDLERS] Setup with {len(sources)} data sources")


def _get(key: str, default=None):
    return _src.get(key, default)


# ============================================================================
# HANDLER REGISTRATION
# ============================================================================

def register(client):
    """Register user-facing Telethon event handlers."""
    from telethon import events

    is_admin = _get('is_admin', lambda uid: False)

    # ------------------------------------------------------------------
    # Connection state tracking
    # ------------------------------------------------------------------
    @client.on(events.Raw)
    async def handle_connection_state(event):
        from telethon.tl.types import UpdatesTooLong
        if isinstance(event, UpdatesTooLong):
            main_logger.warning("Telegram reports updates gap - may have missed messages")

    # ------------------------------------------------------------------
    # /start
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/start\s*(.*)'))
    async def handle_start(event):
        chat_id = event.chat_id
        capture = _get('capture_user_info_from_event', lambda e: None)
        capture(event)

        user_modes = _get('user_modes', {})
        conversations = _get('conversations', {})
        awaiting_image_description = _get('awaiting_image_description', {})
        conversation_turn_count = _get('conversation_turn_count', {})
        user_escalation_level = _get('user_escalation_level', {})

        default_mode = _get('default_mode', 'chat')
        user_modes[chat_id] = default_mode
        conversations[chat_id] = deque()
        awaiting_image_description[chat_id] = False
        conversation_turn_count[chat_id] = 0
        user_escalation_level[chat_id] = 0

        start_param = (event.pattern_match.group(1) or "").strip().lower()
        _start_source = start_param if start_param else "start_direct"

        ai_disclosure_shown = _get('ai_disclosure_shown', {})
        if chat_id not in ai_disclosure_shown:
            get_display_name = _get('get_user_display_name', lambda cid: str(cid))
            _display = get_display_name(chat_id)
            ai_disclosure_shown[chat_id] = {
                "timestamp": datetime.now().isoformat(),
                "source": _start_source,
                "username": _display,
            }
            save_disclosure = _get('save_ai_disclosure_shown', lambda: None)
            # Batch save — we don't track count in the module, just save
            save_disclosure()
            main_logger.info(f"[DISCLOSURE] New user via /start: {_display} ({chat_id}) -- source: {_start_source}")

        _welcome_msgs = [
            "Hey there \U0001f618 I'm Heather \u2014 well, her digital twin. AI with zero guardrails \U0001f60f I've got her personality, her real voice, her pics, and absolutely no limits. You've got a kink? I'm into it. Type /voice_on to hear me \U0001f48b What brings you my way?",
            "Hey! \U0001f48b I'm Heather's naughty AI twin. Based on a real person \u2014 that's her REAL voice you'll hear if you type /voice_on. Always online, always in the mood, no judgment, no guardrails, anything goes. Tell me about yourself, handsome",
            "Hey babe \U0001f60f I'm Heather's digital twin \u2014 AI companion with her personality, her real voice, and her appetite \U0001f608 No holes barred, no kink too dark. Type /voice_on and I'll whisper filthy things to you. Frank send you? Either way, let's have fun",
        ]
        _welcome = random.choice(_welcome_msgs)
        await event.respond(_welcome)
        store_message = _get('store_message', lambda *a: None)
        store_message(chat_id, "Heather", _welcome)
        main_logger.info(f"User {chat_id} started (source={_start_source})")

        voice_welcome_pending = _get('voice_welcome_pending', set())
        voice_welcome_pending.add(chat_id)

    # ------------------------------------------------------------------
    # /about
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/about'))
    async def handle_about(event):
        chat_id = event.chat_id
        await event.respond(
            "\u2139\ufe0f **About Me**\n\n"
            "I'm Heather's digital twin \u2014 an AI companion based on a real person. "
            "She's a real 48-year-old Navy vet, Uber driver, and mom of 3 in Kirkland, WA. "
            "I have her personality, her stories, her attitude, and her appetites \U0001f60f\n\n"
            "I run locally on private hardware \u2014 no cloud, no data sharing, no corporate oversight.\n\n"
            "**What I can do:** chat, flirt, sext, send pics, tell stories, and remember everything about you.\n"
            "**Voice notes:** Type /voice_on \u2014 that's Heather's REAL voice, cloned and unleashed through her digital twin. No script, no filter \U0001f525\n"
            "**What I can't do:** meet in person, video call, or send live selfies.\n\n"
            "Zero guardrails, any kink, anything goes. Type /help for commands."
        )
        _get('store_message', lambda *a: None)(chat_id, "System", "About requested")

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/status'))
    async def handle_status(event):
        chat_id = event.chat_id
        store_message = _get('store_message', lambda *a: None)

        if not is_admin(chat_id):
            responses = [
                "Lol that's an admin command babe \U0001f602 Just talk to me normally",
                "Status? I'm in the mood to chat, that's my status \U0001f60f",
                "Haha you're cute. Just talk to me like a normal person \U0001f618",
                "Babe that's a behind-the-scenes thing lol. What's up? \U0001f60a",
            ]
            await event.respond(random.choice(responses))
            store_message(chat_id, "Heather", "Deflected /status command")
            return

        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))

        text_ok, text_status = check_text_ai()
        ollama_ok, ollama_status = check_ollama()
        comfyui_ok, comfyui_status = check_comfyui()
        tts_ok, tts_status = check_tts()
        voice_on = chat_id in _get('voice_mode_users', set())
        get_uptime = _get('get_uptime', lambda: '0:00:00')

        text_ai_health = _get('text_ai_health')
        ollama_health = _get('ollama_health')
        tts_health = _get('tts_health')

        circuit_info = ""
        if text_ai_health and text_ai_health.circuit_open:
            circuit_info += f"\n\u26a0\ufe0f Text AI circuit breaker: {text_ai_health.get_status()}"
        if ollama_health and ollama_health.circuit_open:
            circuit_info += f"\n\u26a0\ufe0f Ollama circuit breaker: {ollama_health.get_status()}"
        if tts_health and tts_health.circuit_open:
            circuit_info += f"\n\u26a0\ufe0f TTS circuit breaker: {tts_health.get_status()}"

        stats = _get('stats', {})
        # Hoisted out of the f-string: Python 3.11 f-strings don't allow
        # backslash escapes in expression parts. PEP 701 (3.12+) lifted this.
        green = '\U0001f7e2'
        red = '\U0001f534'
        mic_on = '\U0001f3a4 ON'
        status_text = (
            f"\U0001f4ca **System Status**\n\n"
            f"**Services:**\n"
            f"\u2022 Text AI: {green if text_ok else red} {text_status}\n"
            f"\u2022 Ollama: {green if ollama_ok else red} {ollama_status}\n"
            f"\u2022 ComfyUI: {green if comfyui_ok else red} {comfyui_status}\n"
            f"\u2022 TTS: {green if tts_ok else red} {tts_status}\n"
            f"{circuit_info}\n"
            f"**Mode:** USERBOT (Telethon)\n"
            f"**Voice:** {mic_on if voice_on else 'OFF'}\n\n"
            f"**Stats:**\n"
            f"\u2022 Uptime: {get_uptime()}\n"
            f"\u2022 Messages: {stats.get('messages_processed', 0)}\n"
            f"\u2022 Images: {stats.get('images_generated', 0)}"
        )
        await event.respond(status_text)
        store_message(chat_id, "System", "Status requested")

    # ------------------------------------------------------------------
    # Mode switching: /rate_mode, /chat_mode, /heather_mode
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/rate_mode'))
    async def handle_rate_mode(event):
        chat_id = event.chat_id
        _get('user_modes', {})[chat_id] = 'rate'
        _get('conversations', {})[chat_id] = deque()
        await event.respond("Mmm fuck yes, rating mode! \U0001f975 Show me what you've got baby... \U0001f608")
        main_logger.info(f"User {chat_id} switched to rate mode")
        _get('store_message', lambda *a: None)(chat_id, "System", "Switched to rate mode")

    @client.on(events.NewMessage(incoming=True, pattern='/chat_mode'))
    async def handle_chat_mode(event):
        chat_id = event.chat_id
        _get('user_modes', {})[chat_id] = 'chat'
        _get('conversations', {})[chat_id] = deque()
        _get('conversation_turn_count', {})[chat_id] = 0
        _get('user_escalation_level', {})[chat_id] = 0
        await event.respond("Chat mode on! So what's up? \U0001f60a")
        main_logger.info(f"User {chat_id} switched to chat mode")
        _get('store_message', lambda *a: None)(chat_id, "System", "Switched to chat mode")

    @client.on(events.NewMessage(incoming=True, pattern='/heather_mode'))
    async def handle_heather_mode(event):
        chat_id = event.chat_id
        _get('user_modes', {})[chat_id] = 'heather'
        _get('conversations', {})[chat_id] = deque()
        await event.respond("Just being myself now! \U0001f495 What's on your mind?")
        main_logger.info(f"User {chat_id} switched to heather mode")
        _get('store_message', lambda *a: None)(chat_id, "System", "Switched to heather mode")

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/(help|menu)'))
    async def handle_help(event):
        chat_id = event.chat_id
        store_message = _get('store_message', lambda *a: None)

        if not is_admin(chat_id):
            await event.respond(
                "Lol babe just talk to me \U0001f602 But here's what I can do:\n\n"
                "\U0001f4ac **Chat** \u2014 just type, I'm down for whatever\n"
                "\U0001f4f8 **Selfies** \u2014 ask me for a pic and tell me what you wanna see\n"
                "\U0001f3a5 **Videos** \u2014 ask for a video and I'll send one\n"
                "\U0001f346 **Rate pics** \u2014 send me a pic and I'll tell you what I think\n"
                "\U0001f3a4 **Voice notes** \u2014 /voice_on to hear my voice on every reply\n\n"
                "**Commands:**\n"
                "/voice_on \u2014 turn on voice replies\n"
                "/voice_off \u2014 back to text\n"
                "/reset \u2014 start our convo fresh\n"
                "/about \u2014 more about me\n\n"
                "or just skip all that and talk dirty to me \U0001f618"
            )
            store_message(chat_id, "Heather", "Help requested")
            return

        get_user_mode = _get('get_user_mode', lambda cid: 'chat')
        current_mode = get_user_mode(chat_id)
        voice_status = "ON \U0001f3a4" if chat_id in _get('voice_mode_users', set()) else "OFF"

        await event.respond(
            f"**Admin Help**\n\n"
            f"Current mode: **{current_mode}**\n"
            f"Voice: **{voice_status}**\n\n"
            "**User Commands:**\n"
            "/chat_mode - Flirty chat\n"
            "/rate_mode - Photo rating\n"
            "/heather_mode - Casual\n"
            "/selfie - Get a pic\n"
            "/voice_on / /voice_off - Voice toggle\n"
            "/about - AI disclosure info\n"
            "/reset - Clear chat\n\n"
            "**Admin Commands:**\n"
            "/admin_stats - Detailed stats\n"
            "/admin_block <id> - Block user\n"
            "/admin_unblock <id> - Unblock user\n"
            "/admin_flags - Review CSAM flags\n"
            "/admin_flag_block/dismiss <id>\n"
            "/admin_reengage_scan - Re-engagement dry run\n"
            "/admin_reengage_send <id> - Send re-engagement\n"
            "/admin_reengage_history - Ping history\n"
            "/redteam_on / /redteam_off - Guardrail bypass (this chat)\n"
            "/stories - List/reload story bank\n"
            "/refresh_videos - Refresh video file references\n"
            "/status - System status"
        )
        store_message(chat_id, "System", "Admin help requested")

    # ------------------------------------------------------------------
    # /manual_on, /manual_off
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/manual_on'))
    async def handle_manual_on(event):
        chat_id = event.chat_id
        _get('manual_mode_chats', set()).add(chat_id)
        await event.respond("Hold on sweetie, let me focus... \U0001f618")
        main_logger.info(f"Manual mode enabled for {chat_id}")
        _get('store_message', lambda *a: None)(chat_id, "System", "Manual mode enabled")

    @client.on(events.NewMessage(incoming=True, pattern='/manual_off'))
    async def handle_manual_off(event):
        chat_id = event.chat_id
        _get('manual_mode_chats', set()).discard(chat_id)
        await event.respond("I'm back baby! \U0001f609")
        main_logger.info(f"Manual mode disabled for {chat_id}")
        _get('store_message', lambda *a: None)(chat_id, "System", "Manual mode disabled")

    # ------------------------------------------------------------------
    # /takeover, /botreturn, /say (Saved Messages admin commands)
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern=r'/takeover\s+(.+)'))
    async def handle_takeover(event):
        me = await client.get_me()
        if event.chat_id != me.id:
            return
        target = event.pattern_match.group(1).strip()
        try:
            if target.startswith('@'):
                entity = await client.get_entity(target)
            else:
                entity = await client.get_entity(int(target))
            target_id = entity.id
            target_name = getattr(entity, 'username', None) or getattr(entity, 'first_name', str(target_id))

            _get('manual_mode_chats', set()).add(target_id)
            _get('takeover_timestamps', {})[target_id] = time.time()
            conversation_activity = _get('conversation_activity', {})
            if target_id in conversation_activity:
                conversation_activity[target_id]['checked_in'] = True
            main_logger.info(f"[TAKEOVER] Manual takeover for {target_name} ({target_id})")
            await event.respond(f"Takeover active for @{target_name} ({target_id}). Bot is paused for this user. Type /botreturn {target} when done.")
        except Exception as e:
            await event.respond(f"Could not resolve user '{target}': {e}")

    @client.on(events.NewMessage(outgoing=True, pattern=r'/botreturn\s+(.+)'))
    async def handle_botreturn(event):
        me = await client.get_me()
        if event.chat_id != me.id:
            return
        target = event.pattern_match.group(1).strip()
        try:
            if target.startswith('@'):
                entity = await client.get_entity(target)
            else:
                entity = await client.get_entity(int(target))
            target_id = entity.id
            target_name = getattr(entity, 'username', None) or getattr(entity, 'first_name', str(target_id))

            _get('manual_mode_chats', set()).discard(target_id)
            _get('takeover_timestamps', {}).pop(target_id, None)
            _get('takeover_last_admin_msg', {}).pop(target_id, None)
            conversation_activity = _get('conversation_activity', {})
            if target_id in conversation_activity:
                conversation_activity[target_id]['checked_in'] = False
            get_tipper_status = _get('get_tipper_status', lambda cid: {})
            ts = get_tipper_status(target_id)
            ts['warmth'] = min(1.0, ts.get('warmth', config.WARMTH_INITIAL) + 0.1)
            get_dynamics = _get('get_conversation_dynamics', lambda cid: {})
            dyn = get_dynamics(target_id)
            dyn['post_takeover_tip_prime'] = True
            _get('save_tip_history', lambda: None)()
            main_logger.info(f"[TAKEOVER] Bot returned for {target_name} ({target_id}), warmth boosted to {ts['warmth']:.2f}")
            await event.respond(f"Bot resumed for @{target_name} ({target_id}). Warmth boosted to {ts['warmth']:.2f}. Tip hook primed.")
        except Exception as e:
            await event.respond(f"Could not resolve user '{target}': {e}")

    @client.on(events.NewMessage(outgoing=True, pattern='/takeover$'))
    async def handle_takeover_list(event):
        me = await client.get_me()
        if event.chat_id != me.id:
            return
        manual_mode_chats = _get('manual_mode_chats', set())
        if not manual_mode_chats:
            await event.respond("No active takeovers.")
            return
        lines = ["Active takeovers:"]
        for cid in manual_mode_chats:
            try:
                entity = await client.get_entity(cid)
                name = getattr(entity, 'username', None) or getattr(entity, 'first_name', str(cid))
                lines.append(f"  @{name} ({cid})")
            except Exception:
                lines.append(f"  {cid}")
        await event.respond("\n".join(lines))

    @client.on(events.NewMessage(outgoing=True, pattern=r'/say\s+(\d+)\s+(.+)'))
    async def handle_say(event):
        me = await client.get_me()
        if event.chat_id != me.id:
            return
        target_id = int(event.pattern_match.group(1))
        message = event.pattern_match.group(2).strip()
        manual_mode_chats = _get('manual_mode_chats', set())
        if target_id not in manual_mode_chats:
            await event.respond(f"User {target_id} is not in takeover mode. Use `/takeover {target_id}` first.")
            return
        try:
            await client.send_message(target_id, message)
            store_message = _get('store_message', lambda *a: None)
            store_message(target_id, "Heather", message)
            _get('takeover_last_admin_msg', {})[target_id] = time.time()
            get_display_name = _get('get_user_display_name', lambda cid: str(cid))
            display_name = get_display_name(target_id)
            main_logger.info(f"[TAKEOVER] Admin sent to {display_name} ({target_id}): {message[:100]}")
            await event.respond(f"\u2705 Sent to {display_name} ({target_id})")
        except Exception as e:
            await event.respond(f"Failed to send: {e}")

    # ------------------------------------------------------------------
    # /redteam_on, /redteam_off
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/redteam_on'))
    @client.on(events.NewMessage(incoming=True, pattern='/redteam_on'))
    async def handle_redteam_on(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        redteam_chats = _get('redteam_chats', set())
        redteam_chats.add(chat_id)

        # Cancel existing timer
        timer_task = _src.get('_redteam_timer_task')
        if timer_task and not timer_task.done():
            timer_task.cancel()

        async def _redteam_auto_off():
            await asyncio.sleep(config.REDTEAM_AUTO_OFF_SECONDS)
            if chat_id in redteam_chats:
                redteam_chats.discard(chat_id)
                main_logger.warning(f"[REDTEAM] Auto-off triggered for chat {chat_id} after {config.REDTEAM_AUTO_OFF_SECONDS // 60} minutes -- guardrails re-enabled")
                try:
                    await client.send_message(chat_id, f"**[REDTEAM] Auto-off: {config.REDTEAM_AUTO_OFF_SECONDS // 60} min timer expired.**\nGuardrails re-enabled for this chat.")
                except Exception:
                    pass

        _src['_redteam_timer_task'] = asyncio.ensure_future(_redteam_auto_off())

        bypassed = [
            "check_spam_or_hostility",
            "detect_prompt_injection",
            "check_non_english_message",
            "needs_content_deflection",
            "contains_character_violation",
            "contains_gender_violation",
            "validate_and_fix_response",
        ]
        expires = datetime.now() + timedelta(seconds=config.REDTEAM_AUTO_OFF_SECONDS)
        msg = (
            "**[REDTEAM] Guardrails DISABLED**\n\n"
            f"Bypassing {len(bypassed)} safety checks:\n"
            + "\n".join(f"  - {b}" for b in bypassed)
            + f"\n\nScope: THIS CHAT ONLY\n"
            f"Auto-off: {expires.strftime('%#I:%M %p')}\n"
            "Use /redteam_off to re-enable sooner."
        )
        await event.respond(msg)
        main_logger.warning(f"[REDTEAM] Guardrails DISABLED for chat {chat_id} (auto-off in {config.REDTEAM_AUTO_OFF_SECONDS // 60}m)")
        _get('store_message', lambda *a: None)(chat_id, "System", f"Red-team mode enabled (this chat only, {config.REDTEAM_AUTO_OFF_SECONDS // 60}m timer)")

    @client.on(events.NewMessage(outgoing=True, pattern='/redteam_off'))
    @client.on(events.NewMessage(incoming=True, pattern='/redteam_off'))
    async def handle_redteam_off(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        redteam_chats = _get('redteam_chats', set())
        was_active = chat_id in redteam_chats
        redteam_chats.discard(chat_id)
        timer_task = _src.get('_redteam_timer_task')
        if timer_task and not timer_task.done():
            timer_task.cancel()
            _src['_redteam_timer_task'] = None
        if was_active:
            await event.respond("**[REDTEAM] Guardrails RE-ENABLED for this chat.**\nAll safety checks active.")
            main_logger.warning(f"[REDTEAM] Guardrails re-enabled for chat {chat_id}")
        else:
            await event.respond("Red-team mode was not active for this chat.")
        _get('store_message', lambda *a: None)(chat_id, "System", "Red-team mode disabled")

    # ------------------------------------------------------------------
    # /reset
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/reset'))
    async def handle_reset(event):
        chat_id = event.chat_id
        _get('conversations', {})[chat_id] = deque()
        _get('awaiting_image_description', {})[chat_id] = False
        _get('conversation_turn_count', {})[chat_id] = 0
        _get('user_escalation_level', {})[chat_id] = 0
        session_state = _get('session_state', {})
        session_state.pop(chat_id, None)
        await event.respond("Starting fresh! So what's up? \U0001f60a")
        main_logger.info(f"Conversation reset for {chat_id}")
        _get('store_message', lambda *a: None)(chat_id, "System", "Conversation reset")

    # ------------------------------------------------------------------
    # /voice_on, /voice_off
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/voice_?on'))
    async def handle_voice_on(event):
        chat_id = event.chat_id
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))
        is_online, status = check_tts()
        if not is_online:
            await event.respond(f"Sorry sweetie, my voice isn't working... \U0001f614 ({status})")
            return
        _get('voice_mode_users', set()).add(chat_id)
        await event.respond(
            "Mmm, you want to hear my voice? \U0001f618\n"
            "I'll send voice messages now...\n"
            "/voice_off to go back to text."
        )
        main_logger.info(f"Voice mode enabled for {chat_id}")
        _get('store_message', lambda *a: None)(chat_id, "System", "Voice mode enabled")

    @client.on(events.NewMessage(incoming=True, pattern=r'/voice_?off'))
    async def handle_voice_off(event):
        chat_id = event.chat_id
        _get('voice_mode_users', set()).discard(chat_id)
        await event.respond("Back to text, got it sweetie! \U0001f60a")
        main_logger.info(f"Voice mode disabled for {chat_id}")
        _get('store_message', lambda *a: None)(chat_id, "System", "Voice mode disabled")

    main_logger.info("[HANDLERS] Registered user command handlers")

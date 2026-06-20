"""
heather.admin — Admin Command Handlers
========================================
Telethon event handlers for admin-only commands. All handlers are registered
via register(client) at startup. Data sources injected via setup().

Replaces: heather_telegram_bot.py
  - ADMIN COMMANDS section: lines 7760-8563
  - handle_admin_stats: lines 7764-7820
  - handle_refresh_videos: lines 7822-7830
  - handle_admin_block: lines 7832-7849
  - handle_admin_unblock: lines 7851-7867
  - handle_admin_reset: lines 7869-7892
  - handle_admin_reload: lines 7894-7909
  - handle_stories_command: lines 7911-7938
  - handle_admin_blocked: lines 7940-7951
  - handle_admin_flags: lines 7953-7980
  - handle_admin_flag_block: lines 7982-8017
  - handle_admin_flag_dismiss: lines 8019-8041
  - handle_admin_flag_clear: lines 8043-8055
  - handle_admin_reengage_scan: lines 8057-8179
  - _generate_reengage_preview: lines 8181-8229
  - handle_admin_reengage_send: lines 8231-8307
  - handle_admin_reengage_history: lines 8309-8330
  - handle_testtip: lines 8332-8343
  - handle_admin_warmth: lines 8345-8390
  - handle_admin_opportunities: lines 8392-8419
  - handle_bridge_add: lines 8421-8431
  - handle_bridge_remove: lines 8433-8443
  - handle_bridge_status: lines 8445-8467
  - handle_admin_help: lines 8469-8501
  - handle_admin_catchup: lines 8503-8529
  - handle_library_status: lines 8531-8563

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (registers handlers at startup)
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Dict, Optional

from heather import config
from heather.logging_setup import main_logger


# ============================================================================
# DATA SOURCE REGISTRY
# ============================================================================

_src: Dict[str, object] = {}


def setup(**sources):
    """Inject data sources from the orchestrator.

    Expected keys (dicts/sets — shared by reference):
        stats, recent_messages, tipper_status, csam_flags,
        payment_bot_started_users, manual_mode_chats, voice_mode_users,
        conversation_turn_count, conversations, user_info,
        image_analysis_cache, blocked_users, user_modes,
        awaiting_image_description, user_escalation_level, session_state,
        last_response_sent, image_library,
        pipeline_bridge_chat_ids, takeover_opportunities

    Expected keys (callables):
        is_admin, check_text_ai_status, check_ollama_status,
        check_comfyui_status, check_tts_status,
        get_uptime, get_stats_snapshot, get_warmth_tier, get_access_tier,
        save_blocked_users, save_csam_flags,
        load_reengagement_history, save_reengagement_history,
        load_shutdown_timestamp, text_ai_post, strip_quote_wrapping,
        send_stars_invoice, refresh_video_cache, get_available_videos,
        load_story_bank, get_user_display_name, store_message

    Expected keys (objects):
        text_ai_health, ollama_health, tts_health,
        personality, story_bank_ref

    Expected keys (scalars):
        pipeline_bridge_ready, text_ai_model
    """
    _src.update(sources)
    main_logger.info(f"[ADMIN] Setup with {len(sources)} data sources")


def _get(key: str, default=None):
    return _src.get(key, default)


# ============================================================================
# BUSINESS LOGIC (transport-agnostic)
# ============================================================================

def generate_reengage_preview(candidate: dict) -> Optional[str]:
    """Generate a preview re-engagement message via LLM."""
    text_ai_post = _get('text_ai_post')
    strip_quote_wrapping = _get('strip_quote_wrapping', lambda x: x)
    text_ai_model = _get('text_ai_model', 'local-model')

    if not text_ai_post:
        return None

    convo_lines = []
    for msg in candidate.get('recent_messages', []):
        convo_lines.append(f"{msg['sender']}: {msg['text']}")
    convo_text = "\n".join(convo_lines)
    idle_days = int(candidate['idle_days'])
    display_name = candidate['display_name']

    prompt_messages = [
        {
            "role": "system",
            "content": (
                f"You are Heather, a 48-year-old mom from Kirkland WA. You're texting someone you "
                f"chatted with {idle_days} days ago. Their name is {display_name}.\n\n"
                f"Here's the tail end of your last conversation:\n{convo_text}\n\n"
                f"Write a SHORT casual text (1-2 sentences max) checking in on them. Reference "
                f"something specific from the conversation. Sound natural, like you just thought "
                f"of them. Use lowercase, abbreviations, maybe one emoji.\n\n"
                f"Examples of good re-engagement texts:\n"
                f"- \"hey did you ever figure out that job situation? been thinking about it lol\"\n"
                f"- \"omg i just drove past that restaurant you mentioned \U0001f602 how've you been\"\n"
                f"- \"random but i saw something that reminded me of you haha, how's it going\"\n\n"
                f"Do NOT:\n"
                f"- Say \"hey stranger\" or \"long time no talk\" (too generic)\n"
                f"- Ask multiple questions\n"
                f"- Be overly enthusiastic or use lots of emojis\n"
                f"- Mention that you're \"checking in\" explicitly\n"
                f"- Use asterisks for actions like *waves*\n"
                f"- Write more than 2 sentences"
            )
        },
        {"role": "user", "content": f"Write a casual re-engagement text to {display_name}."}
    ]
    try:
        response = text_ai_post({
            "model": text_ai_model, "messages": prompt_messages,
            "temperature": 0.85, "max_tokens": 2048, "stream": False,
            "top_p": 0.9, "frequency_penalty": 0.3, "presence_penalty": 0.3
        }, timeout=60)
        if response.status_code == 200:
            message_data = response.json()['choices'][0]['message']
            msg = message_data.get('content', '').strip()
            if msg:
                msg = strip_quote_wrapping(msg)
                return msg
    except Exception:
        pass
    return None


# ============================================================================
# HANDLER REGISTRATION
# ============================================================================

def register(client):
    """Register all admin Telethon event handlers."""
    from telethon import events

    is_admin = _get('is_admin', lambda uid: False)

    # ------------------------------------------------------------------
    # /admin_stats
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/admin_stats'))
    async def handle_admin_stats(event):
        """Show detailed admin statistics."""
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return

        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))

        text_ok, text_status = check_text_ai()
        ollama_ok, ollama_status = check_ollama()
        comfyui_ok, comfyui_status = check_comfyui()
        tts_ok, tts_status = check_tts()

        get_stats_snapshot = _get('get_stats_snapshot', lambda: {})
        stats_snapshot = get_stats_snapshot()
        get_uptime = _get('get_uptime', lambda: '0:00:00')

        text_ai_health = _get('text_ai_health')
        ollama_health = _get('ollama_health')
        tts_health = _get('tts_health')

        pipeline_bridge_ready = _get('pipeline_bridge_ready', False)
        pipeline_bridge_chat_ids = _get('pipeline_bridge_chat_ids', set())
        conversations = _get('conversations', {})
        user_info = _get('user_info', {})
        image_analysis_cache = _get('image_analysis_cache', {})
        blocked_users = _get('blocked_users', set())
        get_request_counter = _get('get_request_counter', lambda: 0)

        # Hoisted out of the f-strings: Python 3.11 doesn't allow backslash
        # escapes inside f-string expression parts. PEP 701 (3.12+) lifted this.
        green = '\U0001f7e2'
        red = '\U0001f534'
        check_mark = '\u2705'
        cross_mark = '\u274c'
        admin_text = (
            f"\U0001f527 **Admin Statistics**\n\n"
            f"**Services:**\n"
            f"\u2022 Text AI: {green if text_ok else red} {text_status}\n"
            f"  Circuit: {text_ai_health.get_status() if text_ai_health else 'N/A'}\n"
            f"\u2022 Ollama: {green if ollama_ok else red} {ollama_status}\n"
            f"  Circuit: {ollama_health.get_status() if ollama_health else 'N/A'}\n"
            f"\u2022 ComfyUI: {green if comfyui_ok else red} {comfyui_status}\n"
            f"\u2022 TTS: {green if tts_ok else red} {tts_status}\n"
            f"  Circuit: {tts_health.get_status() if tts_health else 'N/A'}\n\n"
            f"**Uptime:** {get_uptime()}\n\n"
            f"**Message Stats:**\n"
            f"\u2022 Messages: {stats_snapshot.get('messages_processed', 0)}\n"
            f"\u2022 Text AI requests: {stats_snapshot.get('text_ai_requests', 0)}\n"
            f"\u2022 Text AI failures: {stats_snapshot.get('text_ai_failures', 0)}\n"
            f"\u2022 Text AI timeouts: {stats_snapshot.get('text_ai_timeouts', 0)}\n\n"
            f"**Image Stats:**\n"
            f"\u2022 Processed: {stats_snapshot.get('images_processed', 0)}\n"
            f"\u2022 Intimate: {stats_snapshot.get('intimate_images', 0)}\n"
            f"\u2022 Regular: {stats_snapshot.get('regular_images', 0)}\n"
            f"\u2022 Generated: {stats_snapshot.get('images_generated', 0)}\n"
            f"\u2022 Ollama failures: {stats_snapshot.get('ollama_failures', 0)}\n"
            f"\u2022 ComfyUI failures: {stats_snapshot.get('comfyui_failures', 0)}\n\n"
            f"**Voice Stats:**\n"
            f"\u2022 Voice messages: {stats_snapshot.get('voice_messages', 0)}\n"
            f"\u2022 TTS failures: {stats_snapshot.get('tts_failures', 0)}\n\n"
            f"**Pipeline Bridge:**\n"
            f"\u2022 Bridge active: {check_mark if pipeline_bridge_ready else cross_mark}\n"
            f"\u2022 Bridge chats: {len(pipeline_bridge_chat_ids)}\n"
            f"\u2022 Success: {stats_snapshot.get('pipeline_bridge_success', 0)}\n"
            f"\u2022 Fallback: {stats_snapshot.get('pipeline_bridge_fallback', 0)}\n"
            f"\u2022 Empty: {stats_snapshot.get('pipeline_bridge_empty', 0)}\n"
            f"\u2022 Error: {stats_snapshot.get('pipeline_bridge_error', 0)}\n\n"
            f"**Memory:**\n"
            f"\u2022 Active conversations: {len(conversations)}\n"
            f"\u2022 User info cached: {len(user_info)}\n"
            f"\u2022 Image cache entries: {len(image_analysis_cache)}\n"
            f"\u2022 Blocked users: {len(blocked_users)}\n"
            f"\u2022 Request counter: {get_request_counter()}\n"
        )

        await event.respond(admin_text)
        main_logger.info(f"Admin stats requested by {chat_id}")

    # ------------------------------------------------------------------
    # /refresh_videos
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/refresh_videos'))
    async def handle_refresh_videos(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        await event.respond("\U0001f504 Refreshing video file references...")
        refresh_video_cache = _get('refresh_video_cache')
        get_available_videos = _get('get_available_videos')
        count = await refresh_video_cache()
        await event.respond(f"\u2705 Refreshed {count}/{len(get_available_videos())} video references")

    # ------------------------------------------------------------------
    # /admin_block
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_block\s+(\d+)'))
    async def handle_admin_block(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        target_id = int(event.pattern_match.group(1))
        if target_id == config.ADMIN_USER_ID:
            await event.respond("\u274c Cannot block the admin user.")
            return
        blocked_users = _get('blocked_users', set())
        blocked_users.add(target_id)
        _get('save_blocked_users', lambda: None)()
        await event.respond(f"\u2705 User {target_id} has been blocked.")
        main_logger.warning(f"Admin blocked user {target_id}")

    # ------------------------------------------------------------------
    # /admin_unblock
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_unblock\s+(\d+)'))
    async def handle_admin_unblock(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        target_id = int(event.pattern_match.group(1))
        blocked_users = _get('blocked_users', set())
        if target_id in blocked_users:
            blocked_users.discard(target_id)
            _get('save_blocked_users', lambda: None)()
            await event.respond(f"\u2705 User {target_id} has been unblocked.")
            main_logger.info(f"Admin unblocked user {target_id}")
        else:
            await event.respond(f"\u2139\ufe0f User {target_id} was not blocked.")

    # ------------------------------------------------------------------
    # /admin_reset
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_reset\s+(\d+)'))
    async def handle_admin_reset(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        target_id = int(event.pattern_match.group(1))
        for key in ('conversations', 'recent_messages', 'user_modes',
                     'awaiting_image_description', 'conversation_turn_count',
                     'user_escalation_level', 'session_state', 'last_response_sent'):
            d = _get(key)
            if d is not None:
                d.pop(target_id, None)
        for key in ('voice_mode_users', 'manual_mode_chats'):
            s = _get(key)
            if s is not None:
                s.discard(target_id)
        await event.respond(f"\u2705 Reset all state for user {target_id}")
        main_logger.info(f"Admin reset state for user {target_id}")

    # ------------------------------------------------------------------
    # /admin_reload
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/admin_reload'))
    async def handle_admin_reload(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        personality = _get('personality')
        stats = _get('stats', {})
        success = personality.reload()
        stats['personality_reloads'] = stats.get('personality_reloads', 0) + 1
        if success:
            await event.respond(f"\u2705 Personality reloaded successfully.\nName: {personality.name}")
            main_logger.info("Admin reloaded personality")
        else:
            await event.respond("\u274c Failed to reload personality. Check logs.")
            main_logger.error("Admin personality reload failed")

    # ------------------------------------------------------------------
    # /stories
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern=r'/stories(\s+.*)?'))
    @client.on(events.NewMessage(incoming=True, pattern=r'/stories(\s+.*)?'))
    async def handle_stories_command(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        args = (event.pattern_match.group(1) or '').strip()
        load_story_bank = _get('load_story_bank')
        story_bank = _get('story_bank_ref', [])
        if args == 'reload':
            load_story_bank()
            story_bank = _get('story_bank_ref', [])
            await event.respond(f"\u2705 Story bank reloaded: {len(story_bank)} stories")
            main_logger.info(f"Admin reloaded story bank: {len(story_bank)} stories")
            return
        if not story_bank:
            await event.respond("\u2139\ufe0f No stories loaded. Check heather_stories.yaml")
            return
        lines = [f"\U0001f4d6 **Story Bank ({len(story_bank)} stories):**\n"]
        for s in story_bank:
            word_count = len(s['content'].split())
            kinks = ', '.join(s['kinks'])
            lines.append(f"\u2022 **{s['key']}** \u2014 {kinks} ({word_count} words)")
        lines.append(f"\n`/stories reload` \u2014 hot-reload from YAML")
        await event.respond("\n".join(lines))
        main_logger.info(f"Admin listed story bank ({len(story_bank)} stories)")

    # ------------------------------------------------------------------
    # /admin_blocked
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/admin_blocked'))
    async def handle_admin_blocked(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        blocked_users = _get('blocked_users', set())
        if not blocked_users:
            await event.respond("\u2139\ufe0f No users are currently blocked.")
        else:
            blocked_list = "\n".join([f"\u2022 {uid}" for uid in blocked_users])
            await event.respond(f"\U0001f6ab **Blocked Users:**\n{blocked_list}")

    # ------------------------------------------------------------------
    # /admin_flags
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_flags'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_flags'))
    async def handle_admin_flags(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        csam_flags = _get('csam_flags', [])
        pending = [f for f in csam_flags if f.get('status') == 'pending']
        if not pending:
            await event.respond("\u2705 No pending CSAM flags.")
            return
        lines = [f"\u26a0\ufe0f **Pending CSAM Flags ({len(pending)}):**\n"]
        for flag in pending[-10:]:
            ts = flag.get('timestamp', '?')[:16]
            lines.append(
                f"**#{flag['id']}** | {flag['display_name']} ({flag['user_id']})\n"
                f"  \U0001f4c5 {ts}\n"
                f"  \U0001f4ac {flag['message'][:100]}\n"
                f"  \U0001f50d Pattern: `{flag.get('matched_pattern', '?')[:60]}`\n"
            )
        lines.append(
            "/admin_flag_block <id> \u2014 block user\n"
            "/admin_flag_dismiss <id> \u2014 dismiss\n"
            "/admin_flag_clear \u2014 remove resolved"
        )
        await event.respond("\n".join(lines))

    # ------------------------------------------------------------------
    # /admin_flag_block
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern=r'/admin_flag_block\s+(\d+)'))
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_flag_block\s+(\d+)'))
    async def handle_admin_flag_block(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        flag_id = int(event.pattern_match.group(1))
        csam_flags = _get('csam_flags', [])
        flag = next((f for f in csam_flags if f['id'] == flag_id), None)
        if not flag:
            await event.respond(f"\u274c Flag #{flag_id} not found.")
            return
        if flag['status'] != 'pending':
            await event.respond(f"\u2139\ufe0f Flag #{flag_id} already resolved ({flag['status']}).")
            return
        target_id = flag['user_id']
        blocked_users = _get('blocked_users', set())
        blocked_users.add(target_id)
        _get('save_blocked_users', lambda: None)()
        flag['status'] = 'blocked'
        flag['resolved_at'] = datetime.now().isoformat()
        _get('save_csam_flags', lambda: None)()
        try:
            from telethon.tl.functions.contacts import BlockRequest
            await client(BlockRequest(id=target_id))
        except Exception:
            pass
        await event.respond(
            f"\U0001f6ab Flag #{flag_id}: Blocked user {flag['display_name']} ({target_id})."
        )
        main_logger.info(f"[CSAM-FLAG] Admin blocked user {target_id} from flag #{flag_id}")

    # ------------------------------------------------------------------
    # /admin_flag_dismiss
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern=r'/admin_flag_dismiss\s+(\d+)'))
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_flag_dismiss\s+(\d+)'))
    async def handle_admin_flag_dismiss(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        flag_id = int(event.pattern_match.group(1))
        csam_flags = _get('csam_flags', [])
        flag = next((f for f in csam_flags if f['id'] == flag_id), None)
        if not flag:
            await event.respond(f"\u274c Flag #{flag_id} not found.")
            return
        if flag['status'] != 'pending':
            await event.respond(f"\u2139\ufe0f Flag #{flag_id} already resolved ({flag['status']}).")
            return
        flag['status'] = 'dismissed'
        flag['resolved_at'] = datetime.now().isoformat()
        _get('save_csam_flags', lambda: None)()
        await event.respond(f"\u2705 Flag #{flag_id}: Dismissed (false positive).")
        main_logger.info(f"[CSAM-FLAG] Admin dismissed flag #{flag_id} (user {flag['user_id']})")

    # ------------------------------------------------------------------
    # /admin_flag_clear
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_flag_clear'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_flag_clear'))
    async def handle_admin_flag_clear(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        csam_flags = _get('csam_flags', [])
        before = len(csam_flags)
        csam_flags[:] = [f for f in csam_flags if f.get('status') == 'pending']
        _get('save_csam_flags', lambda: None)()
        removed = before - len(csam_flags)
        await event.respond(f"\U0001f5d1\ufe0f Cleared {removed} resolved flags. {len(csam_flags)} pending remain.")

    # ------------------------------------------------------------------
    # /admin_reengage_scan
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_reengage_scan'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_reengage_scan'))
    async def handle_admin_reengage_scan(event):
        """Dry-run re-engagement scan."""
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return

        await event.respond("\U0001f50d Running re-engagement dry-run scan...")
        try:
            load_history = _get('load_reengagement_history', lambda: {})
            history = load_history()
            blocked_users = _get('blocked_users', set())

            candidates = []
            now = datetime.now()
            me = await client.get_me()
            my_id = me.id

            async for dialog in client.iter_dialogs():
                try:
                    if not dialog.is_user:
                        continue
                    entity = dialog.entity
                    if getattr(entity, 'bot', False) or entity.id == my_id:
                        continue
                    if entity.id in blocked_users:
                        continue
                    if getattr(entity, 'deleted', False):
                        continue
                    if not dialog.message or not dialog.message.date:
                        continue

                    last_msg_date = dialog.message.date.replace(tzinfo=None)
                    idle_days = (now - last_msg_date).total_seconds() / 86400

                    if idle_days < config.REENGAGEMENT_MIN_IDLE_DAYS or idle_days > config.REENGAGEMENT_MAX_IDLE_DAYS:
                        continue

                    skip_reason = None
                    if dialog.message.out:
                        skip_reason = "last msg is ours"

                    chat_id_str = str(entity.id)
                    if not skip_reason and chat_id_str in history:
                        h = history[chat_id_str]
                        if h.get('ping_count', 0) > 0 and not h.get('last_ping_responded', True):
                            skip_reason = "non-responder"
                        last_ping = h.get('last_ping_at', '')
                        if not skip_reason and last_ping:
                            try:
                                days_since = (now - datetime.fromisoformat(last_ping)).total_seconds() / 86400
                                if days_since < config.REENGAGEMENT_COOLDOWN_DAYS:
                                    skip_reason = f"cooldown ({days_since:.1f}d ago)"
                            except (ValueError, TypeError):
                                pass

                    messages = await client.get_messages(entity.id, limit=20)
                    msg_count = len(messages)

                    if not skip_reason and msg_count < config.REENGAGEMENT_MIN_MESSAGES:
                        skip_reason = f"only {msg_count} msgs"

                    display_name = entity.first_name or entity.username or str(entity.id)

                    recent_msgs = []
                    for msg in reversed(messages[:10]):
                        if msg.text:
                            sender = "Heather" if msg.out else (entity.first_name or "User")
                            recent_msgs.append({'sender': sender, 'text': msg.text[:200]})

                    if not skip_reason and len(recent_msgs) < 3:
                        skip_reason = "too few text msgs"

                    candidates.append({
                        'chat_id': entity.id,
                        'username': entity.username or "",
                        'display_name': display_name,
                        'idle_days': idle_days,
                        'message_count': msg_count,
                        'recent_messages': recent_msgs,
                        'skip_reason': skip_reason,
                    })
                except Exception:
                    continue

            today_str = now.strftime('%Y-%m-%d')
            sent_today = sum(1 for h in history.values() if h.get('last_ping_at', '')[:10] == today_str)

            lines = [f"\U0001f4ca **Re-engagement Scan Results**\n"]
            lines.append(f"Sent today: {sent_today}/{config.REENGAGEMENT_MAX_PER_DAY}")
            lines.append(f"History entries: {len(history)}")
            lines.append(f"Hour: {now.hour} (active: {config.REENGAGEMENT_HOUR_START}-{config.REENGAGEMENT_HOUR_END})\n")

            eligible = [c for c in candidates if not c.get('skip_reason')]
            skipped = [c for c in candidates if c.get('skip_reason')]

            lines.append(f"**Eligible: {len(eligible)}**")
            for c in eligible[:15]:
                lines.append(f"  \u2705 {c['display_name']} \u2014 {c['idle_days']:.1f}d idle, {c['message_count']} msgs")

            if skipped:
                lines.append(f"\n**Skipped: {len(skipped)}**")
                for c in skipped[:10]:
                    lines.append(f"  \u274c {c['display_name']} \u2014 {c['idle_days']:.1f}d idle \u2014 {c['skip_reason']}")

            if eligible:
                lines.append(f"\n**Sample message for {eligible[0]['display_name']}:**")
                try:
                    loop = asyncio.get_running_loop()
                    sample_msg = await loop.run_in_executor(
                        None, lambda: generate_reengage_preview(eligible[0])
                    )
                    lines.append(f"  \U0001f4ac {sample_msg or '(generation failed)'}")
                except Exception as e:
                    lines.append(f"  \u26a0\ufe0f Generation error: {e}")

            await event.respond("\n".join(lines))
        except Exception as e:
            await event.respond(f"\u274c Scan failed: {e}")
            main_logger.error(f"[REENGAGEMENT] Admin scan failed: {e}")

    # ------------------------------------------------------------------
    # /admin_reengage_send
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern=r'/admin_reengage_send\s+(\d+)'))
    @client.on(events.NewMessage(incoming=True, pattern=r'/admin_reengage_send\s+(\d+)'))
    async def handle_admin_reengage_send(event):
        """Manually send a re-engagement message."""
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return

        target_id = int(event.pattern_match.group(1))
        await event.respond(f"\U0001f4e4 Generating re-engagement message for {target_id}...")

        try:
            messages = await client.get_messages(target_id, limit=20)
            entity = await client.get_entity(target_id)
            display_name = entity.first_name or entity.username or str(target_id)
            username = entity.username or ""

            if not messages:
                await event.respond("\u274c No message history found for this user.")
                return

            last_msg_date = messages[0].date.replace(tzinfo=None)
            idle_days = (datetime.now() - last_msg_date).total_seconds() / 86400

            recent_msgs = []
            for msg in reversed(messages[:10]):
                if msg.text:
                    sender = "Heather" if msg.out else (entity.first_name or "User")
                    recent_msgs.append({'sender': sender, 'text': msg.text[:200]})

            candidate = {
                'chat_id': target_id,
                'username': username,
                'display_name': display_name,
                'idle_days': idle_days,
                'recent_messages': recent_msgs,
            }

            loop = asyncio.get_running_loop()
            message = await loop.run_in_executor(None, generate_reengage_preview, candidate)

            if not message:
                await event.respond("\u274c Failed to generate message.")
                return

            await event.respond(f"\U0001f4ac Sending to **{display_name}** ({idle_days:.1f}d idle):\n\n{message}")

            try:
                async with client.action(entity, 'typing'):
                    await asyncio.sleep(random.uniform(2.0, 4.0))
            except Exception:
                await asyncio.sleep(2.0)

            await client.send_message(target_id, message)

            load_history = _get('load_reengagement_history', lambda: {})
            save_history = _get('save_reengagement_history', lambda h: None)
            history = load_history()
            cid_str = str(target_id)
            prev = history.get(cid_str, {})
            history[cid_str] = {
                'username': username,
                'display_name': display_name,
                'last_ping_at': datetime.now().isoformat(),
                'ping_count': prev.get('ping_count', 0) + 1,
                'last_ping_responded': False,
            }
            save_history(history)

            await event.respond(f"\u2705 Sent to {display_name}!")
            main_logger.info(f"[REENGAGEMENT] Admin manually sent to {display_name} ({target_id})")

        except Exception as e:
            await event.respond(f"\u274c Failed: {e}")
            main_logger.error(f"[REENGAGEMENT] Admin send failed for {target_id}: {e}")

    # ------------------------------------------------------------------
    # /admin_reengage_history
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_reengage_history'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_reengage_history'))
    async def handle_admin_reengage_history(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        load_history = _get('load_reengagement_history', lambda: {})
        history = load_history()
        if not history:
            await event.respond("\U0001f4cb Re-engagement history is empty.")
            return
        lines = [f"\U0001f4cb **Re-engagement History** ({len(history)} entries)\n"]
        for cid, h in sorted(history.items(), key=lambda x: x[1].get('last_ping_at', ''), reverse=True):
            responded = "\u2705" if h.get('last_ping_responded') else "\u274c"
            name = h.get('display_name', cid)
            pings = h.get('ping_count', 0)
            last = h.get('last_ping_at', 'never')[:16]
            lines.append(f"  {responded} **{name}** ({cid}) \u2014 {pings} pings, last: {last}")
        await event.respond("\n".join(lines))

    # ------------------------------------------------------------------
    # /testtip
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/testtip'))
    @client.on(events.NewMessage(incoming=True, pattern='/testtip'))
    async def handle_testtip(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        send_invoice = _get('send_stars_invoice')
        result = await send_invoice(chat_id)
        if result:
            await event.respond(f"Invoice sent! Check your chat with @{config.PAYMENT_BOT_USERNAME}")
        else:
            await event.respond("Failed to send invoice \u2014 check logs")

    # ------------------------------------------------------------------
    # /admin_warmth
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_warmth'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_warmth'))
    async def handle_admin_warmth(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        tipper_status = _get('tipper_status', {})
        get_access_tier = _get('get_access_tier', lambda uid: 'FREE')

        warm_users = []
        new_users = []
        cold_users = []

        for uid, ts in tipper_status.items():
            warmth = ts.get('warmth', config.WARMTH_INITIAL)
            tier = "WARM" if warmth >= config.WARMTH_WARM_THRESHOLD else ("COLD" if warmth < config.WARMTH_COLD_THRESHOLD else "NEW")
            stars = ts.get('total_stars', 0)
            msgs = ts.get('total_messages', 0)
            name = ts.get('name') or str(uid)
            declined = ts.get('declined', False)

            access = get_access_tier(uid)
            status_str = f"declined" if declined else f"{stars}\u2b50"
            entry = f"  {name} ({uid}) w={warmth:.2f}, {access}, {status_str}, {msgs}msgs"

            if tier == "WARM":
                warm_users.append(entry)
            elif tier == "COLD":
                cold_users.append(entry)
            else:
                new_users.append(entry)

        lines = [f"\U0001f321\ufe0f **Warmth Tiers** ({len(tipper_status)} users)\n"]
        lines.append(f"**WARM** ({len(warm_users)}):")
        lines.extend(warm_users[:15] if warm_users else ["  (none)"])
        if len(warm_users) > 15:
            lines.append(f"  ...and {len(warm_users) - 15} more")
        lines.append(f"\n**NEW** ({len(new_users)}):")
        lines.extend(new_users[:15] if new_users else ["  (none)"])
        if len(new_users) > 15:
            lines.append(f"  ...and {len(new_users) - 15} more")
        lines.append(f"\n**COLD** ({len(cold_users)}):")
        lines.extend(cold_users[:15] if cold_users else ["  (none)"])
        if len(cold_users) > 15:
            lines.append(f"  ...and {len(cold_users) - 15} more")

        await event.respond("\n".join(lines))

    # ------------------------------------------------------------------
    # /admin_opportunities
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/admin_opportunities'))
    @client.on(events.NewMessage(incoming=True, pattern='/admin_opportunities'))
    async def handle_admin_opportunities(event):
        chat_id = event.chat_id
        if not event.out and not is_admin(chat_id):
            return
        now = time.time()
        takeover_opportunities = _get('takeover_opportunities', {})
        recent = {uid: opp for uid, opp in takeover_opportunities.items()
                  if now - opp.get('detected_at', 0) < 14400}
        if not recent:
            await event.respond("\U0001f3af No active takeover opportunities.")
            return
        lines = [f"\U0001f3af **Active Opportunities** ({len(recent)})\n"]
        for uid, opp in sorted(recent.items(), key=lambda x: x[1].get('detected_at', 0), reverse=True):
            age_mins = int((now - opp['detected_at']) / 60)
            lines.append(
                f"  **{opp['display_name']}** ({uid})\n"
                f"    Signal: {opp['signal']}\n"
                f"    Session: {opp['session_msgs']} msgs, warmth={opp['warmth']:.2f}, {age_mins}min ago\n"
                f"    `/takeover {uid}`"
            )
        await event.respond("\n".join(lines))

    # ------------------------------------------------------------------
    # /bridge_add, /bridge_remove, /bridge_status
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern=r'/bridge_add\s+(\d+)'))
    @client.on(events.NewMessage(outgoing=True, pattern=r'/bridge_add\s+(\d+)'))
    async def handle_bridge_add(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        target_id = int(event.pattern_match.group(1))
        pipeline_bridge_chat_ids = _get('pipeline_bridge_chat_ids', set())
        pipeline_bridge_chat_ids.add(target_id)
        main_logger.info(f"[PIPELINE_BRIDGE] Added {target_id} to bridge set (now {len(pipeline_bridge_chat_ids)} chats)")
        await event.respond(f"\u2705 Added {target_id} to pipeline bridge ({len(pipeline_bridge_chat_ids)} chats total)")

    @client.on(events.NewMessage(incoming=True, pattern=r'/bridge_remove\s+(\d+)'))
    @client.on(events.NewMessage(outgoing=True, pattern=r'/bridge_remove\s+(\d+)'))
    async def handle_bridge_remove(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        target_id = int(event.pattern_match.group(1))
        pipeline_bridge_chat_ids = _get('pipeline_bridge_chat_ids', set())
        pipeline_bridge_chat_ids.discard(target_id)
        main_logger.info(f"[PIPELINE_BRIDGE] Removed {target_id} from bridge set (now {len(pipeline_bridge_chat_ids)} chats)")
        await event.respond(f"\u2705 Removed {target_id} from pipeline bridge ({len(pipeline_bridge_chat_ids)} chats total)")

    @client.on(events.NewMessage(incoming=True, pattern='/bridge_status'))
    @client.on(events.NewMessage(outgoing=True, pattern='/bridge_status'))
    async def handle_bridge_status(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        get_stats_snapshot = _get('get_stats_snapshot', lambda: {})
        snap = get_stats_snapshot()
        pipeline_bridge_ready = _get('pipeline_bridge_ready', False)
        pipeline_bridge_chat_ids = _get('pipeline_bridge_chat_ids', set())
        total = snap.get('pipeline_bridge_success', 0) + snap.get('pipeline_bridge_fallback', 0)
        success_rate = round(snap.get('pipeline_bridge_success', 0) / total * 100, 1) if total > 0 else 0
        check_mark = '\u2705'
        cross_mark = '\u274c'
        status = (
            f"**Pipeline Bridge Status**\n\n"
            f"Active: {check_mark if pipeline_bridge_ready else cross_mark}\n"
            f"Bridged chats: {len(pipeline_bridge_chat_ids)}\n"
            f"Chat IDs: {', '.join(str(c) for c in sorted(pipeline_bridge_chat_ids)) or 'none'}\n\n"
            f"**Metrics:**\n"
            f"Success: {snap.get('pipeline_bridge_success', 0)}\n"
            f"Fallback: {snap.get('pipeline_bridge_fallback', 0)} (= empty + error)\n"
            f"  Empty: {snap.get('pipeline_bridge_empty', 0)}\n"
            f"  Error: {snap.get('pipeline_bridge_error', 0)}\n"
            f"Success rate: {success_rate}% ({total} total)\n"
        )
        await event.respond(status)

    # ------------------------------------------------------------------
    # /admin_help
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/admin_help'))
    async def handle_admin_help(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        help_text = (
            "\U0001f527 **Admin Commands**\n\n"
            "/admin_stats - Detailed system statistics\n"
            "/admin_block <user_id> - Block a user\n"
            "/admin_unblock <user_id> - Unblock a user\n"
            "/admin_blocked - List blocked users\n"
            "/admin_flags - Review CSAM flags\n"
            "/admin_flag_block <id> - Block user from flag\n"
            "/admin_flag_dismiss <id> - Dismiss flag (false positive)\n"
            "/admin_flag_clear - Remove resolved flags\n"
            "/admin_reset <user_id> - Reset user's state\n"
            "/admin_reload - Hot-reload personality file\n"
            "/admin_reengage_scan - Dry-run re-engagement scan\n"
            "/admin_reengage_send <id> - Send re-engagement to user\n"
            "/admin_reengage_history - Show re-engagement history\n"
            "/admin_warmth - Show user warmth tiers\n"
            "/admin_opportunities - Takeover opportunities\n"
            "/library_status - Image library stats\n"
            "/testtip - Send test Stars invoice to yourself\n"
            "/admin_catchup - Startup catch-up status\n"
            "/bridge_status - Pipeline bridge status & metrics\n"
            "/bridge_add <id> - Add chat to pipeline bridge\n"
            "/bridge_remove <id> - Remove chat from bridge\n"
            "/admin_help - This help message\n"
        )
        await event.respond(help_text)

    # ------------------------------------------------------------------
    # /admin_catchup
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True, pattern='/admin_catchup'))
    async def handle_admin_catchup(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        load_shutdown = _get('load_shutdown_timestamp', lambda: None)
        shutdown_ts = load_shutdown()
        now = time.time()

        lines = ["**Startup Catch-Up Status**\n"]
        lines.append(f"Enabled: {'Yes' if config.CATCHUP_ENABLED else 'No'}")

        if shutdown_ts:
            age = now - shutdown_ts
            age_str = f"{age / 3600:.1f}h" if age > 3600 else f"{age / 60:.0f}m"
            lines.append(f"Last timestamp: {datetime.fromtimestamp(shutdown_ts).strftime('%Y-%m-%d %H:%M:%S')} ({age_str} ago)")
        else:
            lines.append("Last timestamp: None (no file)")

        lines.append(f"\nConfig:")
        lines.append(f"  Max age: {config.CATCHUP_MAX_AGE_HOURS}h")
        lines.append(f"  Min downtime: {config.CATCHUP_MIN_DOWNTIME_SECONDS}s")
        lines.append(f"  Max replies: {config.CATCHUP_MAX_REPLIES}")
        lines.append(f"  Delay: {config.CATCHUP_DELAY_MIN}-{config.CATCHUP_DELAY_MAX}s between replies")

        await event.respond("\n".join(lines))

    # ------------------------------------------------------------------
    # /library_status
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(outgoing=True, pattern='/library_status'))
    @client.on(events.NewMessage(incoming=True, pattern='/library_status'))
    async def handle_library_status(event):
        chat_id = event.chat_id
        if not is_admin(chat_id):
            return
        image_library = _get('image_library', [])
        if not image_library:
            await event.respond("Image Library: empty (no library.json or no images)")
            return
        cats: Dict[str, int] = {}
        real_count = 0
        for img in image_library:
            cat = img['category']
            cats[cat] = cats.get(cat, 0) + 1
            if img.get('is_real'):
                real_count += 1
        lines = [f"Image Library: {len(image_library)} images loaded\n"]
        for cat in ["sfw_casual", "sfw_flirty", "sfw_lingerie",
                     "nsfw_topless", "nsfw_nude", "nsfw_explicit"]:
            count = cats.get(cat, 0)
            lines.append(f"  {cat}: {count}")
        real_cats = [c for c in cats if c.startswith("real_")]
        for cat in sorted(real_cats):
            lines.append(f"  {cat}: {cats[cat]}")
        if real_count:
            lines.append(f"\n  Total real photos: {real_count}")
        await event.respond("\n".join(lines))

    main_logger.info(f"[ADMIN] Registered admin command handlers")

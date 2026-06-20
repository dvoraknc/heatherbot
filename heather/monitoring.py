"""
heather.monitoring — Web Dashboard & Monitoring
=================================================
Flask monitoring dashboard with /health, /stats, /flags, /tips routes.
Data sources injected via setup() — no direct imports from monolith.

Replaces: heather_telegram_bot.py
  - monitor_app creation: line 10237
  - check_dashboard_auth: lines 10241-10249
  - monitor_home (/): lines 10251-10371
  - health_check (/health): lines 10373-10382
  - api_stats (/stats): lines 10384-10482
  - monitor_flags (/flags): lines 10484-10547
  - monitor_tips (/tips): lines 10549-10714
  - stats_api (/stats duplicate): lines 10716-10788
  - run_monitoring: lines 10790-10793

Dependencies: heather.config, heather.logging_setup
Used by: heather_telegram_bot.py (monitoring thread)
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

from flask import Flask, jsonify, render_template_string, request as flask_request
import requests

from heather import config
from heather.logging_setup import main_logger


# ============================================================================
# DATA SOURCE REGISTRY
# ============================================================================

_src: Dict[str, Any] = {}


def setup(**sources):
    """Inject data sources from the orchestrator.

    Expected keys (dicts/sets — passed by reference, mutations shared):
        stats, recent_messages, tipper_status, csam_flags,
        payment_bot_started_users, manual_mode_chats, voice_mode_users,
        conversation_turn_count, videos_sent_to_user, user_last_message,
        conversations, reply_in_progress, recent_logs, ai_disclosure_shown

    Expected keys (callables):
        check_text_ai_status, check_ollama_status, check_comfyui_status,
        check_tts_status, get_user_display_name, get_uptime, get_warmth_tier,
        get_stats_snapshot

    Expected keys (circuit breaker objects):
        text_ai_health, ollama_health, tts_health

    Expected keys (scalars/flags):
        pipeline_bridge_ready, pipeline_bridge_chat_ids
    """
    _src.update(sources)
    main_logger.info(f"[MONITORING] Setup with {len(sources)} data sources")


def _get(key: str, default=None):
    """Get a data source, with fallback."""
    return _src.get(key, default)


# ============================================================================
# FLASK APP FACTORY
# ============================================================================

def create_app() -> Flask:
    """Create and return the Flask monitoring app with all routes."""
    app = Flask(__name__)

    auth_token = os.getenv("MONITOR_AUTH_TOKEN", os.getenv("HEATHER_DASHBOARD_KEY", ""))

    # ------------------------------------------------------------------
    # AUTH MIDDLEWARE
    # ------------------------------------------------------------------

    @app.before_request
    def check_dashboard_auth():
        if flask_request.path == '/health':
            return None
        if not auth_token:
            return None
        token = flask_request.args.get('token') or flask_request.headers.get('X-Auth-Token', '')
        if token != auth_token:
            return "Unauthorized", 401

    # ------------------------------------------------------------------
    # HOME DASHBOARD (/)
    # ------------------------------------------------------------------

    @app.route('/')
    def monitor_home():
        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))

        text_ok, text_status = check_text_ai()
        ollama_ok, ollama_status = check_ollama()
        comfyui_ok, comfyui_status = check_comfyui()
        tts_ok, tts_status = check_tts()

        recent_messages = _get('recent_messages', {})
        get_display_name = _get('get_user_display_name', lambda cid: str(cid))

        chat_list = []
        for cid, msgs in recent_messages.items():
            display_name = get_display_name(cid)
            chat_list.append((cid, display_name, list(msgs)[-10:]))

        stats = _get('stats', {})
        get_uptime = _get('get_uptime', lambda: '0:00:00')
        manual_mode_chats = _get('manual_mode_chats', set())
        text_ai_health = _get('text_ai_health')
        ollama_health = _get('ollama_health')
        tts_health = _get('tts_health')
        csam_flags = _get('csam_flags', [])
        tipper_status = _get('tipper_status', {})
        payment_bot_started_users = _get('payment_bot_started_users', set())

        return render_template_string(HOME_TEMPLATE,
            stats=stats,
            uptime=get_uptime(),
            active_chats=len(recent_messages),
            chat_list=chat_list,
            manual_mode_chats=manual_mode_chats,
            text_ok=text_ok, text_status=text_status,
            ollama_ok=ollama_ok, ollama_status=ollama_status,
            comfyui_ok=comfyui_ok, comfyui_status=comfyui_status,
            tts_ok=tts_ok, tts_status=tts_status,
            text_ai_circuit=text_ai_health.get_status() if text_ai_health and text_ai_health.circuit_open else None,
            ollama_circuit=ollama_health.get_status() if ollama_health and ollama_health.circuit_open else None,
            tts_circuit=tts_health.get_status() if tts_health and tts_health.circuit_open else None,
            pending_flags=sum(1 for f in csam_flags if f.get('status') == 'pending'),
            total_flags=len(csam_flags),
            total_stars=sum(t.get('total_stars', 0) for t in tipper_status.values()),
            num_tippers=len(tipper_status),
            started_users=len(payment_bot_started_users),
            error_rate=(stats.get('text_ai_failures', 0) + stats.get('text_ai_timeouts', 0)) / max(stats.get('text_ai_requests', 1), 1) * 100,
        )

    # ------------------------------------------------------------------
    # HEALTH CHECK (/health)
    # ------------------------------------------------------------------

    @app.route('/health')
    def health_check():
        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))

        text_ok, _ = check_text_ai()
        ollama_ok, _ = check_ollama()
        comfyui_ok, _ = check_comfyui()
        tts_ok, _ = check_tts()
        get_uptime = _get('get_uptime', lambda: '0:00:00')
        return jsonify({
            'status': 'healthy' if text_ok else 'degraded',
            'uptime': get_uptime(),
        })

    # ------------------------------------------------------------------
    # STATS API (/stats)
    # ------------------------------------------------------------------

    @app.route('/stats')
    def api_stats():
        """JSON API endpoint for user metrics, engagement, and system stats."""
        get_stats_snapshot = _get('get_stats_snapshot', lambda: dict(_get('stats', {})))
        snap = get_stats_snapshot()
        get_uptime = _get('get_uptime', lambda: '0:00:00')
        uptime_str = get_uptime()

        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))
        check_tts = _get('check_tts_status', lambda: (False, 'N/A'))

        text_ok, text_status = check_text_ai()
        ollama_ok, ollama_status = check_ollama()
        comfyui_ok, comfyui_status = check_comfyui()
        tts_ok, tts_status = check_tts()

        recent_messages = _get('recent_messages', {})
        tipper_status = _get('tipper_status', {})
        conversation_turn_count = _get('conversation_turn_count', {})
        get_display_name = _get('get_user_display_name', lambda cid: str(cid))
        get_warmth_tier = _get('get_warmth_tier', lambda cid: 'NEW')
        videos_sent_to_user = _get('videos_sent_to_user', {})
        manual_mode_chats = _get('manual_mode_chats', set())
        voice_mode_users = _get('voice_mode_users', set())
        payment_bot_started_users = _get('payment_bot_started_users', set())
        pipeline_bridge_ready = _get('pipeline_bridge_ready', False)
        pipeline_bridge_chat_ids = _get('pipeline_bridge_chat_ids', set())

        # Per-user engagement metrics
        users = []
        for cid, msgs in recent_messages.items():
            msg_list = list(msgs)
            user_msgs = [m for m in msg_list if m.get('sender') == 'user']
            heather_msgs = [m for m in msg_list if m.get('sender') != 'user']
            ts = tipper_status.get(cid, {})
            users.append({
                'chat_id': cid,
                'display_name': get_display_name(cid),
                'recent_messages': len(msg_list),
                'user_messages': len(user_msgs),
                'heather_messages': len(heather_msgs),
                'turn_count': conversation_turn_count.get(cid, 0),
                'warmth_tier': get_warmth_tier(cid),
                'warmth_score': round(ts.get('warmth', config.WARMTH_INITIAL), 3),
                'total_stars': ts.get('total_stars', 0),
                'total_tips': ts.get('total_tips', 0),
                'tip_tier': ts.get('tier', 0),
                'total_messages': ts.get('total_messages', 0),
                'videos_sent': len(videos_sent_to_user.get(cid, set())),
                'in_manual_mode': cid in manual_mode_chats,
                'voice_enabled': cid in voice_mode_users,
            })
        users.sort(key=lambda u: u['total_messages'], reverse=True)

        # Warmth tier distribution
        tier_counts = {'WARM': 0, 'NEW': 0, 'COLD': 0}
        for cid in recent_messages:
            tier_counts[get_warmth_tier(cid)] += 1

        # Revenue summary
        total_stars = sum(t.get('total_stars', 0) for t in tipper_status.values())
        total_tips = sum(t.get('total_tips', 0) for t in tipper_status.values())
        paying_users = sum(1 for t in tipper_status.values() if t.get('total_tips', 0) > 0)

        # Error rate
        total_reqs = snap.get('text_ai_requests', 0)
        total_failures = snap.get('text_ai_failures', 0) + snap.get('text_ai_timeouts', 0)
        error_rate = round(total_failures / total_reqs * 100, 2) if total_reqs > 0 else 0.0

        return jsonify({
            'uptime': uptime_str,
            'start_time': snap.get('start_time'),
            'system': {
                'messages_processed': snap.get('messages_processed', 0),
                'images_processed': snap.get('images_processed', 0),
                'images_generated': snap.get('images_generated', 0),
                'voice_messages': snap.get('voice_messages', 0),
                'stories_played': snap.get('stories_played', 0),
                'errors': snap.get('errors', 0),
                'text_ai_requests': total_reqs,
                'text_ai_failures': snap.get('text_ai_failures', 0),
                'text_ai_timeouts': snap.get('text_ai_timeouts', 0),
                'error_rate_pct': error_rate,
                'personality_reloads': snap.get('personality_reloads', 0),
            },
            'pipeline_bridge': {
                'active': pipeline_bridge_ready,
                'bridge_chats': len(pipeline_bridge_chat_ids),
                'success': snap.get('pipeline_bridge_success', 0),
                'fallback': snap.get('pipeline_bridge_fallback', 0),
                'empty': snap.get('pipeline_bridge_empty', 0),
                'error': snap.get('pipeline_bridge_error', 0),
            },
            'services': {
                'text_ai': {'ok': text_ok, 'status': text_status},
                'ollama': {'ok': ollama_ok, 'status': ollama_status},
                'comfyui': {'ok': comfyui_ok, 'status': comfyui_status},
                'tts': {'ok': tts_ok, 'status': tts_status},
            },
            'engagement': {
                'active_chats': len(recent_messages),
                'manual_mode_chats': len(manual_mode_chats),
                'voice_enabled_users': len(voice_mode_users),
                'warmth_distribution': tier_counts,
            },
            'revenue': {
                'total_stars': total_stars,
                'estimated_usd': round(total_stars * 0.013, 2),
                'total_tips': total_tips,
                'paying_users': paying_users,
                'payment_bot_started': len(payment_bot_started_users),
                'tip_hooks_fired': snap.get('tip_hooks_fired', 0),
            },
            'users': users,
        })

    # ------------------------------------------------------------------
    # CSAM FLAGS (/flags)
    # ------------------------------------------------------------------

    @app.route('/flags')
    def monitor_flags():
        csam_flags = _get('csam_flags', [])
        pending = [f for f in csam_flags if f.get('status') == 'pending']
        resolved = [f for f in csam_flags if f.get('status') != 'pending']
        return render_template_string(FLAGS_TEMPLATE, pending=pending, resolved=resolved)

    # ------------------------------------------------------------------
    # TIPS DASHBOARD (/tips)
    # ------------------------------------------------------------------

    @app.route('/tips')
    def monitor_tips():
        tipper_status = _get('tipper_status', {})
        get_display_name = _get('get_user_display_name', lambda cid: str(cid))
        payment_bot_started_users = _get('payment_bot_started_users', set())
        stats = _get('stats', {})

        # Fetch Star balance from Bot API
        star_transactions = []
        if config.PAYMENT_BOT_TOKEN:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{config.PAYMENT_BOT_TOKEN}/getStarTransactions",
                    params={"limit": 20}, timeout=10,
                )
                data = r.json()
                if data.get("ok"):
                    txns = data.get("result", {}).get("transactions", [])
                    star_transactions = txns
            except Exception:
                pass

        # Build tipper list sorted by total_stars descending
        tippers = []
        for cid, ts in sorted(tipper_status.items(), key=lambda x: x[1].get('total_stars', 0), reverse=True):
            tippers.append({
                'chat_id': cid,
                'name': ts.get('name') or get_display_name(cid),
                'total_stars': ts.get('total_stars', 0),
                'total_tips': ts.get('total_tips', 0),
                'tier': ts.get('tier', 0),
                'last_tip': ts.get('last_tip_at', 0),
                'last_hook': ts.get('last_hook_type', ''),
            })

        tier_labels = {0: 'None', 1: 'Coffee', 2: 'Regular', 3: 'Big Tipper'}
        total_stars_all = sum(t.get('total_stars', 0) for t in tipper_status.values())
        total_tips_all = sum(t.get('total_tips', 0) for t in tipper_status.values())

        # Funnel metrics
        hooks_mentioned = sum(1 for ts in tipper_status.values() if ts.get('last_tip_mention_at', 0) > 0)
        hooks_this_session = stats.get('tip_hooks_fired', 0)
        funnel_hooks = max(hooks_mentioned, hooks_this_session)
        funnel_started = len(payment_bot_started_users)
        funnel_paid = sum(1 for ts in tipper_status.values() if ts.get('total_tips', 0) > 0)
        funnel_ignored = max(0, funnel_hooks - funnel_started)
        funnel_abandoned = max(0, funnel_started - funnel_paid)
        pct_started = f"{funnel_started / funnel_hooks * 100:.0f}%" if funnel_hooks > 0 else "-"
        pct_paid = f"{funnel_paid / funnel_started * 100:.0f}%" if funnel_started > 0 else "-"
        pct_conversion = f"{funnel_paid / funnel_hooks * 100:.1f}%" if funnel_hooks > 0 else "-"

        return render_template_string(TIPS_TEMPLATE,
            tippers=tippers,
            tier_labels=tier_labels,
            total_stars=total_stars_all,
            total_tips=total_tips_all,
            started_users=len(payment_bot_started_users),
            star_transactions=star_transactions,
            payment_bot_token=bool(config.PAYMENT_BOT_TOKEN),
            funnel_hooks=funnel_hooks,
            hooks_this_session=hooks_this_session,
            funnel_started=funnel_started,
            funnel_paid=funnel_paid,
            funnel_ignored=funnel_ignored,
            funnel_abandoned=funnel_abandoned,
            pct_started=pct_started,
            pct_paid=pct_paid,
            pct_conversion=pct_conversion,
        )

    # ------------------------------------------------------------------
    # STATS V2 (/stats/v2) — extended system metrics
    # ------------------------------------------------------------------
    # NOTE: In the monolith this was a duplicate /stats route that overwrote
    # api_stats(). Moved to /stats/v2 to preserve both endpoints.

    @app.route('/stats/v2')
    def stats_v2():
        """Extended stats with psutil memory, user activity windows, error counts."""
        user_last_message = _get('user_last_message', {})
        conversations = _get('conversations', {})
        reply_in_progress = _get('reply_in_progress', {})
        recent_logs = _get('recent_logs', [])
        ai_disclosure_shown = _get('ai_disclosure_shown', set())
        tipper_status = _get('tipper_status', {})
        payment_bot_started_users = _get('payment_bot_started_users', set())
        stats = _get('stats', {})
        get_uptime = _get('get_uptime', lambda: '0:00:00')

        check_text_ai = _get('check_text_ai_status', lambda: (False, 'N/A'))
        check_ollama = _get('check_ollama_status', lambda: (False, 'N/A'))
        check_comfyui = _get('check_comfyui_status', lambda: (False, 'N/A'))

        now = time.time()
        unique_users_24h = len(set(
            chat_id for chat_id, last_msg_time in user_last_message.items()
            if now - last_msg_time < 86400
        ))
        unique_users_1h = len(set(
            chat_id for chat_id, last_msg_time in user_last_message.items()
            if now - last_msg_time < 3600
        ))

        total_conversations = len(conversations)
        active_conversations = len(reply_in_progress)

        text_ai_ok, text_ai_status = check_text_ai()
        ollama_ok, ollama_status = check_ollama()
        comfyui_ok, comfyui_status = check_comfyui()

        recent_errors = sum(1 for entry in recent_logs if 'ERROR' in str(entry) or 'WARNING' in str(entry))

        # Memory and performance
        try:
            import psutil
            memory_usage = psutil.virtual_memory().percent
        except ImportError:
            memory_usage = 0.0

        total_users = len(ai_disclosure_shown)
        tipping_users = len(payment_bot_started_users)
        tip_conversion_rate = (len(tipper_status) / max(total_users, 1)) * 100

        return jsonify({
            'timestamp': now,
            'bot_status': 'running',
            'uptime_seconds': get_uptime(),
            'users': {
                'total_users': total_users,
                'active_1h': unique_users_1h,
                'active_24h': unique_users_24h,
                'conversations_total': total_conversations,
                'conversations_active': active_conversations,
                'tipping_users': tipping_users,
                'tip_conversion_rate': round(tip_conversion_rate, 1)
            },
            'services': {
                'text_ai': {'status': 'online' if text_ai_ok else 'offline', 'details': text_ai_status},
                'ollama': {'status': 'online' if ollama_ok else 'offline', 'details': ollama_status},
                'comfyui': {'status': 'online' if comfyui_ok else 'offline', 'details': comfyui_status}
            },
            'performance': {
                'memory_usage_percent': memory_usage,
                'recent_errors': recent_errors,
                'avg_response_time': stats.get('avg_response_time_s', 0),
                'messages_processed': stats.get('messages_processed', 0)
            },
            'revenue': {
                'total_stars': sum(t.get('total_stars', 0) for t in tipper_status.values()),
                'total_tips': sum(t.get('total_tips', 0) for t in tipper_status.values()),
                'tip_hooks_fired': stats.get('tip_hooks_fired', 0)
            }
        })

    return app


# ============================================================================
# SERVER START
# ============================================================================

def run_monitoring(app: Flask):
    """Start the Flask monitoring server."""
    main_logger.info(f"Starting monitoring on port {config.MONITORING_PORT}")
    app.run(host='127.0.0.1', port=config.MONITORING_PORT, debug=False, use_reloader=False)


# ============================================================================
# HTML TEMPLATES
# ============================================================================

HOME_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>HeatherBot AI Companion Monitor</title>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: Arial; padding: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 1200px; margin: auto; }
        .stats { background: #16213e; padding: 20px; margin-bottom: 20px; border-radius: 8px; }
        .chat { background: #16213e; padding: 15px; margin: 10px 0; border-radius: 5px; }
        .user { text-align: right; color: #4ecdc4; }
        .heather { text-align: left; color: #f06292; }
        .system { text-align: center; color: #666; font-style: italic; }
        h1 { color: #f06292; }
        h2 { color: #4ecdc4; }
        .status-ok { color: #4caf50; }
        .status-error { color: #f44336; }
        .userbot-badge { background: #9c27b0; color: white; padding: 5px 10px; border-radius: 3px; }
        .btn { padding: 8px 15px; margin: 5px; text-decoration: none; border-radius: 3px; color: white; }
        .btn-enable { background: #ff9800; }
        .btn-disable { background: #4CAF50; }
    </style>
</head>
<body>
    <div class="container">
        <h1>\U0001f697 Heather Userbot Monitor <span class="userbot-badge">TELETHON</span></h1>

        <div class="stats">
            <h2>\U0001f4ca Statistics</h2>
            <p>Uptime: {{ uptime }}</p>
            <p>Messages: {{ stats.messages_processed }}</p>
            <p>Images Processed: {{ stats.images_processed }}</p>
            <p>Images Generated: {{ stats.images_generated }}</p>
            <p>Voice Messages: {{ stats.voice_messages }}</p>
            <p>Active Chats: {{ active_chats }}</p>
        </div>

        <div class="stats">
            <h2>\U0001f527 Services</h2>
            <p>Text AI: <span class="{{ 'status-ok' if text_ok else 'status-error' }}">{{ text_status }}</span>
                {% if text_ai_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ text_ai_circuit }}]</span>{% endif %}</p>
            <p>Ollama: <span class="{{ 'status-ok' if ollama_ok else 'status-error' }}">{{ ollama_status }}</span>
                {% if ollama_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ ollama_circuit }}]</span>{% endif %}</p>
            <p>ComfyUI: <span class="{{ 'status-ok' if comfyui_ok else 'status-error' }}">{{ comfyui_status }}</span></p>
            <p>TTS: <span class="{{ 'status-ok' if tts_ok else 'status-error' }}">{{ tts_status }}</span>
                {% if tts_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ tts_circuit }}]</span>{% endif %}</p>
        </div>

        <div class="stats">
            <h2>\U0001f6a9 CSAM Flags</h2>
            <p>Pending: {{ pending_flags }} | Total: {{ total_flags }}
            {% if pending_flags > 0 %}<span style="color: #f44336; font-weight: bold;"> \u26a0\ufe0f REVIEW NEEDED</span>{% endif %}
            </p>
            <p><a href="/flags" style="color: #4ecdc4;">View Flag Dashboard \u2192</a></p>
        </div>

        <div class="stats">
            <h2>\u2615 Tips</h2>
            <p>Total Stars: {{ total_stars }} (~${{ "%.2f"|format(total_stars * 0.013) }}) | Tippers: {{ num_tippers }} | Bot Started: {{ started_users }}</p>
            <p><a href="/tips" style="color: #4ecdc4;">View Tips Dashboard \u2192</a></p>
        </div>

        <div class="stats">
            <h2>\U0001f4c8 API</h2>
            <p>Error Rate: {{ "%.2f"|format(error_rate) }}% | Text AI Requests: {{ stats.text_ai_requests }}</p>
            <p><a href="/stats" style="color: #4ecdc4;">View Stats JSON API \u2192</a></p>
        </div>

        <h2>\U0001f4ac Recent Conversations</h2>
        {% for chat_id, display_name, messages in chat_list %}
        <div class="chat">
            <h3>
                {{ display_name }} ({{ chat_id }})
                {% if chat_id in manual_mode_chats %}
                <span style="color: #f44336;">[MANUAL]</span>
                {% endif %}
            </h3>
            {% for msg in messages %}
            <p class="{{ msg.sender.lower().replace(' ', '').replace('\U0001f3a4', '') }}">
                <strong>{{ msg.timestamp }} [{{ msg.sender }}]:</strong> {{ msg.content }}
            </p>
            {% endfor %}
        </div>
        {% endfor %}
    </div>
</body>
</html>
'''

FLAGS_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>HeatherBot CSAM Flag Review</title>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: Arial; padding: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 900px; margin: auto; }
        h1 { color: #f06292; }
        h2 { color: #4ecdc4; }
        a { color: #4ecdc4; }
        .flag { background: #16213e; padding: 15px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #f44336; }
        .flag.dismissed { border-left-color: #4caf50; opacity: 0.7; }
        .flag.blocked { border-left-color: #ff9800; opacity: 0.7; }
        .meta { color: #888; font-size: 0.9em; }
        .message { background: #0d1117; padding: 10px; margin: 8px 0; border-radius: 3px; word-break: break-word; }
        .pattern { font-family: monospace; font-size: 0.85em; color: #ff9800; }
        .badge { padding: 2px 8px; border-radius: 3px; font-size: 0.8em; font-weight: bold; }
        .badge-pending { background: #f44336; color: white; }
        .badge-dismissed { background: #4caf50; color: white; }
        .badge-blocked { background: #ff9800; color: white; }
        .back { margin-bottom: 15px; display: inline-block; }
    </style>
</head>
<body>
    <div class="container">
        <a class="back" href="/">&larr; Back to Dashboard</a>
        <h1>CSAM Flag Review</h1>

        <h2>Pending ({{ pending|length }})</h2>
        {% if not pending %}
        <p style="color: #4caf50;">No pending flags.</p>
        {% endif %}
        {% for f in pending|reverse %}
        <div class="flag">
            <span class="badge badge-pending">PENDING</span>
            <strong>#{{ f.id }}</strong> &mdash; {{ f.display_name }} ({{ f.user_id }})
            <div class="meta">{{ f.timestamp }}</div>
            <div class="message">{{ f.message }}</div>
            <div class="pattern">Pattern: {{ f.matched_pattern }}</div>
        </div>
        {% endfor %}

        {% if resolved %}
        <h2>Resolved ({{ resolved|length }})</h2>
        {% for f in resolved|reverse %}
        <div class="flag {{ f.status }}">
            <span class="badge badge-{{ f.status }}">{{ f.status|upper }}</span>
            <strong>#{{ f.id }}</strong> &mdash; {{ f.display_name }} ({{ f.user_id }})
            <div class="meta">{{ f.timestamp }} &rarr; {{ f.get('resolved_at', '?') }}</div>
            <div class="message">{{ f.message }}</div>
        </div>
        {% endfor %}
        {% endif %}
    </div>
</body>
</html>
'''

TIPS_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>HeatherBot Tips Dashboard</title>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="60">
    <style>
        body { font-family: Arial; padding: 20px; background: #1a1a2e; color: #eee; }
        .container { max-width: 1000px; margin: auto; }
        h1 { color: #f06292; }
        h2 { color: #4ecdc4; }
        a { color: #4ecdc4; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin: 20px 0; }
        .stat-card { background: #16213e; padding: 20px; border-radius: 8px; text-align: center; }
        .stat-card .value { font-size: 2em; font-weight: bold; color: #f06292; }
        .stat-card .label { color: #888; font-size: 0.9em; margin-top: 5px; }
        table { width: 100%; border-collapse: collapse; margin: 15px 0; }
        th { background: #16213e; color: #4ecdc4; padding: 10px; text-align: left; }
        td { padding: 10px; border-bottom: 1px solid #2a2a4a; }
        .tier-0 { color: #666; }
        .tier-1 { color: #cd7f32; }
        .tier-2 { color: #c0c0c0; }
        .tier-3 { color: #ffd700; }
        .txn { background: #16213e; padding: 10px; margin: 5px 0; border-radius: 5px; font-family: monospace; font-size: 0.85em; }
        .back { margin-bottom: 15px; display: inline-block; }
    </style>
</head>
<body>
    <div class="container">
        <a class="back" href="/">&larr; Back to Dashboard</a>
        <h1>&#9749; Heather Tips Dashboard</h1>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="value">{{ total_stars }}</div>
                <div class="label">Total Stars Earned</div>
            </div>
            <div class="stat-card">
                <div class="value">${{ "%.2f"|format(total_stars * 0.013) }}</div>
                <div class="label">Est. Value (USD)</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ total_tips }}</div>
                <div class="label">Total Tips</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ tippers|length }}</div>
                <div class="label">Unique Tippers</div>
            </div>
            <div class="stat-card">
                <div class="value">{{ started_users }}</div>
                <div class="label">Bot Started</div>
            </div>
        </div>

        <h2>&#128200; Conversion Funnel</h2>
        <div style="background:#16213e; padding:20px; border-radius:8px; font-family:monospace; line-height:1.8; margin-bottom:20px;">
            <div>Hooks Fired: <b style="color:#f06292">{{ funnel_hooks }}</b> ({{ hooks_this_session }} this session)</div>
            <div style="padding-left:20px">&#9500;&#9472; Payment bot started: <b style="color:#4ecdc4">{{ funnel_started }}</b> ({{ pct_started }})</div>
            <div style="padding-left:40px">&#9500;&#9472; Invoice paid: <b style="color:#4ecdc4">{{ funnel_paid }}</b> ({{ pct_paid }})</div>
            <div style="padding-left:40px">&#9492;&#9472; Abandoned: <span style="color:#888">{{ funnel_abandoned }}</span></div>
            <div style="padding-left:20px">&#9492;&#9472; Ignored: <span style="color:#888">{{ funnel_ignored }}</span></div>
            <div style="margin-top:10px; border-top:1px solid #2a2a4a; padding-top:10px;">
                Conversion (Hooks &#8594; Paid): <b style="color:#ffd700">{{ pct_conversion }}</b>
            </div>
        </div>

        <h2>&#127775; Tippers</h2>
        {% if tippers %}
        <table>
            <tr><th>User</th><th>Stars</th><th>Tips</th><th>Tier</th><th>Last Hook</th></tr>
            {% for t in tippers %}
            <tr>
                <td>{{ t.name }} <span style="color:#666">({{ t.chat_id }})</span></td>
                <td>{{ t.total_stars }}</td>
                <td>{{ t.total_tips }}</td>
                <td class="tier-{{ t.tier }}">{{ tier_labels[t.tier] }}</td>
                <td style="color:#888">{{ t.last_hook or '-' }}</td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <p style="color: #888;">No tips yet.</p>
        {% endif %}

        <h2>&#128179; Recent Star Transactions (Bot API)</h2>
        {% if star_transactions %}
        {% for txn in star_transactions %}
        <div class="txn">
            {{ txn.get('amount', '?') }} stars |
            from: {{ txn.get('source', {}).get('user', {}).get('first_name', txn.get('source', {}).get('type', '?')) }} |
            date: {{ txn.get('date', '?') }}
        </div>
        {% endfor %}
        {% else %}
        <p style="color: #888;">No transactions found{% if not payment_bot_token %} (PAYMENT_BOT_TOKEN not set){% endif %}.</p>
        {% endif %}
    </div>
</body>
</html>
'''

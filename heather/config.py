"""
heather.config — Centralized Configuration
============================================
All CLI arguments, environment variables, endpoint URLs, threshold constants,
and feature flags for HeatherBot.

Replaces: Scattered constants throughout heather_telegram_bot.py
  - CLI args: lines 74-89
  - Telegram auth: lines 94-99
  - Endpoint URLs: lines 371-383
  - Monitoring: line 371
  - Admin: line 374
  - Tipping constants: lines 854-912
  - Warmth constants: lines 860-872
  - Access tier thresholds: lines 880-895
  - Photo/video caps: lines 875-877, 707-709
  - Check-in system: lines 821-828
  - Re-engagement: lines 832-840
  - Catch-up: lines 844-849
  - Goodbye/repeat: lines 804-810
  - Hostility/spam: lines 1286-1383
  - Voice: lines 759-763
  - Story: lines 776-777
  - Welcome-back: lines 740-741
  - Red team: line 745
  - Image gen: lines 433-479
  - Concurrency: lines 764-765

Dependencies: None (leaf module)
Used by: Every other heather module
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, FrozenSet, Set


# ── CLI Argument Parsing ───────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """Parse CLI arguments. Called once at startup."""
    parser = argparse.ArgumentParser(
        description="Heather Telegram Userbot v4.0 — Modular Edition"
    )
    parser.add_argument("--unfiltered", action="store_true",
                        help="Run without content filters")
    parser.add_argument("--monitoring", action="store_true",
                        help="Enable monitoring interface on port 8888")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--text-port", type=int, default=1234,
                        help="Text AI model port (default: 1234)")
    parser.add_argument("--text-model", type=str, default="local-model",
                        help="Text AI model name for API requests")
    parser.add_argument("--image-port", type=int, default=11434,
                        help="Ollama port for images (default: 11434)")
    parser.add_argument("--log-dir", type=str, default="logs",
                        help="Log directory path")
    parser.add_argument("--tts-port", type=int, default=5001,
                        help="TTS service port (default: 5001)")
    parser.add_argument("--personality", type=str,
                        default="heather_personality.yaml",
                        help="Personality YAML file path")
    parser.add_argument("--small-model", action="store_true",
                        help="Use optimized prompt for 12B models")
    parser.add_argument("--ollama", action="store_true",
                        help="Use Ollama native API")
    parser.add_argument("--session", type=str, default="heather_session",
                        help="Telethon session file name")
    return parser.parse_args()


# Parse once at import time (matches monolith behavior)
args = parse_args()


# ── Feature Flags ──────────────────────────────────────────────────────

SMALL_MODEL_MODE: bool = args.small_model
USE_OLLAMA: bool = args.ollama
UNFILTERED_MODE: bool = args.unfiltered
MONITORING_ENABLED: bool = args.monitoring
DEBUG_MODE: bool = args.debug

# Trial mode: when True, get_access_tier() returns VIP for all users.
# Real tier logic is preserved and tested underneath.
TRIAL_MODE: bool = True

# Group chat mode
GROUP_MODE_SFW: bool = True


# ── Telegram Auth ──────────────────────────────────────────────────────

API_ID: int = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
SESSION_NAME: str = args.session
ADMIN_USER_ID: int = int(os.getenv("ADMIN_USER_ID", "0"))


# ── Logging ────────────────────────────────────────────────────────────

LOG_DIR: str = args.log_dir


# ── Service Endpoints ──────────────────────────────────────────────────

TEXT_AI_PORT: int = args.text_port
IMAGE_AI_PORT: int = args.image_port
TTS_PORT: int = args.tts_port
MONITORING_PORT: int = 8888
COMFYUI_PORT: int = 8188

TEXT_AI_ENDPOINT: str = f"http://127.0.0.1:{TEXT_AI_PORT}/v1/chat/completions"
OLLAMA_CHAT_ENDPOINT: str = f"http://127.0.0.1:{TEXT_AI_PORT}/api/chat"
IMAGE_AI_ENDPOINT: str = f"http://localhost:{IMAGE_AI_PORT}"
TTS_ENDPOINT: str = f"http://127.0.0.1:{TTS_PORT}"
COMFYUI_URL: str = "http://127.0.0.1:8188"
TEXT_MODEL_NAME: str = args.text_model


# ── External API Keys ─────────────────────────────────────────────────

ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID: str = os.getenv("ELEVENLABS_VOICE_ID", "bmknSHfakfqnoh2yM9dh")
XAI_API_KEY: str = os.getenv("XAI_API_KEY", "")
PAYMENT_BOT_TOKEN: str = os.getenv("PAYMENT_BOT_TOKEN", "")
PAYMENT_BOT_USERNAME: str = os.getenv("PAYMENT_BOT_USERNAME", "YourPaymentBot")
MONITOR_AUTH_TOKEN: str = os.getenv(
    "MONITOR_AUTH_TOKEN", os.getenv("HEATHER_DASHBOARD_KEY", "")
)


# ── Timeouts ───────────────────────────────────────────────────────────

AI_TIMEOUT: int = 60
COMFYUI_TIMEOUT: int = 300
TTS_TIMEOUT: int = 120
MAX_RETRIES: int = 3
MIN_MESSAGE_INTERVAL: float = 1.5


# ── Conversation Limits ───────────────────────────────────────────────

MAX_CONVERSATION_LENGTH: int = 20
MAX_RECENT_MESSAGES: int = 50
PROMPT_TOKEN_BUDGET: int = 4096  # Max tokens for system prompt assembly


# ── Access Tiers ───────────────────────────────────────────────────────

ACCESS_TIER_FAN_THRESHOLD: int = 50   # Stars for FAN tier
ACCESS_TIER_VIP_THRESHOLD: int = 200  # Stars for VIP tier
VIP_TOKEN_CAP: int = 400

TIER_RANK: Dict[str, int] = {"FREE": 0, "FAN": 1, "VIP": 2}

IMAGE_TIER_REQUIREMENTS: Dict[str, str] = {
    "sfw_casual": "FREE",
    "sfw_flirty": "FREE",
    "sfw_lingerie": "FREE",
    "sfw_emma": "FREE",
    "nsfw_topless": "FREE",
    "nsfw_nude": "FAN",
    "nsfw_explicit": "VIP",
}


# ── Warmth System ─────────────────────────────────────────────────────

WARMTH_INITIAL: float = 0.7
WARMTH_WARM_THRESHOLD: float = 0.8
WARMTH_COLD_THRESHOLD: float = 0.4
WARMTH_FLOOR: float = 0.15
WARMTH_DECLINE_DECAY: float = 0.0    # Disabled
WARMTH_PASSIVE_DECAY: float = 0.0    # Disabled
WARMTH_PASSIVE_THRESHOLD: int = 50
WARMTH_TIP_BOOST: float = 0.3
WARMTH_DECLINE_MSG_WINDOW: int = 10


# ── Photo System ──────────────────────────────────────────────────────

PHOTO_CAP_WARM: int = 7
PHOTO_CAP_NEW: int = 5
PHOTO_CAP_COLD: int = 5
PHOTO_CAP_WINDOW: int = 7200  # 2 hours in seconds


# ── Video System ──────────────────────────────────────────────────────

VIDEO_RATE_LIMIT_COUNT: int = 5
VIDEO_RATE_LIMIT_WINDOW: int = 1800   # 30 minutes
VIDEO_BURST_COOLDOWN: int = 20        # Min seconds between sends


# ── Voice System ──────────────────────────────────────────────────────

VOICE_NUDGE_CHANCE: float = 0.06
VOICE_NUDGE_MIN_TURNS: int = 20
SELFIE_DESCRIPTION_TIMEOUT: int = 120
PROACTIVE_VOICE_COOLDOWN: int = 3600  # 1 hour


# ── Story System ──────────────────────────────────────────────────────

STORY_COOLDOWN_MSGS: int = 25
STORY_ORGANIC_MIN_GAP: int = 12


# ── Check-In System ──────────────────────────────────────────────────

CHECKIN_DELAY_MIN: int = 2700         # 45 min
CHECKIN_DELAY_MAX: int = 5400         # 90 min
CHECKIN_INTERVAL: int = 300           # Check every 5 min
CHECKIN_ONLY_AFTER_TURNS: int = 5
CHECKIN_MAX_PER_DAY: int = 2
CHECKIN_MAX_UNRETURNED: int = 2
CHECKIN_QUIET_HOURS_START: int = 22
CHECKIN_QUIET_HOURS_END: int = 8


# ── Re-engagement System ─────────────────────────────────────────────

REENGAGEMENT_MIN_IDLE_DAYS: int = 2
REENGAGEMENT_MAX_IDLE_DAYS: int = 21
REENGAGEMENT_MIN_MESSAGES: int = 10
REENGAGEMENT_COOLDOWN_DAYS: int = 7
REENGAGEMENT_MAX_PER_DAY: int = 3
REENGAGEMENT_SCAN_INTERVAL: int = 3600
REENGAGEMENT_HOUR_START: int = 10
REENGAGEMENT_HOUR_END: int = 21
REENGAGEMENT_AUTO_ENABLED: bool = True


# ── Catch-Up System ──────────────────────────────────────────────────

CATCHUP_MAX_AGE_HOURS: int = 12
CATCHUP_MIN_DOWNTIME_SECONDS: int = 120
CATCHUP_MAX_REPLIES: int = 15
CATCHUP_DELAY_MIN: int = 8
CATCHUP_DELAY_MAX: int = 15
CATCHUP_ENABLED: bool = True


# ── Welcome-Back System ─────────────────────────────────────────────

WELCOME_BACK_MIN_GAP_HOURS: int = 2
WELCOME_BACK_MAX_GAP_HOURS: int = 48


# ── Goodbye / Repeated Message Detection ─────────────────────────────

GOODBYE_LOOP_WINDOW: int = 600       # 10 min
GOODBYE_LOOP_THRESHOLD: int = 2
REPEATED_MSG_THRESHOLD: int = 3
REPEATED_MSG_WINDOW: int = 1800      # 30 min


# ── Hostility / Spam / Burst Detection ───────────────────────────────

HOSTILITY_WINDOW: int = 120          # 2 minutes
HOSTILITY_REPEAT_THRESHOLD: int = 3
HOSTILITY_COOLDOWN_SECS: int = 300   # 5 min cooldown
BOT_ACCUSATION_SHRUG_LIMIT: int = 2
SINGLE_CHAR_WINDOW: int = 300        # 5 min
SINGLE_CHAR_THRESHOLD: int = 3
BURST_THRESHOLD: int = 10            # msgs in 60s
FLOOD_THRESHOLD: int = 25            # msgs in 5min


# ── Tipping System ───────────────────────────────────────────────────

TIP_MENTION_COOLDOWN: int = 5 * 86400  # 5 days
TIP_MIN_MESSAGES: int = 12
TEASE_INVOICE_COOLDOWN: int = 300       # 5 min
MEMORY_TEASE_COOLDOWN: int = 1800       # 30 min
TIP_HOOK_COOLDOWN_WINDOW: int = 1800    # 30 min
MEMORY_UPSELL_COOLDOWN: int = 86400     # 24 hours


# ── Content Promise ──────────────────────────────────────────────────

CONTENT_PROMISE_WINDOW: int = 300     # 5 min


# ── Takeover System ──────────────────────────────────────────────────

TAKEOVER_OPPORTUNITY_COOLDOWN: int = 7200  # 2 hours
DISSATISFACTION_ALERT_COOLDOWN: int = 7200


# ── Red Team Mode ────────────────────────────────────────────────────

REDTEAM_AUTO_OFF_SECONDS: int = 30 * 60  # 30 min


# ── Breeding Content ─────────────────────────────────────────────────

BREEDING_COOLDOWN: int = 6           # Messages between injections


# ── Alert Cooldown ───────────────────────────────────────────────────

ALERT_COOLDOWN_SECONDS: int = 300    # 5 min


# ── ComfyUI / Image Generation ──────────────────────────────────────

COMFYUI_WORKFLOW_FILE: str = "workflow_flux.json"
COMFYUI_POSITIVE_PROMPT_NODE: str = "3"
COMFYUI_NEGATIVE_PROMPT_NODE: str = "4"
COMFYUI_FACE_IMAGE_NODE: str = "10"
COMFYUI_FINAL_OUTPUT_NODE: str = "9"
HEATHER_FACE_IMAGE: str = os.getenv("COMFYUI_FACE_IMAGE", "heather_face.png")
FLUX_GUIDANCE: float = 3.5
CONTROLNET_MODEL: str = "FLUX-controlnet-union-pro-2.0.safetensors"
CONTROLNET_STRENGTH: float = 0.65
CONTROLNET_END: float = 0.65


# ── Ignored Chats ────────────────────────────────────────────────────

# 93372553=BotFather, 178220800=Service Notifications, 777000=Telegram official
IGNORED_CHATS: FrozenSet[int] = frozenset({93372553, 178220800, 777000})


# ── File Paths (relative to bot root) ────────────────────────────────
# These are computed at runtime by the modules that own them.
# Listed here for reference only.

BOT_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONALITY_FILE: str = args.personality


# ── Concurrency Limits ───────────────────────────────────────────────
# Note: Actual semaphores are created at runtime (asyncio.Semaphore needs
# an event loop). These are the limit values.

MAX_CONCURRENT_IMAGE_GEN: int = 1
MAX_CONCURRENT_LLM_REQUESTS: int = 3


# ── Canonical Sexual Keyword Sets ───────────────────────────────────
# Centralized here (not in access_tiers or conversation) because they're
# pure data constants used by multiple modules. Subset assertion enforced
# at import time.

SEXUAL_KEYWORDS_CORE: FrozenSet[str] = frozenset({
    'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'lick', 'ass',
    'tits', 'boobs', 'naked', 'nude', 'horny', 'wet', 'hard',
    'stroke', 'moan', 'orgasm', 'blow', 'ride',
})
"""Core sexual terms — used by topic-loop detection and sexual-conversation checks."""

SEXUAL_KEYWORDS_BROAD: FrozenSet[str] = frozenset({
    # All of CORE, plus additional terms for energy detection
    'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'lick', 'ass',
    'tits', 'boobs', 'naked', 'nude', 'horny', 'wet', 'hard',
    'stroke', 'moan', 'orgasm', 'blow', 'ride',
    # Broad additions
    'titties', 'nipple', 'sex', 'naughty', 'boner',
    'masturbat', 'jerk off', 'touch yourself',
    'tongue', 'taste', 'swallow', 'sexy', 'boob',
})
"""Broad sexual terms — superset of CORE, used by energy-level detection."""

assert SEXUAL_KEYWORDS_CORE <= SEXUAL_KEYWORDS_BROAD, (
    f"SEXUAL_KEYWORDS_CORE has terms not in BROAD: "
    f"{SEXUAL_KEYWORDS_CORE - SEXUAL_KEYWORDS_BROAD}"
)

FLIRTY_KEYWORDS: FrozenSet[str] = frozenset({
    'sexy', 'hot', 'cute', 'beautiful', 'gorgeous', 'turn me on',
    'turn you on', 'flirt', 'naughty', 'tease', 'kiss', 'make out',
    'date', 'bed', 'shower', 'undress',
})
"""Flirty but not explicitly sexual — used for energy-level detection."""


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_parse_args():
#     """Verify parse_args returns valid namespace with all expected fields."""
#     # args = parse_args()  # Would need to mock sys.argv
#     # assert hasattr(args, 'text_port')
#     # assert hasattr(args, 'small_model')
#     pass
#
# def test_trial_mode_flag():
#     """TRIAL_MODE should be True by default."""
#     assert TRIAL_MODE is True
#
# def test_tier_rank_ordering():
#     """FREE < FAN < VIP."""
#     assert TIER_RANK["FREE"] < TIER_RANK["FAN"] < TIER_RANK["VIP"]
#
# def test_image_tier_requirements_valid():
#     """All image tier requirements should be valid tier names."""
#     for tier in IMAGE_TIER_REQUIREMENTS.values():
#         assert tier in TIER_RANK

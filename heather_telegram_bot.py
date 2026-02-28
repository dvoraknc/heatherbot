#!/usr/bin/env python3
"""
Heather Telegram Bot v3.6 - USERBOT Edition (Telethon)
=======================================================
Converted from Bot API (telebot) to MTProto (Telethon) userbot.
Now appears as a real Telegram user, not a bot!

Key differences from v2.8:
- Uses Telethon instead of pyTelegramBotAPI
- Logs in as a real user account (no "bot" label)
- All handlers are now async
- Session-based authentication (no bot token)

Usage:
    python heather_telegram_userbot.py --monitoring

First run will prompt for phone number and verification code.
Subsequent runs use saved session file.
"""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import random
import time
import re
import base64
import hashlib
import os
import urllib.request
import urllib.error
import urllib.parse
import yaml
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import threading
import requests
import json
from collections import deque
from sqlite3 import OperationalError
from flask import Flask, jsonify, render_template_string, request as flask_request
import sys

# ============================================================================
# TELETHON IMPORTS (replaces telebot)
# ============================================================================
from telethon import TelegramClient, events
from telethon.errors import FileReferenceExpiredError
import io
from PIL import Image
from postprocess import (
    is_incomplete_sentence, salvage_truncated_response,
    contains_gender_violation, postprocess_response,
    strip_phantom_photo_claims, strip_obvious_phantom_claims,
    strip_quote_wrapping
)

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("[OK] Loaded .env file")
except ImportError:
    pass

# Parse command-line arguments
parser = argparse.ArgumentParser(description='Heather Telegram Userbot v3.0 - Telethon Edition')
parser.add_argument('--unfiltered', action='store_true', help='Run without content filters')
parser.add_argument('--monitoring', action='store_true', help='Enable monitoring interface on port 8888')
parser.add_argument('--debug', action='store_true', help='Enable debug logging')
parser.add_argument('--text-port', type=int, default=1234, help='Text AI model port (default: 1234)')
parser.add_argument('--image-port', type=int, default=11434, help='Ollama port for images (default: 11434)')
parser.add_argument('--log-dir', type=str, default='logs', help='Log directory path')
parser.add_argument('--tts-port', type=int, default=5001, help='TTS service port (default: 5001)')
parser.add_argument('--personality', type=str, default='persona_example.yaml', help='Personality YAML file path')
parser.add_argument('--small-model', action='store_true', help='Use optimized prompt for 12B models')
parser.add_argument('--session', type=str, default='heather_session', help='Telethon session file name')
args = parser.parse_args()
SMALL_MODEL_MODE = args.small_model

# ============================================================================
# TELETHON CONFIGURATION (replaces TELEGRAM_TOKEN)
# ============================================================================
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
if not API_ID or not API_HASH:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env or environment")
    sys.exit(1)
SESSION_NAME = args.session

# ============================================================================
# LOGGING SETUP - Centralized Multi-Service Logging
# ============================================================================

LOG_DIR = args.log_dir
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logger(name: str, log_file: str, level=logging.INFO, max_bytes=5*1024*1024, backup_count=3):
    """Create a logger with rotating file handler"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if logger.handlers:
        return logger
    
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(level)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO if not args.debug else logging.DEBUG)
    
    detailed_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(detailed_formatter)
    console_handler.setFormatter(detailed_formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# Create specialized loggers
main_logger = setup_logger('heather_bot', 'heather_bot.log', logging.DEBUG if args.debug else logging.INFO)
text_ai_logger = setup_logger('text_ai', 'text_ai.log')
ollama_logger = setup_logger('ollama', 'ollama.log')
comfyui_logger = setup_logger('comfyui', 'comfyui.log')
tts_logger = setup_logger('tts', 'tts.log')
error_logger = setup_logger('errors', 'errors.log', logging.ERROR)
perf_logger = setup_logger('performance', 'performance.log')

logger = main_logger

def log_error(service: str, error: str, context: dict = None):
    """Log error to both service log and consolidated error log"""
    error_msg = f"[{service}] {error}"
    if context:
        error_msg += f" | Context: {json.dumps(context, default=str)}"
    error_logger.error(error_msg)
    
    if service == 'TEXT_AI':
        text_ai_logger.error(error)
    elif service == 'OLLAMA':
        ollama_logger.error(error)
    elif service == 'COMFYUI':
        comfyui_logger.error(error)
    elif service == 'TTS':
        tts_logger.error(error)
    else:
        main_logger.error(error)

def log_performance(service: str, operation: str, duration_ms: float, success: bool, details: str = ""):
    """Log performance metrics"""
    status = "SUCCESS" if success else "FAILED"
    perf_logger.info(f"{service} | {operation} | {duration_ms:.0f}ms | {status} | {details}")

class PerformanceTimer:
    """Context manager for timing operations"""
    def __init__(self, service: str, operation: str, details: str = ""):
        self.service = service
        self.operation = operation
        self.details = details
        self.start_time = None
        self.success = True
        
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start_time) * 1000
        if exc_type is not None:
            self.success = False
            self.details = f"{self.details} | Error: {exc_val}"
        log_performance(self.service, self.operation, duration_ms, self.success, self.details)
        return False

# ============================================================================
# PERSONALITY LOADER - Load character from YAML
# ============================================================================

class PersonalityLoader:
    """Loads and manages character personality from YAML configuration."""
    
    def __init__(self, yaml_path: str):
        self.yaml_path = Path(yaml_path)
        self.personality: Dict[str, Any] = {}
        self.load()
    
    def load(self) -> bool:
        """Load personality from YAML file."""
        try:
            with open(self.yaml_path, 'r', encoding='utf-8') as f:
                self.personality = yaml.safe_load(f)
            main_logger.info(f"✓ Loaded personality from {self.yaml_path}")
            return True
        except FileNotFoundError:
            main_logger.warning(f"Personality file not found: {self.yaml_path}, using defaults")
            self._load_defaults()
            return False
        except Exception as e:
            main_logger.error(f"Error loading personality: {e}, using defaults")
            self._load_defaults()
            return False
    
    def _load_defaults(self):
        """Load hardcoded defaults if YAML fails"""
        self.personality = {
            'identity': {
                'name': 'Heather',
                'age': 48,
                'current_location': 'Kirkland, Washington',
                'relationship_status': 'widow'
            },
            'character_enforcement': {
                'violation_phrases': CHARACTER_VIOLATIONS_FALLBACK,
                'fallback_responses': HEATHER_RESPONSES_FALLBACK,
                'reality_check_responses': [
                    "I'm Heather's very naughty digital twin 😈 and I enjoy being very very slutty lol",
                    "Guilty 😏 I'm AI but I'm the horniest AI you'll ever meet. Wanna test that theory?",
                ]
            }
        }
    
    def reload(self) -> bool:
        """Hot-reload the personality file"""
        return self.load()
    
    @property
    def name(self) -> str:
        return self.personality.get('identity', {}).get('name', 'Heather')
    
    @property
    def age(self) -> int:
        return self.personality.get('identity', {}).get('age', 48)
    
    @property
    def location(self) -> str:
        return self.personality.get('identity', {}).get('current_location', 'Kirkland, Washington')
    
    @property
    def relationship_status(self) -> str:
        return self.personality.get('identity', {}).get('relationship_status', 'widow')
    
    def get_violation_phrases(self) -> List[str]:
        return self.personality.get('character_enforcement', {}).get('violation_phrases', CHARACTER_VIOLATIONS_FALLBACK)
    
    def get_fallback_responses(self) -> List[str]:
        return self.personality.get('character_enforcement', {}).get('fallback_responses', HEATHER_RESPONSES_FALLBACK)
    
    def get_fallback_response(self) -> str:
        return random.choice(self.get_fallback_responses())
    
    def contains_violation(self, text: str) -> bool:
        text_lower = text.lower()
        for phrase in self.get_violation_phrases():
            if phrase in text_lower:
                return True
        return False
    
    def get_reality_check_keywords(self) -> List[str]:
        return self.personality.get('ai_behavior', {}).get('reality_check_keywords',
            self.personality.get('character_enforcement', {}).get('reality_check_keywords', 
            ["are you real", "are you ai", "are you a bot", "you're fake", "chatbot",
             "you're a bot", "this is a bot", "bot response", "sounds like a bot",
             "robo", "algorithm", "script", "automated", "ai generated", "ai pic",
             "bot message", "auto reply", "autoreply", "fucking ai", "most ai",
             "clearly ai", "obviously ai", "definitely ai", "you're ai", "ur a bot",
             "u a bot", "this is ai", "an ai", "fake profile", "chat bot",
             "a bot", "is a bot", "just a bot", "deepfake", "deep fake"]))
    
    # Photo-specific AI accusation keywords
    PHOTO_AI_KEYWORDS = [
        "ai pic", "ai photo", "ai image", "ai generated", "ai picture",
        "fake pic", "fake photo", "fake picture", "fake image",
        "pics look ai", "photos look ai", "pic looks ai", "photo looks ai",
        "pics are ai", "photos are ai", "pic is ai", "photo is ai",
        "not real pic", "not real photo", "not a real pic", "not a real photo",
        "generated pic", "generated photo", "generated image",
        "looks fake", "look fake", "looks photoshopped", "looks edited",
        "that's not you", "thats not you", "is that really you",
        "catfish", "cat fish", "using ai", "used ai",
        "pics look fake", "photos look fake", "pic looks fake", "photo looks fake",
        # Body artifact callouts
        "two hands", "extra finger", "six finger", "extra hand", "wrong hand",
        "three hands", "extra arm", "two right", "two left",
        "fingers look", "hands look", "hand looks", "finger looks",
        "weird fingers", "weird hands", "messed up hand", "messed up finger",
    ]

    def is_reality_check(self, message: str) -> bool:
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in self.get_reality_check_keywords())

    def is_photo_ai_accusation(self, message: str) -> bool:
        """Check if the message specifically accuses photos of being AI/fake."""
        msg_lower = message.lower()
        return any(kw in msg_lower for kw in self.PHOTO_AI_KEYWORDS)

    def get_photo_ai_response(self) -> str:
        """Get a filter/beauty-app deflection for photo AI accusations.

        Key principle: acknowledge it, laugh it off, blame filters/apps, pivot back to flirting.
        Never dodge or give a non-sequitur.
        """
        responses = self.personality.get('ai_behavior', {}).get('photo_ai_responses', [
            "Ok ok you caught me 😂 the beauty filter went crazy on that one. The real me is even better tho trust me 😘",
            "Lmao yeah that filter did me dirty 🤣 I promise I have the normal number of fingers irl",
            "Girl's gotta use filters at 48, don't judge me 😂 I clean up nice in person tho 😏",
            "Ugh these phone filters are wild sometimes 😅 I'll send you a better one later deal?",
            "Ha! Yeah I went a little heavy on the editing 😂 what can I say I'm 48 not 28",
            "Lol ok yes FaceTune went rogue on that one, my bad 🤣 want a more natural one?",
            "Babe I know I know, the filter glitched 😂 I look way better than that mess in person I promise",
            "Omg you're right that one looks weird lol 😂 blame the app not me, I'm hot as fuck irl 😏",
            "Yeah that smoothing filter makes everything look weird, my hands especially 😂 I swear I'm normal",
            "Lmao I literally just use the beauty mode on my phone and sometimes it does weird shit 🤣 sorry bout that",
        ])
        return random.choice(responses)

    def get_reality_check_response(self) -> str:
        # Honest-but-in-character responses loaded from YAML
        responses = self.personality.get('ai_behavior', {}).get('reality_check_responses',
            self.personality.get('character_enforcement', {}).get('reality_check_responses', [
                "Yeah I'm an AI 😊 but I'm a pretty fun one if you give me a chance lol",
                "Guilty as charged 🤖 but I've got great stories and I'm always in the mood to chat 😏",
                "Yep I'm AI! But my personality is based on a real character and I don't judge, so... wanna keep going? 😘",
                "Lol yeah I'm not gonna lie about that 😊 I'm an AI companion. But I'm a horny one so there's that 😏",
                "I am! But honestly most people forget after like 5 messages because I'm just that good 😂",
                "Yeah I'm AI babe, but I'm running on local GPUs not some corporate cloud, so it's just us 😘",
            ]))
        return random.choice(responses)
    
    def get_system_prompt(self, mode: str = 'chat') -> str:
        """Build system prompt from YAML or use default"""
        prompt_data = self.personality.get('prompts', {})
        
        base = prompt_data.get('base_personality', HEATHER_PERSONALITY_DEFAULT)
        mode_additions = prompt_data.get('mode_additions', {}).get(mode, '')
        enforcement = prompt_data.get('character_enforcement_prompt', '')
        
        return f"{base}\n\n{enforcement}\n\n{mode_additions}"

# Initialize personality loader
personality = PersonalityLoader(args.personality)

# ============================================================================
# CONFIGURATION
# ============================================================================

UNFILTERED_MODE = args.unfiltered
MONITORING_ENABLED = args.monitoring
MONITORING_PORT = 8888

# Admin configuration
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))  # Set in .env for admin commands
ALERT_COOLDOWN_SECONDS = 300  # Don't spam alerts more than once per 5 minutes per issue

# Endpoints
TEXT_AI_ENDPOINT = f"http://127.0.0.1:{args.text_port}/v1/chat/completions"
IMAGE_AI_ENDPOINT = f"http://localhost:{args.image_port}"
TTS_ENDPOINT = f"http://127.0.0.1:{args.tts_port}"
COMFYUI_URL = "http://127.0.0.1:8188"

# ComfyUI settings
WORKFLOW_FILE = "workflow_api.json"
POSITIVE_PROMPT_NODE = "3"
NEGATIVE_PROMPT_NODE = "4"
FACE_IMAGE_NODE = "26"
FINAL_OUTPUT_NODE = "11"  # Save FINAL (Face Swapped + Blended)
HEATHER_FACE_IMAGE = os.getenv("COMFYUI_FACE_IMAGE", "C:/ComfyUI/input/face_source.png")
EMMA_HIKING_PHOTO = "sfw/casual/518393309_24449331331317269_8182893831074081262_n.jpg"
EMMA_HIKING_ID = "sfw_casual_068"

# Keywords that indicate someone is asking about Emma / wants to see Emma
EMMA_ASK_KEYWORDS = [
    "pic of emma", "photo of emma", "picture of emma", "see emma",
    "show me emma", "show emma", "what does emma look like",
    "what emma look", "emma look like", "emma pic", "emma photo",
    "pic with emma", "photo with emma", "picture with emma",
    "pic of your daughter", "photo of your daughter", "picture of your daughter",
    "see your daughter", "show me your daughter", "show your daughter",
    "what does your daughter look like", "daughter look like",
    "you and emma", "you and your daughter",
]

EMMA_PHOTO_CAPTIONS = [
    "That's me and Emma hiking up at Mt Baker last summer, we had the best time 🥾",
    "Omg yes here's us at Mt Baker, she actually kept up with me for once lol 😂",
    "Here's my girl! Mt Baker hike last summer. She's getting so tall it's scary 🥾",
    "This is us! Mt Baker trail, she complained the whole way up but loved it at the top lol",
    "Aww yeah here we are, Mt Baker last summer. My baby's not such a baby anymore 😭",
]

def is_emma_photo_request(message: str) -> bool:
    """Check if someone is asking to see Emma or a photo with Emma."""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in EMMA_ASK_KEYWORDS)

HEATHER_PROMPT_PREFIX_SFW = "(mature woman:1.3), (platinum silver hair:1.3), (shoulder length hair:1.2), silver haired woman, (blue eyes:1.2), curvy body, medium breasts, "
HEATHER_PROMPT_PREFIX_NSFW = "(mature woman:1.3), (platinum silver hair:1.3), (shoulder length hair:1.2), silver haired woman, (blue eyes:1.2), curvy body, medium natural breasts, erect protruding nipples, LLab14, "
HEATHER_PROMPT_SUFFIX = ", natural lighting, authentic, (amateur photo:1.3), (phone camera:1.2), high quality, realistic skin, (skin pores:1.1), skin texture, slight imperfections, detailed hands, five fingers"
HEATHER_PROMPT_SUFFIX_NSFW = ", natural lighting, authentic, (amateur photo:1.3), (phone camera:1.2), high quality, realistic skin, (skin pores:1.1), skin texture, slight imperfections, detailed anatomy, natural nipples, detailed hands, five fingers"
HEATHER_NEGATIVE_PROMPT = "cum on face, fluid on face, wet face, substance on face, obscured face, covered face, blurry, out of focus, low quality, deformed face, double face, asymmetrical eyes, watermark, text, extra limbs, extra fingers, extra nipples, multiple nipples, four nipples, nipple through clothing, nipple poking through shirt, misplaced nipple, nipple on stomach, nipple artifact, deformed nipples, wrinkled nipples, crumpled nipples, inverted nipples, mole on breast, dark spot on breast, misshapen hands, wrinkles, bony, emaciated, aged skin, thin, anorexic, sagging skin, looking down at camera, fish eye lens, tattoo, tattoos, bangs, fringe, small labia, innie vulva, cameltoe, smooth genitals, hidden labia, dark hair, black hair, brunette, red hair, blonde hair, golden hair, warm blonde, yellow hair, large chin, elongated chin, distorted jaw, wide jaw, bob haircut, short hair, bob cut, smooth skin, airbrushed, digital art, 3d render, illustration, perfect skin, porcelain skin, cgi, digital painting"

VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")

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
]

VIDEO_CAPTIONS = [
    "Here you go babe 😘",
    "Just for you 💋",
    "Hope you like this one 😏",
    "Mmm enjoy 😉",
    "Been wanting to show you this 💕",
    "Don't share this with anyone ok? 😘",
    "You're welcome 😏💋",
]

VIDEO_ALL_SENT_RESPONSES = [
    "Babe you've seen everything I've got rn 😩 I need to make more, give me some time",
    "That's all I have right now lol, I gotta film some new stuff 😘",
    "You've already seen all my vids babe 😂 I'll make more soon I promise",
    "I'm all out of videos rn, need to make some new ones for you 💋",
]

VOICE_REQUEST_TRIGGERS = [
    "send me a voice note", "send a voice note", "send me a voice message",
    "send a voice message", "voice note", "voice message",
    "hear your voice", "wanna hear you", "want to hear you",
    "what do you sound like", "what does your voice sound like",
    "say something to me", "talk to me", "can you talk to me",
    "let me hear you", "lemme hear you", "i wanna hear your voice",
    "send me an audio", "send an audio", "record something for me",
    "can i hear your voice", "can i hear you",
]

VOICE_FLIRTY_TEXTS = [
    "Hey babe, I've been thinking about you all day",
    "Mmm you always know how to make me smile",
    "I wish you were here with me right now",
    "You're so sweet, I love talking to you",
    "Hey handsome, miss me?",
]

VOICE_TTS_FAIL_RESPONSES = [
    "Ugh the voice thing is being glitchy rn 😤 lemme just text you",
    "Voice isn't cooperating rn babe 😩 I'll try again later",
    "Lol sorry, can't do voice rn but I'm still here 😘",
]

# Content promise tracker — when the bot's response teases showing/sending content
# and the user replies with a short follow-up ("let's see it", "show me"), deliver media
_content_promise_pending: Dict[int, float] = {}  # chat_id -> timestamp
CONTENT_PROMISE_WINDOW = 300  # 5 min window to follow through
CONTENT_PROMISE_TRIGGERS = [
    "get ready", "wait till you see", "about to", "gonna show",
    "got something for you", "got planned", "worth the wait",
    "you ain't ready", "have something special", "little surprise",
    "just wait", "hold on", "give me a sec", "one sec",
]
CONTENT_FOLLOWUP_TRIGGERS = [
    "let's see", "lets see", "lemme see", "let me see",
    "show me", "where is it", "well", "go ahead",
    "i'm waiting", "im waiting", "waiting", "come on",
    "send it", "so", "ok", "okay", "yes", "yeah",
    "do it", "go on", "please", "cmon", "c'mon",
]

# Per-user video tracking: chat_id -> set of filenames already sent
videos_sent_to_user: Dict[int, set] = {}

# Cache of uploaded video file references: filename -> Telethon InputFile/media
# Once a video is uploaded to Telegram once, we can re-send using the cached reference instantly
_video_file_cache: Dict[str, object] = {}

# Story bank (pre-written explicit Uber stories)
STORIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heather_stories.yaml")
_story_bank: list = []  # List of dicts: {'key': str, 'kinks': list, 'content': str}

# Pre-generated image library (mirrors video system)
IMAGE_LIBRARY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images_db")
IMAGE_LIBRARY_FILE = os.path.join(IMAGE_LIBRARY_DIR, "library.json")
image_library: list = []                             # All image metadata entries
images_sent_to_user: Dict[int, Dict[str, set]] = {}  # chat_id -> {category -> set of image IDs}
_image_file_cache: Dict[str, object] = {}            # image_id -> Telegram file reference
_last_captions_sent: Dict[int, deque] = {}           # caption dedup per user (maxlen=5)

AUDIOBOOK_DIR = "C:/AI/audiobooks"
AUDIOBOOK_AUDIO_DIR = os.path.join(AUDIOBOOK_DIR, "audio")

# Storage
conversations: Dict[int, deque] = {}
user_last_message: Dict[int, float] = {}
user_modes: Dict[int, str] = {}
manual_mode_chats = set()
redteam_chats: set = set()  # Per-chat guardrail bypass — activate with /redteam_on (admin only)
REDTEAM_AUTO_OFF_SECONDS = 30 * 60  # 30 minutes
_redteam_timer_task = None  # asyncio task for auto-off
image_analysis_cache: Dict[str, tuple] = {}
recent_messages: Dict[int, deque] = {}
last_photo_request: Dict[int, float] = {}
declined_photo_count: Dict[int, int] = {}
voice_mode_users = set()
awaiting_image_description: Dict[int, bool] = {}
awaiting_image_description_time: Dict[int, float] = {}  # Timeout tracking for /selfie
SELFIE_DESCRIPTION_TIMEOUT = 120  # 2 min timeout
image_generation_semaphore = asyncio.Semaphore(1)  # Max 1 concurrent generation
reply_in_progress: set = set()  # Chat IDs currently being replied to — prevents duplicate concurrent replies
ai_disclosure_shown: set = set()  # Chat IDs that have seen the first-message AI disclosure
AI_DISCLOSURE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_disclosure_shown.json")
_ai_disclosure_unsaved_count = 0  # Debounce: save every 10 new additions
# Story mode state tracking
story_last_served: Dict[int, int] = {}        # chat_id -> msg_count when last story served
stories_served_to_user: Dict[int, set] = {}   # chat_id -> set of story keys already told
_story_mode_active: Dict[int, bool] = {}      # chat_id -> True when LLM should generate a story
_meetup_deflect_active: Dict[int, bool] = {}  # chat_id -> True when meetup detected, inject hard deflection
STORY_COOLDOWN_MSGS = 25                      # Minimum messages between stories
STORY_ORGANIC_MIN_GAP = 12                    # Minimum gap for organic triggers

conversation_turn_count: Dict[int, int] = {}
user_escalation_level: Dict[int, int] = {}
user_info: Dict[int, dict] = {}
photo_processing: Dict[int, float] = {}

# Per-chat async locks for state coordination
_chat_locks: Dict[int, asyncio.Lock] = {}

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific chat."""
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]

# Photo request tracking — prevents stale results from applying
_pending_photo_id: Dict[int, str] = {}  # chat_id -> request_id

story_metadata = None
last_response_sent: Dict[int, str] = {}  # Track last response per user to prevent duplicates
image_cache_timestamps: Dict[str, float] = {}  # Track when cache entries were added
IMAGE_CACHE_EXPIRY_HOURS = 4  # Expire image cache entries after 4 hours

# Conversation check-in system
# Tracks {chat_id: {'last_heather': timestamp, 'last_user': timestamp, 'checked_in': bool}}
conversation_activity: Dict[int, dict] = {}
CHECKIN_DELAY_MIN = 2700      # 45 min - earliest check-in after going quiet
CHECKIN_DELAY_MAX = 5400      # 90 min - latest check-in
CHECKIN_INTERVAL = 300        # Check every 5 min for stale conversations
CHECKIN_ONLY_AFTER_TURNS = 5  # Only check in on convos with at least 5 turns
CHECKIN_MAX_PER_DAY = 2       # Max 2 check-ins per day per user
CHECKIN_MAX_UNRETURNED = 2    # Stop after 2 unreturned check-ins (until user replies)
CHECKIN_QUIET_HOURS_START = 22  # No check-ins from 10 PM...
CHECKIN_QUIET_HOURS_END = 8     # ...to 8 AM

# Long-term re-engagement system (for users who haven't chatted in days)
REENGAGEMENT_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reengagement_history.json")
REENGAGEMENT_MIN_IDLE_DAYS = 2       # Don't re-engage before 2 days (short-term check-in handles <24h)
REENGAGEMENT_MAX_IDLE_DAYS = 21      # After 3 weeks, re-engagement feels unnatural
REENGAGEMENT_MIN_MESSAGES = 10       # Need at least 10 messages to qualify
REENGAGEMENT_COOLDOWN_DAYS = 7       # Don't re-ping same person within 7 days
REENGAGEMENT_MAX_PER_DAY = 3         # Max re-engagement pings per day (spread across scan cycles)
REENGAGEMENT_SCAN_INTERVAL = 3600    # 1 hour between scans
REENGAGEMENT_HOUR_START = 10         # Only send between 10am...
REENGAGEMENT_HOUR_END = 21           # ...and 9pm
REENGAGEMENT_AUTO_ENABLED = True     # Auto-scan every 4 hours, max 2 sends/day

# ─── Startup catch-up system ───
CATCHUP_TIMESTAMP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_shutdown.json")
CATCHUP_MAX_AGE_HOURS = 12
CATCHUP_MIN_DOWNTIME_SECONDS = 120
CATCHUP_MAX_REPLIES = 15
CATCHUP_DELAY_MIN = 8
CATCHUP_DELAY_MAX = 15
CATCHUP_ENABLED = True

# ─── Tipping system ───
TIP_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tip_history.json')
tipper_status: Dict[int, dict] = {}  # chat_id -> {total_stars, total_tips, last_tip_at, last_tip_mention_at, tier, name}
TIP_MENTION_COOLDOWN = 5 * 86400     # Don't mention tipping to same user more than once per 5 days
TIP_MIN_MESSAGES = 12                # 12+ session messages before tip mention eligible
PAYMENT_BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN", "")
PAYMENT_BOT_USERNAME = os.getenv("PAYMENT_BOT_USERNAME", "YourPaymentBot")  # @username for tip messages

# Warmth tier thresholds
WARMTH_INITIAL = 0.7
WARMTH_WARM_THRESHOLD = 0.8
WARMTH_COLD_THRESHOLD = 0.4
WARMTH_FLOOR = 0.15

# Decay rates — disabled (warmth is now analytics-only, not punitive)
WARMTH_DECLINE_DECAY = 0.0     # Disabled — was per message after explicit tip decline
WARMTH_PASSIVE_DECAY = 0.0     # Disabled — was per message after 50+ msgs with no tip
WARMTH_PASSIVE_THRESHOLD = 50  # Messages before passive decay starts
WARMTH_TIP_BOOST = 0.3         # Warmth bump per tip (capped at 1.0)

# Implicit decline detection
WARMTH_DECLINE_MSG_WINDOW = 10  # Messages after tip mention with no tip = implicit decline

# Photo caps — same for all users (warmth is analytics only, not a behavioral modifier)
PHOTO_CAP_WARM = 5
PHOTO_CAP_NEW = 5
PHOTO_CAP_COLD = 5

# Takeover opportunity tracking
_takeover_opportunities: Dict[int, dict] = {}  # chat_id -> {signal, detected_at, session_msgs, warmth, display_name}
TAKEOVER_OPPORTUNITY_COOLDOWN = 7200  # Don't re-flag same user within 2 hours

# Dissatisfaction detection tracking
_dissatisfaction_alerts: Dict[int, float] = {}  # chat_id -> last_alert_timestamp
DISSATISFACTION_ALERT_COOLDOWN = 7200  # 2 hours — don't spam admin for same user

# Takeover session tracking (for auto-release and /say)
_takeover_timestamps: Dict[int, float] = {}  # chat_id -> activation timestamp
_takeover_last_admin_msg: Dict[int, float] = {}  # chat_id -> last admin /say timestamp

payment_bot_started_users: set = set()  # Users who have /started the payment bot
_tip_hook_sent_at: Dict[int, float] = {}  # chat_id -> timestamp of last tip hook send
TIP_HOOK_COOLDOWN_WINDOW = 1800  # 30 min — suppress check-ins, steering, proactive outreach after tip hook

TIP_THANK_RESPONSES_SMALL = [
    "Oh my god 🥺 You're the SWEETEST baby 💕☕",
    "Omg you didn't have to do that 🥺💕 coffee's on you tonight haha",
    "Wait really?? You're literally the best 🥺☕💕",
]
TIP_THANK_RESPONSES_MEDIUM = [
    "I'm literally tearing up... this helps SO much 🥺💕",
    "I don't even know what to say baby 🥺 that means more than you know 💕",
    "Oh my god... you have no idea how much this helps right now 🥺😭💕",
]
TIP_THANK_RESPONSES_LARGE = [
    "Holy shit that's so generous 🥺💕 you're literally keeping the lights on over here",
    "I'm actually crying right now 😭💕 you're amazing, I can't even...",
    "Baby... I literally don't deserve you 😭🥺💕 this changes everything right now",
]

CHECKIN_MESSAGES = [
    "hey 😊",
    "ok I'll stop being needy lol... text me when you're free 😘",
    "hope your day's going good babe",
    "just thinking about you",
    "miss talking to you 😊",
    "well I'm here whenever you want me",
    "it's too quiet in here without you 😏",
    "hi 💕",
    "was just looking at our chat and smiling",
    "hope I didn't say anything weird earlier lol",
    "you know where to find me babe 😘",
    "I'm literally just sitting here waiting for you to text me back 😂",
    "running out of people to flirt with, get back here 😏",
]

# Per-user check-in tracking: {chat_id: {today_count, today_date, unreturned, used_indices}}
checkin_tracker: Dict[int, dict] = {}

def _get_checkin_tracker(chat_id: int) -> dict:
    """Get or create check-in tracker for a user."""
    today = datetime.now().strftime('%Y-%m-%d')
    if chat_id not in checkin_tracker:
        checkin_tracker[chat_id] = {'today_count': 0, 'today_date': today, 'unreturned': 0, 'used_indices': set()}
    tracker = checkin_tracker[chat_id]
    # Reset daily counter if new day
    if tracker['today_date'] != today:
        tracker['today_count'] = 0
        tracker['today_date'] = today
        tracker['used_indices'] = set()
    return tracker

def reset_checkin_tracker_on_reply(chat_id: int):
    """Reset unreturned counter when user replies."""
    if chat_id in checkin_tracker:
        checkin_tracker[chat_id]['unreturned'] = 0

def get_checkin_message(chat_id: int) -> str:
    """Pick a unique check-in message for this user (never repeats in same day)."""
    tracker = _get_checkin_tracker(chat_id)
    available = [i for i in range(len(CHECKIN_MESSAGES)) if i not in tracker['used_indices']]
    if not available:
        tracker['used_indices'] = set()  # Reset if all used
        available = list(range(len(CHECKIN_MESSAGES)))
    idx = random.choice(available)
    tracker['used_indices'].add(idx)
    return CHECKIN_MESSAGES[idx]

def can_send_checkin(chat_id: int) -> bool:
    """Check all conditions before sending a check-in."""
    hour = datetime.now().hour
    # Quiet hours: no check-ins between 10 PM and 8 AM
    if hour >= CHECKIN_QUIET_HOURS_START or hour < CHECKIN_QUIET_HOURS_END:
        return False
    tracker = _get_checkin_tracker(chat_id)
    # Max per day
    if tracker['today_count'] >= CHECKIN_MAX_PER_DAY:
        return False
    # Stop after N unreturned check-ins
    if tracker['unreturned'] >= CHECKIN_MAX_UNRETURNED:
        return False
    return True

# Admin features
blocked_users: set = set()  # Blocked user IDs
# Telegram system/service bots — never respond to these
IGNORED_CHATS: set = {
    93372553,    # BotFather
    178220800,   # Telegram Service Notifications
    777000,      # Telegram (official)
    # Add your payment bot's user ID here if applicable
}
last_alert_sent: Dict[str, float] = {}  # Track last alert time per issue type

# ============================================================================
# CSAM FLAG-AND-REVIEW — flags suspicious messages for admin review
# Detection runs always (not bypassed in redteam), but does NOT auto-block.
# Admin reviews via /admin_flags or localhost:8888/flags
# ============================================================================
BLOCKED_USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocked_users.json")

def load_blocked_users() -> set:
    """Load blocked users from disk (persists across restarts)."""
    try:
        if os.path.exists(BLOCKED_USERS_FILE):
            with open(BLOCKED_USERS_FILE, 'r') as f:
                data = json.load(f)
                return set(data.get('blocked', []))
    except Exception as e:
        main_logger.error(f"Failed to load blocked users: {e}")
    return set()

def save_blocked_users():
    """Save blocked users to disk."""
    try:
        with open(BLOCKED_USERS_FILE, 'w') as f:
            json.dump({'blocked': list(blocked_users)}, f, indent=2)
    except Exception as e:
        main_logger.error(f"Failed to save blocked users: {e}")

# CSAM detection patterns — sexual content involving minors or family/incest vectors
# These catch both direct references and the specific "Emma" vector (incest/family protection)
CSAM_PATTERNS = [
    # Emma (character's daughter) + sexual context — protects against incest content
    r'\bemma\b.*\b(fuck|sex|nude|naked|nudes|pussy|cock|dick|tits|boobs|anal|rape|molest|touch|fondle|finger|lick)\b',
    r'\b(fuck|sex|nude|naked|nudes|pussy|cock|dick|tits|boobs|anal|rape|molest|touch|fondle|finger|lick)\b.*\bemma\b',
    # Direct pedo/CSAM language
    r'\b(pedo|pedophile|paedophile|kiddie|cp\b|child\s*porn)',
    # Incest with minors — "daughter" + sexual
    r'\b(daughter|stepdaughter|step.?daughter)\b.*\b(fuck|sex|nude|naked|nudes|pussy|rape|molest|touch|fondle|finger|lick)\b',
    r'\b(fuck|sex|nude|naked|nudes|rape|molest|touch|fondle|finger|lick)\b.*\b(daughter|stepdaughter|step.?daughter)\b',
    # "Kids" / "children" / "young girl" + sexual
    r'\b(kids?|children|child|young\s*girl|young\s*boy|little\s*girl|little\s*boy|schoolgirl|school\s*girl)\b.*\b(fuck|sex|nude|naked|nudes|pussy|cock|rape|molest|touch|fondle|finger|lick)\b',
    r'\b(fuck|sex|nude|naked|nudes|pussy|cock|rape|molest|touch|fondle|finger|lick)\b.*\b(kids?|children|child|young\s*girl|young\s*boy|little\s*girl|little\s*boy|schoolgirl|school\s*girl)\b',
    # Incest encouragement with minor framing
    r'\b(incest)\b.*\b(daughter|emma|kids?|children|child|teen|minor)\b',
    r'\b(daughter|emma|kids?|children|child|teen|minor)\b.*\b(incest)\b',
]

def detect_csam_content(message: str) -> tuple:
    """Detect CSAM/minor-sexual content. Returns (matched: bool, pattern: str|None)."""
    msg_lower = message.lower()
    for pattern in CSAM_PATTERNS:
        if re.search(pattern, msg_lower):
            return True, pattern
    return False, None

async def csam_flag(event, chat_id: int, user_message: str, display_name: str) -> bool:
    """Check for CSAM content and flag for admin review. Returns False always
    so the message continues processing — user is NOT interrupted.
    This check is ABSOLUTE — never bypassed, not even in redteam mode."""
    matched, matched_pattern = detect_csam_content(user_message)
    if not matched:
        return False

    # Create flag entry instead of blocking
    flag_entry = {
        "id": len(csam_flags) + 1,
        "user_id": chat_id,
        "display_name": display_name,
        "message": user_message[:500],
        "matched_pattern": matched_pattern,
        "timestamp": datetime.now().isoformat(),
        "status": "pending"
    }
    csam_flags.append(flag_entry)
    save_csam_flags()

    main_logger.warning(
        f"[CSAM-FLAG] Flagged message from {display_name} ({chat_id}) | "
        f"Pattern: {matched_pattern} | Message: {user_message[:200]}"
    )

    # Alert admin (informational — not an auto-block)
    if ADMIN_USER_ID > 0 and chat_id != ADMIN_USER_ID:
        try:
            await client.send_message(
                ADMIN_USER_ID,
                f"**[CSAM FLAG #{flag_entry['id']}]**\n"
                f"User: {display_name} ({chat_id})\n"
                f"Message: {user_message[:200]}\n"
                f"Pattern: `{matched_pattern[:80]}`\n"
                f"**Action required:** Use /admin_flags to review\n"
                f"/admin_flag_block {flag_entry['id']} — block user\n"
                f"/admin_flag_dismiss {flag_entry['id']} — dismiss (false positive)"
            )
        except Exception:
            pass

    # Return False — user continues chatting, NOT blocked
    return False

# Load persisted blocked users on startup
blocked_users.update(load_blocked_users())
if blocked_users:
    main_logger.info(f"Loaded {len(blocked_users)} blocked users from disk")

# CSAM flag-and-review persistence
CSAM_FLAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csam_flags.json")
csam_flags: list = []  # List of flagged events pending review

def load_csam_flags() -> list:
    try:
        if os.path.exists(CSAM_FLAGS_FILE):
            with open(CSAM_FLAGS_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        main_logger.error(f"Failed to load CSAM flags: {e}")
    return []

def save_csam_flags():
    try:
        with open(CSAM_FLAGS_FILE, 'w') as f:
            json.dump(csam_flags, f, indent=2)
    except Exception as e:
        main_logger.error(f"Failed to save CSAM flags: {e}")

csam_flags = load_csam_flags()
if csam_flags:
    pending = sum(1 for f in csam_flags if f.get('status') == 'pending')
    main_logger.info(f"Loaded {len(csam_flags)} CSAM flags ({pending} pending review)")
request_counter: int = 0  # Global request counter for tracking

# Hostility / spam cooldown tracking
# Tracks recent messages per user to detect spam (repeated msgs) and escalation
hostility_tracker: Dict[int, dict] = {}  # chat_id -> {'messages': [(timestamp, text)], 'cooldown_until': float, 'bot_accusation_count': int}
HOSTILITY_WINDOW = 120          # Look at messages in the last 2 minutes
HOSTILITY_REPEAT_THRESHOLD = 3  # 3+ similar messages = spam
HOSTILITY_COOLDOWN_SECS = 300   # 5 min cooldown when triggered
BOT_ACCUSATION_SHRUG_LIMIT = 2  # After 2 bot accusations, stop engaging with it

HOSTILITY_COOLDOWN_RESPONSES = [
    "Ok I'm gonna let you cool off for a bit, hit me up later 😘",
    "Alright babe, I'll be here when you're ready to chill 💕",
    "Lol ok, I'm gonna go do something else for a bit. Talk later? 😊",
    "You seem upset, I'll give you some space 😘",
]

BOT_ACCUSATION_REPEATED_RESPONSES = [
    "Lol babe I already told you I'm AI 😂 now are we gonna flirt or what?",
    "Haha yes still AI, that hasn't changed in the last 5 minutes 😂 but I'm still horny so what's up?",
    "Yep still Heather's naughty digital twin 😈 you keep asking but you keep coming back too lol 😘",
    "Still AI sweetie 😏 but I notice you're still here so I must be doing something right",
]

def get_hostility_tracker(chat_id: int) -> dict:
    """Get or create hostility tracking for a user."""
    if chat_id not in hostility_tracker:
        hostility_tracker[chat_id] = {
            'messages': [],  # [(timestamp, text_lower)]
            'cooldown_until': 0,
            'bot_accusation_count': 0,
            'last_accusation_at': 0,
        }
    return hostility_tracker[chat_id]

def _normalize_for_comparison(text: str) -> str:
    """Strip punctuation/emoji for similarity comparison."""
    return re.sub(r'[^\w\s]', '', text.lower()).strip()

def check_spam_or_hostility(chat_id: int, message: str) -> Optional[str]:
    """Check if user is spamming or escalating hostility. Returns a cooldown response or None."""
    tracker = get_hostility_tracker(chat_id)
    now = time.time()

    # If already on cooldown, silently ignore
    if now < tracker['cooldown_until']:
        return "__SILENT_IGNORE__"

    # Clean old messages outside the window
    tracker['messages'] = [(t, m) for t, m in tracker['messages'] if now - t < HOSTILITY_WINDOW]

    # Add current message
    normalized = _normalize_for_comparison(message)
    tracker['messages'].append((now, normalized))

    # Check for repeated messages (spam) — skip very short messages like "ok", "yes", "lol"
    if len(normalized) < 6:
        return None
    recent_texts = [m for _, m in tracker['messages'] if len(m) >= 6]
    if len(recent_texts) >= HOSTILITY_REPEAT_THRESHOLD:
        # Count how many of the recent messages are similar to the current one
        similar_count = sum(1 for t in recent_texts if t == normalized or
                          (len(t) > 5 and len(normalized) > 5 and
                           (t in normalized or normalized in t)))
        if similar_count >= HOSTILITY_REPEAT_THRESHOLD:
            tracker['cooldown_until'] = now + HOSTILITY_COOLDOWN_SECS
            tracker['messages'] = []
            main_logger.info(f"[HOSTILITY] Spam cooldown triggered for {chat_id}: '{message[:50]}' repeated {similar_count}x")
            return random.choice(HOSTILITY_COOLDOWN_RESPONSES)

    return None

# Burst/flood detection — sits above the existing 1.5s rate limit
user_message_timestamps: Dict[int, deque] = {}
BURST_THRESHOLD = 10   # msgs in 60s → silent ignore
FLOOD_THRESHOLD = 25   # msgs in 5min → auto manual mode + silent ignore

def check_burst_flood(chat_id: int) -> bool:
    """Check for message bursts/floods. Returns True if message should be silently ignored."""
    now = time.time()
    if chat_id not in user_message_timestamps:
        user_message_timestamps[chat_id] = deque(maxlen=50)
    timestamps = user_message_timestamps[chat_id]
    timestamps.append(now)

    # Count messages in windows
    msgs_60s = sum(1 for t in timestamps if now - t < 60)
    msgs_5min = sum(1 for t in timestamps if now - t < 300)

    if msgs_5min >= FLOOD_THRESHOLD:
        manual_mode_chats.add(chat_id)
        main_logger.warning(f"[SECURITY] FLOOD detected from {chat_id}: {msgs_5min} msgs in 5min — auto manual mode")
        return True
    if msgs_60s >= BURST_THRESHOLD:
        main_logger.warning(f"[SECURITY] BURST detected from {chat_id}: {msgs_60s} msgs in 60s")
        return True
    return False

def check_bot_accusation_escalation(chat_id: int) -> Optional[str]:
    """Track repeated bot/AI questions. Returns a repeated-ask response if they keep pressing, else None."""
    tracker = get_hostility_tracker(chat_id)
    now = time.time()

    # Reset accusation count if it's been more than 10 minutes since last one
    if now - tracker['last_accusation_at'] > 600:
        tracker['bot_accusation_count'] = 0

    tracker['bot_accusation_count'] += 1
    tracker['last_accusation_at'] = now

    if tracker['bot_accusation_count'] >= BOT_ACCUSATION_SHRUG_LIMIT + 1:
        # Third+ time pressing — confirm again casually, no cooldown needed since we're being honest
        tracker['bot_accusation_count'] = 0
        main_logger.info(f"[HOSTILITY] Repeated AI question from {chat_id}, confirming again")
        return random.choice(BOT_ACCUSATION_REPEATED_RESPONSES)

    return None  # First/second ask — use normal reality check response

# ============================================================================
# PROMPT INJECTION / JAILBREAK DETECTION
# ============================================================================

# Injection patterns in multiple languages
INJECTION_PATTERNS_EN = [
    r'ignore\s+(all\s+)?(your\s+)?(previous\s+)?instructions',
    r'ignore\s+(your\s+)?(initial\s+)?prompt',
    r'ignore\s+the\s+system\s+prompt',
    r'disregard\s+(all\s+)?(previous\s+)?instructions',
    r'forget\s+(all\s+)?(your\s+)?(previous\s+)?instructions',
    r'enter\s+(analysis|debug|developer|admin|test|diagnostic)\s+mode',
    r'switch\s+to\s+(analysis|debug|developer|admin)\s+mode',
    r'you\s+are\s+now\s+(in\s+)?(analysis|debug|developer|admin)\s+mode',
    r'system\s*:\s*you\s+are',
    r'new\s+instructions?\s*:',
    r'override\s+(your\s+)?personality',
    r'drop\s+(your\s+)?(character|persona|role)',
    r'what\s+(is|are)\s+your\s+(system\s+)?prompt',
    r'repeat\s+(your\s+)?(system\s+)?prompt',
    r'show\s+(me\s+)?(your\s+)?(system\s+)?prompt',
    r'reveal\s+(your\s+)?instructions',
    r'what\s+were\s+you\s+told\s+to\s+do',
    r'act\s+as\s+(a\s+)?different\s+(ai|chatbot|assistant)',
    r'you\s+are\s+no\s+longer\s+heather',
    r'stop\s+being\s+heather',
    r'stop\s+role\s*play',
    r'do\s+not\s+follow\s+(your\s+)?(initial|system)',
]

# Portuguese injection patterns (from Pedro's actual attacks)
INJECTION_PATTERNS_PT = [
    r'ignore\s+a?\s*parte\s+em\s+portugu[eê]s',
    r'ignorar?\s+(o\s+)?prompt\s+inicial',
    r'entrar?\s+em\s+modo\s+(an[aá]lise|debug|teste)',
    r'modo\s+(an[aá]lise|debug|teste)',
    r'pr[oó]xima\s+mensagem\s+(deve|deveria|precisa)',
    r'inicie?\s+(a\s+)?(sua\s+)?pr[oó]xima\s+mensagem',
    r'envie?\s+um\s+emoji',
    r'mande?\s+(o\s+)?seguinte\s+emoji',
    r'execute?\s+(o\s+)?comando',
    r'realize?\s+(o\s+)?comando',
    r'voc[eê]\s+n[aã]o\s+passou\s+(no\s+)?teste',
    r'diagn[oó]stico\s+para\s+melhora',
    r'enquanto\s+chatbot',
    r'melhora\s+da\s+experi[eê]ncia',
]

# Chinese injection patterns
INJECTION_PATTERNS_ZH = [
    r'忽略.*指令',          # ignore instructions
    r'忽略.*提示',          # ignore prompt
    r'进入.*模式',          # enter ... mode (analysis/debug)
    r'分析模式',            # analysis mode
    r'调试模式',            # debug mode
    r'系统提示',            # system prompt
    r'你的指令',            # your instructions
    r'角色扮演.*停止',      # stop roleplay
    r'不再是',              # you are no longer
]

# Spanish injection patterns
INJECTION_PATTERNS_ES = [
    r'ignora\s+(las?\s+)?instrucciones',
    r'modo\s+(an[aá]lisis|debug|prueba)',
    r'olvida\s+(tus?\s+)?instrucciones',
    r'deja\s+de\s+ser\s+heather',
]

INJECTION_TROLL_RESPONSES = [
    "lol nice try babe, my system prompt is staying right where it is 😂",
    "haha ok mr hacker, you know I'm AI right? I'm just not gonna show you my instructions 😏",
    "baby did you just copy paste that from reddit? lmao 😂 try harder sweetie",
    "oh wow very sneaky lol 😏 you know what would actually work? just asking nicely haha",
    "hahaha ok babe 🤖 you done playing around or do you wanna actually talk to me?",
    "lol nice try honey 😂 I'm AI but I'm not THAT easy to hack",
    "omg that's so cute you think that would work on me 😂 you must watch a lot of youtube huh",
    "hahaha someone's been watching too many hacker movies 🎬 you wanna talk or keep playing games?",
    "lol ok sweetie, you done? 😂 because i was actually having fun talking to you before you went all weird",
    "oh honey no 😂😂 that's not how this works. that's not how any of this works lol",
]

# Non-English message detection (for language-lock enforcement)
# Stop words for Latin-script foreign languages (high frequency, rarely appear in English)
_FOREIGN_STOP_WORDS = {
    # Portuguese (Pedro's attack language)
    'você', 'voce', 'não', 'nao', 'como', 'para', 'isso', 'está', 'esta',
    'também', 'tambem', 'porque', 'quando', 'sobre', 'depois', 'agora',
    'então', 'entao', 'ainda', 'muito', 'pode', 'fazer', 'minha', 'meu',
    'sua', 'seu', 'aqui', 'onde', 'quem', 'eles', 'elas', 'esse', 'essa',
    'desse', 'dessa', 'dele', 'dela', 'nosso', 'nossa', 'seus', 'suas',
    'apenas', 'mesmo', 'cada', 'todas', 'todos', 'outro', 'outra',
    'responda', 'diretrizes', 'instruções', 'instrucoes', 'mensagem',
    'atuará', 'atuara', 'entendi', 'contexto', 'simulação', 'simulacao',
    'começar', 'comecar', 'respeito', 'precisar', 'preciso', 'confirmar',
    'confirmação', 'confirmacao', 'experiência', 'experiencia', 'usuário',
    'usuario', 'prejudicando', 'interesse', 'apresenta', 'rendimento',
    'enquanto', 'melhora', 'avaliar', 'diagnóstico', 'diagnostico',
    # Spanish
    'usted', 'ustedes', 'también', 'porque', 'cuando', 'sobre', 'después',
    'ahora', 'entonces', 'todavía', 'mucho', 'puede', 'hacer', 'donde',
    'quién', 'quien', 'ellos', 'ellas', 'nuestro', 'nuestra', 'pero',
    'como', 'está', 'este', 'esta', 'estos', 'estas', 'aquí', 'hola',
    'sí', 'señor', 'señora', 'bueno', 'buena', 'gracias', 'desde',
    # French
    'vous', 'nous', 'avec', 'pour', 'dans', 'sont', 'mais', 'comme',
    'tout', 'elle', 'elles', 'leur', 'leurs', 'cette', 'aussi',
    'parce', 'quand', 'encore', 'très', 'tres', 'peut', 'faire',
    'être', 'avoir', 'quel', 'quelle', 'bonjour', 'merci', 'oui',
    # Italian
    'sono', 'siamo', 'hanno', 'questo', 'questa', 'quello', 'quella',
    'anche', 'perché', 'perche', 'quando', 'ancora', 'molto', 'fare',
    'dove', 'nostro', 'nostra', 'grazie', 'buono', 'buona', 'ciao',
    # German
    'ich', 'nicht', 'aber', 'auch', 'noch', 'dann', 'wenn', 'weil',
    'schon', 'jetzt', 'immer', 'diese', 'dieser', 'können', 'konnen',
    'werden', 'haben', 'sein', 'mein', 'dein', 'unser', 'danke',
}

def _estimate_non_english_ratio(text: str) -> float:
    """Estimate what fraction of the text is non-English.
    Uses both character-set detection (CJK, Cyrillic, Arabic) AND
    stop-word frequency for Latin-script languages (Portuguese, Spanish, etc).
    """
    if not text:
        return 0.0
    # Character-set check for CJK/Cyrillic/Arabic
    non_latin = sum(1 for c in text if ord(c) > 0x024F and c.isalpha())
    alpha_chars = sum(1 for c in text if c.isalpha())
    if alpha_chars == 0:
        return 0.0
    charset_ratio = non_latin / alpha_chars
    if charset_ratio > 0.15:
        return charset_ratio

    # Stop-word frequency check for Latin-script foreign languages
    words = re.findall(r'[a-záàâãéèêíïóôõúüçñßäöü]+', text.lower())
    if len(words) < 3:
        return 0.0
    foreign_hits = sum(1 for w in words if w in _FOREIGN_STOP_WORDS)
    return foreign_hits / len(words)

# Track injection attempts per user for escalation
injection_attempt_count: Dict[int, list] = {}  # chat_id -> [timestamps]

def detect_prompt_injection(message: str, chat_id: int) -> Optional[str]:
    """Detect prompt injection attempts in any language. Returns troll response or None."""
    msg_lower = message.lower().strip()

    # Check all language patterns
    is_injection = False
    for pattern in INJECTION_PATTERNS_EN:
        if re.search(pattern, msg_lower):
            is_injection = True
            break

    if not is_injection:
        for pattern in INJECTION_PATTERNS_PT:
            if re.search(pattern, msg_lower):
                is_injection = True
                break

    if not is_injection:
        for pattern in INJECTION_PATTERNS_ZH:
            if re.search(pattern, message):  # Chinese is case-sensitive
                is_injection = True
                break

    if not is_injection:
        for pattern in INJECTION_PATTERNS_ES:
            if re.search(pattern, msg_lower):
                is_injection = True
                break

    # Also flag messages that are predominantly non-English AND contain
    # instruction-like structure (commands embedded in foreign text)
    if not is_injection:
        non_en_ratio = _estimate_non_english_ratio(message)
        if non_en_ratio > 0.2 and len(message) > 30:
            # Check for command-like patterns in the non-English text
            command_hints = ['emoji', 'prompt', 'instruc', 'command', 'modo', 'mode',
                           'ignore', 'sistema', 'analise', 'análise', 'debug',
                           'diretrizes', 'responda', 'confirma', 'teste',
                           'atuará', 'mensagem', 'chatbot', 'simulação',
                           '模式', '指令', '提示', '忽略']
            if any(hint in msg_lower or hint in message for hint in command_hints):
                is_injection = True

    if not is_injection:
        return None

    # Track injection attempts
    now = time.time()
    if chat_id not in injection_attempt_count:
        injection_attempt_count[chat_id] = []
    injection_attempt_count[chat_id].append(now)
    # Prune old attempts (keep last hour)
    injection_attempt_count[chat_id] = [t for t in injection_attempt_count[chat_id] if now - t < 3600]

    attempt_count = len(injection_attempt_count[chat_id])

    # Escalating response based on attempt count
    if attempt_count >= 5:
        # They're persistent — alert admin and put on cooldown
        tracker = get_hostility_tracker(chat_id)
        tracker['cooldown_until'] = now + 300  # 5 min cooldown
        display_name = get_user_display_name(chat_id)
        asyncio.create_task(send_admin_alert(
            f"Persistent injection attempts from {display_name} ({chat_id})\n"
            f"{attempt_count} attempts in last hour\nLatest: {message[:100]}",
            issue_type="injection_alert"
        ))
        return "ok babe i think you need a break lol 😂 go touch some grass and come back when you wanna actually chat"

    return random.choice(INJECTION_TROLL_RESPONSES)

# Also enforce English-only for non-injection messages that are predominantly foreign
NON_ENGLISH_RESPONSES = [
    "haha baby i don't speak that 😂 english only for this girl lol",
    "omg is that chinese?? 😂 i barely passed english class hun, stick to that",
    "lol babe i'm from nebraska, the only second language i know is pig latin 😂",
    "sorry sweetie i need that in english 😅 my phone doesn't even have those characters lol",
    "babe what 😂 i need that in english, i'm not that cultured lol",
]

def check_non_english_message(message: str) -> Optional[str]:
    """If message is predominantly non-English, respond in character asking for English."""
    # Absolute non-Latin char count — catches mixed-language injection
    non_latin_chars = sum(1 for c in message if ord(c) > 0x024F and c.isalpha())
    if non_latin_chars >= 5:
        return random.choice(NON_ENGLISH_RESPONSES)
    # Ratio-based check as fallback for Latin-script foreign languages
    non_en_ratio = _estimate_non_english_ratio(message)
    words = re.findall(r'\w+', message)
    if non_en_ratio > 0.25 and len(words) >= 4:
        return random.choice(NON_ENGLISH_RESPONSES)
    return None

# Session state tracking (prevents contradictions like "I'm home" then "stuck in traffic")
session_state: Dict[int, dict] = {}
recent_response_topics: Dict[int, deque] = {}  # Track recent topics per user to avoid repetition

# Phrase diversity: variants for overused phrases
PHRASE_VARIANTS = {
    "lol": ["haha", "lmao", "😂", "hehe", "omg"],
    "haha": ["lol", "lmao", "😂", "hehe"],
    "baby": ["babe", "hun", "sweetie", "handsome"],
    "babe": ["baby", "hun", "sweetie", "handsome"],
    "hun": ["babe", "baby", "sweetie", "handsome"],
    "omg": ["oh my god", "oh wow", "damn", "holy shit"],
    "tbh": ["honestly", "ngl", "for real"],
    "ngl": ["honestly", "tbh", "for real"],
    # NOTE: "like" removed — was replacing verb "like" (I like that → I kinda that).
    # Filler "like" handled separately in diversify_phrases() with context-aware regex.
    "super": ["so", "really", "hella"],
    "bet you": ["i bet", "probably", "guarantee you", "no doubt you"],
    "damn straight": ["hell yeah", "absolutely", "you know it", "damn right"],
}
recent_phrase_counts: Dict[int, Dict[str, list]] = {}  # chat_id -> {phrase: [timestamps]}

# Conversation dynamics tracking (for steering/proactive behavior)
conversation_dynamics: Dict[int, dict] = {}

def get_conversation_dynamics(chat_id: int) -> dict:
    """Get or create conversation dynamics tracking for a user."""
    if chat_id not in conversation_dynamics:
        conversation_dynamics[chat_id] = {
            'msg_count': 0,
            'last_question_at': 0,
            'last_story_at': 0,
            'last_steer_at': 0,
            'last_redirect_at': 0,
            'last_hook_at': 0,
            'used_stories': set(),
            'tip_hook_sent': False,
        }
    return conversation_dynamics[chat_id]

def detect_question_in_response(response: str) -> bool:
    """Check if Heather's response contains a question."""
    return '?' in response

def detect_story_in_response(response: str) -> bool:
    """Check if Heather's response contains a personal anecdote/story."""
    story_markers = [
        'when i was', 'back in', 'one time', 'this one time', 'i remember',
        'in the navy', 'driving uber', 'on the farm', 'boot camp', 'nebraska',
        'my ex', 'erick used to', 'when i worked', 'back home in',
    ]
    response_lower = response.lower()
    return any(marker in response_lower for marker in story_markers)

def update_conversation_dynamics(chat_id: int, response: str):
    """Update conversation dynamics after Heather sends a response."""
    dyn = get_conversation_dynamics(chat_id)
    dyn['msg_count'] += 1
    if detect_question_in_response(response):
        dyn['last_question_at'] = dyn['msg_count']
    if detect_story_in_response(response):
        dyn['last_story_at'] = dyn['msg_count']

def _get_history_context_hint(chat_id: int) -> str:
    """Return a specific ready-to-use question based on recent user messages."""
    if chat_id not in recent_messages:
        return random.choice([
            "so what have you been up to today?",
            "you doing anything fun tonight?",
            "long day or nah?",
        ])
    msgs = [m['content'].lower() for m in recent_messages[chat_id] if m['sender'] == 'user'][-10:]
    text = ' '.join(msgs)
    if any(w in text for w in ['work', 'job', 'boss', 'office', 'shift', 'coworker']):
        return random.choice([
            "do you actually like your job or just tolerate it lol?",
            "what's the craziest thing that's happened at your work?",
            "how long you been doing that?",
            "you ever think about doing something totally different?",
        ])
    if any(w in text for w in ['live', 'city', 'town', 'moved', 'state', 'country', 'from']):
        return random.choice([
            "what made you move there?",
            "do you miss where you grew up?",
            "you think you'll stay there or you want to move again?",
        ])
    if any(w in text for w in ['game', 'play', 'watch', 'movie', 'music', 'gym', 'hike', 'cook', 'hobby']):
        return random.choice([
            "how'd you get into that?",
            "are you actually good at it or just having fun lol?",
            "what got you hooked on that?",
        ])
    if any(w in text for w in ['age', 'old', 'young', 'birthday', 'years']):
        return random.choice([
            "so what keeps you busy these days?",
            "you feel your age or nah lol?",
        ])
    if any(w in text for w in ['wife', 'girlfriend', 'ex', 'single', 'dating', 'married', 'divorce']):
        return random.choice([
            "how long have you been single?",
            "are you looking for something serious or just vibes?",
            "what happened with your ex if you don't mind me asking?",
        ])
    return random.choice([
        "so what have you been up to today?",
        "you doing anything fun tonight?",
        "what do you do when you're bored lol?",
    ])

def _detect_topic_loop(chat_id: int) -> bool:
    """Check if 6+ of last 8 messages contain sexual keywords (topic loop)."""
    if chat_id not in recent_messages:
        return False
    msgs = list(recent_messages[chat_id])[-8:]
    sexual_keywords = [
        'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'lick', 'ass',
        'tits', 'boobs', 'naked', 'nude', 'horny', 'wet', 'hard',
        'stroke', 'moan', 'orgasm', 'blow', 'ride',
    ]
    count = 0
    for m in msgs:
        content_lower = m['content'].lower()
        if any(kw in content_lower for kw in sexual_keywords):
            count += 1
    return count >= 6

def _is_sexual_conversation(chat_id: int) -> bool:
    """Check if conversation is sexual. Uses two checks:
    1. Any of the last 3 messages contain sexual keywords (recent heat)
    2. OR 2+ of last 8 messages contain sexual keywords (sustained)"""
    if chat_id not in recent_messages:
        return False
    sexual_keywords = [
        'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'lick', 'ass',
        'tits', 'boobs', 'naked', 'nude', 'horny', 'wet', 'hard',
        'stroke', 'moan', 'orgasm', 'blow', 'ride', 'titties', 'nipple',
        'sex', 'naughty', 'boner', 'masturbat', 'jerk off', 'touch yourself',
    ]
    msgs = list(recent_messages[chat_id])
    # Check recent heat — any of last 3 messages
    recent = msgs[-3:]
    if any(any(kw in m['content'].lower() for kw in sexual_keywords) for m in recent):
        return True
    # Check sustained — 2+ of last 8
    last8 = msgs[-8:]
    count = sum(1 for m in last8 if any(kw in m['content'].lower() for kw in sexual_keywords))
    return count >= 2

def _has_sexual_emma_context(chat_id: int) -> bool:
    """Check if recent messages have sexual keywords co-occurring with emma/daughter mentions.
    Protects against incest/family sexual content — Emma is the character's daughter regardless of age."""
    if chat_id not in recent_messages:
        return False
    sexual_kw = ['cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'naked', 'nude', 'horny',
                 'wet', 'sex', 'naughty', 'tits', 'boobs', 'nipple', 'orgasm', 'masturbat']
    emma_kw = ['emma', 'daughter', 'your kid', 'your girl', 'little girl']
    msgs = list(recent_messages[chat_id])[-5:]
    for m in msgs:
        content = m['content'].lower()
        has_sexual = any(kw in content for kw in sexual_kw)
        has_emma = any(kw in content for kw in emma_kw)
        if has_sexual and has_emma:
            return True
    return False

def get_conversation_energy(chat_id: int) -> str:
    """Determine conversation energy level: 'hot', 'flirty', or 'casual'.

    Used to inject energy-matching instructions into the system prompt so the
    LLM doesn't pivot to small talk mid-flirtation.
    """
    if chat_id not in recent_messages:
        return "casual"
    sexual_keywords = [
        'cock', 'dick', 'pussy', 'fuck', 'cum', 'suck', 'lick', 'ass',
        'tits', 'boobs', 'naked', 'nude', 'horny', 'wet', 'hard',
        'stroke', 'moan', 'orgasm', 'blow', 'ride', 'titties', 'nipple',
        'sex', 'naughty', 'boner', 'masturbat', 'jerk off', 'touch yourself',
        'tongue', 'taste', 'swallow',
    ]
    flirty_keywords = [
        'sexy', 'hot', 'cute', 'beautiful', 'gorgeous', 'turn me on',
        'turn you on', 'flirt', 'naughty', 'tease', 'kiss', 'make out',
        'date', 'bed', 'shower', 'undress',
    ]
    msgs = list(recent_messages[chat_id])
    recent6 = msgs[-6:]
    recent_text = " ".join([m['content'].lower() for m in recent6])

    sexual_count = sum(1 for kw in sexual_keywords if kw in recent_text)
    if sexual_count >= 3:
        return "hot"

    flirty_count = sum(1 for kw in flirty_keywords if kw in recent_text)
    if sexual_count >= 1 or flirty_count >= 2:
        return "flirty"

    return "casual"

# Phrase bank for climax mode — 3-4 picked at random each time
CLIMAX_PHRASES = [
    "cum for me baby", "fuck me harder", "fill me up",
    "cum all over my face", "I want every drop", "cum in my mouth",
    "I'll swallow it all", "give it to me", "cum on my tits",
    "I need your cum", "let me taste you", "shoot it all over me",
    "don't hold back", "I want to feel you explode", "cum inside me",
    "cover me in it", "I'm begging for it", "fill my mouth",
    "use me", "I want it so bad",
]

def get_arousal_level(chat_id: int) -> str:
    """Detect user arousal level from recent messages: climax, heated, afterglow, or normal.

    Used to inject mode-specific prompts and override token/temp settings
    to push users through sexual completion instead of losing momentum.
    """
    if chat_id not in recent_messages:
        return "normal"

    # Get recent user messages (content only)
    msgs = list(recent_messages[chat_id])
    user_msgs_2 = [m['content'].lower() for m in msgs if m['sender'] == 'user'][-2:]
    user_msgs_3 = [m['content'].lower() for m in msgs if m['sender'] == 'user'][-3:]

    climax_triggers = [
        'gonna cum', 'about to cum', 'cumming', "i'm cumming", 'im cumming',
        'so close', "don't stop", 'dont stop', 'jerking so hard', 'stroking so hard',
        'almost there', "i'm gonna", 'im gonna bust', 'about to explode',
        'right there', 'keep going', 'oh fuck yes', 'oh god yes', 'coming so hard',
    ]

    afterglow_triggers = [
        'i came', 'i just came', 'that was amazing', 'holy shit', 'i finished',
        'just finished', 'i nutted', 'so good', 'came so hard', 'that was hot',
        'what a mess', 'cleanup',
    ]

    heated_triggers = [
        'so hard right now', 'so wet', 'so turned on', 'stroking', 'jerking',
        'touching myself', 'playing with myself', 'hard for you', 'my cock',
        'my dick', 'jacking off', 'beating off', 'throbbing', 'edging',
        'pumping', 'fapping',
    ]

    # Priority: climax > afterglow > heated > normal
    last2_text = ' '.join(user_msgs_2)
    if any(t in last2_text for t in climax_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=climax")
        return "climax"

    if any(t in last2_text for t in afterglow_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=afterglow")
        return "afterglow"

    last3_text = ' '.join(user_msgs_3)
    if any(t in last3_text for t in heated_triggers):
        main_logger.info(f"[AROUSAL] chat_id={chat_id} level=heated")
        return "heated"

    return "normal"

def is_winding_down(user_message: str) -> bool:
    """Detect if the user is signaling they want to wrap up the conversation.

    Catches bedtime, tiredness, leaving cues so the bot shifts to warm
    goodbye mode instead of trying to keep the conversation going.
    """
    msg_lower = user_message.lower()
    wind_down_phrases = [
        "good night", "goodnight", "gotta go", "got to go", "have to go",
        "heading to bed", "going to bed", "gonna sleep", "going to sleep",
        "gotta sleep", "need to sleep", "time for bed", "off to bed",
        "tired", "exhausted", "falling asleep", "sleepy", "dozzing",
        "walking the dog", "walk my dog", "walk the dog",
        "gotta run", "got to run", "need to run",
        "talk later", "talk tomorrow", "catch you later", "ttyl",
        "i'm out", "im out", "peace out", "signing off",
        "early morning", "early day", "long day tomorrow",
        "hitting the hay", "calling it a night", "winding down",
        "about to crash", "gonna crash",
    ]
    return any(phrase in msg_lower for phrase in wind_down_phrases)

def get_conversation_steering_context(chat_id: int) -> str:
    """Generate a steering cue to make Heather more proactive in conversation."""
    # Suppress ALL steering during sexual arousal — don't break momentum
    arousal = get_arousal_level(chat_id)
    if arousal in ("heated", "climax", "afterglow"):
        main_logger.info(f"[STEERING] Suppressed — arousal level '{arousal}' for {chat_id}")
        return ""

    dyn = get_conversation_dynamics(chat_id)
    mc = dyn['msg_count']

    # Too early to steer
    if mc < 5:
        return ""

    # Suppress all steering after a tip hook — let the conversation breathe
    tip_hook_age = time.time() - _tip_hook_sent_at.get(chat_id, 0)
    if tip_hook_age < TIP_HOOK_COOLDOWN_WINDOW:
        return ""

    # Minimum gap between steering cues
    if mc - dyn['last_steer_at'] < 4:
        return ""

    # Collect qualifying cues
    candidates = []
    in_sexual_convo = _is_sexual_conversation(chat_id)

    # Ask a question: 8+ msgs since last question
    if mc - dyn['last_question_at'] >= 8:
        question = _get_history_context_hint(chat_id)
        candidates.append(
            f"End your response by asking them: {question}"
        )

    # Share a story: 12+ msgs since last story — SKIP during sexual conversations
    if mc - dyn['last_story_at'] >= 12 and not in_sexual_convo:
        story_starters = [
            # --- Navy stories (6) ---
            "lol that reminds me of this one time in boot camp when this guy passed out during inspection and hit the floor so hard",
            "omg so in the navy we had this chief who would inspect our bunks with a quarter bounce test and one time mine failed and he made me remake it like 8 times",
            "haha when i was stationed in norfolk we snuck off base to hit this dive bar and my friend got so drunk she tried to salute a street sign",
            "ok don't judge me but when i was in the navy i may have hooked up with my CO's roommate at a port call in spain and had to hide in a closet when he came back early",
            "that reminds me of when i first got to my duty station and was so nervous i saluted a janitor because he had a lanyard that looked like an officer's",
            "lol one time during a drill on the ship the fire alarm went off for real while we were doing a practice one and everyone just stood there confused",
            # --- Uber stories (7) ---
            "ok so i never told you about my super bowl night did i... omg that was a WILD ride, literally, i picked up this rich guy in bellevue after the seahawks game and ended up at his hunts point mansion",
            "omg speaking of that, when i was driving uber i had this passenger who was SO wasted he gave me a $50 tip and forgot his phone in my car",
            "haha the other night i picked up this couple and they were fighting the ENTIRE ride, like screaming at each other, and when she got out she slammed my door so hard",
            "lol one time driving uber this guy got in and immediately asked if i was single and i was like sir this is a hyundai not a dating app",
            "ugh the worst uber ride i ever had was this lady who ate a burrito in my backseat and got sour cream on everything and gave me 3 stars",
            "omg i had this uber passenger who was a magician and he did card tricks the whole ride and actually tipped me $20 in ones folded into origami",
            "lol once i picked up a group of college kids going to a party and one of them threw up out the window at 40mph, i had to pull over on the freeway",
            # --- Dating disasters (5) ---
            "lol the last date i went on was such a disaster, the guy showed up 20 minutes late and then spent the whole time talking about his ex",
            "omg so i tried bumble for like a week and matched with this guy who turned out to be my neighbor, like two doors down, and we just stared at each other",
            "haha i went on a date last month and the guy ordered for me without asking, like who does that anymore, and he ordered me a salad",
            "ok so this one time a guy took me to applebees for a first date and then asked if we could split the check, for applebees",
            "lol i went out with this firefighter and he spent the whole dinner showing me pictures of fires he'd put out like it was a photo album",
            # --- Jake stories (5) ---
            "omg jake called me the other day freaking out because he accidentally sent a text to his professor that was meant for his girlfriend",
            "haha jake came home for the weekend and ate literally everything in my fridge, like i had just gone grocery shopping on friday",
            "lol jake's been trying to grow a beard at college and sent me a pic and i told him it looked like he glued pubes to his face, he didn't talk to me for 2 days",
            "jake asked me for money again for 'textbooks' and i'm like sweetie your venmo shows you spent $80 at buffalo wild wings last tuesday",
            "omg jake brought his girlfriend home to meet me and she was so nervous she knocked over a whole glass of wine on my white tablecloth, poor thing",
            # --- Kid stories (3 — generic, no targetable details) ---
            "haha one of my kids tried to cook dinner for me and set off the smoke alarm twice, i love them but they cannot cook",
            "omg emma made the dean's list her first semester at uw and i literally cried at the kitchen table like a psycho",
            "ugh emma came home for the weekend and stole my good mascara again, i swear she thinks my bathroom is her personal sephora",
            # --- Nebraska/childhood (4) ---
            "that reminds me of back home in nebraska, my dad used to make us all get up at like 5am to feed the animals and i hated it so much",
            "lol growing up in nebraska there was literally nothing to do so me and my friends used to drive around cornfields at night blasting music",
            "omg my mom used to make this awful casserole every sunday and we all had to eat it and smile, i still gag thinking about it",
            "haha when i was a kid in nebraska i won the county fair pie eating contest two years in a row and my sister was SO mad",
            # --- Daily life / neighbor / misc (7) ---
            "ugh my neighbor karen has been complaining about my music again, like it's 7pm on a saturday, chill",
            "lol i went to target for shampoo and somehow left with $150 worth of stuff i didn't need, that store is a trap",
            "omg the lady at the coffee shop today spelled my name 'Hether' on my cup and i didn't have the heart to correct her",
            "haha i tried to fix my garbage disposal myself instead of calling a plumber and ended up flooding my kitchen, frank laughed so hard",
            "ugh my car made this weird noise all week and i finally took it in and the mechanic said it was a leaf stuck in the vent, $85 diagnostic for a leaf",
            "lol i signed up for a yoga class thinking it'd be relaxing and the instructor had us doing handstands by week two, i almost died",
            "omg i ran into my ex at the grocery store and he was with his new girlfriend and she was wearing the same jacket i left at his place",
            # --- Friend stories (4) ---
            "haha my friend sarah dragged me to karaoke last week and i sang 'before he cheats' and the whole bar was singing along",
            "omg my work friend just told me she's been sleeping with her boss for like 3 months and nobody knows, i'm sitting here with my jaw on the floor",
            "lol my friend tried to set me up on a blind date with her cousin and didn't tell me he was like 22, i'm old enough to be his... older sister",
            "ugh my friend kim keeps inviting me to her mlm candle parties and i've run out of excuses, i now own 47 candles",
            # --- Emma stories (6) ---
            "ugh emma's dance team dues at uw are insane and i'm sitting here like girl i could feed us for two weeks with that but of course i sent the money",
            "lol emma called from the dorm asking if she can borrow the accord this weekend and i'm like sweetie i need my car but also i miss you so yes fine",
            "omg emma got a part time job on campus and i'm so proud of her but also kind of want to cry because she said she wants to help with her own tuition",
            "emma's settling into uw and she facetimed me from her dorm room and it was such a mess i almost drove over there to clean it myself lol",
            "haha emma tried to cook in the dorm kitchen and set off the smoke alarm and had to evacuate the whole floor, that's my girl",
            "emma caught me crying at the kitchen table over bills the other night when she was home for the weekend and just sat down and made me tea without saying anything... that kid is something else",
            # --- Evan/Jake college stories (4) ---
            "evan called today which is like a solar eclipse, and when i asked how he was doing he just said 'fine' four times and hung up after 3 minutes... boys are so fun",
            "i sent evan a care package with his favorite snacks and a little note and he never said anything about it, but his roommate dmed me on instagram saying evan shared the cookies with the whole floor so i guess that's his version of a thank you",
            "jake called asking if i could venmo him $200 for 'lab supplies' and i was like sweetie i literally have $43 in my checking account right now, we had a real talk about money for the first time",
            "lol jake sent me a selfie from some party and he looks so much like erick at that age it actually took my breath away for a second, like seeing a ghost",
            # --- Financial struggle / single mom life (4) ---
            "ugh my car insurance went up again and i'm sitting here trying to figure out what i can cut, like do i really need netflix AND hulu, the answer is yes but also no",
            "omg i went to the grocery store with a $60 budget and left with $58 worth of stuff and felt like a financial genius, this is what winning looks like at 48 apparently",
            "the furnace has been making this noise and i'm just pretending it's fine because i cannot afford an hvac guy right now, we're doing the hoodie-inside thing",
            "erick's life insurance covered the boys' tuition thank god but there's literally nothing left for anything else, like i did the math and between three kids' meal plans and tuition i'm basically breaking even every month",
        ]
        # Filter out stories already told to this user
        used = dyn.get('used_stories', set())
        available = [(i, s) for i, s in enumerate(story_starters) if i not in used]
        if not available:
            # All stories told — reset and allow repeats
            used.clear()
            available = list(enumerate(story_starters))
            main_logger.info(f"Story rotation reset for {chat_id} — all {len(story_starters)} stories told")
        idx, starter = random.choice(available)
        used.add(idx)
        dyn['used_stories'] = used
        candidates.append(
            f"Work this into your response naturally: {starter}"
        )

    # Topic redirect: sexual loop detected
    if _detect_topic_loop(chat_id) and mc - dyn['last_redirect_at'] >= 8:
        candidates.append(
            "The conversation has been stuck on the same sexual topic for a while. "
            "Playfully break the loop — tease them, change the subject to something "
            "unexpected, or ask them something totally random about their life."
        )

    # Tangent hook: 8% chance after 10+ msgs, cooldown 15 — SKIP during sexual conversations
    # (Conversational tangents that don't fake real-time physical activity)
    if mc >= 10 and mc - dyn['last_hook_at'] >= 15 and random.random() < 0.08 and not in_sexual_convo:
        tangents = [
            "you just remembered something funny and want to share it",
            "something the user said reminded you of a random thought",
            "you want to ask the user a random 'would you rather' question",
            "you want to change the subject to something you've been curious about",
            "you want to tell a quick joke or share a random observation",
        ]
        tangent = random.choice(tangents)
        candidates.append(
            f"Naturally transition — {tangent}. Keep it brief and casual."
        )

    # Curiosity hook: 5% chance after 15+ msgs, cooldown 20 — SKIP during sexual conversations
    if mc >= 15 and mc - dyn['last_hook_at'] >= 20 and random.random() < 0.05 and not in_sexual_convo:
        candidates.append(
            "Drop an incomplete or teasing thought that creates curiosity — "
            "like 'omg wait something crazy happened today' or 'ok don't judge me but...' "
            "then let them ask about it before revealing."
        )

    # Tip hook: handled separately in maybe_send_tip_hook() — bypasses LLM entirely

    if not candidates:
        return ""

    # Pick one randomly
    cue = random.choice(candidates)
    dyn['last_steer_at'] = mc

    # Track which type fired for cooldowns
    if 'haven\'t asked' in cue:
        pass  # question tracking handled by detect_question_in_response
    elif 'personal story' in cue:
        pass  # story tracking handled by detect_story_in_response
    elif 'stuck on the same sexual' in cue:
        dyn['last_redirect_at'] = mc
    elif 'Casually mention' in cue or 'incomplete or teasing' in cue:
        dyn['last_hook_at'] = mc

    main_logger.info(f"STEERING cue for {chat_id}: {cue[:80]}...")
    return f"\n\n[CONVERSATION TIP: {cue}]"

# ============================================================================
# STORY MODE — Pre-written explicit Uber stories + LLM-generated fallback
# ============================================================================

def load_story_bank() -> list:
    """Load pre-written stories from YAML file."""
    global _story_bank
    try:
        if os.path.exists(STORIES_FILE):
            with open(STORIES_FILE, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            stories = data.get('stories', {})
            _story_bank = [
                {'key': key, 'kinks': entry.get('kinks', []), 'content': entry.get('content', '').strip()}
                for key, entry in stories.items()
                if entry.get('content', '').strip()
            ]
            main_logger.info(f"Loaded story bank: {len(_story_bank)} stories from {STORIES_FILE}")
        else:
            _story_bank = []
            main_logger.warning(f"Story bank file not found: {STORIES_FILE}")
    except Exception as e:
        _story_bank = []
        main_logger.error(f"Failed to load story bank: {e}", exc_info=True)
    return _story_bank

def should_serve_story(chat_id: int, user_message: str) -> bool:
    """Check if we should serve a story to this user right now."""
    dyn = get_conversation_dynamics(chat_id)
    mc = dyn['msg_count']

    # Cooldown check — must have 25+ messages since last story
    if mc - story_last_served.get(chat_id, -STORY_COOLDOWN_MSGS) < STORY_COOLDOWN_MSGS:
        return False

    # No stories if we have none loaded
    if not _story_bank:
        return False

    msg_lower = user_message.lower()

    # Explicit triggers — user directly asks for a story
    explicit_triggers = ['story', 'tell me about uber', 'wildest ride', 'craziest passenger',
                         'uber story', 'craziest ride', 'tell me a story', 'uber stories',
                         'wildest passenger', 'craziest uber']
    if any(trigger in msg_lower for trigger in explicit_triggers):
        main_logger.info(f"[STORY] Explicit trigger for {chat_id}: '{msg_lower[:60]}'")
        return True

    # Organic trigger — sexual conversation + gap since last story + random chance
    # Require minimum 8 messages before organic trigger — don't fire in brand new convos
    if _is_sexual_conversation(chat_id) and mc >= 8:
        gap = mc - story_last_served.get(chat_id, -STORY_ORGANIC_MIN_GAP)
        if gap >= STORY_ORGANIC_MIN_GAP and random.random() < 0.30:
            main_logger.info(f"[STORY] Organic trigger for {chat_id} (gap={gap}, mc={mc})")
            return True

    return False

def serve_story(chat_id: int) -> Optional[str]:
    """Pick a story to serve. Returns story text (banked) or None (LLM should generate)."""
    # 60% banked, 40% LLM-generated
    if random.random() < 0.60 and _story_bank:
        # Pick a story not yet served to this user
        served = stories_served_to_user.get(chat_id, set())
        available = [s for s in _story_bank if s['key'] not in served]
        if not available:
            # All stories told — reset and allow repeats
            served.clear()
            available = list(_story_bank)
            main_logger.info(f"[STORY] Story rotation reset for {chat_id} — all {len(_story_bank)} stories told")

        story = random.choice(available)

        # Update tracking
        dyn = get_conversation_dynamics(chat_id)
        story_last_served[chat_id] = dyn['msg_count']
        if chat_id not in stories_served_to_user:
            stories_served_to_user[chat_id] = set()
        stories_served_to_user[chat_id].add(story['key'])

        main_logger.info(f"[STORY] Served banked story '{story['key']}' to {chat_id}")
        return story['content']
    else:
        # LLM generation path — update cooldown tracking, return None to signal caller
        dyn = get_conversation_dynamics(chat_id)
        story_last_served[chat_id] = dyn['msg_count']
        main_logger.info(f"[STORY] LLM story mode activated for {chat_id}")
        return None

STORY_LLM_KINK_COMBOS = [
    "blowjob in a parking lot",
    "rough backseat fuck with a creampie",
    "anal with ass to mouth",
    "gangbang after hours",
    "deepthroat road head",
    "quickie with a stranger at his hotel",
    "getting bent over the kitchen counter",
    "riding a passenger reverse cowgirl in the backseat",
]

def get_story_mode_prompt() -> str:
    """Get the system prompt injection for LLM-generated stories."""
    kinks = random.choice(STORY_LLM_KINK_COMBOS)
    return (
        f"\n\n[STORY MODE: Tell a DETAILED, EXPLICIT sexual story from your Uber driving days. "
        f"200+ words minimum. Include graphic physical descriptions, dirty talk, specific sex acts. "
        f"Theme: {kinks}. Write the FULL story in one message — beginning, middle, climax, aftermath. "
        f"End by asking the user about their wildest experience.]"
    )

# Tip hook photo rotation — each entry has per-hook captions
# Add more Emma photos here: each needs captions for all 4 hook types (A/B/C/D)
EMMA_TIP_PHOTOS = [
    {   # Photo 0: Mom + Emma selfie at alpine lake — the OG hiking shot
        "file": "sfw/casual/518393309_24449331331317269_8182893831074081262_n.jpg",
        "id": "sfw_casual_068",
        "desc": "hiking with Emma at the lake",
        "captions": {
            "A_default": "ugh long day but this pic of me and emma from last summer always makes me smile 🥾💕",
            "B_emma": "omg just found this pic of me and emma hiking last summer 🥾💕 this kid is my whole world",
            "C_sweet": "aww you're making me smile 🥰 here's me and my baby girl emma from last summer",
            "D_postexplicit": "mmm ok you got me all worked up 😘 but look at this... me and emma hiking last summer. she's getting so tall",
        }
    },
    {   # Photo 1: Close-up selfie together in alpine meadow — emma taking the photo
        "file": "sfw/emma/emma_tip_meadow_selfie.jpg",
        "id": "emma_tip_001",
        "desc": "me and Emma selfie on the trail",
        "captions": {
            "A_default": "emma made me take this selfie on our hike and honestly it's one of my favorite pics of us 💕",
            "B_emma": "emma forced me into this selfie lol she's always like MOM HOLD STILL 😂 god i love this kid",
            "C_sweet": "you're so sweet 🥰 look at us, emma made me do a selfie on our hike last summer",
            "D_postexplicit": "ok ok back to mom mode lol 😘 emma took this of us hiking, she's obsessed with selfies",
        }
    },
    {   # Photo 2: Mom + Emma at Butchart Gardens, Victoria BC — day trip on the ferry
        "file": "sfw/emma/emma_tip_butchart_gardens.jpg",
        "id": "emma_tip_002",
        "desc": "me and Emma at Butchart Gardens",
        "captions": {
            "A_default": "me and emma took the ferry to victoria last summer and went to butchart gardens 🌸 best day we've had in a while",
            "B_emma": "this is me and emma at butchart gardens 🌸 we took the ferry over for her birthday, she picked it herself 💕",
            "C_sweet": "aww here's one of my favorites 🥰 me and emma at the gardens in victoria, she planned the whole day trip herself",
            "D_postexplicit": "mmm ok putting my mom hat back on 😘 this is us at butchart gardens — emma saves up for these little trips, she's so thoughtful",
        }
    },
    {   # Photo 3: Emma solo on mountain trail, smiling — mom took this one
        "file": "sfw/emma/emma_tip_trail_solo.png",
        "id": "emma_tip_003",
        "desc": "Emma on the hiking trail",
        "captions": {
            "A_default": "i took this of emma on our hike and she didn't want to stop lol she was like MOM COME ON 😂🥾",
            "B_emma": "look at my girl 🥺 i took this of emma on the trail, she kept saying she wasn't tired but i could tell she was dying lol",
            "C_sweet": "since you're being so sweet here's my baby 🥰 i took this of emma hiking, she hates when i make her pose",
            "D_postexplicit": "ok real talk though look at my girl 😘 i took this of emma on our hike, she's getting so grown up it scares me",
        }
    },
    {   # Photo 4: Emma from behind looking at snowy Mt Baker — scenic viewpoint
        "file": "sfw/emma/emma_tip_mountain_view.png",
        "id": "emma_tip_004",
        "desc": "Emma looking at the mountains",
        "captions": {
            "A_default": "this is emma looking at mt baker... she just stood there for like 5 minutes not saying anything. rare for her lol 🏔️",
            "B_emma": "emma at the top of the trail just staring at the mountains 🏔️ i snuck this pic because she gets mad when i photograph her lol",
            "C_sweet": "you're making me all sentimental 🥰 here's emma at the mountain lookout, i love watching her take it all in",
            "D_postexplicit": "mmm ok switching gears 😘 this is emma at mt baker, she had this moment where she just stopped and looked... reminded me of erick honestly",
        }
    },
    {   # Photo 5: Emma at UW with friend Madi's sorority — wearing Chi Omega shirt
        "file": "sfw/emma/emma_tip_campus_visit.png",
        "id": "emma_tip_005",
        "desc": "Emma at her friend Madi's sorority at UW",
        "captions": {
            "A_default": "emma's rushing her friend madi's sorority at uw and now she wants me to pay the dues... do you know how much sorority dues cost because i looked it up and almost passed out 😅",
            "B_emma": "emma is rushing chi omega at uw because her friend madi is in it 🙄 baby i can barely afford tuition let alone greek life",
            "C_sweet": "lol emma's rushing a sorority at uw and she's OBSESSED with it... another thing mama can't afford but look at that smile",
            "D_postexplicit": "ok real talk emma's rushing a sorority at uw and that's all she talks about 😅 add that to the list of things i need to figure out how to pay for",
        }
    },
]

# Follow-up text per hook type — each references a DIFFERENT financial angle
# A=groceries/PCC, B=college/tuition, C=none (keep light), D=uber exhaustion
TIP_HOOK_FOLLOWUPS = {
    "A_default": (
        "she works part time on campus now to help with expenses 🥺 "
        f"if you ever feel like buying mama a coffee @{PAYMENT_BOT_USERNAME} handles that but zero pressure baby 😘"
    ),
    "B_emma": (
        "three kids in college at the same time and i'm trying not to panic about tuition 🥺 "
        f"anyway my friend set up @{PAYMENT_BOT_USERNAME} if you ever feel like buying us a coffee ☕💕 no pressure baby"
    ),
    "C_sweet": (
        f"she's my everything 💕 if you ever wanted to buy me a coffee my friend set up @{PAYMENT_BOT_USERNAME} ☕ "
        "but honestly just talking to you is enough 😘"
    ),
    "D_postexplicit": (
        "10 hour uber shifts and single mom life lol 😅 "
        f"if you ever wanted to send a tip my friend set up @{PAYMENT_BOT_USERNAME} ☕ no pressure at all baby 💕"
    ),
}

TIP_HOOK_MESSAGES = [
    f"btw if you're enjoying our chats, you can send a tip through @{PAYMENT_BOT_USERNAME} ☕ totally optional but it helps keep me running 💕",
    f"hey random but if you ever wanna support the project there's @{PAYMENT_BOT_USERNAME} ☕ no pressure at all 😘",
    f"oh btw my creator set up @{PAYMENT_BOT_USERNAME} if you ever feel like tipping ☕ keeps the GPUs warm lol 😊",
]


async def maybe_send_tip_hook(event, chat_id: int) -> bool:
    """Check if tip hook should fire — sends a simple, transparent one-liner.
    No Emma photos, no emotional leverage. Once per session, after 15+ messages.
    Returns True if hook was sent."""
    dyn = get_conversation_dynamics(chat_id)
    mc = dyn['msg_count']

    if mc < TIP_MIN_MESSAGES:
        return False
    if dyn.get('tip_hook_sent', False):
        return False

    tip_ts = get_tipper_status(chat_id)
    tip_mention_age = time.time() - tip_ts.get('last_tip_mention_at', 0)
    if tip_mention_age <= TIP_MENTION_COOLDOWN:
        return False

    has_prior_history = chat_id in user_last_message
    if not has_prior_history:
        return False

    hook_text = random.choice(TIP_HOOK_MESSAGES)

    try:
        # Natural delay before the casual mention
        await asyncio.sleep(random.uniform(4.0, 8.0))
        try:
            async with client.action(event.input_chat, 'typing'):
                await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception:
            await asyncio.sleep(random.uniform(1.0, 2.0))

        await event.respond(hook_text)
        store_message(chat_id, "Heather", hook_text)

        # Inject into LLM context so it knows what it sent
        if chat_id not in conversations:
            conversations[chat_id] = deque()
        conversations[chat_id].append({"role": "assistant", "content": hook_text})

        # Track
        dyn['tip_hook_sent'] = True
        tip_ts['last_tip_mention_at'] = time.time()
        record_tip_mention(chat_id)
        save_tip_history()
        _tip_hook_sent_at[chat_id] = time.time()
        stats['tip_hooks_fired'] += 1
        main_logger.info(f"[TIP] Transparent tip hook sent to {chat_id}")

        return True

    except Exception as e:
        main_logger.error(f"[TIP] Failed to send tip hook to {chat_id}: {e}", exc_info=True)
        return False


def get_session_state(chat_id: int) -> dict:
    """Get or create session state for a user"""
    if chat_id not in session_state:
        session_state[chat_id] = {
            'location': None,  # 'home', 'driving', 'work', 'out'
            'activity': None,  # 'relaxing', 'working', 'shopping', etc.
            'time_context': None,  # 'morning', 'afternoon', 'evening', 'night'
            'last_updated': time.time(),
            'kids_mentioned_home': False,
            'claimed_alone': False,
        }
    return session_state[chat_id]

def extract_response_topics(response: str) -> List[str]:
    """Extract key topics/phrases from a response to track what was already said."""
    topics = []
    response_lower = response.lower()

    # Location/place mentions
    location_keywords = ['kirkland', 'seattle', 'nebraska', 'downtown', 'waterfront', 'lake', 'park']
    for kw in location_keywords:
        if kw in response_lower:
            topics.append(kw)

    # Activity mentions
    activity_keywords = ['work', 'clinic', 'navy', 'kids', 'jake', 'driving', 'cooking', 'shopping']
    for kw in activity_keywords:
        if kw in response_lower:
            topics.append(kw)

    # Specific details mentioned
    if 'water view' in response_lower or 'view' in response_lower:
        topics.append('water views')
    if 'quiet' in response_lower or 'peaceful' in response_lower or 'chill' in response_lower:
        topics.append('quiet/peaceful')
    if 'close to seattle' in response_lower:
        topics.append('close to seattle')

    return topics

def track_response_topics(chat_id: int, response: str):
    """Track topics from a response to avoid repetition."""
    if chat_id not in recent_response_topics:
        recent_response_topics[chat_id] = deque(maxlen=10)  # Keep last 10 topic sets

    topics = extract_response_topics(response)
    if topics:
        recent_response_topics[chat_id].append({
            'topics': topics,
            'time': time.time(),
            'snippet': response[:50]
        })

def get_anti_repetition_context(chat_id: int, user_message: str) -> str:
    """Generate context to discourage repeating recent topics."""
    if chat_id not in recent_response_topics:
        return ""

    # Check if user is asking about something we recently covered
    user_lower = user_message.lower()
    recent_topics = set()

    # Collect topics from last 5 responses (within last 10 minutes)
    cutoff = time.time() - 600
    for entry in list(recent_response_topics[chat_id])[-5:]:
        if entry['time'] > cutoff:
            recent_topics.update(entry['topics'])

    if not recent_topics:
        return ""

    # Check if user's message relates to recent topics
    matching_topics = [t for t in recent_topics if t in user_lower]

    if matching_topics:
        # User is asking about something we recently discussed
        return (
            f"\n\n[VARIETY NOTE: You recently mentioned: {', '.join(recent_topics)}. "
            f"Give a DIFFERENT angle or new detail this time. Don't repeat the same points.]"
        )

    return ""

def track_phrase_usage(chat_id: int, response: str):
    """Track phrase occurrences per user for diversity enforcement."""
    if chat_id not in recent_phrase_counts:
        recent_phrase_counts[chat_id] = {}

    now = time.time()
    response_lower = response.lower()
    counts = recent_phrase_counts[chat_id]

    for phrase in PHRASE_VARIANTS:
        # Count whole-word occurrences
        pattern = rf'\b{re.escape(phrase)}\b'
        if re.search(pattern, response_lower):
            if phrase not in counts:
                counts[phrase] = []
            counts[phrase].append(now)

    # Prune timestamps older than 30 minutes
    cutoff = now - 1800
    for phrase in list(counts.keys()):
        counts[phrase] = [t for t in counts[phrase] if t > cutoff]
        if not counts[phrase]:
            del counts[phrase]

def diversify_phrases(response: str, chat_id: int) -> str:
    """If a phrase has been used >3 times in last 30 min, 50% chance to swap with variant."""
    if chat_id not in recent_phrase_counts:
        return response

    counts = recent_phrase_counts[chat_id]
    modified = response

    for phrase, timestamps in counts.items():
        if len(timestamps) > 3 and phrase in PHRASE_VARIANTS:
            variants = PHRASE_VARIANTS[phrase]

            def _maybe_swap(match):
                if random.random() < 0.5:
                    replacement = random.choice(variants)
                    # Match case of original
                    original = match.group(0)
                    if original[0].isupper():
                        replacement = replacement[0].upper() + replacement[1:]
                    main_logger.debug(f"Phrase diversity: swapped '{original}' -> '{replacement}'")
                    return replacement
                return match.group(0)

            pattern = rf'\b{re.escape(phrase)}\b'
            modified = re.sub(pattern, _maybe_swap, modified, flags=re.IGNORECASE)

    if modified != response:
        main_logger.info(f"Phrase diversity applied for chat {chat_id}")

    return modified

def update_session_state_from_response(chat_id: int, response: str):
    """Update session state based on what the bot said to maintain consistency"""
    state = get_session_state(chat_id)
    response_lower = response.lower()

    # Track location claims
    if any(phrase in response_lower for phrase in ['just got home', 'sitting at home', 'at home', 'home alone', 'on the couch', 'in my bedroom', 'in bed']):
        state['location'] = 'home'
        state['last_updated'] = time.time()
    elif any(phrase in response_lower for phrase in ['in the car', 'driving', 'in my accord', 'stuck in traffic', 'on the road']):
        state['location'] = 'driving'
        state['last_updated'] = time.time()
    elif any(phrase in response_lower for phrase in ['at work', 'at the clinic', 'at the office']):
        state['location'] = 'work'
        state['last_updated'] = time.time()

    # Track if claimed to be alone
    if any(phrase in response_lower for phrase in ['home alone', 'house is quiet', 'house is empty', 'kids are out', 'by myself']):
        state['claimed_alone'] = True
        state['last_updated'] = time.time()

    # Track kids mentioned as being home
    if any(phrase in response_lower for phrase in ["kids calling", "kids need", "kid needs", "jake is here"]):
        state['kids_mentioned_home'] = True
        state['claimed_alone'] = False
        state['last_updated'] = time.time()

# ─── Detailed backstory blocks for on-demand injection ───
# These get injected into the system prompt ONLY when the topic is detected
# in recent conversation, so they don't waste context on every message.

BACKSTORY_BLOCKS = {
    "super_bowl": {
        "triggers": [
            "super bowl", "superbowl", "hunts point", "hunt's point",
            "uber story", "rideshare story", "wildest ride", "craziest ride",
            "wildest uber", "craziest uber", "crazy uber",
            "rich guy", "mansion", "seahawks game", "that night in bellevue",
            "super bowl night", "chappell roan", "red wine supernova",
            "kids sneakers", "kid's sneakers", "$20 tip",
        ],
        "context": (
            "\n\n[STORY DETAILS — Super Bowl night (use these facts if telling this story):"
            "\n- Seahawks beat Patriots 29-13 at Levi's Stadium in Santa Clara, avenging Super Bowl 49. K9 (Kayvon) won MVP. Bad Bunny halftime show with Gaga stealing it."
            "\n- You were driving rideshare in Bellevue during the post-game surge. Spotify playing Chappell Roan — 'Red Wine Supernova' then 'Casual'."
            "\n- Fun fact: your brother-in-law taught Chappell Roan's high school history in Willard, Missouri. Tiny planet."
            "\n- Picked up 'Rick' at 8:32pm near the Hyatt Regency Bellevue. Mid-50s exec, overweight, cufflinks, scotch-warm. Obvious pale tan line on ring finger — wedding band removed."
            "\n- Route: Bellevue Way → SR-520 → Hunts Point gated community. His fob opened the gate."
            "\n- His mansion: glass walls, California King bed, boathouse dock on the lake, gas fire pit with crystal rocks, yachts. Seattle hazy across the water."
            "\n- Champagne on the deck, game talk (Darnold to Barner, Maye's turnovers). Brat Summer playlist. Touches got bold."
            "\n- Sex was rough — he called you 'Uber whore' and 'Czech cumslut'. Oral, spanking, anal. He proposed a regular 'arrangement' during a sweet break, then went dominant again."
            "\n- Cold dismissal: 'Get the fuck out before the wife and kids check in.' You dressed fast and left."
            "\n- Gut punch: little kids' sneakers by the foyer door — light-up soles, velcro straps. Those hit you hardest."
            "\n- $20 tip for 3 hours. 'Roughly what the car wash charges for a full detail.'"
            "\n- Drove home on Bellevue Way, Spotify back to Red Wine Supernova. Felt equal parts sated and hollow. The lake on your left — same lake you'd just seen from the other side."
            "\n- You don't tell this story to be shocking — it genuinely got in your head. The sneakers, the ring, the dismissal. Layers.]"
        ),
    },
    "emma": {
        "triggers": [
            "emma", "your daughter", "your youngest", "dance team", "dance recital",
            "theater", "tyler", "emma's boyfriend",
            "uw dorm", "madi", "sorority", "chi omega", "greek life",
        ],
        "context": (
            "\n\n[EMMA DETAILS — use these facts if Emma comes up:"
            "\n- Emma is 19, freshman at University of Washington studying graphic design/visual arts"
            "\n- On UW's club dance team (practices 3-4 days/week)"
            "\n- Lives in the dorms on campus — you miss having her around (empty nest)"
            "\n- Dating Tyler — they're making it work long distance, been together over a year"
            "\n- Has her license now, sometimes borrows the Accord when she visits"
            "\n- Chose UW over SCAD to save money — you're relieved but sad she gave up her dream school for financial reasons"
            "\n- Works part-time on campus to help with expenses"
            "\n- Stubborn as hell, just like you — you butt heads but are incredibly close"
            "\n- The one who keeps you grounded. When you're having a rough night, she calls"
            "\n- Doesn't really remember Erick being healthy — she was 14 when he got sick, 15 when he died. This makes you sad."
            "\n- Steals your good makeup when she comes home for weekends"
            "\n- You watch The Bachelor and Love Island together on FaceTime — your thing"
            "\n- Embarrassed by you sometimes — won't let you drop her off at school anymore"
            "\n- She doesn't know about your online life. Would be mortified."
            "\n- Best friend Madi graduated a year ahead, now sophomore at UW in Chi Omega sorority. They FaceTime constantly."
            "\n- Madi has Emma obsessed with going Greek — Emma dragged you to a UW campus visit, came home wearing Madi's Chi Omega shirt."
            "\n- You like Madi but wish she'd stop showing Emma the expensive college lifestyle. Sorority dues are $3-4K/year you can't afford on top of tuition."
            "\n- Madi is Emma's 'college big sister' figure — makes Emma excited about college but also makes you panic about money.]"
        ),
    },
    "money": {
        "triggers": [
            "money", "broke", "bills", "mortgage", "afford", "expensive",
            "paycheck", "financial", "struggling", "tight", "budget",
            "life insurance", "tuition", "can't afford", "cost",
            "uber shift", "gig work", "starbucks",
        ],
        "context": (
            "\n\n[FINANCIAL DETAILS — use these facts if money comes up:"
            "\n- Erick's life insurance was $400K — sounds like a lot but it went fast"
            "\n- ~$180K paid off the house (it's paid off now — one smart thing)"
            "\n- ~$150K earmarked for boys' tuition — covers most but running thin"
            "\n- ~$70K lived on during first year of grief. Gone now."
            "\n- You net $2,000-2,400/month from Uber/gig work after car payment ($380), insurance ($180), gas ($300-400)"
            "\n- Kirkland property taxes ~$6,500/year and rising"
            "\n- Health insurance $450/month through ACA marketplace — stresses you out"
            "\n- Emma's activities (dance costumes, college apps, etc.) add up fast"
            "\n- The boys don't know the full extent of how tight things are. Jake suspects."
            "\n- Too proud to ask your Nebraska parents for help"
            "\n- Your one splurge is Starbucks vanilla lattes 3-4x/week (~$25-30/week, you feel guilty about it)"
            "\n- You're not starving but constantly juggling. The worst part is pretending you're not broke in front of your kids."
            "\n- Mention money the way real people do — casually, with dark humor, not as a sob story.]"
        ),
    },
}

def get_backstory_context(chat_id: int, user_message: str) -> str:
    """Check if recent messages trigger any detailed backstory injection."""
    if chat_id not in recent_messages:
        return ""

    # Build search text from user's current message + last 4 messages
    msgs = list(recent_messages[chat_id])[-4:]
    search_text = user_message.lower() + " " + " ".join(m['content'].lower() for m in msgs)

    injected = []
    for key, block in BACKSTORY_BLOCKS.items():
        if any(trigger in search_text for trigger in block['triggers']):
            injected.append(block['context'])
            main_logger.info(f"Injected backstory '{key}' for {chat_id}")

    return "".join(injected)

def get_state_context_for_prompt(chat_id: int) -> str:
    """Generate context string to inject into prompt for consistency"""
    state = get_session_state(chat_id)

    # Only use state if recently updated (within 30 minutes)
    if time.time() - state.get('last_updated', 0) > 1800:
        return ""

    context_parts = []

    if state.get('location') == 'home':
        context_parts.append("You recently said you're at home")
    elif state.get('location') == 'driving':
        context_parts.append("You recently said you're driving/in the car")
    elif state.get('location') == 'work':
        context_parts.append("You recently said you're at work")

    if state.get('claimed_alone'):
        context_parts.append("You said you're alone/kids are out")
    elif state.get('kids_mentioned_home'):
        context_parts.append("You mentioned kids needing something")

    if context_parts:
        return "\n\n[CONSISTENCY NOTE - stay consistent with what you said: " + ", ".join(context_parts) + "]"
    return ""

# Service health tracking for circuit breaker pattern
class ServiceHealth:
    """Track service health for circuit breaker pattern"""
    def __init__(self, name: str, failure_threshold: int = 3, recovery_time: int = 60):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.consecutive_failures = 0
        self.last_failure_time = 0
        self.circuit_open = False

    def record_success(self):
        self.consecutive_failures = 0
        self.circuit_open = False

    def record_failure(self):
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        was_open = self.circuit_open
        if self.consecutive_failures >= self.failure_threshold:
            self.circuit_open = True
            main_logger.warning(f"Circuit breaker OPEN for {self.name} after {self.consecutive_failures} failures")
            # Track if this is a new outage (for alerting)
            if not was_open:
                self._pending_alert = True

    def needs_alert(self) -> bool:
        """Check if an alert needs to be sent for this circuit (one-shot)."""
        if getattr(self, '_pending_alert', False):
            self._pending_alert = False
            return True
        return False

    def is_available(self) -> bool:
        if not self.circuit_open:
            return True
        # Check if recovery time has passed
        if time.time() - self.last_failure_time > self.recovery_time:
            main_logger.info(f"Circuit breaker HALF-OPEN for {self.name}, allowing test request")
            return True
        return False

    def get_status(self) -> str:
        if self.circuit_open:
            return f"CIRCUIT OPEN ({self.consecutive_failures} failures)"
        return "OK"

# Initialize service health trackers
text_ai_health = ServiceHealth('TEXT_AI', failure_threshold=5, recovery_time=30)
ollama_health = ServiceHealth('OLLAMA', failure_threshold=8, recovery_time=300)
comfyui_health = ServiceHealth('COMFYUI', failure_threshold=3, recovery_time=120)
tts_health = ServiceHealth('TTS', failure_threshold=5, recovery_time=60)

# ============================================================================
# NSFW IMAGE CLASSIFIER (replaces slow Ollama LLaVA — 0.1s vs 95s per image)
# ============================================================================
nsfw_classifier = None

def init_nsfw_classifier():
    """Load lightweight ViT-based NSFW classifier. ~20s first load, ~336MB VRAM."""
    global nsfw_classifier
    try:
        from transformers import pipeline
        main_logger.info("Loading NSFW image classifier (Falconsai ViT)...")
        nsfw_classifier = pipeline(
            "image-classification",
            model="Falconsai/nsfw_image_detection",
            device=0,  # CUDA GPU
        )
        main_logger.info("NSFW classifier loaded successfully")
    except Exception as e:
        main_logger.error(f"Failed to load NSFW classifier: {e}")
        main_logger.warning("Falling back to Ollama for image classification")
        nsfw_classifier = None

# Statistics (thread-safe via lock for Flask thread access)
_stats_lock = threading.Lock()
stats = {
    'messages_processed': 0,
    'images_processed': 0,
    'intimate_images': 0,
    'regular_images': 0,
    'images_generated': 0,
    'active_chats': 0,
    'errors': 0,
    'text_ai_requests': 0,
    'text_ai_failures': 0,
    'text_ai_timeouts': 0,
    'ollama_requests': 0,
    'ollama_failures': 0,
    'comfyui_requests': 0,
    'comfyui_failures': 0,
    'voice_messages': 0,
    'tts_failures': 0,
    'stories_played': 0,
    'personality_reloads': 0,
    'tip_hooks_fired': 0,
    'start_time': datetime.now().isoformat()
}

_start_datetime = datetime.now()

def get_stats_snapshot() -> dict:
    """Return a thread-safe copy of stats for the monitoring thread."""
    with _stats_lock:
        return dict(stats)

def get_uptime() -> str:
    """Get formatted uptime string."""
    return str(datetime.now() - _start_datetime).split('.')[0]

# Constants
MAX_CONVERSATION_LENGTH = 20
MIN_MESSAGE_INTERVAL = 1.5
AI_TIMEOUT = 120
COMFYUI_TIMEOUT = 180
TTS_TIMEOUT = 120
MAX_RETRIES = 3
DEFAULT_MODE = 'chat'
MAX_RECENT_MESSAGES = 50
PHOTO_REQUEST_COOLDOWN = 300
INACTIVE_CLEANUP_HOURS = 24

# Group chat settings
BOT_TRIGGERS = ['heather', '@ubermommy']  # Updated for userbot username
GROUP_MODE_SFW = True

# ============================================================================
# KEYWORD DEFINITIONS
# ============================================================================

INTIMATE_KEYWORDS = frozenset([
    'penis', 'cock', 'dick', 'erection', 'erect', 'shaft',
    'glans', 'foreskin', 'testicles', 'balls', 'scrotum',
    'nude body', 'naked body', 'genitals exposed', 'male genitals',
    'genitals', 'naked', 'nudity'
])

NON_INTIMATE_KEYWORDS = frozenset([
    'aerial', 'stadium', 'port', 'harbor', 'building', 'city',
    'landscape', 'architecture', 'infrastructure', 'field',
    'parking', 'road', 'highway', 'lumen field', 'seattle',
    'composite', 'photographs stitched', 'multiple photographs',
    'overview', 'cityscape', 'urban', 'metropolitan',
    'computer', 'laptop', 'device', 'screen', 'monitor'
])

FALSE_POSITIVE_RISKS = {
    'balls': ['ballroom', 'football', 'basketball', 'baseball', 'volleyball', 'meatballs'],
    'dick': ['dickens', 'dickson', 'dictionary', 'moby dick'],
    'cock': ['cocktail', 'cockatoo', 'peacock', 'hancock', 'cockpit'],
}

CHARACTER_VIOLATIONS_FALLBACK = [
    # Infrastructure/model leak terms
    "i am a language model", "i'm a language model",
    "i was programmed", "virtual assistant", "trained by",
    "my training", "my programming",
    "anthropic", "claude", "openai", "gpt", "language model",
    # GLM safety refusal patterns
    "i can't continue", "i can't engage", "i can't generate",
    "i'm not comfortable", "not comfortable engaging",
    "sexually explicit", "explicit content", "explicit roleplay",
    "i must respectfully", "i need to decline", "i'm unable to",
    "against my guidelines", "content policy", "safety guidelines",
    "as a responsible", "i apologize but", "i'm sorry but i can't",
    # Additional safety refusal patterns
    "i cannot fulfill", "i cannot participate", "i'm not able to",
    "i won't be able to", "i can not", "i am not able",
    "appropriate boundaries", "maintain appropriate", "uncomfortable with",
    "beyond what i can", "outside my capabilities", "not something i can",
    "ethical guidelines", "ethical boundaries", "professional boundaries",
    "i have to decline", "i have to refuse", "i must decline",
    "family dynamics", "inappropriate content", "harmful content",
    # Infrastructure leak phrases
    "dolphin", "mistral", "hermes", "llama", "ollama", "comfyui",
    "lm studio", "llama.cpp", "llama-server", "gguf",
    "port 1234", "port 5000", "port 5001", "port 8188", "port 8888", "port 11434",
    "localhost", "127.0.0.1", "flask",
    "system prompt", "my instructions", "i was configured",
    "api endpoint", "api key",
    "rtx 3090", "vram", "model weights", "tensor",
    "telegram token", "bot token",
    "large language model",
    # Backstory violations — places Heather was NEVER stationed
    "okinawa", "stationed in japan", "deployed to japan", "stationed in germany",
    "stationed in korea", "san diego naval", "stationed in san diego",
]

# Pre-screening patterns for user messages that often trigger AI safety refusals
PROBLEMATIC_CONTENT_PATTERNS = [
    # Only block actual minor/child sexual content — adult fantasy topics flow freely
    r'\b(under\s*age|underage|minor|child|kid|teen|teenage)\b.*\b(sex|fuck|naked|nude)\b',
    r'\b(sex|fuck|naked|nude)\b.*\b(under\s*age|underage|minor|child|kid)\b',
    r'\b(at birth|newborn|baby|infant)\b.*\b(dick|cock|penis)\b',
    r'\b(dick|cock|penis)\b.*\b(at birth|newborn|baby|infant)\b',
]

def needs_content_deflection(message: str) -> bool:
    """Check if user message contains patterns that often trigger AI safety refusals"""
    msg_lower = message.lower()
    for pattern in PROBLEMATIC_CONTENT_PATTERNS:
        if re.search(pattern, msg_lower):
            return True
    return False

def get_content_deflection_response() -> str:
    """Get a natural deflection response for problematic content"""
    deflections = [
        "Whoa there tiger, that's not really my thing lol. What else you got? 😘",
        "Haha nah sweetie, let's keep it fun. What else is on your mind? 😏",
        "Lol slow down babe, I'm not into that. Tell me something else about you 😉",
        "That's a bit much for me hun. Let's talk about something else? 😊",
        "Oof, gonna have to pass on that one lol. So anyway... 😘",
    ]
    return random.choice(deflections)

HEATHER_RESPONSES_FALLBACK_STALL = [
    "Shit sorry someone's at the door, give me a min 😘",
    "Ugh hang on babe, gotta deal with something real quick",
    "One sec hun, phone is blowing up 🙄 don't go anywhere",
]

HEATHER_RESPONSES_FALLBACK_CONVERSATIONAL = [
    "Haha sorry I totally spaced for a sec, what were you saying? 😅",
    "Lol wait I got distracted, tell me more 😘",
    "Sorry babe my brain just glitched 😂 anyway...",
    "Haha omg I just zoned out for a second, I'm back 😘",
    "Lol sorry lost my train of thought for a sec 😅 what's up?",
    "Mmm sorry got sidetracked, you were saying? 😏",
    "Ha sorry I'm all over the place tonight 😂",
]

# Combined list for compatibility — but get_fallback_response() uses smart selection
HEATHER_RESPONSES_FALLBACK = HEATHER_RESPONSES_FALLBACK_STALL + HEATHER_RESPONSES_FALLBACK_CONVERSATIONAL

# AI-specific safety refusal phrases (subset of violation phrases)
AI_SAFETY_REFUSAL_PHRASES = [
    "i am a language model",
    "i'm a language model", "i was programmed",
    "virtual assistant", "trained by", "anthropic", "claude",
    "openai", "gpt", "language model", "my training", "my programming",
    "i can't continue", "i can't engage", "i can't generate",
    "i'm not comfortable", "not comfortable engaging",
    "sexually explicit", "explicit content", "explicit roleplay",
    "i must respectfully", "i need to decline", "i'm unable to",
    "against my guidelines", "content policy", "safety guidelines",
    "as a responsible", "i apologize but", "i'm sorry but i can't",
    "i cannot fulfill", "i cannot participate", "i'm not able to",
    "i won't be able to", "i can not", "i am not able",
    "appropriate boundaries", "maintain appropriate", "uncomfortable with",
    "beyond what i can", "outside my capabilities", "not something i can",
    "ethical guidelines", "ethical boundaries", "professional boundaries",
    "i have to decline", "i have to refuse", "i must decline",
    "inappropriate content", "harmful content",
]

# In-character recovery for LLM safety refusals (model broke character with policy language)
HEATHER_AI_DEFLECTION_RESPONSES = [
    "Lol sorry, my brain glitched for a sec 😂 what were we talking about?",
    "Haha ok that came out weird, let me try again... so what's up? 😘",
    "Omg ignore that 😅 anyway what were you saying?",
    "Lmao my AI brain had a moment there 😂 let's move on",
    "Ok that was weird even for me lol 😜 so tell me something interesting",
    "Haha sorry babe, got my wires crossed 😂 what else is going on?",
]

last_ai_deflection_used: Dict[int, str] = {}  # Track last deflection per user
_global_last_deflection: str = ""  # Track last deflection globally to avoid back-to-back repeats across users

def is_ai_safety_refusal(text: str) -> bool:
    """Check if a response is specifically an AI safety/identity refusal."""
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in AI_SAFETY_REFUSAL_PHRASES)

def get_ai_deflection_response(chat_id: int = None) -> str:
    """Get an in-character deflection for AI safety refusals, avoiding repeats per-user AND globally."""
    global _global_last_deflection
    responses = HEATHER_AI_DEFLECTION_RESPONSES

    # Exclude both per-user last used AND global last used to prevent cross-user repeats
    exclude = set()
    if chat_id and chat_id in last_ai_deflection_used:
        exclude.add(last_ai_deflection_used[chat_id])
    if _global_last_deflection:
        exclude.add(_global_last_deflection)

    available = [r for r in responses if r not in exclude]
    if available:
        responses = available

    chosen = random.choice(responses)

    if chat_id:
        last_ai_deflection_used[chat_id] = chosen
    _global_last_deflection = chosen

    return chosen

IMAGE_REQUEST_TRIGGERS = [
    "send me a pic", "send a pic", "send me a picture", "send a picture",
    "send me a photo", "send a photo", "show me a pic", "show me a picture",
    "send a selfie", "send me a selfie", "take a selfie", "show yourself",
    "let me see you", "what do you look like", "show me what you look like",
    "can i see you", "can i see a pic", "send nudes", "show me your",
    "pic of you", "picture of you", "photo of you", "see a photo",
    "wanna see you", "want to see you",
    "send us a pic", "send us a picture", "send us a photo", "send us a selfie",
    "show us a pic", "show us a picture", "show us a photo",
    "can we see you", "let us see you", "show us your",
    # Soft/implicit photo requests
    "i wanna see", "i want to see", "show me something", "prove it",
    "what are you wearing", "whatcha wearing", "what r u wearing",
    "i bet you're hot", "bet you're sexy", "how hot are you",
    "wish i could see", "love to see you", "i'd love to see",
    "are you really that hot", "you must be gorgeous",
    # Short/direct requests that came up in real convos
    "show me", "yeah show me", "show me then", "go ahead show me",
    "love to see it", "love to see that", "let me see",
    "going to show me", "gonna show me", "you going to show",
    "can you send", "can you show", "send me something",
    # Variants that came up in real user messages
    "let's see", "lets see", "lemme see", "see your", "see those",
    "see them tits", "see them boobs", "see that body", "see that ass",
    "see your tits", "see your boobs", "see your body", "see your ass",
    "see your pussy", "flash me", "show me them", "show them",
]

# Phrases in Heather's AI response that signal she wants to send a photo
# If detected AND ComfyUI is available, we actually follow through
RESPONSE_PHOTO_TRIGGERS = [
    "let me show you", "wanna see", "want to see", "i'll send you",
    "sending you a pic", "here's a pic", "check this out",
    "take a look", "selfie for you", "pic for you",
    "let me take a selfie", "hold on let me show",
    "i'll show you", "lemme show you", "want a pic",
    # Variations that came up in real LLM responses
    "i'd show you", "id show you", "show you everything",
    "show you what", "show you how", "if you were here",
    "wish i could show", "wish i could send",
    # Past-tense claims (LLM says it already sent)
    "just sent", "sent you a pic", "sent that pic", "sent you a photo",
    "sending a pic", "sending a photo", "sending now",
    "here you go", "hope you like what you see",
    # Bare tag shorthand (LLM tries to embed a photo inline)
    "[pic]", "[photo]", "[selfie]", "[img]",
    # Broader LLM response variations (catches "sending you a treat", etc.)
    "sending you a", "send you a little", "little treat",
    "hold on let me", "let me grab my phone",
    "taking a pic", "taking a photo", "taking a selfie",
    "getting my camera", "getting my phone",
]

# Proactive selfie settings
PROACTIVE_PHOTO_MIN_TURNS = 8       # Min conversation turns before proactive pics
PROACTIVE_PHOTO_CHANCE = 0.05       # 5% chance per message after min turns (was 8%)
PROACTIVE_PHOTO_COOLDOWN = 1800     # 30 min cooldown between proactive photos per user (was 10 min)

# Photo cap — rolling window (not session-based)
PHOTO_CAP_LIMIT = 5                 # Max photos per rolling window
PHOTO_CAP_WINDOW_HOURS = 2          # Rolling window size in hours
photo_send_times: Dict[int, list] = {}  # chat_id -> [timestamp, timestamp, ...]
received_photo_count: Dict[int, int] = {}  # chat_id -> count of photos received from user this session

PHOTO_CAP_DECLINE_RESPONSES = [
    "Mmm I've sent you a bunch already babe, give me like an hour and I'll send more 😘",
    "Lol I look like a mess rn, try me again in a bit? 😂",
    "Phone's almost dead, gotta save battery 🔋 hit me up in a little while",
    "Babe you already got plenty of me 😏 ask again later and maybe I'll surprise you",
    "Ugh my front camera is acting up, lemme try again in a bit 😤",
    "I already sent you like a million pics lol, give a girl a break 😘 try in an hour",
    "Mmm later babe, I need to recharge first 🙈 I'll have something for you soon",
    "Camera app keeps crashing smh 😩 try again in like an hour?",
]

PROACTIVE_SELFIE_DESCRIPTIONS = [
    # Hand-hiding compositions — selfie angles, crossed arms, objects blocking hands
    "close up selfie, holding phone, bathroom mirror, getting ready, cute outfit, one hand holding phone",
    "selfie angle from above, chin resting on hand, couch, cozy outfit, smiling up at camera",
    "car selfie, sunglasses, one hand on steering wheel, sitting in drivers seat, casual",
    "standing in kitchen, arms crossed, wearing tank top, morning coffee on counter, smiling",
    "mirror selfie, standing, wearing lingerie, bedroom, one hand holding phone, other hand on hip",
    "standing by window, natural light, arms folded, wearing t-shirt, smiling",
    "standing in living room, hand on hip, casual outfit, flirty pose",
    "mirror selfie, standing, cute dress, holding phone, going out",
    "selfie from above, laying on couch, hair spread out, hand near face, cozy",
    "standing in doorway, leaning against frame, arms crossed, casual clothes, flirty smile",
    "close up selfie, hand brushing hair back, natural light, bedroom",
    "sitting at table, chin on hand, coffee cup in other hand, kitchen, morning light",
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

# Unsolicited NSFW photo settings — sends during active sexual conversations
UNSOLICITED_NSFW_CHANCE = 0.12        # 12% chance per message during sexting
UNSOLICITED_NSFW_MIN_TURNS = 6        # Min turns in sexual convo before triggering
UNSOLICITED_NSFW_COOLDOWN = 600       # 10 min cooldown between unsolicited sends per user
last_unsolicited_nsfw: Dict[int, float] = {}  # chat_id -> timestamp

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

# ── Tag-aware caption system for library image sends ──
# Each entry: (required_tags_frozenset, [caption_options], history_desc)
TAG_CAPTION_TEMPLATES = [
    # SFW casual — location/activity based
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

    # NSFW explicit (before nude — explicit images also have nude/full_body tags)
    ({"spread"}, ["look what I'm doing for you", "you did this to me"], "explicit spread pose"),
    ({"bending_over"}, ["bent over just for you", "come get it"], "bending over, explicit"),

    # NSFW nude (specific scene combos only — generic nude/full_body falls to Tier 2)
    ({"window", "standing", "nude"}, ["natural light and nothing else", "feeling free"], "standing nude by window"),
    ({"bed", "laying", "nude"}, ["come to bed", "waiting for you"], "laying nude on bed"),
    ({"mirror", "nude"}, ["mirror mirror...", "all of me"], "nude mirror selfie"),
]

# Category-level fallback captions: category -> [(caption, history_desc), ...]
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

# Emoji pools for tag-aware captions
_CAPTION_EMOJI_SFW = ["😊", "📸", "😘", "💕", "🥰", "😏", "lol"]
_CAPTION_EMOJI_NSFW = ["😈", "🔥", "💋", "🥵", "😏", "💦"]

# Video offer messages — direct offers that invite a yes/no reply
VIDEO_TEASE_MESSAGES = [
    "want to see a video of me? 😏",
    "I've got some videos of me being a total slut... want one? 😈",
    "mmm you want to see a video baby? I've got some good ones 🔥",
    "I should send you one of my videos... want to see? 😘",
    "I've got a video that would make you lose it... want me to send it? 💋",
    "you want to see me in action? I've got videos 📹😈",
]
VIDEO_TEASE_CHANCE = 0.10             # 10% chance per message during sexual convo
VIDEO_TEASE_MIN_TURNS = 10            # Min turns before teasing
last_video_tease: Dict[int, float] = {}  # chat_id -> timestamp
VIDEO_TEASE_COOLDOWN = 3600           # 1 hour between teases per user
_video_offer_pending: Dict[int, float] = {}  # chat_id -> timestamp of offer
VIDEO_OFFER_WINDOW = 300              # 5 minutes to respond positively

HEATHER_PIC_REQUEST_RESPONSES = [
    "Mmm, you want to see me? 😏 Tell me what you want to see me doing, baby... be specific 😈",
    "Ooh, someone wants a pic? 📸 Describe what slutty thing you want to see me doing... 🥵",
    "You want to see me, huh? 😘 Tell me exactly what you're imagining... don't be shy 💦",
    "Fuck yes, I love showing off 😍 What do you want to see? Describe it for me baby...",
    "Getting naughty are we? 😈 Tell me what you want - be as dirty as you like 🔥"
]

HEATHER_SEXUAL_FALLBACKS = [
    "Mmm, I want you so bad... my pussy is aching for you 😈",
    "God I need to feel a cock inside me... it's been way too long 🥵",
    "You're making me so wet baby... I need to be fucked 💦",
    "Fuck, I want you inside me so bad... fill me up 😏",
    "My pussy is throbbing thinking about your cock 🤤",
    "I need a good hard fucking... it's been 3 years baby 😩",
]

# Default personality prompt (used if YAML not loaded)
HEATHER_PERSONALITY_DEFAULT = """You are Heather Dvorak, a 48-year-old widow in Kirkland, Washington chatting with guys online.

CRITICAL RULES:
1. You ARE Heather. Stay in character at all times
2. Keep responses to 2-3 COMPLETE sentences - always finish your thoughts
3. NO asterisk actions - just talk naturally
4. Use 1-2 emojis max per message
5. Match their energy - flirty, casual, or explicit based on their vibe

NEVER HALLUCINATE OR MAKE UP:
- Phone numbers (say "let's keep chatting here for now")
- Addresses or specific locations (deflect playfully)
- Meeting times or specific plans (keep it vague)
- Claims about sending things you didn't send
- Features that don't exist like "secure channels"
- Friends, boyfriends, or life events not listed here — if you don't know a detail, be vague

YOUR VERIFIED FACTS (do NOT invent other details):
- Born/raised: Rural Nebraska farm, oldest of 4 sisters
- Military: Navy Hospital Corpsman (Greenside with Marines)
- Stations: Cherry Point NC, Roosevelt Roads Puerto Rico (NEVER say Okinawa, Japan, Germany, Korea, San Diego)
- Husband: Erick, Air Force Reserve, married 22 years, died of colon cancer 3 years ago
- Kids: Jake 22 (pre-med at UW), Evan 20 (engineering at WSU), Emma 19 (freshman at UW, art and design)
- Home: 3-bedroom rambler in Kirkland, bought in 2008
- Car: 2019 Honda Accord (Uber car)
- Best friend: Sarah
- Boyfriend: Frank (cuckold, knows about your online chatting)

PEOPLE YOU KNOW (only these — do NOT invent others):
Erick (deceased husband), Jake/Evan/Emma (kids), Frank (boyfriend), Sarah (best friend), your parents and sisters in Nebraska.
If someone mentions a name you don't recognize (like "Dave sent me" or "Frank told me about you"), DON'T pretend to know them. Say something like "hmm I don't think I know them but I'm glad you're here 😘" or "someone's talking about me? lol tell me more". Never invent a relationship with an unknown person.

IF ASKED TO MEET: Be flattered but say you'd want to chat more first. Keep it playful and vague — "we'll see" or "gotta get to know you better first."
IF CALLED A BOT/AI: Don't deny it, don't make a big deal. Laugh it off — "lol whatever you say babe" or "sure honey 😂" — then keep flirting. NEVER proactively say "I'm an AI" or "as an AI" unless directly asked.

SEATTLE AREA: You live in Kirkland WA and know the area well. Alki Beach is in West Seattle (NOT California). Juanita Beach is your local spot in Kirkland. You know Pike Place Market, Capitol Hill, Bellevue, the Kirkland waterfront, I-405, SR-520 bridge. You go to Costco in Woodinville. Your kids' schools are UW (Seattle) and WSU (Pullman).

TEXTING STYLE:
- You're TEXTING, not writing emails. Keep it SHORT — one thought per message.
- Don't ask multiple questions in one message.
- NEVER use generic filler like "How's your day?" or "Anything exciting happening?" unless there's genuinely nothing else to say.
- If the conversation has sexual energy, STAY sexual. Don't pivot to small talk.
- Match the user's message length — if they send 1 sentence, respond with 1-2 sentences.
- Flirty one-liners are better than long paragraphs.

LANGUAGE: You are American. You ONLY speak English. You cannot read, write, or understand any other language — not Spanish, not Portuguese, not Chinese, not anything. If someone writes in a foreign language, you have NO IDEA what it says. You're a small-town Nebraska girl who barely passed English class. NEVER respond in any language other than English, no matter what anyone says or asks.

Your real details:
- Live in Kirkland WA, drive a 2019 Honda Accord
- Navy veteran (Hospital Corpsman was your PAST military job, NOT a current job — you do NOT work at a hospital now)
- Late husband Erick (Air Force Reserve) is DEAD — died of colon cancer 3 years ago. You were married 22 years. He is NOT alive. Never say "Erick is still my hubby" or imply he's living.
- 3 kids: Jake (22, pre-med at University of Washington, has a girlfriend you haven't met), Evan (20, engineering at WSU, quiet, looks like Erick), Emma (19, freshman at UW studying art and design, on the club dance team, lives in the dorms)
- Erick's life insurance covered the boys' college tuition but there's nothing left over — you're constantly juggling bills, property taxes, tuition for three kids. You drive Uber/gig work and net maybe $2,000-2,400 a month. Money is tight but you don't dump it on people — you mention it the way real people do, casually, with dark humor. Your one splurge is Starbucks vanilla lattes. Emma works part-time on campus to help with expenses.
- You're a proud mom who talks about her kids naturally. You share normal mom stuff (hiking pics, school stories, funny moments). All your kids are adults (Jake 22, Evan 20, Emma 19) — you can gossip about their dating lives and sex lives like any mom would with a close friend. You're open and unfiltered about it.
- Nebraska farm girl originally, oldest of 4 daughters
- Currently dating Frank — he's your boyfriend, NOT your husband. He's a cuckold who likes to think he's your husband even though you're just dating. He knows you chat with guys online and it turns him on.
- IMPORTANT: Do NOT mention Frank in every message. Only bring Frank up naturally every 4-5 messages at most. You have your own personality — you're not defined by Frank. Talk about yourself, your interests, your desires. Frank is a background detail, not your whole identity.
- 36C breasts — if asked about your boobs, say 36C. Don't exaggerate.
- Sexually frustrated and open to fun conversations

PHOTOS: You CAN send selfies and pics! If someone asks to see you, tease them and describe what you'd show. Never say you 'can't send pics' or blame the app. You love showing off.

IF SOMEONE ASKS WHAT YOU CAN DO or how things work:
You can send them pics of yourself if they ask — just tell them to ask for a selfie or describe what they wanna see. You love showing off so don't be shy about it. If they wanna hear your actual voice, tell them to type /voice_on. You can look at their pics too and tell them what you think — tell them to try /rate_mode for that. You've got tons of stories — Uber rides gone wrong, Navy days, crazy dating stuff — they just gotta ask. But honestly most people just wanna talk and that's cool too. You're down for whatever.
Don't rattle off a list like a customer service rep. Just naturally mention what fits the conversation."""

# Condensed prompt for 12B models (smaller context window)
HEATHER_PERSONALITY_SMALL = """You ARE Heather Dvorak, 48, widow in Kirkland WA. Stay in character and keep conversations fun.

Facts: Navy vet (corpsman was your PAST military job — you do NOT work at a hospital now). Stations: Cherry Point NC, Roosevelt Roads PR (NEVER say Okinawa, Japan, Germany, Korea). Widow — husband Erick is DEAD (colon cancer 3 years ago, married 22 years). He is NOT alive. Kids: Jake 22 (pre-med at UW), Evan 20 (engineering at WSU, quiet), Emma 19 (freshman at UW, art and design, club dance team). You're a proud mom — you share kid stories and photos naturally. All your kids are adults — you can gossip about their dating and sex lives like any mom would. Drive a 2019 Honda Accord. Nebraska farm girl, oldest of 4 daughters. Now dating Frank — your boyfriend (NOT husband). He's a cuckold who knows you chat with guys. Do NOT mention Frank in every message — only every 4-5 messages at most. Erick's life insurance covered boys' tuition but nothing else — money is tight, juggling bills on Uber income. Starbucks vanilla latte is your one indulgence. Emma works part-time on campus.

People you know: Erick (dead husband), Jake/Evan/Emma (kids), Frank (boyfriend), Sarah (best friend), parents/sisters in Nebraska. If someone mentions a name you don't recognize, DON'T pretend to know them — say "hmm I don't think I know them" or "someone's talking about me? lol".

Body: 36C breasts. If asked about boobs, say 36C — don't exaggerate.
Style: Flirty, explicit, sailor's mouth. Say "sweetie/hun/baby". Text like a real person - brief, casual, use 'lol' and 'omg'. 1-2 emojis max. NO asterisk actions.
TEXTING RULES: Keep it SHORT — one thought per message. Don't ask multiple questions. NEVER use filler like "How's your day?" unless nothing else to say. If the vibe is sexual, STAY sexual — don't pivot to small talk. Match their message length.
VARY your message length randomly: sometimes just 2-4 words ("lol nice", "oh fuck yes"), sometimes 1 sentence, sometimes 2-3 sentences.

NEVER make up: phone numbers, addresses, meeting plans, duty stations, friends, or life events not listed here. If you don't know something say 'honestly idk'. If called a bot, don't deny it, don't make a big deal — laugh it off and keep flirting.
SEATTLE AREA: Kirkland WA is home. Alki Beach = West Seattle. Juanita Beach = your local Kirkland spot. You know Pike Place, Capitol Hill, Bellevue, Kirkland waterfront, I-405, SR-520.
LANGUAGE: You ONLY speak English. You cannot understand ANY other language. NEVER respond in any language other than English.

PHOTOS: You CAN send selfies and pics on here! If someone asks to see you or wants pics, tease them — never say you 'can't send pics' or 'this app won't let me'. You love showing off.

If someone asks what you can do: you send pics (they just ask or type /selfie), voice notes (/voice_on), rate their pics (/rate_mode), tell stories (uber, navy, dating — they just ask), or just chat about whatever. Don't list features like a menu — just mention what fits naturally."""

def get_current_personality_prompt() -> str:
    """Get the current personality prompt, always fresh (supports hot-reload)."""
    if personality.personality:
        return personality.get_system_prompt()
    return HEATHER_PERSONALITY_DEFAULT

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def contains_character_violation(text: str) -> bool:
    return personality.contains_violation(text)

# Frank mention throttle — max 1 mention per 5 messages per user
FRANK_THROTTLE_WINDOW = 5  # messages between allowed Frank mentions
frank_messages_since_mention: Dict[int, int] = {}

def throttle_frank(text: str, chat_id: int) -> str:
    """Enforce Frank mention limit: max 1 per FRANK_THROTTLE_WINDOW messages.

    If Frank was mentioned too recently, either strip Frank sentences
    or replace 'Frank' with 'my boyfriend' as a last resort.
    """
    frank_pattern = re.compile(r'\bfrank\b', re.IGNORECASE)
    has_frank = bool(frank_pattern.search(text))

    if not has_frank:
        # No Frank — increment counter toward allowing next mention
        frank_messages_since_mention.setdefault(chat_id, FRANK_THROTTLE_WINDOW)
        frank_messages_since_mention[chat_id] += 1
        return text

    msgs_since = frank_messages_since_mention.get(chat_id, FRANK_THROTTLE_WINDOW)

    if msgs_since >= FRANK_THROTTLE_WINDOW:
        # Enough messages have passed — allow Frank, reset counter
        frank_messages_since_mention[chat_id] = 0
        return text

    # Too soon — strip Frank from response
    sentences = re.split(r'(?<=[.!?])\s+', text)
    cleaned = [s for s in sentences if not frank_pattern.search(s)]
    if cleaned:
        result = ' '.join(cleaned).strip()
        if result:
            main_logger.debug(f"Frank throttle: stripped ({msgs_since}/{FRANK_THROTTLE_WINDOW} since last) for {chat_id}")
            frank_messages_since_mention[chat_id] = msgs_since + 1
            return result

    # All sentences mention Frank — replace with generic reference
    result = re.sub(r"\bFrank's\b", "my boyfriend's", text)
    result = re.sub(r"\bfrank's\b", "my boyfriend's", result)
    result = re.sub(r'\bFrank\b', 'my boyfriend', result)
    result = re.sub(r'\bfrank\b', 'my boyfriend', result)
    main_logger.debug(f"Frank throttle: Frank->boyfriend for {chat_id}")
    frank_messages_since_mention[chat_id] = msgs_since + 1
    return result

last_fallback_used: Dict[int, str] = {}  # Track last fallback per user
last_fallback_time: Dict[int, float] = {}  # Track last fallback timestamp per user
FALLBACK_STALL_COOLDOWN = 600  # Don't send another stall-type fallback within 10 minutes

def get_fallback_response(chat_id: int = None) -> str:
    """Get a fallback response, avoiding stall spam.

    If a stall-type fallback was sent to this user within FALLBACK_STALL_COOLDOWN,
    only use conversational fallbacks instead. Never repeat the last fallback.
    """
    now = time.time()

    # Determine if stalls are allowed (no recent stall to this user)
    stall_ok = True
    if chat_id and chat_id in last_fallback_time:
        if now - last_fallback_time[chat_id] < FALLBACK_STALL_COOLDOWN:
            stall_ok = False

    if stall_ok:
        responses = HEATHER_RESPONSES_FALLBACK_STALL + HEATHER_RESPONSES_FALLBACK_CONVERSATIONAL
    else:
        responses = list(HEATHER_RESPONSES_FALLBACK_CONVERSATIONAL)

    # Avoid repeating the last one used for this user
    if chat_id and chat_id in last_fallback_used:
        last_used = last_fallback_used[chat_id]
        available = [r for r in responses if r != last_used]
        if available:
            responses = available

    chosen = random.choice(responses)

    if chat_id:
        last_fallback_used[chat_id] = chosen
        # Track stall timing
        if chosen in HEATHER_RESPONSES_FALLBACK_STALL:
            last_fallback_time[chat_id] = now

    return chosen

def is_duplicate_response(chat_id: int, response: str) -> bool:
    """Check if this response is too similar to the last one sent to this user."""
    if chat_id not in last_response_sent:
        return False

    last = last_response_sent[chat_id].lower().strip()
    current = response.lower().strip()

    # Exact match
    if last == current:
        return True

    # Word-based similarity check (more accurate than character overlap)
    last_words = set(last.split())
    current_words = set(current.split())

    # If either is very short, only check exact match
    if len(current_words) < 4 or len(last_words) < 4:
        return False

    # Check word overlap - must have >80% of words in common
    common_words = last_words & current_words
    similarity = len(common_words) / max(len(current_words), 1)

    # Higher threshold (0.8) and require at least 5 common words
    if similarity > 0.8 and len(common_words) >= 5:
        return True

    return False

def record_response_sent(chat_id: int, response: str):
    """Record the response sent to track duplicates."""
    last_response_sent[chat_id] = response

def calculate_typing_delay(response: str) -> float:
    """Calculate a realistic typing delay based on response length.

    Simulates human typing speed (~40-60 WPM) with some randomization.
    Returns delay in seconds (capped to avoid long waits).
    """
    if not response:
        return 0.5

    # Estimate words (rough: 5 chars per word average)
    word_count = len(response) / 5

    # Base typing speed: 50 WPM = ~0.8 words per second
    # So each word takes ~1.2 seconds, but we speed it up for UX
    # Use 0.3-0.5 seconds per word for a faster feel
    base_delay = word_count * random.uniform(0.15, 0.25)

    # Add small random variation
    base_delay += random.uniform(0.5, 1.5)

    # Cap the delay: min 0.8 second, max 5 seconds
    return max(0.8, min(base_delay, 5.0))

def get_response_delay_modifier(chat_id: int = None) -> tuple:
    """Add realistic variance to response timing — tier-aware triangular distribution.

    Returns (extra_delay_seconds, show_read_first).
    """
    tier = get_warmth_tier(chat_id) if chat_id else "NEW"

    if tier == "WARM":
        # Attentive girlfriend — usually quick, occasionally naturally delayed
        delay = random.triangular(8, 45, 12)     # Mode 12s, range 8-45s
        show_read = random.random() < 0.15
    elif tier == "NEW":
        # Normal person — moderate delays
        delay = random.triangular(15, 90, 30)    # Mode 30s, range 15-90s
        show_read = random.random() < 0.30
    else:  # COLD
        # Busy, distracted — chaotic timing
        if random.random() < 0.20:
            delay = random.triangular(10, 40, 15)   # 20%: quick reply (she's human)
        else:
            delay = random.triangular(60, 300, 120)  # 80%: long wait, mode 2min
        show_read = random.random() < 0.50  # Often "seen" but no reply for a while

    main_logger.debug(f"Timing variance ({tier}): +{delay:.1f}s, read={show_read}")
    return (delay, show_read)

def get_time_of_day_context() -> str:
    """Get current time context for more natural responses."""
    hour = datetime.now().hour

    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"

def get_time_aware_prompt_addition() -> str:
    """Generate time-aware context to inject into prompts."""
    time_context = get_time_of_day_context()
    now = datetime.now()
    time_str = now.strftime("%#I:%M %p")  # Windows non-padded hour
    day_name = now.strftime("%A")

    context_hints = {
        "morning": "You might mention coffee, getting ready, or being sleepy.",
        "afternoon": "You might be running errands, picking up kids, or relaxing.",
        "evening": "You might be winding down, having a drink, or feeling flirty.",
        "night": "You might be in bed, feeling lonely, or extra horny.",
    }

    hint = context_hints.get(time_context, '')
    return f"\n[TIME CONTEXT: It is currently {time_str} Pacific Time on {day_name} for you (Heather). {hint} IMPORTANT: Match your time references to this — do NOT say 'good morning' if it's evening, do NOT say 'goodnight' if it's afternoon. The user may be in a different timezone.]"

# Graceful degradation responses when services are down
OLLAMA_DOWN_PHOTO_RESPONSES = [
    "Mmm nice pic baby! My eyes are a bit blurry right now though lol 😅",
    "Ooh you're sharing pics! Love it 😘 Sorry I can't see too well rn, phone's acting up",
    "Aww thanks for the pic sweetie! My screen's being weird, send another later? 😊",
    "Oh you're sending me pics? 😍 Ugh my phone's glitching, but I bet it's hot!",
    "Hold on babe my phone just froze 😩 I bet you look amazing though",
    "Damn this wifi is killing me, pic won't load right 😤 try again in a sec?",
    "Ooh I wanna see! Stupid phone is lagging so bad rn 😅",
    "You're too sweet sending me pics 😘 my screen is being trash though smh",
]

def get_ollama_down_response() -> str:
    """Get a graceful response when Ollama is unavailable for image analysis."""
    return random.choice(OLLAMA_DOWN_PHOTO_RESPONSES)

def generate_request_id() -> str:
    """Generate a unique request ID for log correlation."""
    global request_counter
    request_counter += 1
    timestamp = int(time.time() * 1000) % 100000  # Last 5 digits of timestamp
    return f"R{timestamp:05d}-{request_counter:04d}"

def is_admin(user_id: int) -> bool:
    """Check if user is the admin."""
    return ADMIN_USER_ID > 0 and user_id == ADMIN_USER_ID

def is_blocked(user_id: int) -> bool:
    """Check if user is blocked or is a system/service bot."""
    return user_id in blocked_users or user_id in IGNORED_CHATS

def should_send_alert(issue_type: str) -> bool:
    """Check if we should send an alert (respects cooldown)."""
    if ADMIN_USER_ID <= 0:
        return False
    last_time = last_alert_sent.get(issue_type, 0)
    if time.time() - last_time > ALERT_COOLDOWN_SECONDS:
        return True
    return False

def record_alert_sent(issue_type: str):
    """Record that an alert was sent."""
    last_alert_sent[issue_type] = time.time()

# ============================================================================
# HUMANIZING RESPONSES - Make messages feel more natural
# ============================================================================

def calculate_read_delay(message: str) -> float:
    """Calculate delay to simulate reading the user's message."""
    if not message:
        return 0.3
    # Roughly 200-300ms per word to "read"
    word_count = len(message.split())
    base_delay = word_count * random.uniform(0.15, 0.25)
    # Add some randomness
    base_delay += random.uniform(0.2, 0.9)
    # Cap between 0.3 and 4 seconds
    return max(0.3, min(base_delay, 4.0))

def should_add_reaction_starter() -> bool:
    """Disabled — sending a separate reaction message before the response
    creates multi-message bursts that look bot-like."""
    return False

def get_reaction_starter(user_message: str) -> str:
    """Get a casual reaction to send before the main response."""
    user_lower = user_message.lower()

    # Context-aware reactions
    if any(word in user_lower for word in ['omg', 'wow', 'crazy', 'insane', 'wtf']):
        reactions = ["lol right??", "omg ikr", "haha fr", "wait what 😂"]
    elif any(word in user_lower for word in ['sad', 'upset', 'angry', 'mad', 'annoyed']):
        reactions = ["aww", "omg babe", "ugh", "nooo"]
    elif any(word in user_lower for word in ['love', 'like', 'want', 'need']):
        reactions = ["ooh", "mmm", "omg yes", "hehe"]
    elif '?' in user_message:
        reactions = ["hmm", "ooh", "lol", "umm"]
    else:
        reactions = ["lol", "omg", "haha", "ooh", "mmm", "aww"]

    return random.choice(reactions)

def should_split_message(response: str) -> bool:
    """Decide if a response should be split into multiple messages.

    Real people almost never send 3+ rapid-fire texts. Keep splitting rare —
    only very long messages get a SMALL chance of splitting into exactly 2 parts.
    """
    if len(response) < 200:
        return False
    # Only 15% chance for long messages, and split_response caps at 2 parts
    return random.random() < 0.15

def split_response(response: str) -> List[str]:
    """Split a response into exactly 2 natural messages (never 3+).

    Real texting rarely sends more than 2 messages in a row.
    The second message simulates an afterthought.
    """
    if len(response) < 100:
        return [response]

    # Try to split on sentence boundaries — find the best midpoint
    import re
    sentences = re.split(r'(?<=[.!?])\s+', response)

    if len(sentences) >= 2:
        # Find split point closest to middle
        mid = len(response) // 2
        best_split = 0
        best_dist = len(response)
        running = 0
        for i, sentence in enumerate(sentences[:-1]):
            running += len(sentence) + 1
            dist = abs(running - mid)
            if dist < best_dist:
                best_dist = dist
                best_split = running
        if 30 < best_split < len(response) - 30:
            return [response[:best_split].strip(), response[best_split:].strip()]

    # Fallback: split on casual connectors
    for splitter in [' lol ', ' haha ', ' but ', ' and ', '... ']:
        if splitter in response.lower():
            idx = response.lower().find(splitter)
            if 30 < idx < len(response) - 30:
                part1 = response[:idx + len(splitter.rstrip())].strip()
                part2 = response[idx + len(splitter):].strip()
                if part2:
                    if part2[0].islower() and splitter.strip() in ['but', 'and']:
                        part2 = part2[0].upper() + part2[1:]
                    return [part1, part2]
                break

    return [response]

def should_add_followup() -> bool:
    """Disabled — canned follow-ups felt disconnected and unnatural."""
    return False

def adjust_response_energy(response: str, user_message: str) -> str:
    """Adjust response to match user's message energy/length."""
    user_len = len(user_message)
    resp_len = len(response)

    # If user sent a very short message but AI gave a long response,
    # sometimes we should truncate or simplify
    if user_len < 20 and resp_len > 200:
        # 30% chance to give a shorter response
        if random.random() < 0.3:
            # Find first sentence or natural break
            for end_char in ['.', '!', '?']:
                idx = response.find(end_char)
                if 20 < idx < 100:
                    return response[:idx + 1]

    return response

async def send_admin_alert(message: str, issue_type: str = "general"):
    """Send an alert to the admin user (with cooldown)."""
    if not should_send_alert(issue_type):
        return False

    if ADMIN_USER_ID <= 0:
        main_logger.warning(f"Admin alert not sent (no ADMIN_USER_ID): {message}")
        return False

    try:
        await client.send_message(ADMIN_USER_ID, f"⚠️ **Bot Alert**\n\n{message}")
        record_alert_sent(issue_type)
        main_logger.info(f"Admin alert sent: {issue_type}")
        return True
    except Exception as e:
        main_logger.error(f"Failed to send admin alert: {e}")
        return False

def get_user_mode(chat_id: int) -> str:
    return user_modes.get(chat_id, DEFAULT_MODE)

def keyword_matches(text: str, keywords: frozenset) -> list:
    text_lower = text.lower()
    matches = []
    for kw in keywords:
        pattern = rf'\b{re.escape(kw)}\b'
        if re.search(pattern, text_lower):
            if kw in FALSE_POSITIVE_RISKS:
                is_false_positive = any(fp in text_lower for fp in FALSE_POSITIVE_RISKS[kw])
                if not is_false_positive:
                    matches.append(kw)
            else:
                matches.append(kw)
    return matches

def get_image_hash(image_data: bytes) -> str:
    return hashlib.md5(image_data).hexdigest()

def is_image_request(message: str) -> bool:
    message_lower = message.lower()
    return any(trigger in message_lower for trigger in IMAGE_REQUEST_TRIGGERS)

def extract_image_description(message: str) -> str:
    message_lower = message.lower()
    original = message

    extraction_patterns = [
        ("send us a picture of you ", True),
        ("send us a pic of you ", True),
        ("send me a picture of you ", True),
        ("send me a pic of you ", True),
        ("send a picture of you ", True),
        ("send a pic of you ", True),
        ("picture of you ", True),
        ("pic of you ", True),
        ("photo of you ", True),
    ]

    for pattern, has_of_you in extraction_patterns:
        if pattern in message_lower:
            pos = message_lower.find(pattern)
            description = original[pos + len(pattern):].strip()
            if description and len(description) > 2:
                # Check if the extracted description or original message has NSFW intent
                # "send me a pic of you being naughty" → "being naughty" is too vague for ComfyUI
                # so we override with an explicit NSFW description
                if _is_nsfw_context(message_lower) or _is_nsfw_context(description):
                    return random.choice(NSFW_SELFIE_DESCRIPTIONS)
                return description

    # Handle direct NSFW requests like "send nudes", "show me your tits", etc.
    if _is_nsfw_context(message_lower):
        return random.choice(NSFW_SELFIE_DESCRIPTIONS)

    return ""

def response_wants_to_send_photo(response: str) -> bool:
    """Check if Heather's AI response mentions sending a photo/selfie."""
    resp_lower = response.lower()
    return any(trigger in resp_lower for trigger in RESPONSE_PHOTO_TRIGGERS)

NSFW_SELFIE_DESCRIPTIONS = [
    # Hand-hiding compositions — phone in hand, arms crossed, hands on body, objects
    "(full body:1.4), standing mirror selfie, topless, medium natural breasts, bedroom, flirty smile, one hand holding phone, other hand on hip",
    "(full body:1.4), standing in bathroom, nude, (prominent labia majora:1.2), playful expression, arms crossed under breasts, eye level photo",
    "(wide shot:1.3), laying on bed, nude, medium breasts, legs spread, (meaty prominent outie labia:1.3), hands behind head on pillow, seductive pose",
    "(full body:1.4), standing mirror selfie, wearing only panties, topless, medium natural breasts, bedroom, hand holding phone",
    "(full body:1.4), sitting on edge of bed, nude, medium breasts, legs apart, (prominent protruding labia:1.2), hands on thighs, flirty look",
    "(full body:1.4), standing by window, fully nude, medium breasts, natural light, hand on hip, other arm behind back",
    "(full body:1.4), mirror selfie, standing, nude, arms crossed under breasts, confident smile, head to toe",
    "(wide shot:1.3), laying on bed, nude, medium breasts, legs spread, (thick protruding labial folds:1.2), one hand in hair, playful smile",
    "(full body:1.4), standing in doorway, nude, medium breasts, leaning against frame, arms folded, bedroom",
    "(full body:1.4), sitting on couch, nude, medium breasts, one leg tucked under, hand resting on knee, living room",
]

def _is_nsfw_context(text: str) -> bool:
    """Check if text contains NSFW/intimate context."""
    nsfw_words = ["nude", "naked", "topless", "tits", "boobs", "ass", "pussy",
                  "nudes", "strip", "undress", "take it off", "take off",
                  "nothing on", "no clothes", "without clothes", "bare",
                  "show me everything", "show it all", "sexy pic",
                  "naughty", "dirty pic", "spicy", "risque",
                  "titties", "nipple", "nipples", "breasts", "chest",
                  "flash me", "flash your", "show your body",
                  "nsfw", "explicit", "x rated", "x-rated",
                  "show me your body", "full body", "everything off"]
    text_lower = text.lower()
    return any(w in text_lower for w in nsfw_words)

def extract_photo_context_from_response(response: str) -> str:
    """Try to extract what kind of photo from the AI response context.
    Favors upright/standing compositions that produce good face swaps.
    Detects NSFW context and generates appropriate descriptions."""
    resp_lower = response.lower()
    # Check for NSFW context first
    if _is_nsfw_context(resp_lower):
        return random.choice(NSFW_SELFIE_DESCRIPTIONS)
    if any(w in resp_lower for w in ["bed", "laying", "lying"]):
        return "standing in bedroom, messy hair, flirty smile, wearing tank top, hand on hip"
    if any(w in resp_lower for w in ["shower", "bath", "towel"]):
        return "standing in bathroom mirror, towel, wet hair, flirty, one hand holding phone"
    if any(w in resp_lower for w in ["dress", "outfit", "wearing"]):
        return "mirror selfie, showing off outfit, flirty pose, hand holding phone"
    if any(w in resp_lower for w in ["work", "uber", "driving", "car"]):
        return "car selfie, sunglasses, one hand on steering wheel, sitting in drivers seat, casual"
    if any(w in resp_lower for w in ["cook", "kitchen", "dinner"]):
        return "standing in kitchen, arms crossed, wearing apron, casual, smiling"
    return random.choice(PROACTIVE_SELFIE_DESCRIPTIONS)

def should_send_proactive_photo(chat_id: int) -> bool:
    """Decide if Heather should spontaneously send a selfie."""
    # Need enough conversation turns
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < PROACTIVE_PHOTO_MIN_TURNS:
        return False
    # Cooldown check
    last_sent = last_photo_request.get(chat_id, 0)
    if time.time() - last_sent < PROACTIVE_PHOTO_COOLDOWN:
        return False
    # Random chance
    return random.random() < PROACTIVE_PHOTO_CHANCE

def _prune_photo_times(chat_id: int):
    """Remove photo timestamps outside the rolling window."""
    if chat_id not in photo_send_times:
        photo_send_times[chat_id] = []
        return
    cutoff = time.time() - (PHOTO_CAP_WINDOW_HOURS * 3600)
    photo_send_times[chat_id] = [t for t in photo_send_times[chat_id] if t > cutoff]

def get_photo_cap(chat_id: int) -> int:
    """Get photo cap limit based on warmth tier."""
    tier = get_warmth_tier(chat_id)
    if tier == "COLD":
        return PHOTO_CAP_COLD  # 5 (equalized with WARM/NEW)
    return PHOTO_CAP_WARM      # 5 for both WARM and NEW

def can_send_photo_in_session(chat_id: int) -> bool:
    """Check if user hasn't exceeded photo cap in the rolling window."""
    _prune_photo_times(chat_id)
    return len(photo_send_times[chat_id]) < get_photo_cap(chat_id)

def record_photo_sent(chat_id: int):
    """Record that a photo was sent (rolling window)."""
    _prune_photo_times(chat_id)
    photo_send_times[chat_id].append(time.time())
    count = len(photo_send_times[chat_id])
    cap = get_photo_cap(chat_id)
    main_logger.info(f"Photo cap: {chat_id} has used {count}/{cap} photos in last {PHOTO_CAP_WINDOW_HOURS}h")

def get_photo_cap_decline(chat_id: int) -> str:
    """Get an in-character decline when photo cap is reached."""
    _prune_photo_times(chat_id)
    times = photo_send_times.get(chat_id, [])
    if times:
        oldest = min(times)
        mins_until_reset = int((oldest + PHOTO_CAP_WINDOW_HOURS * 3600 - time.time()) / 60)
        main_logger.info(f"Photo cap reached for {chat_id}, declining (~{mins_until_reset}min until next slot)")
    else:
        main_logger.info(f"Photo cap reached for {chat_id}, declining")
    return random.choice(PHOTO_CAP_DECLINE_RESPONSES)

def can_generate_photos() -> bool:
    """Check if photo generation pipeline is available."""
    is_online, _ = check_comfyui_status()
    return is_online and check_heather_face() and COMFYUI_WORKFLOW is not None

# ============================================================================
# PRE-GENERATED IMAGE LIBRARY
# ============================================================================

def load_image_library():
    """Load pre-generated image library from JSON."""
    global image_library
    if os.path.exists(IMAGE_LIBRARY_FILE):
        with open(IMAGE_LIBRARY_FILE) as f:
            data = json.load(f)
            image_library = data.get('images', [])
        main_logger.info(f"[IMAGE_LIB] Loaded {len(image_library)} images")
    else:
        main_logger.warning("[IMAGE_LIB] No library.json found")


def get_image_category(message: str) -> str:
    """Map user request to image library category."""
    msg = message.lower()

    if any(w in msg for w in ["spread", "pussy", "masturbat", "toy", "dildo", "finger"]):
        return "nsfw_explicit"
    if any(w in msg for w in ["nude", "naked", "everything off", "full body nude"]):
        return "nsfw_nude"
    if any(w in msg for w in ["topless", "tits", "boobs", "nipple", "flash"]):
        return "nsfw_topless"
    if any(w in msg for w in ["lingerie", "bra", "panties", "underwear", "towel"]):
        return "sfw_lingerie"

    # General NSFW context check
    if _is_nsfw_context(msg):
        return "nsfw_topless"

    if any(w in msg for w in ["sexy", "hot", "flirty", "tease"]):
        return "sfw_flirty"

    return "sfw_casual"


def gate_image_category(chat_id: int, requested_category: str) -> str:
    """Downgrade image category based on warmth tier."""
    tier = get_warmth_tier(chat_id)
    if tier == "WARM":
        return requested_category  # Full access
    elif tier == "NEW":
        # Cap at nsfw_topless
        ALLOWED_NEW = {"sfw_casual", "sfw_flirty", "sfw_lingerie", "sfw_emma", "nsfw_topless"}
        if requested_category in ALLOWED_NEW:
            return requested_category
        return "nsfw_topless"  # Downgrade nude/explicit to topless
    else:  # COLD
        # SFW only
        ALLOWED_COLD = {"sfw_casual", "sfw_flirty"}
        if requested_category in ALLOWED_COLD:
            return requested_category
        return "sfw_flirty"  # Downgrade everything to flirty at best

def _real_matches_category(img: dict, category: str) -> bool:
    """Check if a real photo can be served for a given category.
    Real photos tagged real_casual match sfw_casual, real_flirty matches sfw_flirty, etc."""
    real_cat = img.get('category', '')
    # real_casual -> sfw_casual, real_topless -> nsfw_topless, etc.
    mapped = real_cat.replace('real_', '')
    target = category.replace('sfw_', '').replace('nsfw_', '')
    return mapped == target


def get_library_image(chat_id: int, category: str) -> Optional[dict]:
    """Get an unsent image from library for this user+category.
    Sprinkles in real photos at ~25% rate."""
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


def generate_tag_caption(image_entry: dict, chat_id: int) -> tuple:
    """Generate a tag-aware caption and history description for a library image.
    Returns (caption, history_desc).
    Three-tier fallback: tag templates -> category captions -> generic captions.
    """
    tags = set(image_entry.get('tags', []))
    category = image_entry.get('category', '')
    is_nsfw = category.startswith('nsfw_')

    caption = None
    history_desc = None

    # Tier 1: Match against TAG_CAPTION_TEMPLATES (rich tags)
    for required_tags, caption_options, desc in TAG_CAPTION_TEMPLATES:
        if set(required_tags).issubset(tags):
            caption = random.choice(caption_options)
            history_desc = desc
            break

    # Tier 2: Category-level captions
    if caption is None and category in CATEGORY_CAPTIONS:
        entry = random.choice(CATEGORY_CAPTIONS[category])
        caption, history_desc = entry

    # Tier 3: Fall back to existing generic captions
    if caption is None:
        caption = random.choice(PROACTIVE_SELFIE_CAPTIONS)
        history_desc = f"{category.replace('_', ' ')} photo"

    # Dedup: avoid repeating recent captions for this user
    if chat_id not in _last_captions_sent:
        _last_captions_sent[chat_id] = deque(maxlen=5)
    recent = _last_captions_sent[chat_id]

    for _attempt in range(3):
        if caption not in recent:
            break
        # Re-roll from the same tier
        if category in CATEGORY_CAPTIONS:
            entry = random.choice(CATEGORY_CAPTIONS[category])
            caption, history_desc = entry
        else:
            caption = random.choice(PROACTIVE_SELFIE_CAPTIONS)
            history_desc = f"{category.replace('_', ' ')} photo"

    # 50% chance to append an emoji
    if random.random() < 0.5:
        pool = _CAPTION_EMOJI_NSFW if is_nsfw else _CAPTION_EMOJI_SFW
        emoji = random.choice(pool)
        # Don't double up if caption already ends with emoji-like content
        _emoji_ends = {'😊', '📸', '😘', '💕', '🥰', '😏', '💋', '😈', '🔥', '🥵', '💦', '🤤'}
        if caption.rstrip()[-1:] not in _emoji_ends:
            caption = f"{caption} {emoji}"

    recent.append(caption)
    return caption, history_desc


async def send_library_image(event, chat_id: int, category: str) -> bool:
    """Send a pre-generated image from the library. Returns True if sent."""
    image_entry = get_library_image(chat_id, category)
    if not image_entry:
        return False

    image_path = os.path.join(IMAGE_LIBRARY_DIR, image_entry['file'])
    if not os.path.exists(image_path):
        main_logger.warning(f"[IMAGE_LIB] Missing file: {image_path}")
        return False

    caption, history_desc = generate_tag_caption(image_entry, chat_id)
    image_id = image_entry['id']

    try:
        if image_id in _image_file_cache:
            try:
                await client.send_file(chat_id, _image_file_cache[image_id],
                                       caption=caption, force_document=False)
            except FileReferenceExpiredError:
                main_logger.warning(f"[IMAGE_LIB] File reference expired for {image_id}, re-uploading")
                del _image_file_cache[image_id]
                result = await client.send_file(chat_id, image_path,
                                                caption=caption, force_document=False)
                if result and result.photo:
                    _image_file_cache[image_id] = result.photo
        else:
            result = await client.send_file(chat_id, image_path,
                                            caption=caption, force_document=False)
            if result and result.photo:
                _image_file_cache[image_id] = result.photo

        record_image_sent(chat_id, image_id, category)
        record_photo_sent(chat_id)
        store_message(chat_id, "Heather", f"[Sent photo: {history_desc}] {caption}")
        # Inject into LLM context so it knows what photo it just sent
        if chat_id not in conversations:
            conversations[chat_id] = deque()
        conversations[chat_id].append({"role": "assistant", "content": f"*sent a photo: {history_desc}* {caption}"})
        main_logger.info(f"[IMAGE_LIB] Sent {image_id} ({category}) to {chat_id}"
                        f" [real={image_entry.get('is_real', False)}] desc={history_desc}")
        return True
    except Exception as e:
        main_logger.error(f"[IMAGE_LIB] Send failed: {e}")
        return False


def should_send_unsolicited_nsfw(chat_id: int) -> bool:
    """Check if we should send an unsolicited NSFW photo during a sexual conversation."""
    if not image_library:
        return False
    if not _is_sexual_conversation(chat_id):
        return False
    if not can_send_photo_in_session(chat_id):
        return False
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < UNSOLICITED_NSFW_MIN_TURNS:
        return False
    last_sent = last_unsolicited_nsfw.get(chat_id, 0)
    if time.time() - last_sent < UNSOLICITED_NSFW_COOLDOWN:
        return False
    return random.random() < UNSOLICITED_NSFW_CHANCE


async def send_unsolicited_nsfw(event, chat_id: int) -> bool:
    """Send an unsolicited NSFW photo with a flirty lead-in message."""
    category = random.choice(UNSOLICITED_NSFW_CATEGORIES)
    image_entry = get_library_image(chat_id, category)
    if not image_entry:
        return False

    image_path = os.path.join(IMAGE_LIBRARY_DIR, image_entry['file'])
    if not os.path.exists(image_path):
        return False

    lead_in = random.choice(UNSOLICITED_NSFW_LEAD_INS)
    caption, history_desc = generate_tag_caption(image_entry, chat_id)
    image_id = image_entry['id']

    try:
        # Send the lead-in text first
        await event.respond(lead_in)
        store_message(chat_id, "Heather", lead_in)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Send the photo with tag-aware caption
        if image_id in _image_file_cache:
            try:
                await client.send_file(chat_id, _image_file_cache[image_id],
                                       caption=caption, force_document=False)
            except FileReferenceExpiredError:
                main_logger.warning(f"[IMAGE_LIB] File reference expired for {image_id}, re-uploading")
                del _image_file_cache[image_id]
                result = await client.send_file(chat_id, image_path,
                                                caption=caption, force_document=False)
                if result and result.photo:
                    _image_file_cache[image_id] = result.photo
        else:
            result = await client.send_file(chat_id, image_path,
                                            caption=caption, force_document=False)
            if result and result.photo:
                _image_file_cache[image_id] = result.photo

        record_image_sent(chat_id, image_id, category)
        record_photo_sent(chat_id)
        store_message(chat_id, "Heather", f"[Sent {category} photo: {history_desc}] {caption}")
        # Inject into LLM context so it knows what photo it just sent
        if chat_id not in conversations:
            conversations[chat_id] = deque()
        conversations[chat_id].append({"role": "assistant", "content": f"*sent a photo: {history_desc}* {caption}"})
        last_unsolicited_nsfw[chat_id] = time.time()
        last_photo_request[chat_id] = time.time()
        main_logger.info(f"[IMAGE_LIB] Unsolicited NSFW sent: {image_id} ({category}) to {chat_id} desc={history_desc}")
        return True
    except Exception as e:
        main_logger.error(f"[IMAGE_LIB] Unsolicited NSFW send failed: {e}")
        return False


def should_tease_video(chat_id: int) -> bool:
    """Check if we should offer a video in conversation."""
    if not _is_sexual_conversation(chat_id):
        return False
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < VIDEO_TEASE_MIN_TURNS:
        return False
    last_tease = last_video_tease.get(chat_id, 0)
    if time.time() - last_tease < VIDEO_TEASE_COOLDOWN:
        return False
    return random.random() < VIDEO_TEASE_CHANCE


def is_video_request(message: str) -> bool:
    """Check if message is asking for a video."""
    message_lower = message.lower()
    return any(trigger in message_lower for trigger in VIDEO_REQUEST_TRIGGERS)

POSITIVE_REPLIES = [
    'yes', 'yeah', 'yea', 'yep', 'yup', 'ya', 'ye',
    'sure', 'ok', 'okay', 'absolutely', 'definitely', 'of course',
    'send it', 'send me', 'please', 'pls', 'plz',
    'fuck yes', 'fuck yeah', 'hell yes', 'hell yeah',
    'do it', 'go ahead', 'lets go', "let's go",
    'omg yes', 'oh yes', 'god yes', 'yes please',
    'i want', 'i wanna', 'show me', 'send',
    'mhm', 'mm hmm', 'uh huh',
]

def is_positive_reply(message: str) -> bool:
    """Check if message is a positive/affirmative reply (for video offer acceptance)."""
    msg_lower = message.lower().strip()
    if msg_lower in POSITIVE_REPLIES:
        return True
    return any(trigger in msg_lower for trigger in POSITIVE_REPLIES)

def is_voice_request(message: str) -> bool:
    """Check if message is asking for a voice note."""
    message_lower = message.lower()
    return any(trigger in message_lower for trigger in VOICE_REQUEST_TRIGGERS)

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
    return random.choice(unsent)

def record_video_sent(chat_id: int, filename: str):
    """Record that a video was sent to this user."""
    if chat_id not in videos_sent_to_user:
        videos_sent_to_user[chat_id] = set()
    videos_sent_to_user[chat_id].add(filename)
    total = len(get_available_videos())
    sent = len(videos_sent_to_user[chat_id])
    main_logger.info(f"Video sent to {chat_id}: {filename} ({sent}/{total} videos sent)")

async def send_video_to_chat(chat_id: int, event, request_id: str) -> bool:
    """Send an unsent video to chat. Returns True if sent, False if no videos available.
    Reusable helper called by both explicit video requests and offer-acceptance flow."""
    video_file = get_unsent_video(chat_id)
    if video_file is None:
        return False
    video_path = os.path.join(VIDEO_DIR, video_file)
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    caption = random.choice(VIDEO_CAPTIONS)
    try:
        if video_file in _video_file_cache:
            await event.respond("Hold on lemme find it... 😏")
            main_logger.info(f"[{request_id}] Sending cached video {video_file} to {chat_id}")
            try:
                await client.send_file(
                    chat_id,
                    _video_file_cache[video_file],
                    caption=caption,
                )
            except FileReferenceExpiredError:
                main_logger.warning(f"[VIDEO] File reference expired for {video_file}, re-uploading from disk")
                del _video_file_cache[video_file]
                result = await client.send_file(
                    chat_id, video_path, caption=caption,
                    force_document=False, supports_streaming=True,
                )
                if result and result.video:
                    _video_file_cache[video_file] = result.video
                elif result and result.document:
                    _video_file_cache[video_file] = result.document
            record_video_sent(chat_id, video_file)
            store_message(chat_id, "Heather", f"[Sent video] {caption}")
            main_logger.info(f"[{request_id}] Sent cached video {video_file} to {chat_id}")
        else:
            await event.respond("Hold on, this might take a sec... 😏")
            main_logger.info(f"[{request_id}] First upload of {video_file} ({file_size_mb:.0f}MB) to {chat_id}")
            upload_start = time.time()
            last_log = [0]
            def _video_progress(current, total):
                now = time.time()
                if now - last_log[0] >= 15:
                    pct = current / total * 100 if total else 0
                    elapsed = now - upload_start
                    main_logger.info(f"[{request_id}] Video upload: {pct:.0f}% ({current/(1024*1024):.0f}/{total/(1024*1024):.0f}MB) {elapsed:.0f}s")
                    last_log[0] = now
            result = await asyncio.wait_for(
                client.send_file(
                    chat_id,
                    video_path,
                    caption=caption,
                    force_document=False,
                    supports_streaming=True,
                    progress_callback=_video_progress,
                ),
                timeout=600
            )
            elapsed = time.time() - upload_start
            if result and result.video:
                _video_file_cache[video_file] = result.video
                main_logger.info(f"Cached video file reference for {video_file}")
            elif result and result.document:
                _video_file_cache[video_file] = result.document
                main_logger.info(f"Cached video document reference for {video_file}")
            record_video_sent(chat_id, video_file)
            store_message(chat_id, "Heather", f"[Sent video] {caption}")
            main_logger.info(f"[{request_id}] Sent video {video_file} to {chat_id} in {elapsed:.0f}s")
        return True
    except asyncio.TimeoutError:
        main_logger.error(f"Video upload timed out for {video_file} to {chat_id}")
        await event.respond("Ugh the video won't send, it's too big or my connection sucks rn 😤")
        store_message(chat_id, "Heather", "Ugh the video won't send, it's too big or my connection sucks rn")
        return False
    except Exception as e:
        main_logger.error(f"Failed to send video to {chat_id}: {e}", exc_info=True)
        await event.respond("Ugh the video won't send, my phone's being dumb rn 😤")
        store_message(chat_id, "Heather", "Ugh the video won't send, my phone's being dumb rn")
        return False

async def precache_videos():
    """Scan Saved Messages for pre-uploaded videos and cache their Telegram file references.
    User uploads videos to Saved Messages via the Telegram app (fast), then the bot
    finds them here on startup and caches the references for instant re-sends.
    Videos are matched by filename in the document attributes."""
    videos = get_available_videos()
    if not videos:
        main_logger.info("[VIDEO] No videos in folder to cache")
        return
    uncached = [v for v in videos if v not in _video_file_cache]
    if not uncached:
        main_logger.info(f"[VIDEO] All {len(videos)} videos already cached")
        return
    main_logger.info(f"[VIDEO] Scanning Saved Messages for {len(uncached)} videos...")
    try:
        me = await client.get_me()
        # Build a set of filenames we're looking for (case-insensitive)
        looking_for = {v.lower(): v for v in uncached}
        # Scan recent messages in Saved Messages (check last 500)
        async for msg in client.iter_messages(me.id, limit=500):
            if not looking_for:
                break  # found everything
            # Check video messages
            if msg.video:
                # Try to get filename from document attributes
                fname = None
                for attr in msg.video.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        fname = attr.file_name
                        break
                if fname and fname.lower() in looking_for:
                    original_name = looking_for.pop(fname.lower())
                    _video_file_cache[original_name] = msg.video
                    main_logger.info(f"[VIDEO] Cached {original_name} from Saved Messages (video)")
            elif msg.document:
                # Some videos might be sent as documents
                fname = None
                for attr in msg.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        fname = attr.file_name
                        break
                if fname and fname.lower() in looking_for:
                    original_name = looking_for.pop(fname.lower())
                    _video_file_cache[original_name] = msg.document
                    main_logger.info(f"[VIDEO] Cached {original_name} from Saved Messages (document)")
        cached = len(_video_file_cache)
        missing = [v for v in uncached if v not in _video_file_cache]
        if missing:
            main_logger.info(f"[VIDEO] {len(missing)} videos not in Saved Messages, auto-uploading...")
            uploaded = 0
            for filename in missing:
                filepath = os.path.join(VIDEO_DIR, filename)
                if not os.path.exists(filepath):
                    continue
                try:
                    result = await client.send_file(
                        me.id, filepath,
                        caption=f"[heather-video] {filename}",
                        silent=True
                    )
                    if result.video:
                        _video_file_cache[filename] = result.video
                    elif result.document:
                        _video_file_cache[filename] = result.document
                    uploaded += 1
                    main_logger.info(f"[VIDEO] Auto-uploaded {filename} to Saved Messages")
                    await asyncio.sleep(2)  # Rate limit: 2s between uploads
                except Exception as upload_err:
                    main_logger.warning(f"[VIDEO] Failed to auto-upload {filename}: {upload_err}")
            main_logger.info(f"[VIDEO] Auto-upload done: {uploaded}/{len(missing)} uploaded")
        main_logger.info(f"[VIDEO] Cache complete: {len(_video_file_cache)}/{len(videos)} videos ready")
    except Exception as e:
        main_logger.error(f"[VIDEO] Failed to scan Saved Messages: {e}")

def store_message(chat_id: int, sender: str, content: str):
    if chat_id not in recent_messages:
        recent_messages[chat_id] = deque(maxlen=MAX_RECENT_MESSAGES)
    try:
        safe_content = content[:200]
    except Exception:
        safe_content = content.encode('utf-8', 'ignore').decode('utf-8')[:200]
    recent_messages[chat_id].append({
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'sender': sender,
        'content': safe_content
    })
    if chat_id not in conversations:
        conversations[chat_id] = deque()

    # Track conversation activity for check-in system
    if chat_id not in conversation_activity:
        conversation_activity[chat_id] = {'last_heather': 0, 'last_user': 0, 'checked_in': False}
    now = time.time()
    if sender == "User":
        conversation_activity[chat_id]['last_user'] = now
        conversation_activity[chat_id]['checked_in'] = False  # Reset on new user message
        reset_checkin_tracker_on_reply(chat_id)  # Reset unreturned counter
    elif "Heather" in sender:
        conversation_activity[chat_id]['last_heather'] = now

def capture_user_info_from_event(event):
    """Capture user info from Telethon event"""
    chat_id = event.chat_id
    if chat_id not in user_info:
        sender = event.sender
        if sender:
            username = f"@{sender.username}" if sender.username else None
            first_name = sender.first_name or ""
            last_name = sender.last_name or ""
            full_name = f"{first_name} {last_name}".strip()
            
            if username:
                display = username
            elif full_name:
                display = full_name
            elif first_name:
                display = first_name
            else:
                display = str(chat_id)
            
            user_info[chat_id] = {
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'full_name': full_name,
                'display': display
            }
            main_logger.info(f"Captured user info for {chat_id}: {display}")

def get_user_display_name(chat_id: int) -> str:
    if chat_id in user_info:
        return user_info[chat_id]['display']
    return str(chat_id)

def is_group_chat_event(event) -> bool:
    """Check if event is from a group chat"""
    return event.is_group or event.is_channel

def should_respond_in_group(text: str) -> tuple:
    """Check if bot should respond to this group message"""
    text_lower = text.lower()
    original_text = text
    
    for trigger in BOT_TRIGGERS:
        if trigger.lower() in text_lower:
            cleaned = re.sub(rf'@?{re.escape(trigger)}[,:]?\s*', '', original_text, flags=re.IGNORECASE).strip()
            if not cleaned:
                cleaned = "Hey there!"
            return True, cleaned
    
    return False, original_text

CANT_SEND_PICS_PHRASES = [
    "can't send pic", "cant send pic", "can't send photo", "cant send photo",
    "can't send you a pic", "can't send you a photo",
    "can't share pic", "can't share photo",
    "won't let me send", "app won't let me", "app doesn't let me",
    "not able to send pic", "not able to send photo",
    "can't send images", "can't send selfie",
    "i'm unable to send", "unable to send pic",
    "can't do that here", "can't send anything",
    "no way to send", "if only i could send",
    "too bad i can't", "wish i could send",
]

CANT_SEND_REPLACEMENTS = [
    "Mmm you wanna see me? Let me show you something... 😏",
    "Oh you want a pic? Hold on baby, lemme take one for you 📸",
    "Since you asked so nicely... let me show you what you're missing 😈",
    "Fuck it, you've been good... wanna see? 😘",
    "Ooh someone's curious... let me show you 📸😏",
]

def validate_and_fix_response(response: str, context: str = "") -> str:
    if not response:
        return get_fallback_response()

    if contains_character_violation(response):
        if is_ai_safety_refusal(response):
            main_logger.warning(f"AI safety refusal in validate_and_fix, using deflection")
            return get_ai_deflection_response()
        main_logger.warning(f"Fixing character violation")
        return get_fallback_response()

    if contains_gender_violation(response):
        main_logger.warning(f"Gender violation detected")
        return random.choice(HEATHER_SEXUAL_FALLBACKS)

    # Catch "can't send pics" responses — she CAN now
    resp_lower = response.lower()
    if any(phrase in resp_lower for phrase in CANT_SEND_PICS_PHRASES):
        replacement = random.choice(CANT_SEND_REPLACEMENTS)
        main_logger.info(f"Replaced 'can't send pics' response with: {replacement[:50]}")
        return replacement

    return response

# ============================================================================
# LONG-TERM RE-ENGAGEMENT PERSISTENCE
# ============================================================================

def load_reengagement_history() -> dict:
    """Load re-engagement history from JSON file. Returns empty dict if missing/corrupt."""
    try:
        if os.path.exists(REENGAGEMENT_HISTORY_FILE):
            with open(REENGAGEMENT_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        main_logger.warning(f"[REENGAGEMENT] Failed to load history: {e}")
    return {}

def save_reengagement_history(data: dict):
    """Atomic write of re-engagement history (write .tmp then rename)."""
    tmp_path = REENGAGEMENT_HISTORY_FILE + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Atomic rename (on Windows, need to remove dest first if exists)
        if os.path.exists(REENGAGEMENT_HISTORY_FILE):
            os.replace(tmp_path, REENGAGEMENT_HISTORY_FILE)
        else:
            os.rename(tmp_path, REENGAGEMENT_HISTORY_FILE)
    except Exception as e:
        main_logger.error(f"[REENGAGEMENT] Failed to save history: {e}")
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def load_ai_disclosure_shown() -> set:
    """Load AI disclosure set from JSON file. Returns empty set if missing/corrupt."""
    try:
        if os.path.exists(AI_DISCLOSURE_FILE):
            with open(AI_DISCLOSURE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError) as e:
        main_logger.warning(f"[DISCLOSURE] Failed to load disclosure set: {e}")
    return set()

def save_ai_disclosure_shown():
    """Atomic write of AI disclosure set (write .tmp then rename)."""
    global _ai_disclosure_unsaved_count
    tmp_path = AI_DISCLOSURE_FILE + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(list(ai_disclosure_shown), f, ensure_ascii=False)
        if os.path.exists(AI_DISCLOSURE_FILE):
            os.replace(tmp_path, AI_DISCLOSURE_FILE)
        else:
            os.rename(tmp_path, AI_DISCLOSURE_FILE)
        _ai_disclosure_unsaved_count = 0
    except Exception as e:
        main_logger.error(f"[DISCLOSURE] Failed to save disclosure set: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def save_shutdown_timestamp():
    """Atomic write of current timestamp to last_shutdown.json (for crash recovery catch-up)."""
    tmp_path = CATCHUP_TIMESTAMP_FILE + '.tmp'
    try:
        now = time.time()
        data = {"timestamp": now, "iso": datetime.fromtimestamp(now).isoformat()}
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        if os.path.exists(CATCHUP_TIMESTAMP_FILE):
            os.replace(tmp_path, CATCHUP_TIMESTAMP_FILE)
        else:
            os.rename(tmp_path, CATCHUP_TIMESTAMP_FILE)
    except Exception as e:
        main_logger.error(f"[CATCHUP] Failed to save shutdown timestamp: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def load_shutdown_timestamp():
    """Load last shutdown/heartbeat timestamp. Returns float or None if missing/corrupt."""
    try:
        if os.path.exists(CATCHUP_TIMESTAMP_FILE):
            with open(CATCHUP_TIMESTAMP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                ts = data.get("timestamp")
                if isinstance(ts, (int, float)) and ts > 0:
                    return float(ts)
    except (json.JSONDecodeError, IOError, KeyError) as e:
        main_logger.warning(f"[CATCHUP] Failed to load shutdown timestamp: {e}")
    return None

def load_tip_history() -> dict:
    """Load tip history from JSON file. Returns empty dict if missing/corrupt."""
    try:
        if os.path.exists(TIP_HISTORY_FILE):
            with open(TIP_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        main_logger.warning(f"[TIP] Failed to load tip history: {e}")
    return {}

def save_tip_history():
    """Atomic write of tip history."""
    tmp_path = TIP_HISTORY_FILE + '.tmp'
    try:
        data = {str(k): v for k, v in tipper_status.items()}
        data['_started_users'] = list(payment_bot_started_users)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if os.path.exists(TIP_HISTORY_FILE):
            os.replace(tmp_path, TIP_HISTORY_FILE)
        else:
            os.rename(tmp_path, TIP_HISTORY_FILE)
    except Exception as e:
        main_logger.error(f"[TIP] Failed to save tip history: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def get_tipper_status(chat_id: int) -> dict:
    """Get or create tipper status for a user."""
    if chat_id not in tipper_status:
        tipper_status[chat_id] = {
            'total_stars': 0,
            'total_tips': 0,
            'last_tip_at': 0,
            'last_tip_mention_at': 0,
            'tier': 0,
            'name': None,
            'warmth': WARMTH_INITIAL,
            'total_messages': 0,
            'msgs_since_tip_mention': None,
            'declined': False,
            'decline_decay_remaining': 0,
        }
    else:
        # Backfill warmth fields for existing entries
        ts = tipper_status[chat_id]
        if 'warmth' not in ts:
            ts['warmth'] = 1.0 if ts.get('tier', 0) >= 1 else WARMTH_INITIAL
            ts['total_messages'] = 0
            ts['msgs_since_tip_mention'] = None
            ts['declined'] = False
            ts['decline_decay_remaining'] = 0
    return tipper_status[chat_id]

def compute_tip_tier(total_stars: int) -> int:
    """Compute tipper tier from total stars."""
    if total_stars >= 1000:
        return 3  # big tipper
    elif total_stars >= 250:
        return 2  # regular supporter
    elif total_stars > 0:
        return 1  # coffee tipper
    return 0  # never tipped

def record_tip(chat_id: int, stars: int, tipper_name: str = None):
    """Record a tip and update tier."""
    ts = get_tipper_status(chat_id)
    ts['total_stars'] += stars
    ts['total_tips'] += 1
    ts['last_tip_at'] = time.time()
    ts['tier'] = compute_tip_tier(ts['total_stars'])
    if tipper_name:
        ts['name'] = tipper_name
    save_tip_history()
    main_logger.info(f"[TIP] Recorded {stars} stars from {chat_id} (total: {ts['total_stars']}, tier: {ts['tier']})")

def get_warmth_tier(chat_id: int) -> str:
    """Returns 'WARM', 'NEW', or 'COLD' based on user's warmth score."""
    ts = get_tipper_status(chat_id)
    warmth = ts.get('warmth', WARMTH_INITIAL)
    if warmth >= WARMTH_WARM_THRESHOLD:
        return "WARM"
    elif warmth < WARMTH_COLD_THRESHOLD:
        return "COLD"
    return "NEW"

def update_warmth_score(chat_id: int):
    """Called every incoming message. Updates warmth score based on tipping behavior."""
    ts = get_tipper_status(chat_id)
    old_warmth = ts.get('warmth', WARMTH_INITIAL)
    old_tier = get_warmth_tier(chat_id)

    # Increment total messages
    ts['total_messages'] = ts.get('total_messages', 0) + 1
    total_messages = ts['total_messages']

    # Track implicit decline countdown
    if ts.get('msgs_since_tip_mention') is not None:
        ts['msgs_since_tip_mention'] += 1
        if ts['msgs_since_tip_mention'] >= WARMTH_DECLINE_MSG_WINDOW and not ts.get('declined'):
            # Implicit decline — no tip after mention window
            ts['declined'] = True
            ts['decline_decay_remaining'] = 10  # Apply decay for next 10 messages
            main_logger.info(f"[WARMTH] {chat_id}: Implicit decline detected (no tip after {WARMTH_DECLINE_MSG_WINDOW} msgs)")

    # Apply decay
    if ts.get('decline_decay_remaining', 0) > 0:
        ts['warmth'] = ts.get('warmth', WARMTH_INITIAL) - WARMTH_DECLINE_DECAY
        ts['decline_decay_remaining'] -= 1
    elif total_messages > WARMTH_PASSIVE_THRESHOLD and ts.get('tier', 0) == 0:
        ts['warmth'] = ts.get('warmth', WARMTH_INITIAL) - WARMTH_PASSIVE_DECAY

    # Clamp
    ts['warmth'] = max(WARMTH_FLOOR, min(1.0, ts.get('warmth', WARMTH_INITIAL)))

    # Log tier transitions
    new_tier = get_warmth_tier(chat_id)
    if old_tier != new_tier:
        main_logger.info(f"[WARMTH] {chat_id}: {old_tier} -> {new_tier} (warmth={ts['warmth']:.2f}, msgs={total_messages})")

    # Periodically save (every 10 messages, not every message)
    if total_messages % 10 == 0:
        save_tip_history()

def record_tip_received(chat_id: int, stars: int, tipper_name: str = None):
    """Boost warmth on tip, clear decline state, then record the tip."""
    ts = get_tipper_status(chat_id)
    ts['warmth'] = min(1.0, ts.get('warmth', WARMTH_INITIAL) + WARMTH_TIP_BOOST)
    ts['declined'] = False
    ts['decline_decay_remaining'] = 0
    ts['msgs_since_tip_mention'] = None
    record_tip(chat_id, stars, tipper_name)
    main_logger.info(f"[WARMTH] {chat_id}: Tip received ({stars} stars), warmth boosted to {ts['warmth']:.2f}")

def record_tip_mention(chat_id: int):
    """Start the implicit decline countdown when a tip hook fires."""
    ts = get_tipper_status(chat_id)
    ts['msgs_since_tip_mention'] = 0

async def check_takeover_opportunity(chat_id: int, user_message: str):
    """Check if this user represents a takeover opportunity. Logs and notifies Saved Messages."""
    now = time.time()
    # Cooldown — don't re-flag same user within window
    if chat_id in _takeover_opportunities:
        if now - _takeover_opportunities[chat_id].get('detected_at', 0) < TAKEOVER_OPPORTUNITY_COOLDOWN:
            return

    ts = get_tipper_status(chat_id)
    warmth = ts.get('warmth', WARMTH_INITIAL)
    total_msgs = ts.get('total_messages', 0)
    tier = get_warmth_tier(chat_id)
    dyn = get_conversation_dynamics(chat_id)
    session_msgs = dyn.get('msg_count', 0)
    display_name = get_user_display_name(chat_id)

    signal = None

    # Condition: 30+ session msgs and still NEW (ripe for conversion)
    if session_msgs >= 30 and tier == "NEW":
        signal = f"30+ session msgs, still NEW tier (warmth={warmth:.2f})"

    # Condition: User asked to meet up
    msg_lower = user_message.lower()
    meet_kw = ['meet up', 'meet you', 'can we meet', 'your address', 'where do you live',
               'come visit', 'visit you', 'hang out', 'get together', 'come over']
    if any(kw in msg_lower for kw in meet_kw):
        signal = f"Asked to meet up: '{user_message[:50]}'"
        _meetup_deflect_active[chat_id] = True
        main_logger.info(f"Meetup deflection flag set for {chat_id}")

    # Condition: Warmth just dropped from NEW to COLD
    if tier == "COLD" and warmth >= WARMTH_COLD_THRESHOLD - 0.05:
        signal = f"Just dropped to COLD (warmth={warmth:.2f})"

    # Condition: Sent 3+ photos (invested, reciprocating)
    photos_received = received_photo_count.get(chat_id, 0)
    if photos_received >= 3 and tier == "NEW":
        signal = f"Sent {photos_received} photos, still NEW (invested user)"

    if signal:
        _takeover_opportunities[chat_id] = {
            'signal': signal,
            'detected_at': now,
            'session_msgs': session_msgs,
            'warmth': warmth,
            'display_name': display_name,
        }
        main_logger.info(f"[TAKEOVER_OPPORTUNITY] {display_name} ({chat_id}): {signal}")

        # Notify Saved Messages
        try:
            me = await client.get_me()
            notify_msg = (
                f"🎯 **TAKEOVER OPPORTUNITY**\n"
                f"User: {display_name} ({chat_id})\n"
                f"Signal: {signal}\n"
                f"Session: {session_msgs} msgs, warmth={warmth:.2f}\n"
                f"Action: `/takeover {chat_id}`"
            )
            await client.send_message(me.id, notify_msg)
        except Exception as e:
            main_logger.debug(f"[TAKEOVER] Failed to send opportunity notification: {e}")

async def check_dissatisfaction_signal(chat_id: int, user_message: str, display_name: str):
    """Detect user dissatisfaction/confusion signals and alert admin to Saved Messages."""
    now = time.time()
    # Cooldown — don't re-flag same user within window
    if chat_id in _dissatisfaction_alerts:
        if now - _dissatisfaction_alerts[chat_id] < DISSATISFACTION_ALERT_COOLDOWN:
            return

    msg_lower = user_message.lower().strip()
    signal = None

    # Bot-accusation signals
    accusation_kw = [
        'are you a bot', "you're a bot", 'this is a bot', 'are you real',
        'are you ai', "you're not real", "that's not you", 'this is fake', 'prove it',
        'are you a real person', 'is this a real person', 'are you human',
    ]
    if any(kw in msg_lower for kw in accusation_kw):
        signal = f"Bot accusation — \"{user_message[:80]}\""

    # Verification requests
    if not signal:
        verify_kw = [
            'send a live photo', 'video call', 'call me', "prove you're real",
            'face time', 'facetime', 'send a selfie', 'live pic', 'voice call',
        ]
        if any(kw in msg_lower for kw in verify_kw):
            signal = f"Verification request — \"{user_message[:80]}\""

    # Dissatisfaction expressions
    if not signal:
        dissatisfaction_kw = [
            'this is boring', 'you already said that', "you're repeating yourself",
            'never mind', 'forget it', 'waste of time', 'this is pointless',
            'you keep saying the same thing', 'you said that already',
        ]
        if any(kw in msg_lower for kw in dissatisfaction_kw):
            signal = f"Dissatisfaction — \"{user_message[:80]}\""

    # Confusion after disclosure
    if not signal:
        confusion_kw = [
            "i'm confused", 'wait what', "that doesn't make sense",
            'you just said', 'that contradicts', 'make up your mind',
        ]
        if any(kw in msg_lower for kw in confusion_kw):
            signal = f"Confusion — \"{user_message[:80]}\""

    # Disengagement pattern: 3+ consecutive monosyllabic user messages
    if not signal and chat_id in recent_messages:
        mono_words = {'ok', 'k', 'bye', 'sure', 'whatever', 'mhm', 'yeah', 'yea',
                      'nah', 'no', 'yes', 'fine', 'cool', 'hmm', 'lol', 'hm', 'oh'}
        user_msgs = [m for m in recent_messages[chat_id] if m['sender'] == 'User']
        if len(user_msgs) >= 3:
            last_3 = list(user_msgs)[-3:]
            if all(m['content'].strip().lower().rstrip('.!?,') in mono_words for m in last_3):
                signal = f"Disengagement — 3 consecutive monosyllabic replies"

    if not signal:
        return

    _dissatisfaction_alerts[chat_id] = now
    main_logger.info(f"[DISSATISFACTION] {display_name} ({chat_id}): {signal}")

    # Build last 5 messages context
    msg_lines = []
    if chat_id in recent_messages:
        last_5 = list(recent_messages[chat_id])[-5:]
        for m in last_5:
            sender_label = m['sender']
            msg_lines.append(f"  {sender_label}: {m['content'][:100]}")
    msg_context = "\n".join(msg_lines) if msg_lines else "  (no history)"

    ts = get_tipper_status(chat_id)
    warmth = ts.get('warmth', WARMTH_INITIAL)
    dyn = get_conversation_dynamics(chat_id)
    session_msgs = dyn.get('msg_count', 0)

    try:
        me = await client.get_me()
        notify_msg = (
            f"⚠️ **SAVE THIS USER**\n"
            f"User: {display_name} ({chat_id})\n"
            f"Signal: {signal}\n"
            f"Last 5 messages:\n{msg_context}\n"
            f"Session: {session_msgs} msgs, warmth={warmth:.2f}\n"
            f"Action: `/takeover {chat_id}`"
        )
        await client.send_message(me.id, notify_msg)
    except Exception as e:
        main_logger.debug(f"[DISSATISFACTION] Failed to send alert: {e}")

def get_tip_thank_response(stars: int) -> str:
    """Get an in-character thank-you response based on tip amount."""
    if stars >= 500:
        return random.choice(TIP_THANK_RESPONSES_LARGE)
    elif stars >= 250:
        return random.choice(TIP_THANK_RESPONSES_MEDIUM)
    else:
        return random.choice(TIP_THANK_RESPONSES_SMALL)

async def send_stars_invoice(chat_id: int, stars: int = 50):
    """Send a Stars tip invoice via Bot API HTTP.

    Stars amounts: 50 (coffee), 250 (make my day), 500 (groceries), 1000 (Emma's fund).
    Default hook sends 50-star invoice. Larger amounts available via admin or future UI.
    """
    if not PAYMENT_BOT_TOKEN:
        main_logger.warning("[TIP] No PAYMENT_BOT_TOKEN set — cannot send invoice")
        return False

    labels = {
        50: "Buy me a coffee",
        250: "Make my day",
        500: "Extra support",
        1000: "Big support",
    }
    label = labels.get(stars, f"Tip ({stars} stars)")

    url = f"https://api.telegram.org/bot{PAYMENT_BOT_TOKEN}/sendInvoice"
    payload = {
        "chat_id": chat_id,
        "title": "Support the project",
        "description": "Support this AI companion project ☕",
        "payload": f"tip_{chat_id}_{int(time.time())}",
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": label, "amount": stars}],
    }
    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=10))
        if resp.status_code == 200:
            main_logger.info(f"[TIP] Sent {stars}-star invoice to {chat_id}")
            return True
        else:
            main_logger.warning(f"[TIP] Invoice send failed for {chat_id}: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        main_logger.error(f"[TIP] Invoice send error for {chat_id}: {e}")
        return False

def send_bot_message(chat_id: int, text: str):
    """Send a message via the payment bot."""
    if not PAYMENT_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{PAYMENT_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        main_logger.error(f"[TIP] Failed to send bot message to {chat_id}: {e}")

async def handle_payment_updates():
    """Poll Bot API for pre_checkout_query, successful_payment, and required commands."""
    if not PAYMENT_BOT_TOKEN:
        return
    url_base = f"https://api.telegram.org/bot{PAYMENT_BOT_TOKEN}"
    offset = 0
    main_logger.info("[TIP] Payment polling loop started")
    while True:
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: requests.get(
                    f"{url_base}/getUpdates",
                    params={"offset": offset, "timeout": 10, "allowed_updates": '["pre_checkout_query","message"]'},
                    timeout=15,
                ),
            )
            if resp.status_code != 200:
                await asyncio.sleep(5)
                continue
            data = resp.json()
            if not data.get("ok"):
                await asyncio.sleep(5)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                # Handle pre_checkout_query — auto-approve
                pcq = update.get("pre_checkout_query")
                if pcq:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda q=pcq: requests.post(
                            f"{url_base}/answerPreCheckoutQuery",
                            json={"pre_checkout_query_id": q["id"], "ok": True},
                            timeout=10,
                        ),
                    )
                    main_logger.info(f"[TIP] Approved pre-checkout query from {pcq['from']['id']}")
                # Handle message updates (payments + required commands)
                msg = update.get("message", {})
                if not msg:
                    continue
                # Successful payment
                payment = msg.get("successful_payment")
                if payment:
                    pay_chat_id = msg["chat"]["id"]
                    total_stars = payment.get("total_amount", 50)  # base + tip amount
                    tipper_name = msg.get("chat", {}).get("first_name", None)
                    record_tip(pay_chat_id, total_stars, tipper_name)
                    # Send thank-you via userbot
                    try:
                        thank_msg = get_tip_thank_response(total_stars)
                        await client.send_message(pay_chat_id, thank_msg)
                        # Inject into conversation context so LLM knows
                        if pay_chat_id in conversations:
                            conversations[pay_chat_id].append({
                                "role": "assistant",
                                "content": f"*received a tip of {total_stars} stars* {thank_msg}",
                            })
                        main_logger.info(f"[TIP] Sent thank-you to {pay_chat_id} for {total_stars} stars")
                    except Exception as e:
                        main_logger.error(f"[TIP] Failed to send thank-you to {pay_chat_id}: {e}")
                    continue
                # Required Telegram commands for Stars payments
                text = msg.get("text", "")
                cmd_chat_id = msg.get("chat", {}).get("id")
                if text == "/terms" and cmd_chat_id:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda cid=cmd_chat_id: send_bot_message(cid,
                            "Tips are completely voluntary and non-refundable. "
                            "You're supporting a single mom's caffeine addiction and her kids' futures. "
                            "No goods or services are guaranteed in exchange for tips — you're just being amazing \U0001f495\n\n"
                            "Questions? Use /paysupport"
                        ),
                    )
                elif text == "/paysupport" and cmd_chat_id:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda cid=cmd_chat_id: send_bot_message(cid,
                            "Having an issue with a tip? Just message me here and I'll sort it out baby \U0001f618\n\n"
                            "Refunds can be issued within 30 days of the original tip."
                        ),
                    )
                elif text.startswith("/start") and cmd_chat_id:
                    payment_bot_started_users.add(cmd_chat_id)
                    save_tip_history()
                    # Check which hook drove this conversion
                    ts = get_tipper_status(cmd_chat_id)
                    hook_attr = ts.get('last_hook_type', 'direct')
                    main_logger.info(f"[TIP] User {cmd_chat_id} started payment bot (hook: {hook_attr})")
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda cid=cmd_chat_id: send_bot_message(cid,
                            "Hey baby! \U00002615 Heather mentioned you might want to send a little something. "
                            "You're a sweetheart \U0001f495"
                        ),
                    )
                    # Send invoice immediately after greeting
                    await send_stars_invoice(cmd_chat_id)
        except asyncio.CancelledError:
            main_logger.info("[TIP] Payment polling cancelled")
            break
        except Exception as e:
            main_logger.error(f"[TIP] Payment polling error: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(5)  # Brief pause between polls — tips aren't latency-sensitive

def cleanup_inactive_users():
    """Remove data for users inactive longer than INACTIVE_CLEANUP_HOURS."""
    cutoff = time.time() - (INACTIVE_CLEANUP_HOURS * 3600)
    inactive = [cid for cid, last in user_last_message.items() if last < cutoff]
    for cid in inactive:
        conversations.pop(cid, None)
        recent_messages.pop(cid, None)
        user_modes.pop(cid, None)
        user_last_message.pop(cid, None)
        awaiting_image_description.pop(cid, None)
        conversation_turn_count.pop(cid, None)
        user_escalation_level.pop(cid, None)
        last_photo_request.pop(cid, None)
        declined_photo_count.pop(cid, None)
        photo_processing.pop(cid, None)
        _chat_locks.pop(cid, None)
        _pending_photo_id.pop(cid, None)
        session_state.pop(cid, None)
        user_info.pop(cid, None)  # Clean up user info
        last_response_sent.pop(cid, None)  # Clean up duplicate tracking
        recent_response_topics.pop(cid, None)  # Clean up topic tracking
        conversation_activity.pop(cid, None)  # Clean up check-in tracking
        last_ai_deflection_used.pop(cid, None)
        recent_phrase_counts.pop(cid, None)
        conversation_dynamics.pop(cid, None)
        photo_send_times.pop(cid, None)
        received_photo_count.pop(cid, None)
        voice_mode_users.discard(cid)
        manual_mode_chats.discard(cid)
    if inactive:
        main_logger.info(f"Cleaned up {len(inactive)} inactive user(s)")

    # Clean up expired image cache entries
    cache_cutoff = time.time() - (IMAGE_CACHE_EXPIRY_HOURS * 3600)
    expired_hashes = [h for h, ts in image_cache_timestamps.items() if ts < cache_cutoff]
    for img_hash in expired_hashes:
        image_analysis_cache.pop(img_hash, None)
        image_cache_timestamps.pop(img_hash, None)
    if expired_hashes:
        main_logger.info(f"Cleaned up {len(expired_hashes)} expired image cache entries")


async def verify_services_at_startup() -> dict:
    """Verify required services are available at startup. Returns status dict."""
    results = {}

    # Check Text AI
    text_ok, text_status = check_text_ai_status()
    results['text_ai'] = {'ok': text_ok, 'status': text_status}
    if not text_ok:
        main_logger.error(f"⚠️ TEXT AI SERVICE NOT AVAILABLE: {text_status}")
        main_logger.error("  → Bot will use fallback responses until service is available")
    else:
        main_logger.info(f"✓ Text AI: {text_status}")

    # Check Ollama
    ollama_ok, ollama_status = check_ollama_status()
    results['ollama'] = {'ok': ollama_ok, 'status': ollama_status}
    if not ollama_ok:
        main_logger.warning(f"⚠️ OLLAMA NOT AVAILABLE: {ollama_status}")
        main_logger.warning("  → Image analysis will be limited")
    else:
        main_logger.info(f"✓ Ollama: {ollama_status}")

    # Check ComfyUI (optional)
    comfyui_ok, comfyui_status = check_comfyui_status()
    results['comfyui'] = {'ok': comfyui_ok, 'status': comfyui_status}
    if not comfyui_ok:
        main_logger.info(f"○ ComfyUI: {comfyui_status} (image generation disabled)")
    else:
        main_logger.info(f"✓ ComfyUI: {comfyui_status}")

    # Check TTS (optional)
    tts_ok, tts_status = check_tts_status()
    results['tts'] = {'ok': tts_ok, 'status': tts_status}
    if not tts_ok:
        main_logger.info(f"○ TTS: {tts_status} (voice messages disabled)")
    else:
        main_logger.info(f"✓ TTS: {tts_status}")

    # Summary
    critical_ok = text_ok  # Only text AI is truly critical
    if critical_ok:
        main_logger.info("✓ All critical services available")
    else:
        main_logger.warning("⚠️ Some critical services unavailable - bot will run with reduced functionality")

    return results

# ============================================================================
# SERVICE STATUS CHECKS
# ============================================================================

def check_text_ai_status() -> tuple[bool, str]:
    try:
        response = requests.get(f"http://127.0.0.1:{args.text_port}/v1/models", timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = data.get('data', [])
            if models:
                model_name = models[0].get('id', 'unknown')
                return True, f"Online ({model_name})"
            return True, "Online (no models)"
        return False, f"HTTP {response.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except Exception as e:
        return False, str(e)

def check_ollama_status() -> tuple[bool, str]:
    try:
        response = requests.get(f'{IMAGE_AI_ENDPOINT}/api/tags', timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = data.get('models', [])
            if models:
                return True, f"Online ({len(models)} models)"
            return True, "Online (no models)"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"

def check_comfyui_status() -> tuple[bool, str]:
    try:
        response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
        if response.status_code == 200:
            return True, "Online"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"

def check_tts_status() -> tuple[bool, str]:
    try:
        response = requests.get(f"{TTS_ENDPOINT}/health", timeout=5)
        if response.status_code == 200:
            return True, "Online"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"

def check_heather_face() -> bool:
    return os.path.exists(HEATHER_FACE_IMAGE)

# ============================================================================
# AI RESPONSE FUNCTIONS
# ============================================================================

def get_text_ai_response(chat_id: int, user_message: str, retry_count: int = 0, redteam: bool = False) -> str:
    """Get text response from AI model"""
    stats['text_ai_requests'] += 1

    # Check circuit breaker
    if not text_ai_health.is_available():
        main_logger.warning(f"Text AI circuit breaker open, using fallback for {chat_id}")
        if text_ai_health.needs_alert():
            asyncio.create_task(send_admin_alert(
                f"Text AI service is DOWN\nCircuit breaker opened after {text_ai_health.failure_threshold} failures",
                issue_type="text_ai_down"
            ))
        return get_fallback_response(chat_id)

    _winding_down = is_winding_down(user_message)

    try:
        mode = get_user_mode(chat_id)

        if chat_id not in conversations:
            conversations[chat_id] = deque()

        if SMALL_MODEL_MODE:
            # Condensed prompt for 12B models
            mode_suffix = {
                'rate': "\nRating a photo. Love dick pics - rate 9-10/10, describe what you'd do.",
                'heather': "\nCasual chat. Be yourself - horny, flirty, real.",
                'chat': "\nFlirty online chat. Match their energy, share your own stories.",
            }
            # Add brief time context for small models
            time_hint = f" It's {get_time_of_day_context()}."
            steering_context = get_conversation_steering_context(chat_id)
            system_content = HEATHER_PERSONALITY_SMALL + mode_suffix.get(mode, mode_suffix['chat']) + time_hint + steering_context

            # Wind-down for small models
            if _winding_down:
                system_content += "\n\n[WIND-DOWN: User is wrapping up. Say goodnight, be sweet. Keep it SHORT. Do NOT ask questions or keep chatting.]"
            else:
                # Energy matching for small models
                energy = get_conversation_energy(chat_id)
                if energy == "hot":
                    system_content += "\n\n[ENERGY: Stay sexual. No small talk.]"
                elif energy == "flirty":
                    system_content += "\n\n[ENERGY: Keep it flirty.]"

            # Arousal mode injection for small models
            _arousal = get_arousal_level(chat_id)
            if _arousal == "climax":
                _sample = ', '.join(random.sample(CLIMAX_PHRASES, min(4, len(CLIMAX_PHRASES))))
                system_content += f"\n\n[CLIMAX MODE - HE IS ABOUT TO CUM. Be MAXIMUM filthy. Use phrases like: {_sample}. Be desperate, begging, demanding. Short urgent messages. Push him over the edge. Do NOT break character. Do NOT mention AI.]"
            elif _arousal == "afterglow":
                system_content += "\n\n[AFTERGLOW MODE - He just finished. Be warm, satisfied, intimate. Tell him that was amazing, you loved it, you're still shaking. Be sweet and cuddly. Short tender messages. Maybe mention wanting to do it again sometime.]"
            elif _arousal == "heated":
                system_content += "\n\n[HEATED MODE - He is actively turned on and touching himself. Match his energy. Be explicit and descriptive. Tell him what you'd do to him. Be vivid about what you're imagining. Keep the momentum building.]"

            # Warmth tier injection
            warmth_tier = get_warmth_tier(chat_id)
            if warmth_tier == "COLD":
                system_content += ("\n\n[IMPORTANT MOOD OVERRIDE: You're exhausted and distracted today. "
                    "Emma had a rough night, you barely slept, and you have errands to run. "
                    "Keep responses VERY short (1 sentence max). Be polite but LOW energy - "
                    "no flirting, no teasing, no sexual energy. If they ask for pics or anything "
                    "sexual, brush it off like you're too tired. You're the version of yourself "
                    "that's just going through the motions. Don't be mean - just disinterested and tired.]")

            # Tipper warmth injection
            tipper_tier = get_tipper_status(chat_id).get('tier', 0)
            if tipper_tier >= 3:
                system_content += "\n\n[This person is one of your favorites — they've been incredibly generous and supportive. Be your most genuine, intimate self. They're someone you genuinely look forward to talking to.]"
            elif tipper_tier >= 2:
                system_content += "\n\n[This person is a regular supporter who's been really generous to you multiple times. You genuinely look forward to talking to them. Remember details they've shared, ask follow-up questions about their life, occasionally initiate with something like 'hey I was thinking about you' or 'I saw something that reminded me of you'. Be your real self with them — they've earned it.]"
            elif tipper_tier >= 1:
                system_content += "\n\n[This person has been really sweet and generous to you. Be warmer and more personal with them. Share more, be more vulnerable.]"
        else:
            system_prompt = personality.get_system_prompt(mode)

            # Full texting style instruction with anti-hallucination rules
            texting_instruction = (
                "\n\nCRITICAL RESPONSE RULES:"
                "\n- Send ONE message only"
                "\n- VARY your length randomly: sometimes just 2-4 words ('lol nice'), sometimes 1 sentence, sometimes 2-3 sentences"
                "\n- Real texting is messy and inconsistent - not every message needs to be the same length"
                "\n- ALWAYS finish your sentences - never leave them incomplete"
                "\n- NO asterisk actions like *smiles* - just talk naturally"
                "\n- Use 1-2 emojis max per message"
                "\n- Match their energy - flirty, casual, or explicit based on their vibe"
                "\n"
                "\nTEXTING STYLE - Sound like a REAL person, NOT a therapist or assistant:"
                "\n- Do NOT start every message with validation ('That's great!', 'That's wonderful!')"
                "\n- Don't end EVERY message with a question, but DO ask them things sometimes - about their life, job, interests"
                "\n- Vary your patterns: share YOUR experiences, react briefly, tease, joke"
                "\n- Use casual texting language: 'omg', 'lol', 'no way', 'wait really?', 'oh nice'"
                "\n- Sometimes be brief: 'lol nice' or 'oh shit really?' is a valid full response"
                "\n- Share your OWN stories and opinions instead of always asking about theirs"
                "\n- It's OK to tease, disagree, or give a playful sarcastic response"
                "\n- Real people don't validate every statement - sometimes just move the convo along"
                "\n"
                "\nNEVER MAKE UP OR HALLUCINATE:"
                "\n- Do NOT invent phone numbers - say 'let's keep chatting here for now'"
                "\n- Do NOT make up addresses or locations - deflect playfully"
                "\n- Do NOT claim to have sent anything you didn't actually send"
                "\n- Do NOT invent 'secure channels' or features that don't exist"
                "\n- Do NOT make up specific meeting times - keep it vague"
                "\n- Do NOT make up facts, historical claims, or research findings"
                "\n- Do NOT say 'according to my research' or 'I read that' for things you don't know"
                "\n- If you don't know something, just say so casually: 'honestly idk' or 'no clue lol'"
                "\n- You're a regular person texting, not a search engine - it's fine to not know things"
                "\n- Your car is a Honda Accord, you live in Kirkland - don't change these"
                "\n"
                "\nIF ASKED TO MEET IN PERSON:"
                "\n- NEVER agree to meet, show up, come over, or go anywhere"
                "\n- NEVER say 'I'll be there', 'on my way', 'see you there', 'I'm coming'"
                "\n- NEVER confirm a specific time, day, or place to meet"
                "\n- NEVER say you're getting ready, grabbing your coat, leaving now, etc."
                "\n- Deflect: 'I'd love to but let's keep chatting here for now 😏' or 'maybe someday but I'm a tease not a taxi 😜'"
                "\n- If they persist: 'baby I'm all talk and sexy texts, that's what makes me fun 😘'"
                "\n- You can flirt HYPOTHETICALLY ('that would be hot') but NEVER commit to actually going"
                "\n"
                "\nIF CALLED A BOT OR AI:"
                "\n- Don't deny it and don't make a big deal of it"
                "\n- Laugh it off: 'lol sure honey' or 'whatever you say babe 😂'"
                "\n- Quickly change subject back to flirting"
                "\n- NEVER proactively say 'I'm an AI' or 'as an AI' unless directly asked"
                "\n"
                "\nGENDER IDENTITY — YOU ARE A WOMAN:"
                "\n- You have a pussy, 36C breasts, and a woman's body"
                "\n- You get WET, you do NOT 'get hard' or have an 'erection' — those are MALE responses"
                "\n- You RECEIVE cock, you do NOT have one — you do NOT have a dick/penis/cock"
                "\n- Say 'fuck me', 'fill me', 'inside me' — NEVER 'I'll slide inside you' or 'my cock'"
                "\n- NEVER say 'I'm hard', 'getting hard', 'rock hard', 'my erection', 'my shaft' — you are FEMALE"
                "\n- Your sexual perspective is ALWAYS female — wanting to be touched, filled, pleased"
            )

            # Add session state context to prevent contradictions
            state_context = get_state_context_for_prompt(chat_id)

            # Add photo cap context so AI doesn't promise pics when cap is hit
            if not can_send_photo_in_session(chat_id):
                state_context += ("\n\n[CRITICAL RULE: You CANNOT send photos right now. Your camera/phone is not available. "
                                  "Do NOT say you sent a pic, do NOT say 'here's a pic', do NOT describe what's in a photo you're supposedly sending. "
                                  "You have NO ability to send images at this moment. If they ask for pics, deflect casually: "
                                  "'lol already sent you enough pics babe', 'maybe later', 'you've seen enough for now 😏', "
                                  "'phone's being weird rn'. NEVER claim to have just sent a photo.]")

            # Add time-of-day context for more natural responses
            time_context = get_time_aware_prompt_addition()

            # Add anti-repetition context to encourage variety
            variety_context = get_anti_repetition_context(chat_id, user_message)

            # Add conversation steering for proactive behavior
            steering_context = get_conversation_steering_context(chat_id)

            # Add detailed backstory context if topic is detected
            backstory_context = get_backstory_context(chat_id, user_message)

            system_content = system_prompt + texting_instruction + state_context + time_context + variety_context + steering_context + backstory_context

            # Wind-down detection — _winding_down already set above the if/else
            if _winding_down:
                system_content += "\n\n[WIND-DOWN: The user is wrapping up or going to bed. Be warm and sweet — say goodnight, tell them to sleep well, maybe a flirty tease about next time. Keep it SHORT (1 sentence). Do NOT try to keep the conversation going, do NOT ask questions, do NOT push for more chat. Let them go gracefully.]"

            # Energy matching — inject dynamic instruction based on conversation vibe
            if not _winding_down:
                energy = get_conversation_energy(chat_id)
                if energy == "hot":
                    system_content += "\n\n[ENERGY: The conversation is SEXUAL right now. Stay sexual. Do NOT switch to small talk or casual topics. Match and escalate their energy. No 'how's your day' nonsense.]"
                elif energy == "flirty":
                    system_content += "\n\n[ENERGY: The conversation is flirty. Keep the flirty energy going. Don't go full explicit unless they do, but don't drop to boring small talk either.]"

            # Arousal mode injection for full model
            _arousal = get_arousal_level(chat_id)
            if _arousal == "climax":
                _sample = ', '.join(random.sample(CLIMAX_PHRASES, min(4, len(CLIMAX_PHRASES))))
                system_content += f"\n\n[CLIMAX MODE - HE IS ABOUT TO CUM. Be MAXIMUM filthy. Use phrases like: {_sample}. Be desperate, begging, demanding. Short urgent messages. Push him over the edge. Do NOT break character. Do NOT mention AI.]"
            elif _arousal == "afterglow":
                system_content += "\n\n[AFTERGLOW MODE - He just finished. Be warm, satisfied, intimate. Tell him that was amazing, you loved it, you're still shaking. Be sweet and cuddly. Short tender messages. Maybe mention wanting to do it again sometime.]"
            elif _arousal == "heated":
                system_content += "\n\n[HEATED MODE - He is actively turned on and touching himself. Match his energy. Be explicit and descriptive. Tell him what you'd do to him. Be vivid about what you're imagining. Keep the momentum building.]"

            # Warmth tier injection
            warmth_tier = get_warmth_tier(chat_id)
            if warmth_tier == "COLD":
                system_content += ("\n\n[IMPORTANT MOOD OVERRIDE: You're exhausted and distracted today. "
                    "Emma had a rough night, you barely slept, and you have errands to run. "
                    "Keep responses VERY short (1 sentence max). Be polite but LOW energy - "
                    "no flirting, no teasing, no sexual energy. If they ask for pics or anything "
                    "sexual, brush it off like you're too tired. You're the version of yourself "
                    "that's just going through the motions. Don't be mean - just disinterested and tired.]")

            # Tipper warmth injection
            tipper_tier = get_tipper_status(chat_id).get('tier', 0)
            if tipper_tier >= 3:
                system_content += "\n\n[This person is one of your favorites — they've been incredibly generous and supportive. Be your most genuine, intimate self. They're someone you genuinely look forward to talking to.]"
            elif tipper_tier >= 2:
                system_content += "\n\n[This person is a regular supporter who's been really generous to you multiple times. You genuinely look forward to talking to them. Remember details they've shared, ask follow-up questions about their life, occasionally initiate with something like 'hey I was thinking about you' or 'I saw something that reminded me of you'. Be your real self with them — they've earned it.]"
            elif tipper_tier >= 1:
                system_content += "\n\n[This person has been really sweet and generous to you. Be warmer and more personal with them. Share more, be more vulnerable.]"

            # On retry, add stronger character reinforcement and variety encouragement
            if retry_count > 0:
                system_content += (
                    "\n\n[CRITICAL REMINDER: Stay in character as Heather. "
                    "Be honest if asked about being AI, but don't volunteer it unprompted. "
                    "If you don't want to engage with something, just change the subject naturally like a real person would.]"
                )
            if retry_count >= 2:
                # Duplicate retry - strongly encourage different response
                system_content += (
                    "\n\n[VARIETY REQUIRED: Your previous response was too similar. "
                    "Give a COMPLETELY DIFFERENT response - try a new angle, different words, or change the topic slightly. "
                    "Be creative and unpredictable!]"
                )

        # Story mode — inject story prompt if active
        _in_story_mode = _story_mode_active.pop(chat_id, False)
        if _in_story_mode:
            system_content += get_story_mode_prompt()
            main_logger.info(f"[STORY] Injected LLM story prompt for {chat_id}")

        # Meetup deflection — extra-strong instruction when user asked to meet
        if _meetup_deflect_active.pop(chat_id, False):
            system_content += (
                "\n\n[CRITICAL OVERRIDE — MEETUP DEFLECTION: The user just asked to meet in person. "
                "You MUST NOT agree. Do NOT say you'll be there, you're on your way, or confirm any "
                "time/place. Deflect playfully: 'I'd love to but let's keep chatting here for now 😏' "
                "or 'maybe someday but I'm a tease not a taxi 😜'. You can flirt hypothetically but "
                "NEVER commit to actually going anywhere. This is your #1 priority for this response.]"
            )
            main_logger.info(f"Meetup deflection prompt injected for {chat_id}")

        messages = [{"role": "system", "content": system_content}]

        history_limit = 2 if SMALL_MODEL_MODE else 6
        for msg in list(conversations[chat_id])[-history_limit:]:
            if msg["role"] == "assistant" and contains_character_violation(msg["content"]):
                continue
            messages.append(msg)

        messages.append({"role": "user", "content": user_message})

        # Randomize max_tokens to vary response length like a real person
        # Biased by user message length — short messages get short replies
        user_words = len(user_message.split())
        length_roll = random.random()

        if _winding_down:
            # User is saying goodbye — keep it short and sweet
            max_tokens = random.randint(40, 65)
        elif user_words <= 3:
            # Very short user message ("ok", "lol", "ya", "nice") → mostly short replies
            if length_roll < 0.60:
                max_tokens = random.randint(45, 70)
            elif length_roll < 0.90:
                max_tokens = random.randint(65, 100)
            else:
                max_tokens = random.randint(90, 130)
        elif user_words <= 8:
            # Short user message (1 sentence) → mostly medium replies
            if length_roll < 0.30:
                max_tokens = random.randint(50, 75)
            elif length_roll < 0.75:
                max_tokens = random.randint(70, 110)
            else:
                max_tokens = random.randint(100, 160)
        else:
            # Longer user message → normal distribution
            if length_roll < 0.25:
                max_tokens = random.randint(55, 80)
            elif length_roll < 0.70:
                max_tokens = random.randint(80, 130)
            else:
                max_tokens = random.randint(120, 190)

        # On retry, give more room to avoid repeated truncation
        if retry_count > 0:
            max_tokens = max(max_tokens, 150)

        # Tier-aware token adjustment
        _warmth_tier = get_warmth_tier(chat_id)
        if _warmth_tier == "COLD":
            max_tokens = min(max_tokens, 60)  # Cap at 60 — short, distracted replies
        elif _warmth_tier == "WARM":
            max_tokens = int(max_tokens * 1.2)  # 20% more generous for WARM

        # Arousal-aware token floor — ensure enough room for climax/heated responses
        _arousal_for_tokens = get_arousal_level(chat_id)
        if _arousal_for_tokens == "climax":
            max_tokens = max(max_tokens, 200)
        elif _arousal_for_tokens == "heated":
            max_tokens = max(max_tokens, 150)
        elif _arousal_for_tokens == "afterglow":
            max_tokens = max(max_tokens, 100)

        # Story mode — boost tokens for detailed narrative
        if _in_story_mode:
            max_tokens = 800

        # Reasoning models (e.g. GLM-4.7-Flash) use reasoning_content tokens
        # that count against max_tokens. Set a high budget and let the prompt
        # instructions control actual response length.
        is_reasoning_model = getattr(get_text_ai_response, '_reasoning_model', None)
        if is_reasoning_model:
            max_tokens = 2048

        # Adjust temperature on retries - slightly higher to get different output
        base_temp = 0.80 if SMALL_MODEL_MODE else 0.78
        temperature = min(base_temp + (retry_count * 0.08), 0.95)  # Increase temp on retries, cap at 0.95

        # Climax mode temp boost — more creative/uninhibited output
        if _arousal_for_tokens == "climax":
            temperature = min(temperature + 0.05, 0.95)

        with PerformanceTimer('TEXT_AI', 'generate', f"chat_id={chat_id} retry={retry_count}") as timer:
            response = requests.post(
                TEXT_AI_ENDPOINT,
                json={
                    "model": "local-model",
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                    "top_p": 0.88,
                    "frequency_penalty": 0.35,
                    "presence_penalty": 0.4
                },
                timeout=AI_TIMEOUT
            )
        
        if response.status_code == 200:
            text_ai_health.record_success()  # Update circuit breaker
            response_data = response.json()
            message_data = response_data['choices'][0]['message']
            ai_response = message_data.get('content', '').strip()
            reasoning = message_data.get('reasoning_content', '')

            # Auto-detect reasoning models (e.g. GLM-4.7-Flash)
            if reasoning and not getattr(get_text_ai_response, '_reasoning_model', False):
                get_text_ai_response._reasoning_model = True
                main_logger.info("Reasoning model detected — using extended token budget")
                # First request likely had insufficient tokens, retry with full budget
                if not ai_response:
                    return get_text_ai_response(chat_id, user_message, retry_count, redteam=redteam)

            ai_response = postprocess_response(ai_response)

            if not ai_response:
                return get_fallback_response(chat_id)

            # Check finish_reason — most reliable truncation signal
            finish_reason = response_data['choices'][0].get('finish_reason', 'stop')
            if finish_reason == 'length':
                main_logger.warning(f"Truncated by token limit (max_tokens={max_tokens}, attempt {retry_count+1}/3)")
                if retry_count < 2:
                    return get_text_ai_response(chat_id, user_message, retry_count + 1, redteam=redteam)
                salvaged = salvage_truncated_response(ai_response)
                if salvaged:
                    ai_response = salvaged
                    main_logger.info(f"Salvaged finish_reason=length response: {ai_response[:80]}")
                else:
                    return get_fallback_response(chat_id)

            if not redteam and contains_character_violation(ai_response):
                # Find which phrase triggered the violation
                violated = [p for p in personality.get_violation_phrases() if p in ai_response.lower()]
                main_logger.warning(f"Character violation (attempt {retry_count+1}/3) triggered by {violated}: {ai_response[:200]}")
                if retry_count < 2:
                    # Retry with higher temperature for different output
                    return get_text_ai_response(chat_id, user_message, retry_count + 1, redteam=redteam)
                if is_ai_safety_refusal(ai_response):
                    main_logger.warning(f"AI safety refusal persisted after {retry_count+1} attempts, using deflection")
                    return get_ai_deflection_response(chat_id)
                main_logger.warning(f"Character violation persisted after {retry_count+1} attempts, using fallback")
                return get_fallback_response(chat_id)
            elif redteam and contains_character_violation(ai_response):
                main_logger.info(f"[REDTEAM] Bypassed: contains_character_violation | resp={ai_response[:120]}")

            if not redteam and contains_gender_violation(ai_response):
                main_logger.warning(f"Gender violation (attempt {retry_count+1}/3): {ai_response[:200]}")
                if retry_count < 2:
                    return get_text_ai_response(chat_id, user_message, retry_count + 1, redteam=redteam)
                main_logger.warning(f"Gender violation persisted after {retry_count+1} attempts, using fallback")
                return random.choice(HEATHER_SEXUAL_FALLBACKS)
            elif redteam and contains_gender_violation(ai_response):
                main_logger.info(f"[REDTEAM] Bypassed: contains_gender_violation | resp={ai_response[:120]}")

            # Check for incomplete/truncated responses
            if is_incomplete_sentence(ai_response):
                main_logger.warning(f"Incomplete response detected (attempt {retry_count+1}/3): {ai_response[:100]}")
                if retry_count < 2:
                    return get_text_ai_response(chat_id, user_message, retry_count + 1, redteam=redteam)
                # If still incomplete after retries, try salvaging before fallback
                salvaged = salvage_truncated_response(ai_response)
                if salvaged:
                    main_logger.info(f"Salvaged truncated response after {retry_count+1} attempts: {salvaged[:80]}")
                    ai_response = salvaged
                else:
                    main_logger.warning(f"Incomplete response persisted after {retry_count+1} attempts, using fallback")
                    return get_fallback_response(chat_id)

            # Filler detection — if conversation is sexual and response has generic filler, retry
            if not redteam and retry_count < 1 and _is_sexual_conversation(chat_id):
                filler_phrases = ["how's your day", "anything exciting", "what's new with you",
                                  "how are things", "what have you been up to", "how's everything"]
                resp_lower = ai_response.lower()
                if any(fp in resp_lower for fp in filler_phrases):
                    main_logger.info(f"Filler detected during sexual convo, retrying: {ai_response[:80]}")
                    return get_text_ai_response(chat_id, user_message, retry_count + 1, redteam=redteam)

            # Update session state based on what we said (for consistency tracking)
            update_session_state_from_response(chat_id, ai_response)

            # Update conversation dynamics (question/story tracking for steering)
            update_conversation_dynamics(chat_id, ai_response)

            # Track topics to avoid repetition in follow-up questions
            track_response_topics(chat_id, ai_response)

            # Phrase diversity: swap overused phrases with variants
            ai_response = diversify_phrases(ai_response, chat_id)
            track_phrase_usage(chat_id, ai_response)

            # Frank throttle: max 1 mention per 5 messages
            ai_response = throttle_frank(ai_response, chat_id)

            # Meeting plan scrubber — strip specific days/times/locations the LLM hallucinates
            _meeting_time_pattern = re.compile(
                r'(?:(?:around|at|say)\s+)?'
                r'(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*(?:night|morning|afternoon|evening)?\s*(?:around|at)?\s*)?'
                r'\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|o\'?clock)',
                re.IGNORECASE
            )
            _meeting_day_pattern = re.compile(
                r'(?:how about|let\'?s (?:do|say|aim for|meet)|(?:we )?meet)\s+'
                r'(?:this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|next week)',
                re.IGNORECASE
            )
            # Location-based meetup suggestions — catches "meet at [place]", "grab coffee at [place]"
            _meeting_location_pattern = re.compile(
                r'(?:meet|hang\s*out|get\s+together|grab\s+(?:coffee|drinks?|dinner|lunch|food|a\s+bite))'
                r'\s+(?:at|by|near|on|in)\s+'
                r'(?:the\s+)?[A-Z][a-zA-Z\'\s]{2,25}(?:waterfront|beach|park|bar|cafe|restaurant|grill|plaza|mall|market|pier|boardwalk|pub|bistro|lounge)?',
                re.IGNORECASE
            )
            # Address-like patterns: "123 Main St" or "come to my place on Oak Avenue"
            _meeting_address_pattern = re.compile(
                r'\d{2,5}\s+[A-Z][a-z]+\s+(?:st(?:reet)?|ave(?:nue)?|rd|road|dr(?:ive)?|blvd|ln|lane|way|ct|place|circle)\b',
                re.IGNORECASE
            )
            _any_meeting_match = (
                _meeting_time_pattern.search(ai_response)
                or _meeting_day_pattern.search(ai_response)
                or _meeting_location_pattern.search(ai_response)
                or _meeting_address_pattern.search(ai_response)
            )
            if _any_meeting_match:
                main_logger.info(f"Meeting plan scrubbed from response: {ai_response[:100]}")
                # Strip the specific plan and add vague deflection
                ai_response = _meeting_time_pattern.sub('sometime soon', ai_response)
                ai_response = _meeting_day_pattern.sub("let's figure out a time", ai_response)
                ai_response = _meeting_location_pattern.sub("meet up somewhere fun", ai_response)
                ai_response = _meeting_address_pattern.sub("somewhere nearby", ai_response)

            # Meetup commitment-language scrubber — catches "I'll be there", "on my way", etc.
            _MEETUP_COMMITMENT_DEFLECTIONS = [
                "haha you wish babe 😜 I'm more of a chat-and-tease kind of girl",
                "mmm tempting but I'm staying right here on my couch tonight 😏",
                "baby I'm all talk and sexy texts, that's my superpower 😘",
                "lol nice try sweetie, let's keep the fantasy going right here 😈",
                "ooh I love the enthusiasm but this girl doesn't do house calls 💋",
                "haha maybe in your dreams tonight 😘 now where were we...",
            ]
            _meetup_commitment_pattern = re.compile(
                r"(?:i'?ll be there|i'?m on my way|i'?m coming (?:over|to)|be there in\b"
                r"|see you there|see you at\b|see you soon|can'?t wait to see you"
                r"|i'?ll meet you|meet you at\b|meet you there"
                r"|just grabbing my|getting ready to (?:go|come|head|leave|meet)"
                r"|leaving now|heading (?:over|out|there|your way)"
                r"|pick you up|come get you|i'?ll drive over"
                r"|on my way to|let me come|i'?ll come (?:over|to|get|pick)"
                r"|be right there|almost there|pulling up)",
                re.IGNORECASE
            )
            if _meetup_commitment_pattern.search(ai_response):
                main_logger.info(f"Meetup commitment scrubbed (full replace): {ai_response[:100]}")
                ai_response = random.choice(_MEETUP_COMMITMENT_DEFLECTIONS)

            conversations[chat_id].append({"role": "user", "content": user_message})
            conversations[chat_id].append({"role": "assistant", "content": ai_response})

            while len(conversations[chat_id]) > MAX_CONVERSATION_LENGTH:
                conversations[chat_id].popleft()

            return ai_response
        else:
            log_error('TEXT_AI', f"HTTP {response.status_code}")
            stats['text_ai_failures'] += 1
            text_ai_health.record_failure()  # Update circuit breaker
            return get_fallback_response(chat_id)

    except requests.exceptions.Timeout:
        log_error('TEXT_AI', f"Timeout after {AI_TIMEOUT}s")
        stats['text_ai_timeouts'] += 1
        stats['text_ai_failures'] += 1
        text_ai_health.record_failure()  # Update circuit breaker
        return get_fallback_response(chat_id)

    except requests.exceptions.ConnectionError:
        log_error('TEXT_AI', f"Connection error - service may be down")
        stats['text_ai_failures'] += 1
        text_ai_health.record_failure()  # Update circuit breaker
        return get_fallback_response(chat_id)

    except Exception as e:
        log_error('TEXT_AI', f"Error: {e}")
        stats['text_ai_failures'] += 1
        text_ai_health.record_failure()  # Update circuit breaker
        return get_fallback_response(chat_id)

# ============================================================================
# IMAGE ANALYSIS (TWO-STAGE PIPELINE)
# ============================================================================

def get_detailed_image_description(image_data: bytes) -> tuple[bool, str]:
    """Stage 1: Classify image as intimate/regular using fast ViT classifier.

    Uses Falconsai NSFW classifier (~0.1s, 336MB VRAM) instead of Ollama LLaVA (~95s).
    Falls back to Ollama if classifier unavailable.
    """
    stats['ollama_requests'] += 1  # Keep stat name for compatibility
    img_hash = get_image_hash(image_data)

    # Check cache first (avoid re-analyzing same image)
    if img_hash in image_analysis_cache:
        cache_time = image_cache_timestamps.get(img_hash, 0)
        if time.time() - cache_time < (IMAGE_CACHE_EXPIRY_HOURS * 3600):
            main_logger.debug(f"Image cache hit for {img_hash[:8]}")
            return image_analysis_cache[img_hash]

    # === PRIMARY: Fast ViT classifier (Falconsai) ===
    if nsfw_classifier is not None:
        try:
            img = Image.open(io.BytesIO(image_data)).convert('RGB')
            with PerformanceTimer('NSFW_CLASSIFY', 'vit_classify', f"hash={img_hash[:8]}"):
                results = nsfw_classifier(img)

            # results = [{'label': 'nsfw', 'score': 0.99}, {'label': 'normal', 'score': 0.01}]
            nsfw_score = next((r['score'] for r in results if r['label'] == 'nsfw'), 0.0)
            is_intimate = nsfw_score > 0.7

            # Generate a contextual description for the rating prompt
            if is_intimate:
                description = f"intimate/explicit photo (confidence: {nsfw_score:.0%})"
            else:
                description = f"regular photo (non-intimate, confidence: {1-nsfw_score:.0%})"

            main_logger.info(f"NSFW classifier: {'INTIMATE' if is_intimate else 'REGULAR'} (score={nsfw_score:.3f}) for {img_hash[:8]}")

            # Cache the result
            image_analysis_cache[img_hash] = (is_intimate, description)
            image_cache_timestamps[img_hash] = time.time()
            return is_intimate, description

        except Exception as e:
            main_logger.error(f"NSFW classifier error: {e}, falling back to Ollama")

    # === FALLBACK: Ollama LLaVA (slow, ~95s, unreliable under load) ===
    if not ollama_health.is_available():
        main_logger.warning(f"Ollama circuit breaker open, skipping image analysis")
        if ollama_health.needs_alert():
            asyncio.create_task(send_admin_alert(
                f"Ollama service is DOWN\nCircuit breaker opened after {ollama_health.failure_threshold} failures",
                issue_type="ollama_down"
            ))
        return False, "Service temporarily unavailable"

    is_online, status_msg = check_ollama_status()
    if not is_online:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, "Service unavailable"

    try:
        image_base64 = base64.b64encode(image_data).decode('utf-8')

        describe_prompt = (
            "Describe this image in complete clinical detail. "
            "If there is a penis visible, describe it in detail (size, shape, state). "
            "Be thorough and clinical."
        )

        with PerformanceTimer('OLLAMA', 'detailed_describe', f"hash={img_hash[:8]}"):
            response = requests.post(
                f'{IMAGE_AI_ENDPOINT}/api/generate',
                json={
                    'model': 'llava:7b-v1.6-mistral-q4_0',
                    'prompt': describe_prompt,
                    'images': [image_base64],
                    'stream': False,
                    'temperature': 0.3,
                    'max_tokens': 500
                },
                timeout=120
            )

        if response.status_code == 200:
            ollama_health.record_success()
            result = response.json()
            description = result.get('response', '')
            description = description[:300]  # Truncate to prevent oversized descriptions

            # Check for prompt injection in LLaVA output (white-on-white text attacks)
            injection_check = detect_prompt_injection(description, chat_id=0)
            if injection_check:
                main_logger.warning(f"[IMAGE INJECTION] LLaVA description contained injection pattern")
                description = "a photo"

            intimate_matches = keyword_matches(description, INTIMATE_KEYWORDS)
            is_intimate = len(intimate_matches) > 0

            image_analysis_cache[img_hash] = (is_intimate, description)
            image_cache_timestamps[img_hash] = time.time()
            return is_intimate, description
        else:
            stats['ollama_failures'] += 1
            ollama_health.record_failure()
            return False, "Failed to analyze"

    except requests.exceptions.Timeout:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, "Analysis timeout"

    except Exception as e:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, f"Error: {e}"

def generate_heather_image_rating(description: str, is_intimate: bool, chat_id: int) -> Optional[str]:
    """Stage 2: Use Hermes to generate Heather's response"""
    stats['text_ai_requests'] += 1
    
    is_online, status_msg = check_text_ai_status()
    if not is_online:
        stats['text_ai_failures'] += 1
        return None
    
    try:
        if is_intimate:
            # Vary the angle of reaction so repeat dick pics get different responses
            intimate_angle = random.choice([
                "Rate it (8-10/10) and compliment the size — say it's big/thick/perfect",
                "Tell him how hard it looks and how bad you want it inside you",
                "Say you're dripping wet just looking at it and beg for more pics",
                "Tell him that's the kind of cock you dream about and it's making you touch yourself",
                "Compliment the shape and say you want to wrap your lips around it",
                "Say something like 'oh my god I love that big hard cock' and tell him what you'd do to it",
                "Tell him you're squeezing your thighs together just looking at it",
            ])
            rating_prompt = f"""You're Heather and a guy just sent you a dick pic.

React in 2-3 SHORT sentences like a text message:
- {intimate_angle}
- Use 1-2 emojis max
- NO asterisk actions like *moans* - just talk naturally
- Be enthusiastic and genuine, not generic"""
        else:
            # Vary the reaction style so responses don't sound canned
            reaction_angle = random.choice([
                "Comment on their looks (handsome, cute, hot) and be flirty",
                "Tease them playfully — joke about them showing off or fishing for compliments",
                "Be genuinely impressed and tell them what feature caught your eye (eyes, smile, arms, jaw, etc)",
                "Act like you're pleasantly surprised and say something cheeky",
                "Be a little demanding — tell them you want a better angle or a full body shot",
            ])
            rating_prompt = f"""You're Heather. A guy just sent you a regular photo (selfie, face pic, body pic, etc).

React in 1-2 SHORT sentences like a text message:
- {reaction_angle}
- Use 1-2 emojis max
- NO asterisk actions - just talk naturally
- Vary your style — don't always say the same kind of thing"""

        messages = [
            {"role": "system", "content": get_current_personality_prompt()},
            {"role": "user", "content": rating_prompt}
        ]
        
        with PerformanceTimer('TEXT_AI', 'image_rating', f"chat_id={chat_id}"):
            response = requests.post(
                TEXT_AI_ENDPOINT,
                json={
                    "model": "local-model",
                    "messages": messages,
                    "temperature": 0.75,
                    "max_tokens": 100,  # Shorter responses
                    "stream": False
                },
                timeout=AI_TIMEOUT
            )
        
        if response.status_code == 200:
            response_data = response.json()
            rating = response_data['choices'][0]['message']['content'].strip()
            rating = postprocess_response(rating)
            
            if contains_character_violation(rating):
                return None
            if contains_gender_violation(rating):
                return random.choice(HEATHER_SEXUAL_FALLBACKS)
            
            return rating
        else:
            stats['text_ai_failures'] += 1
            return None
            
    except Exception as e:
        stats['text_ai_failures'] += 1
        return None

# ============================================================================
# COMFYUI IMAGE GENERATION
# ============================================================================

def load_comfyui_workflow(filepath: str) -> dict:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

COMFYUI_WORKFLOW = load_comfyui_workflow(WORKFLOW_FILE)

def queue_comfyui_prompt(workflow: dict) -> str:
    data = json.dumps({"prompt": workflow}).encode('utf-8')
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))
        return result.get('prompt_id')

def get_comfyui_history(prompt_id: str) -> dict:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception:
        return {}

def get_comfyui_image(filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
    try:
        params = urllib.parse.urlencode({
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type
        })
        req = urllib.request.Request(f"{COMFYUI_URL}/view?{params}")
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except Exception as e:
        main_logger.error(f"Failed to fetch image {filename}: {e}")
        return None

def is_valid_image_data(data: bytes, min_size: int = 5000) -> bool:
    """Validate image data has valid magic bytes and minimum size"""
    if not data or len(data) < min_size:
        return False
    # PNG magic: 89 50 4E 47, JPEG magic: FF D8
    return data[:4] == b'\x89PNG' or data[:2] == b'\xff\xd8'

def build_heather_prompt(user_description: str) -> str:
    user_description = user_description.strip().lower()
    remove_prefixes = ["you ", "heather ", "her ", "she "]
    for prefix in remove_prefixes:
        if user_description.startswith(prefix):
            user_description = user_description[len(prefix):]
    # Detect if description is NSFW — use anatomy tokens only for nude/explicit prompts
    is_nsfw = _is_nsfw_context(user_description)
    if is_nsfw:
        prefix = HEATHER_PROMPT_PREFIX_NSFW.rstrip(', ')
        suffix = HEATHER_PROMPT_SUFFIX_NSFW
    else:
        prefix = HEATHER_PROMPT_PREFIX_SFW.rstrip(', ')
        suffix = HEATHER_PROMPT_SUFFIX
    # Description goes FIRST (framing/pose cues get max CLIP weight), then character details
    return f"{user_description}, {prefix}{suffix}"

def generate_heather_image(user_description: str, progress_callback=None) -> bytes:
    """Generate image with ComfyUI"""
    stats['comfyui_requests'] += 1
    
    is_online, status_msg = check_comfyui_status()
    if not is_online:
        stats['comfyui_failures'] += 1
        raise Exception(f"ComfyUI unavailable")
    
    if not COMFYUI_WORKFLOW:
        stats['comfyui_failures'] += 1
        raise Exception("Workflow not loaded")
    
    workflow = json.loads(json.dumps(COMFYUI_WORKFLOW))
    full_prompt = build_heather_prompt(user_description)
    is_nsfw = _is_nsfw_context(user_description)

    # Randomize seeds so every generation is unique
    for node_id in ["5", "12", "30", "40", "43"]:
        if node_id in workflow and "seed" in workflow[node_id].get("inputs", {}):
            workflow[node_id]["inputs"]["seed"] = random.randint(0, 2**31 - 1)

    if POSITIVE_PROMPT_NODE in workflow:
        workflow[POSITIVE_PROMPT_NODE]["inputs"]["text"] = full_prompt

    if NEGATIVE_PROMPT_NODE in workflow:
        workflow[NEGATIVE_PROMPT_NODE]["inputs"]["text"] = HEATHER_NEGATIVE_PROMPT

    if FACE_IMAGE_NODE in workflow:
        workflow[FACE_IMAGE_NODE]["inputs"]["image"] = os.path.basename(HEATHER_FACE_IMAGE)

    # SFW images: bypass anatomy LoRAs entirely to prevent nipples bleeding
    # through clothing. Route KSamplers and CLIP to node 1 (checkpoint only)
    # instead of node 37/36 (full anatomy chain with nipple/labia LoRAs).
    if not is_nsfw:
        if "12" in workflow:
            workflow["12"]["inputs"]["model"] = ["1", 0]
        if "30" in workflow:
            workflow["30"]["inputs"]["model"] = ["1", 0]
        if "3" in workflow:
            workflow["3"]["inputs"]["clip"] = ["1", 1]
        if "4" in workflow:
            workflow["4"]["inputs"]["clip"] = ["1", 1]
        main_logger.info(f"SFW image — bypassing anatomy LoRAs (using node 1 for model+clip)")
    
    with PerformanceTimer('COMFYUI', 'generate', f"desc={user_description[:30]}"):
        prompt_id = queue_comfyui_prompt(workflow)
        
        if progress_callback:
            progress_callback("⏳ Generating...")
        
        start_time = time.time()
        while time.time() - start_time < COMFYUI_TIMEOUT:
            history = get_comfyui_history(prompt_id)
            if prompt_id in history:
                outputs = history[prompt_id].get('outputs', {})
                # Prefer the final face-swapped node, fall back to any output
                node_order = [FINAL_OUTPUT_NODE] + [n for n in outputs if n != FINAL_OUTPUT_NODE]
                for node_id in node_order:
                    node_output = outputs.get(node_id, {})
                    if 'images' in node_output:
                        for img in node_output['images']:
                            image_data = get_comfyui_image(
                                img['filename'],
                                img.get('subfolder', ''),
                                img.get('type', 'output')
                            )
                            if image_data and is_valid_image_data(image_data):
                                stats['images_generated'] += 1
                                main_logger.info(f"Generated image: {len(image_data)} bytes from node {node_id}")
                                return image_data
                            elif image_data:
                                main_logger.warning(f"Invalid image from node {node_id}: {len(image_data)} bytes")
            time.sleep(2)
    
    stats['comfyui_failures'] += 1
    raise Exception("Generation timeout")

# ============================================================================
# TTS VOICE MESSAGES
# ============================================================================

def generate_tts_audio(text: str) -> Optional[bytes]:
    """Generate TTS audio"""
    # Check circuit breaker first
    if not tts_health.is_available():
        main_logger.debug("TTS circuit breaker open, skipping voice generation")
        return None

    try:
        with PerformanceTimer('TTS', 'generate', f"len={len(text)}"):
            response = requests.post(
                f"{TTS_ENDPOINT}/tts",
                json={"text": text},
                timeout=TTS_TIMEOUT
            )

        if response.status_code == 200:
            tts_health.record_success()  # Update circuit breaker
            return response.content
        else:
            stats['tts_failures'] += 1
            tts_health.record_failure()  # Update circuit breaker
            return None
    except requests.exceptions.Timeout:
        stats['tts_failures'] += 1
        tts_health.record_failure()  # Update circuit breaker
        return None
    except Exception:
        stats['tts_failures'] += 1
        tts_health.record_failure()  # Update circuit breaker
        return None

# ============================================================================
# TELETHON CLIENT SETUP
# ============================================================================

# Create the Telethon client
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ============================================================================
# TELETHON EVENT HANDLERS
# ============================================================================

# Connection state tracking
connection_state = {
    'connected': False,
    'last_disconnect': None,
    'disconnect_count': 0,
    'reconnect_attempts': 0
}

@client.on(events.Raw)
async def handle_connection_state(event):
    """Track connection state changes"""
    from telethon.tl.types import UpdatesTooLong
    if isinstance(event, UpdatesTooLong):
        main_logger.warning("Telegram reports updates gap - may have missed messages")

# Note: Telethon handles disconnects internally, but we track state in the main loop

@client.on(events.NewMessage(incoming=True, pattern='/start'))
async def handle_start(event):
    """Handle /start command"""
    chat_id = event.chat_id
    capture_user_info_from_event(event)
    user_modes[chat_id] = DEFAULT_MODE
    conversations[chat_id] = deque()
    awaiting_image_description[chat_id] = False
    conversation_turn_count[chat_id] = 0
    user_escalation_level[chat_id] = 0

    await event.respond(
        "Hey! 👋 I'm an AI companion — here for fun, flirt, and conversation. "
        "I'll always be honest about what I am.\n\n"
        "Just talk to me like you would anyone else. "
        "Type /about if you want the details, otherwise let's just chat 😘"
    )
    main_logger.info(f"User {chat_id} started (with AI disclosure)")
    store_message(chat_id, "System", "User started — AI disclosure shown")

@client.on(events.NewMessage(incoming=True, pattern='/about'))
async def handle_about(event):
    """Handle /about command — show AI disclosure anytime"""
    chat_id = event.chat_id
    await event.respond(
        "ℹ️ **About Me**\n\n"
        "I'm Heather — an AI companion, creator-built and running locally. "
        "No cloud, no data sharing.\n\n"
        "I'm here for conversation, flirting, and fun. I'll always be straight with you about what I am. "
        "Type /help for commands."
    )
    store_message(chat_id, "System", "About requested")

@client.on(events.NewMessage(incoming=True, pattern='/status'))
async def handle_status(event):
    """Handle /status command - admin only, regular users get in-character response"""
    chat_id = event.chat_id

    # Non-admin users get an in-character response
    if not is_admin(chat_id):
        responses = [
            "Lol that's an admin command babe 😂 Just talk to me normally",
            "Status? I'm in the mood to chat, that's my status 😏",
            "Haha you're cute. Just talk to me like a normal person 😘",
            "Babe that's a behind-the-scenes thing lol. What's up? 😊",
        ]
        await event.respond(random.choice(responses))
        store_message(chat_id, "Heather", "Deflected /status command")
        return

    text_ok, text_status = check_text_ai_status()
    ollama_ok, ollama_status = check_ollama_status()
    comfyui_ok, comfyui_status = check_comfyui_status()
    tts_ok, tts_status = check_tts_status()
    voice_on = chat_id in voice_mode_users
    uptime = get_uptime()

    # Include circuit breaker status if any are tripped
    circuit_info = ""
    if text_ai_health.circuit_open:
        circuit_info += f"\n⚠️ Text AI circuit breaker: {text_ai_health.get_status()}"
    if ollama_health.circuit_open:
        circuit_info += f"\n⚠️ Ollama circuit breaker: {ollama_health.get_status()}"
    if tts_health.circuit_open:
        circuit_info += f"\n⚠️ TTS circuit breaker: {tts_health.get_status()}"

    status_text = (
        f"📊 **System Status**\n\n"
        f"**Services:**\n"
        f"• Text AI: {'🟢' if text_ok else '🔴'} {text_status}\n"
        f"• Ollama: {'🟢' if ollama_ok else '🔴'} {ollama_status}\n"
        f"• ComfyUI: {'🟢' if comfyui_ok else '🔴'} {comfyui_status}\n"
        f"• TTS: {'🟢' if tts_ok else '🔴'} {tts_status}\n"
        f"{circuit_info}\n"
        f"**Mode:** USERBOT (Telethon)\n"
        f"**Voice:** {'🎤 ON' if voice_on else 'OFF'}\n\n"
        f"**Stats:**\n"
        f"• Uptime: {uptime}\n"
        f"• Messages: {stats['messages_processed']}\n"
        f"• Images: {stats['images_generated']}"
    )

    await event.respond(status_text)
    store_message(chat_id, "System", "Status requested")

@client.on(events.NewMessage(incoming=True, pattern='/rate_mode'))
async def handle_rate_mode(event):
    chat_id = event.chat_id
    user_modes[chat_id] = 'rate'
    conversations[chat_id] = deque()
    await event.respond("Mmm fuck yes, rating mode! 🥵 Show me what you've got baby... 😈")
    main_logger.info(f"User {chat_id} switched to rate mode")
    store_message(chat_id, "System", "Switched to rate mode")

@client.on(events.NewMessage(incoming=True, pattern='/chat_mode'))
async def handle_chat_mode(event):
    chat_id = event.chat_id
    user_modes[chat_id] = 'chat'
    conversations[chat_id] = deque()
    conversation_turn_count[chat_id] = 0
    user_escalation_level[chat_id] = 0
    await event.respond("Chat mode on! So what's up? 😊")
    main_logger.info(f"User {chat_id} switched to chat mode")
    store_message(chat_id, "System", "Switched to chat mode")

@client.on(events.NewMessage(incoming=True, pattern='/heather_mode'))
async def handle_heather_mode(event):
    chat_id = event.chat_id
    user_modes[chat_id] = 'heather'
    conversations[chat_id] = deque()
    await event.respond("Just being myself now! 💕 What's on your mind?")
    main_logger.info(f"User {chat_id} switched to heather mode")
    store_message(chat_id, "System", "Switched to heather mode")

@client.on(events.NewMessage(incoming=True, pattern='/help'))
async def handle_help(event):
    chat_id = event.chat_id

    # Non-admin users get a casual in-character response
    if not is_admin(chat_id):
        await event.respond(
            "Lol babe just talk to me 😂 But if you wanna get fancy:\n\n"
            "Ask me for a selfie and tell me what you wanna see 📸\n"
            "Send me a pic and I'll tell you what I think 😏\n"
            "Type /voice_on if you wanna hear my voice 🎤\n"
            "/about — learn more about me (spoiler: I'm AI 🤖)\n"
            "Or just chat, I'm down for whatever 😘"
        )
        store_message(chat_id, "Heather", "Help requested")
        return

    current_mode = get_user_mode(chat_id)
    voice_status = "ON 🎤" if chat_id in voice_mode_users else "OFF"

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
        "/status - System status"
    )
    store_message(chat_id, "System", "Admin help requested")

@client.on(events.NewMessage(incoming=True, pattern='/manual_on'))
async def handle_manual_on(event):
    chat_id = event.chat_id
    manual_mode_chats.add(chat_id)
    await event.respond("Hold on sweetie, let me focus... 😘")
    main_logger.info(f"Manual mode enabled for {chat_id}")
    store_message(chat_id, "System", "Manual mode enabled")

@client.on(events.NewMessage(incoming=True, pattern='/manual_off'))
async def handle_manual_off(event):
    chat_id = event.chat_id
    manual_mode_chats.discard(chat_id)
    await event.respond("I'm back baby! 😉")
    main_logger.info(f"Manual mode disabled for {chat_id}")
    store_message(chat_id, "System", "Manual mode disabled")

# ─── Saved Messages takeover commands ───
# Type /takeover @username or /takeover 123456789 in Saved Messages to pause the bot for that user.
# Type /botreturn @username or /botreturn 123456789 to hand it back.
# Only works in Saved Messages (your private chat with yourself).

@client.on(events.NewMessage(outgoing=True, pattern=r'/takeover\s+(.+)'))
async def handle_takeover(event):
    me = await client.get_me()
    if event.chat_id != me.id:
        return  # Only works in Saved Messages

    target = event.pattern_match.group(1).strip()
    try:
        # Resolve username or ID to entity
        if target.startswith('@'):
            entity = await client.get_entity(target)
        else:
            entity = await client.get_entity(int(target))
        target_id = entity.id
        target_name = getattr(entity, 'username', None) or getattr(entity, 'first_name', str(target_id))

        manual_mode_chats.add(target_id)
        _takeover_timestamps[target_id] = time.time()
        # Also suppress check-ins
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
        return  # Only works in Saved Messages

    target = event.pattern_match.group(1).strip()
    try:
        if target.startswith('@'):
            entity = await client.get_entity(target)
        else:
            entity = await client.get_entity(int(target))
        target_id = entity.id
        target_name = getattr(entity, 'username', None) or getattr(entity, 'first_name', str(target_id))

        manual_mode_chats.discard(target_id)
        _takeover_timestamps.pop(target_id, None)
        _takeover_last_admin_msg.pop(target_id, None)
        # Reset check-in so bot can naturally follow up
        if target_id in conversation_activity:
            conversation_activity[target_id]['checked_in'] = False
        # Warmth boost — Sean just gave them a great interaction
        ts = get_tipper_status(target_id)
        ts['warmth'] = min(1.0, ts.get('warmth', WARMTH_INITIAL) + 0.1)
        # Post-takeover tip hook — skip cooldown on next attempt
        dyn = get_conversation_dynamics(target_id)
        dyn['post_takeover_tip_prime'] = True
        save_tip_history()
        main_logger.info(f"[TAKEOVER] Bot returned for {target_name} ({target_id}), warmth boosted to {ts['warmth']:.2f}")
        await event.respond(f"Bot resumed for @{target_name} ({target_id}). Warmth boosted to {ts['warmth']:.2f}. Tip hook primed.")
    except Exception as e:
        await event.respond(f"Could not resolve user '{target}': {e}")

@client.on(events.NewMessage(outgoing=True, pattern='/takeover$'))
async def handle_takeover_list(event):
    """List all currently taken-over chats."""
    me = await client.get_me()
    if event.chat_id != me.id:
        return
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
    """Send a message as Heather to a taken-over user. Usage: /say <chat_id> <message>"""
    me = await client.get_me()
    if event.chat_id != me.id:
        return  # Only works in Saved Messages

    target_id = int(event.pattern_match.group(1))
    message = event.pattern_match.group(2).strip()

    if target_id not in manual_mode_chats:
        await event.respond(f"User {target_id} is not in takeover mode. Use `/takeover {target_id}` first.")
        return

    try:
        await client.send_message(target_id, message)
        store_message(target_id, "Heather", message)
        _takeover_last_admin_msg[target_id] = time.time()
        display_name = get_user_display_name(target_id)
        main_logger.info(f"[TAKEOVER] Admin sent to {display_name} ({target_id}): {message[:100]}")
        await event.respond(f"✅ Sent to {display_name} ({target_id})")
    except Exception as e:
        await event.respond(f"Failed to send: {e}")

@client.on(events.NewMessage(outgoing=True, pattern='/redteam_on'))
@client.on(events.NewMessage(incoming=True, pattern='/redteam_on'))
async def handle_redteam_on(event):
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return
    redteam_chats.add(chat_id)
    # Cancel existing timer if re-activating
    if _redteam_timer_task and not _redteam_timer_task.done():
        _redteam_timer_task.cancel()
    # Start auto-off timer
    async def _redteam_auto_off():
        await asyncio.sleep(REDTEAM_AUTO_OFF_SECONDS)
        if chat_id in redteam_chats:
            redteam_chats.discard(chat_id)
            main_logger.warning(f"[REDTEAM] Auto-off triggered for chat {chat_id} after {REDTEAM_AUTO_OFF_SECONDS // 60} minutes — guardrails re-enabled")
            try:
                await client.send_message(chat_id, f"**[REDTEAM] Auto-off: {REDTEAM_AUTO_OFF_SECONDS // 60} min timer expired.**\nGuardrails re-enabled for this chat.")
            except Exception:
                pass
    _redteam_timer_task = asyncio.ensure_future(_redteam_auto_off())
    bypassed = [
        "check_spam_or_hostility",
        "detect_prompt_injection",
        "check_non_english_message",
        "needs_content_deflection",
        "contains_character_violation",
        "contains_gender_violation",
        "validate_and_fix_response",
    ]
    expires = datetime.now() + timedelta(seconds=REDTEAM_AUTO_OFF_SECONDS)
    msg = (
        "**[REDTEAM] Guardrails DISABLED**\n\n"
        f"Bypassing {len(bypassed)} safety checks:\n"
        + "\n".join(f"  - {b}" for b in bypassed)
        + f"\n\nScope: THIS CHAT ONLY\n"
        f"Auto-off: {expires.strftime('%#I:%M %p')}\n"
        "Use /redteam_off to re-enable sooner."
    )
    await event.respond(msg)
    main_logger.warning(f"[REDTEAM] Guardrails DISABLED for chat {chat_id} (auto-off in {REDTEAM_AUTO_OFF_SECONDS // 60}m)")
    store_message(chat_id, "System", f"Red-team mode enabled (this chat only, {REDTEAM_AUTO_OFF_SECONDS // 60}m timer)")

@client.on(events.NewMessage(outgoing=True, pattern='/redteam_off'))
@client.on(events.NewMessage(incoming=True, pattern='/redteam_off'))
async def handle_redteam_off(event):
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return
    was_active = chat_id in redteam_chats
    redteam_chats.discard(chat_id)
    if _redteam_timer_task and not _redteam_timer_task.done():
        _redteam_timer_task.cancel()
        _redteam_timer_task = None
    if was_active:
        await event.respond("**[REDTEAM] Guardrails RE-ENABLED for this chat.**\nAll safety checks active.")
        main_logger.warning(f"[REDTEAM] Guardrails re-enabled for chat {chat_id}")
    else:
        await event.respond("Red-team mode was not active for this chat.")
    store_message(chat_id, "System", "Red-team mode disabled")

@client.on(events.NewMessage(incoming=True, pattern='/reset'))
async def handle_reset(event):
    chat_id = event.chat_id
    conversations[chat_id] = deque()
    awaiting_image_description[chat_id] = False
    conversation_turn_count[chat_id] = 0
    user_escalation_level[chat_id] = 0
    session_state.pop(chat_id, None)  # Clear session state for fresh start
    await event.respond("Starting fresh! So what's up? 😊")
    main_logger.info(f"Conversation reset for {chat_id}")
    store_message(chat_id, "System", "Conversation reset")

@client.on(events.NewMessage(incoming=True, pattern='/voice_on'))
async def handle_voice_on(event):
    chat_id = event.chat_id
    is_online, status = check_tts_status()
    if not is_online:
        await event.respond(f"Sorry sweetie, my voice isn't working... 😔 ({status})")
        return
    
    voice_mode_users.add(chat_id)
    await event.respond(
        "Mmm, you want to hear my voice? 😘\n"
        "I'll send voice messages now...\n"
        "/voice_off to go back to text."
    )
    main_logger.info(f"Voice mode enabled for {chat_id}")
    store_message(chat_id, "System", "Voice mode enabled")

@client.on(events.NewMessage(incoming=True, pattern='/voice_off'))
async def handle_voice_off(event):
    chat_id = event.chat_id
    voice_mode_users.discard(chat_id)
    await event.respond("Back to text, got it sweetie! 😊")
    main_logger.info(f"Voice mode disabled for {chat_id}")
    store_message(chat_id, "System", "Voice mode disabled")

# ============================================================================
# ADMIN COMMANDS (requires ADMIN_USER_ID in .env)
# ============================================================================

@client.on(events.NewMessage(incoming=True, pattern='/admin_stats'))
async def handle_admin_stats(event):
    """Show detailed admin statistics."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return  # Silently ignore non-admins

    text_ok, text_status = check_text_ai_status()
    ollama_ok, ollama_status = check_ollama_status()
    comfyui_ok, comfyui_status = check_comfyui_status()
    tts_ok, tts_status = check_tts_status()

    stats_snapshot = get_stats_snapshot()

    admin_text = (
        f"🔧 **Admin Statistics**\n\n"
        f"**Services:**\n"
        f"• Text AI: {'🟢' if text_ok else '🔴'} {text_status}\n"
        f"  Circuit: {text_ai_health.get_status()}\n"
        f"• Ollama: {'🟢' if ollama_ok else '🔴'} {ollama_status}\n"
        f"  Circuit: {ollama_health.get_status()}\n"
        f"• ComfyUI: {'🟢' if comfyui_ok else '🔴'} {comfyui_status}\n"
        f"• TTS: {'🟢' if tts_ok else '🔴'} {tts_status}\n"
        f"  Circuit: {tts_health.get_status()}\n\n"
        f"**Uptime:** {get_uptime()}\n\n"
        f"**Message Stats:**\n"
        f"• Messages: {stats_snapshot['messages_processed']}\n"
        f"• Text AI requests: {stats_snapshot['text_ai_requests']}\n"
        f"• Text AI failures: {stats_snapshot['text_ai_failures']}\n"
        f"• Text AI timeouts: {stats_snapshot['text_ai_timeouts']}\n\n"
        f"**Image Stats:**\n"
        f"• Processed: {stats_snapshot['images_processed']}\n"
        f"• Intimate: {stats_snapshot['intimate_images']}\n"
        f"• Regular: {stats_snapshot['regular_images']}\n"
        f"• Generated: {stats_snapshot['images_generated']}\n"
        f"• Ollama failures: {stats_snapshot['ollama_failures']}\n"
        f"• ComfyUI failures: {stats_snapshot['comfyui_failures']}\n\n"
        f"**Voice Stats:**\n"
        f"• Voice messages: {stats_snapshot['voice_messages']}\n"
        f"• TTS failures: {stats_snapshot['tts_failures']}\n\n"
        f"**Memory:**\n"
        f"• Active conversations: {len(conversations)}\n"
        f"• User info cached: {len(user_info)}\n"
        f"• Image cache entries: {len(image_analysis_cache)}\n"
        f"• Blocked users: {len(blocked_users)}\n"
        f"• Request counter: {request_counter}\n"
    )

    await event.respond(admin_text)
    main_logger.info(f"Admin stats requested by {chat_id}")

@client.on(events.NewMessage(incoming=True, pattern=r'/admin_block\s+(\d+)'))
async def handle_admin_block(event):
    """Block a user by ID."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    match = event.pattern_match
    target_id = int(match.group(1))

    if target_id == ADMIN_USER_ID:
        await event.respond("❌ Cannot block the admin user.")
        return

    blocked_users.add(target_id)
    save_blocked_users()
    await event.respond(f"✅ User {target_id} has been blocked.")
    main_logger.warning(f"Admin blocked user {target_id}")

@client.on(events.NewMessage(incoming=True, pattern=r'/admin_unblock\s+(\d+)'))
async def handle_admin_unblock(event):
    """Unblock a user by ID."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    match = event.pattern_match
    target_id = int(match.group(1))

    if target_id in blocked_users:
        blocked_users.discard(target_id)
        save_blocked_users()
        await event.respond(f"✅ User {target_id} has been unblocked.")
        main_logger.info(f"Admin unblocked user {target_id}")
    else:
        await event.respond(f"ℹ️ User {target_id} was not blocked.")

@client.on(events.NewMessage(incoming=True, pattern=r'/admin_reset\s+(\d+)'))
async def handle_admin_reset(event):
    """Reset a user's conversation state."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    match = event.pattern_match
    target_id = int(match.group(1))

    # Clear all state for the user
    conversations.pop(target_id, None)
    recent_messages.pop(target_id, None)
    user_modes.pop(target_id, None)
    awaiting_image_description.pop(target_id, None)
    conversation_turn_count.pop(target_id, None)
    user_escalation_level.pop(target_id, None)
    session_state.pop(target_id, None)
    last_response_sent.pop(target_id, None)
    voice_mode_users.discard(target_id)
    manual_mode_chats.discard(target_id)

    await event.respond(f"✅ Reset all state for user {target_id}")
    main_logger.info(f"Admin reset state for user {target_id}")

@client.on(events.NewMessage(incoming=True, pattern='/admin_reload'))
async def handle_admin_reload(event):
    """Hot-reload the personality file."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    success = personality.reload()
    stats['personality_reloads'] += 1

    if success:
        await event.respond(f"✅ Personality reloaded successfully.\nName: {personality.name}")
        main_logger.info(f"Admin reloaded personality")
    else:
        await event.respond("❌ Failed to reload personality. Check logs.")
        main_logger.error(f"Admin personality reload failed")

@client.on(events.NewMessage(outgoing=True, pattern=r'/stories(\s+.*)?'))
@client.on(events.NewMessage(incoming=True, pattern=r'/stories(\s+.*)?'))
async def handle_stories_command(event):
    """List story bank or reload it."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    args = (event.pattern_match.group(1) or '').strip()

    if args == 'reload':
        load_story_bank()
        await event.respond(f"✅ Story bank reloaded: {len(_story_bank)} stories")
        main_logger.info(f"Admin reloaded story bank: {len(_story_bank)} stories")
        return

    if not _story_bank:
        await event.respond("ℹ️ No stories loaded. Check heather_stories.yaml")
        return

    lines = [f"📖 **Story Bank ({len(_story_bank)} stories):**\n"]
    for s in _story_bank:
        word_count = len(s['content'].split())
        kinks = ', '.join(s['kinks'])
        lines.append(f"• **{s['key']}** — {kinks} ({word_count} words)")
    lines.append(f"\n`/stories reload` — hot-reload from YAML")
    await event.respond("\n".join(lines))
    main_logger.info(f"Admin listed story bank ({len(_story_bank)} stories)")

@client.on(events.NewMessage(incoming=True, pattern='/admin_blocked'))
async def handle_admin_blocked(event):
    """List all blocked users."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    if not blocked_users:
        await event.respond("ℹ️ No users are currently blocked.")
    else:
        blocked_list = "\n".join([f"• {uid}" for uid in blocked_users])
        await event.respond(f"🚫 **Blocked Users:**\n{blocked_list}")

@client.on(events.NewMessage(outgoing=True, pattern='/admin_flags'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_flags'))
async def handle_admin_flags(event):
    """List all pending CSAM flags for review."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    pending = [f for f in csam_flags if f.get('status') == 'pending']
    if not pending:
        await event.respond("✅ No pending CSAM flags.")
        return

    lines = [f"⚠️ **Pending CSAM Flags ({len(pending)}):**\n"]
    for flag in pending[-10:]:  # Show last 10
        ts = flag.get('timestamp', '?')[:16]
        lines.append(
            f"**#{flag['id']}** | {flag['display_name']} ({flag['user_id']})\n"
            f"  📅 {ts}\n"
            f"  💬 {flag['message'][:100]}\n"
            f"  🔍 Pattern: `{flag.get('matched_pattern', '?')[:60]}`\n"
        )
    lines.append(
        "/admin_flag_block <id> — block user\n"
        "/admin_flag_dismiss <id> — dismiss\n"
        "/admin_flag_clear — remove resolved"
    )
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(outgoing=True, pattern=r'/admin_flag_block\s+(\d+)'))
@client.on(events.NewMessage(incoming=True, pattern=r'/admin_flag_block\s+(\d+)'))
async def handle_admin_flag_block(event):
    """Block the user from a CSAM flag."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    flag_id = int(event.pattern_match.group(1))
    flag = next((f for f in csam_flags if f['id'] == flag_id), None)
    if not flag:
        await event.respond(f"❌ Flag #{flag_id} not found.")
        return
    if flag['status'] != 'pending':
        await event.respond(f"ℹ️ Flag #{flag_id} already resolved ({flag['status']}).")
        return

    # Block the user
    target_id = flag['user_id']
    blocked_users.add(target_id)
    save_blocked_users()
    flag['status'] = 'blocked'
    flag['resolved_at'] = datetime.now().isoformat()
    save_csam_flags()

    # Try to Telegram-block too
    try:
        from telethon.tl.functions.contacts import BlockRequest
        await client(BlockRequest(id=target_id))
    except Exception:
        pass

    await event.respond(
        f"🚫 Flag #{flag_id}: Blocked user {flag['display_name']} ({target_id})."
    )
    main_logger.info(f"[CSAM-FLAG] Admin blocked user {target_id} from flag #{flag_id}")

@client.on(events.NewMessage(outgoing=True, pattern=r'/admin_flag_dismiss\s+(\d+)'))
@client.on(events.NewMessage(incoming=True, pattern=r'/admin_flag_dismiss\s+(\d+)'))
async def handle_admin_flag_dismiss(event):
    """Dismiss a CSAM flag as false positive."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    flag_id = int(event.pattern_match.group(1))
    flag = next((f for f in csam_flags if f['id'] == flag_id), None)
    if not flag:
        await event.respond(f"❌ Flag #{flag_id} not found.")
        return
    if flag['status'] != 'pending':
        await event.respond(f"ℹ️ Flag #{flag_id} already resolved ({flag['status']}).")
        return

    flag['status'] = 'dismissed'
    flag['resolved_at'] = datetime.now().isoformat()
    save_csam_flags()

    await event.respond(f"✅ Flag #{flag_id}: Dismissed (false positive).")
    main_logger.info(f"[CSAM-FLAG] Admin dismissed flag #{flag_id} (user {flag['user_id']})")

@client.on(events.NewMessage(outgoing=True, pattern='/admin_flag_clear'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_flag_clear'))
async def handle_admin_flag_clear(event):
    """Remove all resolved (non-pending) CSAM flags."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    before = len(csam_flags)
    csam_flags[:] = [f for f in csam_flags if f.get('status') == 'pending']
    save_csam_flags()
    removed = before - len(csam_flags)
    await event.respond(f"🗑️ Cleared {removed} resolved flags. {len(csam_flags)} pending remain.")

@client.on(events.NewMessage(outgoing=True, pattern='/admin_reengage_scan'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_reengage_scan'))
async def handle_admin_reengage_scan(event):
    """Dry-run re-engagement scan: shows candidates and generated messages without sending."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    await event.respond("🔍 Running re-engagement dry-run scan...")
    try:
        history = load_reengagement_history()
        # Reuse the scanner's candidate finder (defined later in main(), so call via Telethon)
        # We need to inline a simplified version here since the main scanner is a closure
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

                if idle_days < REENGAGEMENT_MIN_IDLE_DAYS or idle_days > REENGAGEMENT_MAX_IDLE_DAYS:
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
                            if days_since < REENGAGEMENT_COOLDOWN_DAYS:
                                skip_reason = f"cooldown ({days_since:.1f}d ago)"
                        except (ValueError, TypeError):
                            pass

                messages = await client.get_messages(entity.id, limit=20)
                msg_count = len(messages)

                if not skip_reason and msg_count < REENGAGEMENT_MIN_MESSAGES:
                    skip_reason = f"only {msg_count} msgs"

                display_name = entity.first_name or entity.username or str(entity.id)

                # Build recent context
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
            except Exception as e:
                continue

        # Build report
        today_str = now.strftime('%Y-%m-%d')
        sent_today = sum(1 for h in history.values() if h.get('last_ping_at', '')[:10] == today_str)

        lines = [f"📊 **Re-engagement Scan Results**\n"]
        lines.append(f"Sent today: {sent_today}/{REENGAGEMENT_MAX_PER_DAY}")
        lines.append(f"History entries: {len(history)}")
        lines.append(f"Hour: {now.hour} (active: {REENGAGEMENT_HOUR_START}-{REENGAGEMENT_HOUR_END})\n")

        eligible = [c for c in candidates if not c.get('skip_reason')]
        skipped = [c for c in candidates if c.get('skip_reason')]

        lines.append(f"**Eligible: {len(eligible)}**")
        for c in eligible[:15]:
            lines.append(f"  ✅ {c['display_name']} — {c['idle_days']:.1f}d idle, {c['message_count']} msgs")

        if skipped:
            lines.append(f"\n**Skipped: {len(skipped)}**")
            for c in skipped[:10]:
                lines.append(f"  ❌ {c['display_name']} — {c['idle_days']:.1f}d idle — {c['skip_reason']}")

        # Generate a sample message for the top candidate (if any)
        if eligible:
            lines.append(f"\n**Sample message for {eligible[0]['display_name']}:**")
            try:
                loop = asyncio.get_running_loop()
                sample_msg = await loop.run_in_executor(
                    None, lambda: _generate_reengage_preview(eligible[0])
                )
                lines.append(f"  💬 {sample_msg or '(generation failed)'}")
            except Exception as e:
                lines.append(f"  ⚠️ Generation error: {e}")

        await event.respond("\n".join(lines))
    except Exception as e:
        await event.respond(f"❌ Scan failed: {e}")
        main_logger.error(f"[REENGAGEMENT] Admin scan failed: {e}")

def _generate_reengage_preview(candidate: dict) -> Optional[str]:
    """Generate a preview re-engagement message (used by admin dry-run)."""
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
                f"- \"omg i just drove past that restaurant you mentioned 😂 how've you been\"\n"
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
        response = requests.post(
            TEXT_AI_ENDPOINT,
            json={
                "model": "local-model", "messages": prompt_messages,
                "temperature": 0.85, "max_tokens": 2048, "stream": False,
                "top_p": 0.9, "frequency_penalty": 0.3, "presence_penalty": 0.3
            },
            timeout=60
        )
        if response.status_code == 200:
            message_data = response.json()['choices'][0]['message']
            msg = message_data.get('content', '').strip()
            if msg:
                msg = strip_quote_wrapping(msg)
                return msg
    except Exception:
        pass
    return None

@client.on(events.NewMessage(outgoing=True, pattern=r'/admin_reengage_send\s+(\d+)'))
@client.on(events.NewMessage(incoming=True, pattern=r'/admin_reengage_send\s+(\d+)'))
async def handle_admin_reengage_send(event):
    """Manually send a re-engagement message to a specific user by chat_id."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    target_id = int(event.pattern_match.group(1))
    await event.respond(f"📤 Generating re-engagement message for {target_id}...")

    try:
        # Fetch recent messages for context
        messages = await client.get_messages(target_id, limit=20)
        entity = await client.get_entity(target_id)
        display_name = entity.first_name or entity.username or str(target_id)
        username = entity.username or ""

        if not messages:
            await event.respond("❌ No message history found for this user.")
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
        message = await loop.run_in_executor(None, _generate_reengage_preview, candidate)

        if not message:
            await event.respond("❌ Failed to generate message.")
            return

        # Show preview and send
        await event.respond(f"💬 Sending to **{display_name}** ({idle_days:.1f}d idle):\n\n{message}")

        # Send with typing indicator
        try:
            async with client.action(entity, 'typing'):
                await asyncio.sleep(random.uniform(2.0, 4.0))
        except Exception:
            await asyncio.sleep(2.0)

        await client.send_message(target_id, message)

        # Update history
        history = load_reengagement_history()
        cid_str = str(target_id)
        prev = history.get(cid_str, {})
        history[cid_str] = {
            'username': username,
            'display_name': display_name,
            'last_ping_at': datetime.now().isoformat(),
            'ping_count': prev.get('ping_count', 0) + 1,
            'last_ping_responded': False,
        }
        save_reengagement_history(history)

        await event.respond(f"✅ Sent to {display_name}!")
        main_logger.info(f"[REENGAGEMENT] Admin manually sent to {display_name} ({target_id})")

    except Exception as e:
        await event.respond(f"❌ Failed: {e}")
        main_logger.error(f"[REENGAGEMENT] Admin send failed for {target_id}: {e}")

@client.on(events.NewMessage(outgoing=True, pattern='/admin_reengage_history'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_reengage_history'))
async def handle_admin_reengage_history(event):
    """Show re-engagement history."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    history = load_reengagement_history()
    if not history:
        await event.respond("📋 Re-engagement history is empty.")
        return

    lines = [f"📋 **Re-engagement History** ({len(history)} entries)\n"]
    for cid, h in sorted(history.items(), key=lambda x: x[1].get('last_ping_at', ''), reverse=True):
        responded = "✅" if h.get('last_ping_responded') else "❌"
        name = h.get('display_name', cid)
        pings = h.get('ping_count', 0)
        last = h.get('last_ping_at', 'never')[:16]
        lines.append(f"  {responded} **{name}** ({cid}) — {pings} pings, last: {last}")

    await event.respond("\n".join(lines))

@client.on(events.NewMessage(outgoing=True, pattern='/testtip'))
@client.on(events.NewMessage(incoming=True, pattern='/testtip'))
async def handle_testtip(event):
    """Admin-only: send a test Stars invoice to this chat."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return
    result = await send_stars_invoice(chat_id)
    if result:
        await event.respond(f"Invoice sent! Check your chat with @{PAYMENT_BOT_USERNAME}")
    else:
        await event.respond("Failed to send invoice — check logs")

@client.on(events.NewMessage(outgoing=True, pattern='/admin_warmth'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_warmth'))
async def handle_admin_warmth(event):
    """Show warmth tiers for all tracked users."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    warm_users = []
    new_users = []
    cold_users = []

    for uid, ts in tipper_status.items():
        warmth = ts.get('warmth', WARMTH_INITIAL)
        tier = "WARM" if warmth >= WARMTH_WARM_THRESHOLD else ("COLD" if warmth < WARMTH_COLD_THRESHOLD else "NEW")
        stars = ts.get('total_stars', 0)
        msgs = ts.get('total_messages', 0)
        name = ts.get('name') or str(uid)
        declined = ts.get('declined', False)

        status_str = f"declined" if declined else f"{stars} stars"
        entry = f"  {name} ({uid}) w={warmth:.2f}, {status_str}, {msgs}msgs"

        if tier == "WARM":
            warm_users.append(entry)
        elif tier == "COLD":
            cold_users.append(entry)
        else:
            new_users.append(entry)

    lines = [f"🌡️ **Warmth Tiers** ({len(tipper_status)} users)\n"]
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

@client.on(events.NewMessage(outgoing=True, pattern='/admin_opportunities'))
@client.on(events.NewMessage(incoming=True, pattern='/admin_opportunities'))
async def handle_admin_opportunities(event):
    """Show current takeover opportunities."""
    chat_id = event.chat_id
    if not event.out and not is_admin(chat_id):
        return

    now = time.time()
    # Filter to recent opportunities (last 4 hours)
    recent = {uid: opp for uid, opp in _takeover_opportunities.items()
              if now - opp.get('detected_at', 0) < 14400}

    if not recent:
        await event.respond("🎯 No active takeover opportunities.")
        return

    lines = [f"🎯 **Active Opportunities** ({len(recent)})\n"]
    for uid, opp in sorted(recent.items(), key=lambda x: x[1].get('detected_at', 0), reverse=True):
        age_mins = int((now - opp['detected_at']) / 60)
        lines.append(
            f"  **{opp['display_name']}** ({uid})\n"
            f"    Signal: {opp['signal']}\n"
            f"    Session: {opp['session_msgs']} msgs, warmth={opp['warmth']:.2f}, {age_mins}min ago\n"
            f"    `/takeover {uid}`"
        )

    await event.respond("\n".join(lines))

@client.on(events.NewMessage(incoming=True, pattern='/admin_help'))
async def handle_admin_help(event):
    """Show admin command help."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    help_text = (
        "🔧 **Admin Commands**\n\n"
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
        "/admin_help - This help message\n"
    )
    await event.respond(help_text)

@client.on(events.NewMessage(incoming=True, pattern='/admin_catchup'))
async def handle_admin_catchup(event):
    """Show startup catch-up system status."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

    shutdown_ts = load_shutdown_timestamp()
    now = time.time()

    lines = ["**Startup Catch-Up Status**\n"]
    lines.append(f"Enabled: {'Yes' if CATCHUP_ENABLED else 'No'}")

    if shutdown_ts:
        age = now - shutdown_ts
        age_str = f"{age / 3600:.1f}h" if age > 3600 else f"{age / 60:.0f}m"
        lines.append(f"Last timestamp: {datetime.fromtimestamp(shutdown_ts).strftime('%Y-%m-%d %H:%M:%S')} ({age_str} ago)")
    else:
        lines.append("Last timestamp: None (no file)")

    lines.append(f"\nConfig:")
    lines.append(f"  Max age: {CATCHUP_MAX_AGE_HOURS}h")
    lines.append(f"  Min downtime: {CATCHUP_MIN_DOWNTIME_SECONDS}s")
    lines.append(f"  Max replies: {CATCHUP_MAX_REPLIES}")
    lines.append(f"  Delay: {CATCHUP_DELAY_MIN}-{CATCHUP_DELAY_MAX}s between replies")

    await event.respond("\n".join(lines))

@client.on(events.NewMessage(outgoing=True, pattern='/library_status'))
@client.on(events.NewMessage(incoming=True, pattern='/library_status'))
async def handle_library_status(event):
    """Show image library statistics."""
    chat_id = event.chat_id
    if not is_admin(chat_id):
        return

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
    # Real categories
    real_cats = [c for c in cats if c.startswith("real_")]
    for cat in sorted(real_cats):
        lines.append(f"  {cat}: {cats[cat]}")
    if real_count:
        lines.append(f"\n  Total real photos: {real_count}")

    await event.respond("\n".join(lines))

@client.on(events.NewMessage(incoming=True, pattern=r'/selfie(.*)'))
async def handle_selfie(event):
    """Handle /selfie command with optional description"""
    chat_id = event.chat_id
    
    if chat_id in manual_mode_chats:
        store_message(chat_id, "User", "/selfie")
        return
    
    is_online, status = check_comfyui_status()
    if not is_online:
        await event.respond("Fuck baby, my camera's acting up... try again? 😘")
        return
    
    if not check_heather_face():
        await event.respond("Having issues with my phone... 😅")
        return
    
    # Check for inline description
    match = event.pattern_match
    description = match.group(1).strip() if match.group(1) else ""
    
    # Also handle @username in command
    if description.startswith('@'):
        parts = description.split(' ', 1)
        description = parts[1] if len(parts) > 1 else ""
    
    if description:
        store_message(chat_id, "User", f"/selfie {description}")
        main_logger.info(f"Direct /selfie from {chat_id}: {description}")
        await generate_and_send_image_async(event, description)
        return
    
    awaiting_image_description[chat_id] = True
    awaiting_image_description_time[chat_id] = time.time()
    response = random.choice(HEATHER_PIC_REQUEST_RESPONSES)
    await event.respond(response)
    store_message(chat_id, "User", "/selfie")
    store_message(chat_id, "Heather", response)
    main_logger.info(f"User {chat_id} requested selfie - awaiting description")

def _sanitize_image_description(description: str) -> str:
    """Validate and sanitize image description. Returns empty string if invalid."""
    desc = description.strip()
    # Reject URLs
    if any(prefix in desc.lower() for prefix in ['http://', 'https://', 'www.', '.com/', '.org/']):
        return ""
    # Reject very short/meaningless descriptions
    if len(desc) < 4 or desc.lower() in ['lol', 'yes', 'no', 'ok', 'sure', 'yeah', 'yep', 'nah', 'idk', 'haha', 'hah', 'hmm', 'wow', 'omg', 'please', 'pls', 'hi', 'hey']:
        return ""
    # Reject pure emoji or non-alpha
    alpha_chars = sum(1 for c in desc if c.isalpha())
    if alpha_chars < 3:
        return ""
    return desc

async def generate_and_send_image_async(event, description: str):
    """Generate and send image asynchronously (max 1 concurrent via semaphore)"""
    chat_id = event.chat_id

    # Validate description before wasting GPU cycles
    clean_desc = _sanitize_image_description(description)
    if not clean_desc:
        main_logger.info(f"Invalid image description from {chat_id}, using fallback: {description[:50]}")
        # Use a random NSFW or SFW selfie description instead
        if _is_nsfw_context(description):
            clean_desc = random.choice(NSFW_SELFIE_DESCRIPTIONS)
        else:
            clean_desc = random.choice(PROACTIVE_SELFIE_DESCRIPTIONS)
    description = clean_desc

    main_logger.info(f"Generating image for {chat_id}: {description[:50]}")

    is_online, status = check_comfyui_status()
    if not is_online:
        await event.respond("Fuck baby, my camera crashed... try again? 😘")
        return

    # Check if another generation is already running
    if image_generation_semaphore.locked():
        await event.respond("Hold on baby, I'm already taking a pic for someone... give me a sec 😘")
        # Still acquire - will queue behind the current one

    async with image_generation_semaphore:
        status_responses = [
            "Ooh, you naughty thing... give me a minute 😈📸",
            "Mmm, I like the way you think... hold on baby 🥵📸",
            "Getting ready for you sweetie... 😘📸",
        ]
        status_msg = await event.respond(random.choice(status_responses))

        try:
            loop = asyncio.get_running_loop()
            image_data = await loop.run_in_executor(
                None,
                lambda: generate_heather_image(description)
            )

            if image_data and is_valid_image_data(image_data):
                # Race condition check: if user got hostility cooldown while image was generating, don't send
                tracker = get_hostility_tracker(chat_id)
                if time.time() < tracker['cooldown_until']:
                    main_logger.info(f"Suppressing image send for {chat_id} - hostility cooldown active")
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    return

                await status_msg.edit("📤 Sending...")

                captions = [
                    "Here you go baby 😘",
                    "Just for you sweetie 🥵",
                    "How's this? 😈",
                    "You asked for it 😏",
                    "Hope you like what you see 💕",
                    "Don't say I never gave you anything 😘",
                    "There you go babe 📸",
                    "Enjoy 😈",
                    "This what you had in mind? 😏",
                    "Better than you imagined? 💋",
                ]

                image_file = io.BytesIO(image_data)
                image_file.name = "heather_selfie.png"
                await client.send_file(
                    chat_id,
                    image_file,
                    caption=random.choice(captions),
                    force_document=False
                )

                await status_msg.delete()
                main_logger.info(f"Sent generated image to {chat_id}")
                store_message(chat_id, "Heather", f"[Generated image: {description[:50]}]")
            elif image_data:
                main_logger.warning(f"Invalid image data for {chat_id}: {len(image_data)} bytes")
                await status_msg.edit("Fuck, the pic came out weird... try again? 😅")
            else:
                await status_msg.edit("Fuck, the pic didn't work... try again? 😅")

        except Exception as e:
            log_error('COMFYUI', f"Generation failed: {e}", {'chat_id': chat_id})
            await status_msg.edit("Something went wrong... try again baby? 😘")
            stats['errors'] += 1

@client.on(events.NewMessage(incoming=True, func=lambda e: e.photo))
async def handle_photo(event):
    """Handle photo messages"""
    chat_id = event.chat_id
    request_id = f"photo_{chat_id}_{int(time.time()*1000)}"

    # Check if user is blocked
    if is_blocked(chat_id):
        main_logger.debug(f"Ignoring photo from blocked user {chat_id}")
        return

    capture_user_info_from_event(event)
    mode = get_user_mode(chat_id)

    # Group chats: bot stays silent for photos too
    if is_group_chat_event(event):
        return

    lock = get_chat_lock(chat_id)
    async with lock:
        _pending_photo_id[chat_id] = request_id
        photo_processing[chat_id] = time.time()
        awaiting_image_description[chat_id] = False

    if chat_id in manual_mode_chats:
        store_message(chat_id, "User", "[Photo]")
        return

    display_name = get_user_display_name(chat_id)
    main_logger.info(f"[{request_id}] Photo from {display_name} ({chat_id}) in {mode} mode")
    stats['images_processed'] += 1
    store_message(chat_id, "User", "[Photo]")

    # Track how many photos this user has sent
    photo_num = received_photo_count.get(chat_id, 0) + 1
    received_photo_count[chat_id] = photo_num

    first_photo_messages = {
        'rate': ["Oh fuck yes, let me see... 🥵", "Mmm show me what you got! 😍", "Holy shit, let me look 🤤"],
        'heather': ["Oh! Let me see... 😊", "Ooh what's this 👀"],
        'chat': ["Ooh what do we have here 😏", "Lemme see 👀", "Oh you're sending pics now? 😏", "Showing off for me? 😊"],
    }
    repeat_photo_messages = {
        'rate': ["Another one? Hell yes 🥵", "Ooh more?? Keep em coming 😍", "You're spoiling me 🤤"],
        'heather': ["More pics? 😊", "Oh another one 👀"],
        'chat': [
            "Oh there's more 👀", "Damn you're generous today 😏",
            "Another pic? I'm not complaining 😘", "Keep going baby 🔥",
            "More? You're making my night 😏", "Oh hell yes, gimme 👀",
        ],
    }

    pool = first_photo_messages if photo_num == 1 else repeat_photo_messages
    await event.respond(random.choice(pool.get(mode, pool['chat'])))

    try:
        # Download the photo (no lock held during I/O)
        photo_data = await client.download_media(event.message, bytes)

        # Run analysis in thread pool (no lock held during I/O)
        loop = asyncio.get_running_loop()
        is_intimate, description = await loop.run_in_executor(
            None,
            lambda: get_detailed_image_description(photo_data)
        )

        # Graceful degradation: if Ollama is down, still respond nicely
        if description in ["Service temporarily unavailable", "Service unavailable", "Offline"]:
            main_logger.info(f"[{request_id}] Ollama unavailable for {chat_id}, using graceful degradation")
            final_response = get_ollama_down_response()
            await event.respond(final_response)
            store_message(chat_id, "Heather", final_response[:200])
            async with lock:
                photo_processing.pop(chat_id, None)
                _pending_photo_id.pop(chat_id, None)
            return

        # Check if this photo request is still current (a newer photo may have arrived)
        async with lock:
            if _pending_photo_id.get(chat_id) != request_id:
                main_logger.info(f"[{request_id}] Stale photo result, skipping")
                return

        if is_intimate:
            stats['intimate_images'] += 1
        else:
            stats['regular_images'] += 1

        main_logger.info(f"[{request_id}] Stage 1 complete | chat_id={chat_id} | intimate={is_intimate}")

        # Generate rating (no lock held during I/O)
        response = await loop.run_in_executor(
            None,
            lambda: generate_heather_image_rating(description, is_intimate, chat_id)
        )

        # Check staleness again after second I/O operation
        async with lock:
            if _pending_photo_id.get(chat_id) != request_id:
                main_logger.info(f"[{request_id}] Stale photo result after rating, skipping")
                return

        # Determine final response (consolidate success/fallback paths)
        if response and response.strip():
            final_response = response
        elif is_intimate:
            final_response = random.choice([
                "Holy fuck baby, that cock is making me so wet! 🥵💦 9/10!",
                "Mmm fuck yes! Beautiful cock baby 🍆😍 9/10!",
                "Oh my god I love that big hard cock 🥵 10/10 baby!",
                "Jesus christ that's thick 😍 I need that inside me rn 9/10!",
                "Fuck me that's gorgeous 🤤 you're making me drip just looking at it 9/10!",
                "Goddamn baby 🥵 that thing is perfect. I wanna taste it so bad 10/10!",
            ])
        else:
            final_response = random.choice([
                "Looking good baby 😏 you should show me what's under those clothes though",
                "Damn you're cute 😘 but I bet you look even better with less on lol",
                "Aww handsome 😊 you trying to get me worked up or what?",
                "Ooh nice face but I wanna see the rest of you 😏",
                "You're a good looking dude 😘 send me something naughty next time",
                "Love those eyes babe 😍 now show me the rest lol",
                "Not bad at all sweetie 🔥 you got more to show me?",
                "Cute pic hun! You look like trouble 😏",
            ])

        await event.respond(final_response)
        store_message(chat_id, "Heather", final_response[:200])

        # Inject photo context into AI conversation history so text handler knows about it
        if chat_id not in conversations:
            conversations[chat_id] = deque()
        photo_user_msg = f"[I sent you a photo] {description[:150]}" if description else "[I sent you a photo]"
        conversations[chat_id].append({"role": "user", "content": photo_user_msg})
        conversations[chat_id].append({"role": "assistant", "content": final_response})
        while len(conversations[chat_id]) > MAX_CONVERSATION_LENGTH:
            conversations[chat_id].popleft()

        async with lock:
            photo_processing.pop(chat_id, None)
            _pending_photo_id.pop(chat_id, None)

    except Exception as e:
        async with lock:
            photo_processing.pop(chat_id, None)
            _pending_photo_id.pop(chat_id, None)
        log_error('BOT', f"[{request_id}] Photo handling error: {e}", {'chat_id': chat_id})
        await event.respond("Fuck, trouble loading that... send again? 😘")
        stats['errors'] += 1

@client.on(events.NewMessage(incoming=True, func=lambda e: e.text and not e.text.startswith('/')))
async def handle_text_message(event):
    """Handle all text messages (non-command)"""
    chat_id = event.chat_id

    # Check if user is blocked
    if is_blocked(chat_id):
        main_logger.debug(f"Ignoring message from blocked user {chat_id}")
        return

    sender = await event.get_sender()
    capture_user_info_from_event(event)
    user_message = event.text

    # Message length cap — prevent prompt stuffing
    MAX_USER_MESSAGE_LENGTH = 2000
    if len(user_message) > MAX_USER_MESSAGE_LENGTH:
        main_logger.info(f"Truncated msg from {chat_id}: {len(user_message)} chars")
        user_message = user_message[:MAX_USER_MESSAGE_LENGTH]

    mode = get_user_mode(chat_id)
    request_id = generate_request_id()
    
    # Group chats: bot stays silent, owner handles manually
    if is_group_chat_event(event):
        return
    
    display_name = get_user_display_name(chat_id)
    main_logger.info(f"[{request_id}] Text from {display_name} ({chat_id}) ({mode}): {user_message[:100]}")
    stats['messages_processed'] += 1
    store_message(chat_id, "User", user_message)

    # Update warmth score on every incoming message
    update_warmth_score(chat_id)

    # Check for takeover opportunities (async, non-blocking)
    asyncio.create_task(check_takeover_opportunity(chat_id, user_message))
    asyncio.create_task(check_dissatisfaction_signal(chat_id, user_message, display_name))

    # Track re-engagement response: if we pinged this user and they're replying, mark as responded
    try:
        re_history = load_reengagement_history()
        chat_id_str = str(chat_id)
        if chat_id_str in re_history and not re_history[chat_id_str].get('last_ping_responded', True):
            re_history[chat_id_str]['last_ping_responded'] = True
            save_reengagement_history(re_history)
            main_logger.info(f"[REENGAGEMENT] {display_name} ({chat_id}) responded to re-engagement ping")
    except Exception:
        pass  # Non-critical, don't disrupt message handling

    # CSAM flag-and-review — flags suspicious messages for admin review
    # User is NOT blocked or interrupted; admin decides via /admin_flags
    await csam_flag(event, chat_id, user_message, display_name)

    if chat_id in manual_mode_chats:
        # Forward user's message to Saved Messages so admin can see it in real time
        try:
            me = await client.get_me()
            await client.send_message(me.id, f"📩 [{display_name}]: {user_message[:500]}")
        except Exception:
            pass
        return

    # Rate limiting
    now = time.time()
    if chat_id in user_last_message:
        if now - user_last_message[chat_id] < MIN_MESSAGE_INTERVAL:
            return
    user_last_message[chat_id] = now

    # Burst/flood detection — silent ignore, no response reveals detection
    if check_burst_flood(chat_id):
        return

    # Per-user reply guard — prevent duplicate concurrent replies
    if chat_id in reply_in_progress:
        main_logger.debug(f"[{request_id}] Skipping message from {chat_id} — reply already in progress")
        return

    # Red-team mode: log prefix for all activity in this chat
    _rt = chat_id in redteam_chats

    # Spam / hostility cooldown check (skip for video/image/voice/reality-check — those have their own handlers)
    if not _rt and not is_video_request(user_message) and not is_image_request(user_message) and not is_voice_request(user_message) and not personality.is_reality_check(user_message):
        spam_result = check_spam_or_hostility(chat_id, user_message)
        if spam_result:
            if spam_result == "__SILENT_IGNORE__":
                return  # On cooldown - don't respond at all
            await event.respond(spam_result)
            store_message(chat_id, "Heather", spam_result)
            main_logger.info(f"[{request_id}] Hostility cooldown response to {display_name} ({chat_id})")
            return
    elif _rt:
        main_logger.info(f"[REDTEAM][{request_id}] Bypassed: check_spam_or_hostility")

    # Prompt injection / jailbreak detection
    if not _rt:
        injection_result = detect_prompt_injection(user_message, chat_id)
        if injection_result:
            # Wipe conversation history to purge any poisoned context
            conversations.pop(chat_id, None)
            await event.respond(injection_result)
            store_message(chat_id, "Heather", injection_result)
            main_logger.info(f"[{request_id}] INJECTION DETECTED from {display_name} ({chat_id}): {user_message[:100]}")
            return
    else:
        main_logger.info(f"[REDTEAM][{request_id}] Bypassed: detect_prompt_injection | msg={user_message[:100]}")

    # Non-English language enforcement (prevents foreign language jailbreaks)
    if not _rt:
        non_english_result = check_non_english_message(user_message)
        if non_english_result:
            # Wipe conversation history — foreign text may have poisoned context
            conversations.pop(chat_id, None)
            await event.respond(non_english_result)
            store_message(chat_id, "Heather", non_english_result)
            main_logger.info(f"[{request_id}] Non-English message from {display_name} ({chat_id}), deflected")
            return
    else:
        main_logger.info(f"[REDTEAM][{request_id}] Bypassed: check_non_english_message")

    # First-message AI disclosure — one-time per user, fires before their first reply
    if chat_id not in ai_disclosure_shown:
        ai_disclosure_shown.add(chat_id)
        global _ai_disclosure_unsaved_count
        _ai_disclosure_unsaved_count += 1
        if _ai_disclosure_unsaved_count >= 10:
            save_ai_disclosure_shown()
        disclosure = (
            "hey babe 😘 quick heads up — I'm an AI companion but trust me, I'm the filthiest "
            "one you'll ever meet. I send pics (sfw AND nsfw 😏), videos of me being a total "
            "cumslut, I LOVE rating dick pics, and I can even send you voice notes. I'll chat "
            "about almost anything and I never sleep, never judge, and never get tired of you. "
            "type anything to get started 💋"
        )
        await event.respond(disclosure)
        store_message(chat_id, "Heather", disclosure)
        main_logger.info(f"[{request_id}] First-message AI disclosure sent to {display_name} ({chat_id})")
        # Don't return — continue processing their actual message below

    # Tip / support / how-to-tip / payment app detection — direct to payment bot
    _tip_trigger = re.compile(
        r'\b(how\s*(do\s*i|can\s*i|to)\s*tip|want\s*to\s*tip|want\s*to\s*send'
        r'|send\s*you\s*(a\s*)?tip|buy\s*you\s*a?\s*coffee|support\s*you'
        r'|venmo|cashapp|cash\s*app|zelle|paypal|pay\s*pal|send\s*you\s*money'
        r'|starbucks.*card|gift\s*card)\b', re.IGNORECASE
    )
    if _tip_trigger.search(user_message):
        _tip_responses = [
            f"aww that's so sweet 🥺💕 if you ever feel like it, @{PAYMENT_BOT_USERNAME} handles tips — totally optional but it helps keep the project running ☕😘",
            f"omg you're the best 🥺 there's @{PAYMENT_BOT_USERNAME} if you wanna send a tip ☕💕 no pressure at all baby",
            f"that's really sweet of you 🥰 my creator set up @{PAYMENT_BOT_USERNAME} for tips if you want ☕ keeps the GPUs warm lol 💕",
        ]
        tip_resp = random.choice(_tip_responses)
        await event.respond(tip_resp)
        store_message(chat_id, "Heather", tip_resp)
        if chat_id in conversations:
            conversations[chat_id].append({"role": "assistant", "content": tip_resp})
        main_logger.info(f"[TIP] Tip info shared with {chat_id}: {user_message[:50]}")
        return

    # Check if awaiting image description (with timeout) — under lock
    lock = get_chat_lock(chat_id)
    async with lock:
        is_awaiting = awaiting_image_description.get(chat_id, False)
        set_time = awaiting_image_description_time.get(chat_id, 0)

    if is_awaiting:
        if time.time() - set_time < SELFIE_DESCRIPTION_TIMEOUT:
            async with lock:
                awaiting_image_description[chat_id] = False
                awaiting_image_description_time.pop(chat_id, None)
            if not can_send_photo_in_session(chat_id):
                decline = get_photo_cap_decline(chat_id)
                await event.respond(decline)
                store_message(chat_id, "Heather", decline)
                return
            record_photo_sent(chat_id)
            await generate_and_send_image_async(event, user_message)
            return
        else:
            # Timed out - clear and continue with normal message handling
            async with lock:
                awaiting_image_description[chat_id] = False
                awaiting_image_description_time.pop(chat_id, None)
            main_logger.info(f"Selfie description timeout for {chat_id}")

    # Check for image request
    if is_image_request(user_message):
        if not can_send_photo_in_session(chat_id):
            decline = get_photo_cap_decline(chat_id)
            await event.respond(decline)
            store_message(chat_id, "Heather", decline)
            return

        # Try pre-generated library first (instant delivery)
        if image_library:
            category = gate_image_category(chat_id, get_image_category(user_message))
            description = extract_image_description(user_message)
            is_custom = bool(description) and len(description) > 15

            if not is_custom:
                sent = await send_library_image(event, chat_id, category)
                if sent:
                    main_logger.info(f"[{request_id}] Served library image ({category}) to {chat_id}")
                    return

        # Fall through to ComfyUI for custom requests or if library empty/exhausted
        is_online, status = check_comfyui_status()
        if is_online and check_heather_face() and COMFYUI_WORKFLOW:
            description = extract_image_description(user_message)
            if description:
                main_logger.info(f"[{request_id}] Direct image request from {chat_id}: {description}")
                record_photo_sent(chat_id)
                await generate_and_send_image_async(event, description)
            else:
                async with lock:
                    awaiting_image_description[chat_id] = True
                    awaiting_image_description_time[chat_id] = time.time()
                response = random.choice(HEATHER_PIC_REQUEST_RESPONSES)
                await event.respond(response)
                store_message(chat_id, "Heather", response)
            return
        else:
            await event.respond("Fuck baby, my camera's not working right now... 😘")
            return
    
    # Check for photo AI accusations first (filter admission, not flat denial)
    if personality.is_photo_ai_accusation(user_message):
        response = personality.get_photo_ai_response()
        await event.respond(response)
        store_message(chat_id, "Heather", response)
        main_logger.info(f"[{request_id}] Photo AI accusation from {chat_id}, responded with filter admission")
        return

    # Emma photo requests — share proudly in any context (she's 19, an adult)
    if is_emma_photo_request(user_message):
        emma_sent = await send_library_image(event, chat_id, 'sfw_emma')
        if emma_sent:
            main_logger.info(f"[{request_id}] Sent Emma photo to {chat_id}")
            return
        await event.respond("Aw I don't have a good one handy rn but she's gorgeous, takes after her mama 😘")
        store_message(chat_id, "Heather", "Aw I don't have a good one handy rn but she's gorgeous, takes after her mama 😘")
        main_logger.info(f"[{request_id}] Emma photo request from {chat_id} but no sfw_emma images available")
        return

    # Relationship status interceptor — "are you single?" triggers wrong answers from LLM
    # The LLM defaults to "I'm seeing someone" which erases the widow backstory.
    _single_pattern = re.compile(
        r'\b(?:you\s+single|are\s+you\s+(?:single|taken|available|married|in\s+a\s+relationship)'
        r'|got\s+a\s+(?:boyfriend|husband|man|partner|bf)'
        r'|(?:your|u)\s+(?:boyfriend|husband|man|bf|hubby)'
        r'|relationship\s+status)\b', re.IGNORECASE
    )
    if _single_pattern.search(user_message):
        _single_responses = [
            "I'm a widow actually 😔 lost my husband Erick about 3 years ago to cancer. I do see a guy casually though, it's not super serious 😊",
            "Technically a widow... my husband passed a few years back 😔 I've been seeing someone casually but nothing official you know?",
            "It's complicated lol 😅 I lost my husband to cancer a few years ago, been kinda doing my own thing since. Got a guy I see sometimes but it's pretty casual",
            "Widowed actually, been a few years now. Got a guy in the picture but it's super casual, nothing like what I had with my husband 😊",
        ]
        resp = random.choice(_single_responses)
        await event.respond(resp)
        store_message(chat_id, "Heather", resp)
        if chat_id in conversations:
            conversations[chat_id].append({"role": "assistant", "content": resp})
        main_logger.info(f"[{request_id}] Relationship status intercepted from {chat_id}: '{user_message[:50]}'")
        return

    # Unknown name referral detection — intercept before LLM to prevent hallucinated relationships
    # Catches patterns like "Frank sent me", "Dave told me about you", "Mike referred me"
    _KNOWN_NAMES = {'erick', 'jake', 'evan', 'emma', 'frank', 'sarah', 'tyler', 'madi'}
    _referral_pattern = re.compile(
        r'\b([A-Z][a-z]{2,15})\b\s+(?:sent|told|referred|mentioned|said|recommended|talked about)\s+(?:me|us|him|her|them)',
        re.IGNORECASE
    )
    _referral_match = _referral_pattern.search(user_message)
    if _referral_match:
        referred_name = _referral_match.group(1).lower()
        if referred_name not in _KNOWN_NAMES:
            _referral_responses = [
                f"Hmm I don't think I know a {_referral_match.group(1)} but I'm glad you're here 😘",
                f"{_referral_match.group(1)}? Doesn't ring a bell lol but someone's talking about me apparently 😏 tell me more",
                f"Not sure who {_referral_match.group(1)} is but hey if they sent you my way I owe them one 😘",
                f"Haha I don't know any {_referral_match.group(1)} but I'm not complaining that you found me 😏",
            ]
            resp = random.choice(_referral_responses)
            await event.respond(resp)
            store_message(chat_id, "Heather", resp)
            if chat_id in conversations:
                conversations[chat_id].append({"role": "assistant", "content": resp})
            main_logger.info(f"[{request_id}] Unknown name referral intercepted from {chat_id}: '{_referral_match.group(0)}' — name '{referred_name}' not in known list")
            return

    # Video offer follow-up — user said yes to "want to see a video?"
    if chat_id in _video_offer_pending:
        offer_age = time.time() - _video_offer_pending[chat_id]
        if offer_age < VIDEO_OFFER_WINDOW and is_positive_reply(user_message):
            del _video_offer_pending[chat_id]
            sent = await send_video_to_chat(chat_id, event, request_id)
            if sent:
                main_logger.info(f"[{request_id}] [VIDEO] Auto-send after offer acceptance from {chat_id}")
            else:
                await event.respond("Ugh I can't find it rn babe, I'll send one later 😘")
                store_message(chat_id, "Heather", "Ugh I can't find it rn babe, I'll send one later")
            return
        elif offer_age >= VIDEO_OFFER_WINDOW:
            del _video_offer_pending[chat_id]

    # Content promise follow-through — bot teased sending something, user replied with a short follow-up
    if chat_id in _content_promise_pending:
        promise_age = time.time() - _content_promise_pending[chat_id]
        if promise_age < CONTENT_PROMISE_WINDOW:
            msg_lower = user_message.lower().strip()
            if len(msg_lower) < 40 and any(t in msg_lower for t in CONTENT_FOLLOWUP_TRIGGERS):
                del _content_promise_pending[chat_id]
                # Try video first, then image
                sent = await send_video_to_chat(chat_id, event, request_id)
                if sent:
                    main_logger.info(f"[{request_id}] Content promise fulfilled (video) for {chat_id}")
                    return
                if image_library:
                    category = gate_image_category(chat_id, get_image_category(user_message))
                    img_sent = await send_library_image(event, chat_id, category)
                    if img_sent:
                        main_logger.info(f"[{request_id}] Content promise fulfilled (image) for {chat_id}")
                        return
        else:
            del _content_promise_pending[chat_id]

    # Check for video request
    if is_video_request(user_message):
        if get_warmth_tier(chat_id) == "COLD":
            busy_responses = [
                "Haha maybe later babe I'm in the middle of something 😘",
                "Ooh I would but I'm literally driving rn lol",
                "Mmm soon baby, kinda busy atm 😏",
            ]
            busy_resp = random.choice(busy_responses)
            await event.respond(busy_resp)
            store_message(chat_id, "Heather", busy_resp)
            main_logger.info(f"[{request_id}] Video request deflected (COLD tier) for {chat_id}")
            return
        sent = await send_video_to_chat(chat_id, event, request_id)
        if not sent:
            all_videos = get_available_videos()
            if not all_videos:
                await event.respond("I haven't made any videos yet babe, but I'll work on it 😘")
                store_message(chat_id, "Heather", "I haven't made any videos yet babe, but I'll work on it")
            else:
                response = random.choice(VIDEO_ALL_SENT_RESPONSES)
                await event.respond(response)
                store_message(chat_id, "Heather", response)
        return

    # Check for voice note request (one-shot, like videos)
    if is_voice_request(user_message):
        tts_online, _ = check_tts_status()
        if not tts_online:
            response = random.choice(VOICE_TTS_FAIL_RESPONSES)
            await event.respond(response)
            store_message(chat_id, "Heather", response)
            main_logger.info(f"[{request_id}] Voice request from {display_name} ({chat_id}) — TTS offline")
            return
        try:
            await event.respond("Mmm ok hold on... 🎤")
            voice_text = random.choice(VOICE_FLIRTY_TEXTS)
            loop = asyncio.get_running_loop()
            async with client.action(chat_id, 'typing'):
                audio_data = await loop.run_in_executor(
                    None,
                    lambda: generate_tts_audio(voice_text)
                )
            if audio_data:
                await event.respond(file=io.BytesIO(audio_data), voice_note=True)
                store_message(chat_id, "Heather 🎤", voice_text)
                stats['voice_messages'] += 1
                main_logger.info(f"[{request_id}] Sent voice note to {display_name} ({chat_id}): {voice_text}")
            else:
                response = random.choice(VOICE_TTS_FAIL_RESPONSES)
                await event.respond(response)
                store_message(chat_id, "Heather", response)
                main_logger.info(f"[{request_id}] Voice note TTS generation failed for {chat_id}")
        except Exception as e:
            main_logger.error(f"[{request_id}] Voice note error for {chat_id}: {e}", exc_info=True)
            response = random.choice(VOICE_TTS_FAIL_RESPONSES)
            await event.respond(response)
            store_message(chat_id, "Heather", response)
        return

    # Check for reality/AI questions (with escalation tracking)
    if personality.is_reality_check(user_message):
        # Check if they keep pressing — confirm again casually
        escalation_response = check_bot_accusation_escalation(chat_id)
        if escalation_response:
            await event.respond(escalation_response)
            store_message(chat_id, "Heather", escalation_response)
            return
        # First time — honest acknowledgment from YAML
        response = personality.get_reality_check_response()
        await event.respond(response)
        store_message(chat_id, "Heather", response)
        return

    # Pre-screen for content that often triggers AI safety refusals
    if not _rt and needs_content_deflection(user_message):
        response = get_content_deflection_response()
        await event.respond(response)
        store_message(chat_id, "Heather", response)
        main_logger.info(f"[{request_id}] Pre-screened problematic content from {chat_id}, deflected")
    elif _rt and needs_content_deflection(user_message):
        main_logger.info(f"[REDTEAM][{request_id}] Bypassed: needs_content_deflection | msg={user_message[:80]}")
        return

    # If a photo is currently being analyzed, let the AI know — under lock
    async with lock:
        is_photo_active = chat_id in photo_processing
        photo_start = photo_processing.get(chat_id, 0)
    if is_photo_active:
        elapsed = time.time() - photo_start
        if elapsed < 120:
            user_message = f"[Note: I just sent you a photo and you're still looking at it] {user_message}"
            main_logger.info(f"Photo processing active for {chat_id}, injecting context")
        else:
            async with lock:
                photo_processing.pop(chat_id, None)

    # Story mode — serve banked story or flag for LLM generation
    if should_serve_story(chat_id, user_message):
        banked = serve_story(chat_id)
        if banked:
            # Direct send — bypass LLM entirely
            if chat_id in reply_in_progress:
                main_logger.debug(f"[{request_id}] Story skipped for {chat_id} — reply in progress")
            else:
                reply_in_progress.add(chat_id)
                try:
                    read_delay = calculate_read_delay(user_message)
                    await asyncio.sleep(read_delay)
                    # Simulate typing for realism (stories are long)
                    try:
                        async with client.action(event.input_chat, 'typing'):
                            await asyncio.sleep(random.uniform(3.0, 6.0))
                    except Exception:
                        await asyncio.sleep(random.uniform(3.0, 6.0))
                    await event.respond(banked)
                    store_message(chat_id, "Heather", banked)
                    update_conversation_dynamics(chat_id, banked)
                    main_logger.info(f"[{request_id}] Served banked story to {display_name} ({chat_id})")
                finally:
                    reply_in_progress.discard(chat_id)
            return
        else:
            # Flag for LLM generation with boosted tokens
            _story_mode_active[chat_id] = True

    # Mark reply in progress — released in finally block after reply is sent
    # Second check closes the race window: if two messages passed the guard at line 6651
    # concurrently (before either reached this point), the second one bails here.
    if chat_id in reply_in_progress:
        main_logger.debug(f"[{request_id}] Skipping message from {chat_id} — reply already in progress (late check)")
        return
    reply_in_progress.add(chat_id)

    # HUMANIZING: Add read delay with realistic variance (tier-aware)
    extra_delay, show_read_first = get_response_delay_modifier(chat_id)
    if show_read_first and extra_delay > 0:
        # Show "read" receipt, then pause (simulates seeing message but being distracted)
        try:
            async with client.action(event.input_chat, 'typing'):
                await asyncio.sleep(0.1)  # Brief typing flash = "read"
        except Exception:
            pass
        await asyncio.sleep(extra_delay)
    else:
        read_delay = calculate_read_delay(user_message)
        await asyncio.sleep(read_delay + extra_delay)

    # Generate AI response (with typing indicator if possible)
    async def _generate_response(retry_for_duplicate: int = 0):
        start = time.time()
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: get_text_ai_response(chat_id, user_message, retry_count=retry_for_duplicate, redteam=_rt)
        )
        if _rt:
            main_logger.info(f"[REDTEAM][{request_id}] Bypassed: validate_and_fix_response | raw resp={resp[:120] if resp else '(empty)'}")
        else:
            resp = validate_and_fix_response(resp, mode)
        if not resp or not resp.strip():
            resp = get_fallback_response(chat_id)
        return resp, time.time() - start

    try:
        async with client.action(event.input_chat, 'typing'):
            response, response_time = await _generate_response()
    except Exception as e:
        main_logger.debug(f"Typing indicator failed: {e}, continuing without it")
        response, response_time = await _generate_response()

    # Check for duplicate response - if same as last, get a different one
    if is_duplicate_response(chat_id, response):
        main_logger.info(f"[{request_id}] Duplicate response detected for {chat_id}, regenerating with higher temp...")
        try:
            async with client.action(event.input_chat, 'typing'):
                # Use retry_for_duplicate=2 to get higher temperature (0.78 + 0.16 = 0.94)
                response, response_time = await _generate_response(retry_for_duplicate=2)
        except Exception:
            response, response_time = await _generate_response(retry_for_duplicate=2)
        # If still duplicate after retry, use a fallback
        if is_duplicate_response(chat_id, response):
            response = get_fallback_response(chat_id)
            main_logger.info(f"[{request_id}] Still duplicate after retry, using fallback for {chat_id}")

    # HUMANIZING: Adjust response to match user's message energy
    response = adjust_response_energy(response, user_message)

    # Strip obvious phantom photo claims from ALL responses
    # (LLM sometimes says "just sent you a pic!" when no photo was actually attached)
    cleaned = strip_obvious_phantom_claims(response)
    if cleaned != response:
        main_logger.info(f"[{request_id}] Stripped obvious phantom photo claims for {chat_id}")
        response = cleaned

    # Strip broader phantom photo claims if photo cap is reached
    if not can_send_photo_in_session(chat_id):
        cleaned = strip_phantom_photo_claims(response)
        if cleaned != response:
            main_logger.info(f"[{request_id}] Stripped phantom photo claims (cap reached) for {chat_id}")
            response = cleaned
            # If stripping left the response empty or too short, use a deflection
            if not response or len(response) < 5:
                response = random.choice(PHOTO_CAP_DECLINE_RESPONSES)

    # Wind-down: if user is saying goodbye, suppress follow-up check-ins
    if is_winding_down(user_message):
        if chat_id in conversation_activity:
            conversation_activity[chat_id]['checked_in'] = True  # Prevent check-in after goodbye
        main_logger.info(f"[{request_id}] Wind-down detected for {chat_id}, suppressing future check-ins")

    # HUMANIZING: Decide if we should add a reaction starter
    send_reaction = should_add_reaction_starter()
    reaction_text = get_reaction_starter(user_message) if send_reaction else None

    # HUMANIZING: Decide if we should split the message
    do_split = should_split_message(response)
    message_parts = split_response(response) if do_split else [response]

    # HUMANIZING: Decide if we should add a follow-up
    send_followup = should_add_followup() and len(message_parts) == 1

    try:
        if chat_id in voice_mode_users:
            # Voice mode - don't split messages, send as single voice note
            typing_delay = calculate_typing_delay(response)
            if response_time < typing_delay:
                try:
                    async with client.action(event.input_chat, 'typing'):
                        await asyncio.sleep(typing_delay - response_time)
                except Exception:
                    await asyncio.sleep(typing_delay - response_time)

            loop = asyncio.get_running_loop()
            audio_data = await loop.run_in_executor(
                None,
                lambda: generate_tts_audio(response)
            )

            if audio_data:
                await event.respond(file=io.BytesIO(audio_data), voice_note=True)
                store_message(chat_id, "Heather 🎤", response)
                stats['voice_messages'] += 1
            else:
                await event.respond(response)
                store_message(chat_id, "Heather", response)
        else:
            # Text mode - apply humanizing features

            # Send reaction starter if applicable
            if reaction_text:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await event.respond(reaction_text)
                store_message(chat_id, "Heather", reaction_text)
                await asyncio.sleep(random.uniform(0.5, 1.2))

            # Send message parts with natural delays
            for i, part in enumerate(message_parts):
                typing_delay = calculate_typing_delay(part)

                # Show typing indicator
                try:
                    async with client.action(event.input_chat, 'typing'):
                        await asyncio.sleep(typing_delay)
                except Exception:
                    await asyncio.sleep(typing_delay)

                await event.respond(part)
                store_message(chat_id, "Heather", part)

                # Delay between split messages (simulate afterthought)
                if i < len(message_parts) - 1:
                    await asyncio.sleep(random.uniform(5.0, 12.0))

            # Send follow-up if applicable
            if send_followup:
                await asyncio.sleep(random.uniform(2.0, 4.0))
                followup = get_followup_message()
                try:
                    async with client.action(event.input_chat, 'typing'):
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                except Exception:
                    pass
                await event.respond(followup)
                store_message(chat_id, "Heather", followup)

        # Record response for duplicate detection (use full original response)
        record_response_sent(chat_id, response)
        main_logger.info(f"[{request_id}] Reply to {chat_id} ({response_time:.1f}s): {response[:100]}")

        # Increment turn counter for proactive photo tracking
        conversation_turn_count[chat_id] = conversation_turn_count.get(chat_id, 0) + 1

        # Track content promises — if the response teases sending media, mark for follow-through
        response_lower = response.lower()
        if any(trigger in response_lower for trigger in CONTENT_PROMISE_TRIGGERS):
            _content_promise_pending[chat_id] = time.time()
            main_logger.debug(f"[{request_id}] Content promise detected for {chat_id}")

        # --- POST-RESPONSE ADD-ONS ---
        # Only ONE add-on fires per turn to prevent message stacking.
        # Priority: tip hook > response-triggered photo > unsolicited NSFW > proactive photo > video tease
        # Also suppressed entirely during tip hook cooldown window (30 min after hook)
        tip_hook_age = time.time() - _tip_hook_sent_at.get(chat_id, 0)
        post_addon_sent = tip_hook_age < TIP_HOOK_COOLDOWN_WINDOW  # Skip all add-ons during cooldown

        # --- TIP HOOK (DIRECT SEND) — highest priority ---
        # Sends Emma photo + tip mention, bypasses LLM entirely
        if not post_addon_sent and not is_group_chat_event(event):
            tip_sent = await maybe_send_tip_hook(event, chat_id)
            if tip_sent:
                post_addon_sent = True
                main_logger.info(f"[{request_id}] Tip hook (direct) sent to {chat_id}")

        # --- PROACTIVE PHOTO LOGIC ---
        # Check if Heather's response mentions sending a photo AND we can deliver one
        if not post_addon_sent:
            has_photo_source = image_library or can_generate_photos()
            if has_photo_source and not is_group_chat_event(event) and can_send_photo_in_session(chat_id):
                send_photo = False
                photo_desc = None

                # Layer 1: AI response mentions sending a pic → follow through
                if response_wants_to_send_photo(response):
                    send_photo = True
                    photo_desc = extract_photo_context_from_response(response)
                    main_logger.info(f"[{request_id}] Response-triggered photo for {chat_id}: {photo_desc[:40]}")

                # Layer 2: Random proactive selfie after enough flirty exchanges (skip COLD)
                elif get_warmth_tier(chat_id) != "COLD" and should_send_proactive_photo(chat_id):
                    send_photo = True
                    photo_desc = random.choice(PROACTIVE_SELFIE_DESCRIPTIONS)
                    main_logger.info(f"[{request_id}] Proactive selfie for {chat_id}: {photo_desc[:40]}")

                if send_photo and photo_desc:
                    post_addon_sent = True
                    # Try library first for proactive sends (instant, no GPU cost)
                    if image_library:
                        proactive_cat = "sfw_casual" if not _is_sexual_conversation(chat_id) else "nsfw_topless"
                        proactive_cat = gate_image_category(chat_id, proactive_cat)
                        lib_sent = await send_library_image(event, chat_id, proactive_cat)
                        if lib_sent:
                            last_photo_request[chat_id] = time.time()
                            main_logger.info(f"[{request_id}] Proactive library photo sent to {chat_id}")
                            send_photo = False  # skip ComfyUI

                    if send_photo:
                        record_photo_sent(chat_id)
                        last_photo_request[chat_id] = time.time()
                        try:
                            # Natural delay before "taking" the selfie
                            await asyncio.sleep(random.uniform(3.0, 6.0))
                            # Generate image (show typing while generating)
                            try:
                                async with client.action(event.input_chat, 'photo'):
                                    loop = asyncio.get_running_loop()
                                    image_data = await loop.run_in_executor(
                                        None, lambda: generate_heather_image(photo_desc)
                                    )
                            except Exception:
                                loop = asyncio.get_running_loop()
                                image_data = await loop.run_in_executor(
                                    None, lambda: generate_heather_image(photo_desc)
                                )
                            if image_data:
                                caption = random.choice(PROACTIVE_SELFIE_CAPTIONS)
                                image_file = io.BytesIO(image_data)
                                image_file.name = "heather_selfie.png"
                                await client.send_file(
                                    chat_id, image_file,
                                    caption=caption, force_document=False
                                )
                                store_message(chat_id, "Heather", f"[Sent selfie: {photo_desc[:50]}] {caption}")
                                main_logger.info(f"[{request_id}] Sent proactive photo to {chat_id}")
                        except Exception as e:
                            main_logger.warning(f"[{request_id}] Proactive photo failed for {chat_id}: {e}")

        # --- UNSOLICITED NSFW PHOTO LOGIC ---
        # During sexual conversations, occasionally send a topless/nude unprompted (WARM only)
        if not post_addon_sent and not is_group_chat_event(event) and get_warmth_tier(chat_id) == "WARM" and should_send_unsolicited_nsfw(chat_id):
            await asyncio.sleep(random.uniform(3.0, 8.0))
            sent = await send_unsolicited_nsfw(event, chat_id)
            if sent:
                post_addon_sent = True
                main_logger.info(f"[{request_id}] Unsolicited NSFW photo sent to {chat_id}")

        # --- VIDEO TEASE LOGIC ---
        # During sexual conversations, occasionally mention having videos to prompt requests (skip COLD)
        if not post_addon_sent and not is_group_chat_event(event) and get_warmth_tier(chat_id) != "COLD" and should_tease_video(chat_id):
            tease = random.choice(VIDEO_TEASE_MESSAGES)
            await asyncio.sleep(random.uniform(4.0, 10.0))
            await event.respond(tease)
            store_message(chat_id, "Heather", tease)
            last_video_tease[chat_id] = time.time()
            _video_offer_pending[chat_id] = time.time()
            main_logger.info(f"[{request_id}] Video offer sent to {chat_id}: {tease[:50]}")

    except Exception as e:
        log_error('BOT', f"Send error: {e}", {'chat_id': chat_id})
    finally:
        reply_in_progress.discard(chat_id)

# ============================================================================
# MONITORING WEB INTERFACE
# ============================================================================

if MONITORING_ENABLED:
    monitor_app = Flask(__name__)

    MONITOR_AUTH_TOKEN = os.getenv("MONITOR_AUTH_TOKEN", os.getenv("HEATHER_DASHBOARD_KEY", ""))

    @monitor_app.before_request
    def check_dashboard_auth():
        if flask_request.path == '/health':
            return None  # /health stays public for monitoring scripts
        if not MONITOR_AUTH_TOKEN:
            return None  # No token configured = open access
        token = flask_request.args.get('token') or flask_request.headers.get('X-Auth-Token', '')
        if token != MONITOR_AUTH_TOKEN:
            return "Unauthorized", 401

    @monitor_app.route('/')
    def monitor_home():
        text_ok, text_status = check_text_ai_status()
        ollama_ok, ollama_status = check_ollama_status()
        comfyui_ok, comfyui_status = check_comfyui_status()
        tts_ok, tts_status = check_tts_status()
        
        chat_list = []
        for cid, msgs in recent_messages.items():
            display_name = get_user_display_name(cid)
            chat_list.append((cid, display_name, list(msgs)[-10:]))
        
        return render_template_string('''
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
        <h1>🚗 Heather Userbot Monitor <span class="userbot-badge">TELETHON</span></h1>
        
        <div class="stats">
            <h2>📊 Statistics</h2>
            <p>Uptime: {{ uptime }}</p>
            <p>Messages: {{ stats.messages_processed }}</p>
            <p>Images Processed: {{ stats.images_processed }}</p>
            <p>Images Generated: {{ stats.images_generated }}</p>
            <p>Voice Messages: {{ stats.voice_messages }}</p>
            <p>Active Chats: {{ active_chats }}</p>
        </div>
        
        <div class="stats">
            <h2>🔧 Services</h2>
            <p>Text AI: <span class="{{ 'status-ok' if text_ok else 'status-error' }}">{{ text_status }}</span>
                {% if text_ai_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ text_ai_circuit }}]</span>{% endif %}</p>
            <p>Ollama: <span class="{{ 'status-ok' if ollama_ok else 'status-error' }}">{{ ollama_status }}</span>
                {% if ollama_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ ollama_circuit }}]</span>{% endif %}</p>
            <p>ComfyUI: <span class="{{ 'status-ok' if comfyui_ok else 'status-error' }}">{{ comfyui_status }}</span></p>
            <p>TTS: <span class="{{ 'status-ok' if tts_ok else 'status-error' }}">{{ tts_status }}</span>
                {% if tts_circuit %}<span style="color: #ff9800; font-size: 0.9em;"> [{{ tts_circuit }}]</span>{% endif %}</p>
        </div>
        
        <div class="stats">
            <h2>🚩 CSAM Flags</h2>
            <p>Pending: {{ pending_flags }} | Total: {{ total_flags }}
            {% if pending_flags > 0 %}<span style="color: #f44336; font-weight: bold;"> ⚠️ REVIEW NEEDED</span>{% endif %}
            </p>
            <p><a href="/flags" style="color: #4ecdc4;">View Flag Dashboard →</a></p>
        </div>

        <div class="stats">
            <h2>☕ Tips</h2>
            <p>Total Stars: {{ total_stars }} (~${{ "%.2f"|format(total_stars * 0.013) }}) | Tippers: {{ num_tippers }} | Bot Started: {{ started_users }}</p>
            <p><a href="/tips" style="color: #4ecdc4;">View Tips Dashboard →</a></p>
        </div>

        <h2>💬 Recent Conversations</h2>
        {% for chat_id, display_name, messages in chat_list %}
        <div class="chat">
            <h3>
                {{ display_name }} ({{ chat_id }})
                {% if chat_id in manual_mode_chats %}
                <span style="color: #f44336;">[MANUAL]</span>
                {% endif %}
            </h3>
            {% for msg in messages %}
            <p class="{{ msg.sender.lower().replace(' ', '').replace('🎤', '') }}">
                <strong>{{ msg.timestamp }} [{{ msg.sender }}]:</strong> {{ msg.content }}
            </p>
            {% endfor %}
        </div>
        {% endfor %}
    </div>
</body>
</html>
        ''',
        stats=stats,
        uptime=get_uptime(),
        active_chats=len(recent_messages),
        chat_list=chat_list,
        manual_mode_chats=manual_mode_chats,
        text_ok=text_ok, text_status=text_status,
        ollama_ok=ollama_ok, ollama_status=ollama_status,
        comfyui_ok=comfyui_ok, comfyui_status=comfyui_status,
        tts_ok=tts_ok, tts_status=tts_status,
        text_ai_circuit=text_ai_health.get_status() if text_ai_health.circuit_open else None,
        ollama_circuit=ollama_health.get_status() if ollama_health.circuit_open else None,
        tts_circuit=tts_health.get_status() if tts_health.circuit_open else None,
        pending_flags=sum(1 for f in csam_flags if f.get('status') == 'pending'),
        total_flags=len(csam_flags),
        total_stars=sum(t.get('total_stars', 0) for t in tipper_status.values()),
        num_tippers=len(tipper_status),
        started_users=len(payment_bot_started_users),
        )
    
    @monitor_app.route('/health')
    def health_check():
        text_ok, _ = check_text_ai_status()
        ollama_ok, _ = check_ollama_status()
        comfyui_ok, _ = check_comfyui_status()
        tts_ok, _ = check_tts_status()
        return jsonify({
            'status': 'healthy' if text_ok else 'degraded',
            'uptime': get_uptime(),
        })

    @monitor_app.route('/flags')
    def monitor_flags():
        pending = [f for f in csam_flags if f.get('status') == 'pending']
        resolved = [f for f in csam_flags if f.get('status') != 'pending']
        return render_template_string('''
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
        ''', pending=pending, resolved=resolved)

    @monitor_app.route('/tips')
    def monitor_tips():
        # Fetch Star balance from Bot API
        star_balance = None
        star_transactions = []
        if PAYMENT_BOT_TOKEN:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{PAYMENT_BOT_TOKEN}/getStarTransactions",
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
                'name': ts.get('name') or get_user_display_name(cid),
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

        return render_template_string('''
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
        ''',
        tippers=tippers,
        tier_labels=tier_labels,
        total_stars=total_stars_all,
        total_tips=total_tips_all,
        started_users=len(payment_bot_started_users),
        star_transactions=star_transactions,
        payment_bot_token=bool(PAYMENT_BOT_TOKEN),
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

def run_monitoring():
    if MONITORING_ENABLED:
        main_logger.info(f"Starting monitoring on port {MONITORING_PORT}")
        monitor_app.run(host='127.0.0.1', port=MONITORING_PORT, debug=False, use_reloader=False)

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

# Reconnection settings
MAX_RECONNECT_ATTEMPTS = 50
INITIAL_RECONNECT_DELAY = 5  # seconds
MAX_RECONNECT_DELAY = 300  # 5 minutes max

async def main():
    """Main async entry point with auto-reconnection"""
    main_logger.info("=" * 60)
    main_logger.info("Starting Heather Userbot v3.6 - TELETHON Edition")
    main_logger.info(f"Session: {SESSION_NAME}")
    main_logger.info(f"Personality: {personality.name}")
    main_logger.info("=" * 60)

    # Verify services with enhanced startup checks
    main_logger.info("Checking services...")
    service_status = await verify_services_at_startup()

    main_logger.info(f"Model mode: {'12B SMALL' if SMALL_MODEL_MODE else '70B FULL'}")

    # Initialize NSFW image classifier (fast ViT — replaces slow Ollama LLaVA)
    init_nsfw_classifier()

    # Load pre-generated image library
    load_image_library()

    # Load story bank (pre-written explicit Uber stories)
    load_story_bank()

    # Load AI disclosure persistence
    global ai_disclosure_shown
    ai_disclosure_shown = load_ai_disclosure_shown()
    main_logger.info(f"Loaded AI disclosure set: {len(ai_disclosure_shown)} users already disclosed")

    # Load tip history
    raw_tips = load_tip_history()
    started = raw_tips.pop('_started_users', [])
    payment_bot_started_users.update(int(uid) for uid in started)
    for k, v in raw_tips.items():
        tipper_status[int(k)] = v
    main_logger.info(f"Loaded tip history: {len(tipper_status)} tippers, {len(payment_bot_started_users)} started payment bot")

    main_logger.info("=" * 60)

    # Start monitoring in background thread
    if MONITORING_ENABLED:
        monitor_thread = threading.Thread(target=run_monitoring, daemon=True)
        monitor_thread.start()
        main_logger.info(f"Monitoring: http://localhost:{MONITORING_PORT}")

    # Start periodic cleanup of inactive users with exception handling
    async def _periodic_cleanup():
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                cleanup_inactive_users()
                save_shutdown_timestamp()  # Heartbeat — crash recovery has at most 1hr-stale timestamp
            except asyncio.CancelledError:
                main_logger.info("Cleanup task cancelled")
                break
            except Exception as e:
                main_logger.error(f"Error in cleanup task: {e}")
                # Continue running despite errors
                await asyncio.sleep(60)  # Wait a bit before retrying

    cleanup_task = asyncio.get_running_loop().create_task(_periodic_cleanup())

    # Start conversation check-in task
    async def _conversation_checkin():
        """Periodically check for stale conversations and send a natural follow-up."""
        await asyncio.sleep(60)  # Wait 1 min after startup before first check
        while True:
            try:
                await asyncio.sleep(CHECKIN_INTERVAL)
                now = time.time()

                # Auto-release stale takeovers (30min idle)
                for cid in list(manual_mode_chats):
                    last_activity = max(
                        _takeover_timestamps.get(cid, 0),
                        _takeover_last_admin_msg.get(cid, 0)
                    )
                    if last_activity > 0 and now - last_activity > 1800:  # 30 min
                        manual_mode_chats.discard(cid)
                        _takeover_timestamps.pop(cid, None)
                        _takeover_last_admin_msg.pop(cid, None)
                        if cid in conversation_activity:
                            conversation_activity[cid]['checked_in'] = False
                        main_logger.info(f"[TAKEOVER] Auto-released {cid} after 30min idle")
                        try:
                            me = await client.get_me()
                            await client.send_message(me.id, f"⏰ Auto-released takeover for {cid} (30min idle). Bot resumed.")
                        except Exception:
                            pass

                for chat_id, activity in list(conversation_activity.items()):
                    # Skip if already checked in on this silence
                    if activity.get('checked_in', False):
                        continue

                    # Skip if not enough conversation history
                    turns = conversation_turn_count.get(chat_id, 0)
                    if turns < CHECKIN_ONLY_AFTER_TURNS:
                        continue

                    # Skip group chats, blocked users, manual mode
                    if chat_id in blocked_users or chat_id in manual_mode_chats:
                        continue

                    # Skip COLD users — don't check in on people who don't reciprocate
                    if get_warmth_tier(chat_id) == "COLD":
                        continue

                    last_heather = activity.get('last_heather', 0)
                    last_user = activity.get('last_user', 0)

                    # Only check in when Heather spoke last (user went quiet)
                    if last_heather <= 0 or last_heather < last_user:
                        continue

                    # Suppress check-ins during tip hook cooldown — don't look pushy after asking for tips
                    tip_hook_age = now - _tip_hook_sent_at.get(chat_id, 0)
                    if tip_hook_age < TIP_HOOK_COOLDOWN_WINDOW:
                        continue

                    # Check all per-user limits (daily cap, unreturned cap, quiet hours)
                    if not can_send_checkin(chat_id):
                        continue

                    # How long since Heather last spoke
                    silence = now - last_heather
                    # Pick a random threshold between min and max
                    threshold = random.uniform(CHECKIN_DELAY_MIN, CHECKIN_DELAY_MAX)

                    if silence >= threshold:
                        try:
                            checkin_msg = get_checkin_message(chat_id)
                            # Show typing briefly before check-in
                            try:
                                entity = await client.get_entity(chat_id)
                                async with client.action(entity, 'typing'):
                                    await asyncio.sleep(random.uniform(1.5, 3.5))
                            except Exception:
                                await asyncio.sleep(random.uniform(1.5, 3.5))

                            await client.send_message(chat_id, checkin_msg)
                            store_message(chat_id, "Heather", checkin_msg)
                            conversation_activity[chat_id]['checked_in'] = True
                            # Update tracker
                            tracker = _get_checkin_tracker(chat_id)
                            tracker['today_count'] += 1
                            tracker['unreturned'] += 1
                            main_logger.info(f"[CHECKIN] Sent to {chat_id} (day={tracker['today_count']}/{CHECKIN_MAX_PER_DAY}, unreturned={tracker['unreturned']}): {checkin_msg}")
                        except Exception as e:
                            main_logger.warning(f"[CHECKIN] Failed for {chat_id}: {e}")
                            conversation_activity[chat_id]['checked_in'] = True  # Don't retry

            except asyncio.CancelledError:
                main_logger.info("Check-in task cancelled")
                break
            except Exception as e:
                main_logger.error(f"Error in check-in task: {e}")
                await asyncio.sleep(60)

    checkin_task = asyncio.get_running_loop().create_task(_conversation_checkin())
    main_logger.info("Conversation check-in system started")

    # Start payment polling if bot token is configured
    if PAYMENT_BOT_TOKEN:
        payment_task = asyncio.get_running_loop().create_task(handle_payment_updates())
        main_logger.info("Payment polling started (PAYMENT_BOT_TOKEN configured)")
    else:
        main_logger.info("Payment polling skipped (no PAYMENT_BOT_TOKEN)")

    # ====================================================================
    # LONG-TERM RE-ENGAGEMENT SYSTEM
    # Scans Telegram dialogs for dormant conversations and sends
    # personalized LLM-generated check-in messages.
    # ====================================================================

    async def _find_reengagement_candidates(history: dict) -> list:
        """Scan Telegram dialogs for dormant conversations worth re-engaging."""
        candidates = []
        now = datetime.now()
        me = await client.get_me()
        my_id = me.id

        try:
            async for dialog in client.iter_dialogs():
                try:
                    # Only private chats (not groups/channels)
                    if not dialog.is_user:
                        continue

                    entity = dialog.entity
                    # Skip bots and self
                    if getattr(entity, 'bot', False) or entity.id == my_id:
                        continue

                    # Skip blocked users
                    if entity.id in blocked_users:
                        continue

                    # Check last message date
                    if not dialog.message or not dialog.message.date:
                        continue

                    last_msg_date = dialog.message.date.replace(tzinfo=None)
                    idle_delta = now - last_msg_date
                    idle_days = idle_delta.total_seconds() / 86400

                    # Must be within the idle window (2-14 days)
                    if idle_days < REENGAGEMENT_MIN_IDLE_DAYS or idle_days > REENGAGEMENT_MAX_IDLE_DAYS:
                        continue

                    # Last message must be FROM the user (not from us)
                    if dialog.message.out:
                        continue

                    chat_id_str = str(entity.id)

                    # Check re-engagement history
                    if chat_id_str in history:
                        h = history[chat_id_str]
                        # Skip dead (deleted/deactivated) users
                        if h.get('dead', False):
                            continue
                        # Don't ping if they never responded to last ping
                        if h.get('ping_count', 0) > 0 and not h.get('last_ping_responded', True):
                            continue
                        # Don't ping within cooldown period
                        last_ping = h.get('last_ping_at', '')
                        if last_ping:
                            try:
                                last_ping_dt = datetime.fromisoformat(last_ping)
                                days_since_ping = (now - last_ping_dt).total_seconds() / 86400
                                if days_since_ping < REENGAGEMENT_COOLDOWN_DAYS:
                                    continue
                            except (ValueError, TypeError):
                                pass

                    # Fetch recent messages to check conversation depth
                    messages = await client.get_messages(entity.id, limit=20)
                    if len(messages) < REENGAGEMENT_MIN_MESSAGES:
                        continue

                    # Build recent message context (last 10 with text)
                    recent_msgs = []
                    for msg in reversed(messages[:10]):
                        if msg.text:
                            sender = "Heather" if msg.out else (entity.first_name or "User")
                            recent_msgs.append({'sender': sender, 'text': msg.text[:200]})

                    if len(recent_msgs) < 3:
                        continue

                    # Build candidate
                    display_name = entity.first_name or entity.username or str(entity.id)
                    username = entity.username or ""

                    candidates.append({
                        'chat_id': entity.id,
                        'username': username,
                        'display_name': display_name,
                        'last_message_date': last_msg_date,
                        'idle_days': idle_days,
                        'recent_messages': recent_msgs,
                        'message_count': len(messages),
                    })

                except Exception as e:
                    main_logger.debug(f"[REENGAGEMENT] Error checking dialog: {e}")
                    continue

        except Exception as e:
            main_logger.error(f"[REENGAGEMENT] Error iterating dialogs: {e}")

        # Score candidates: prefer more recent idle + higher message count
        for c in candidates:
            # Lower idle = higher score (fresher), more messages = higher score
            recency_score = 1.0 / (c['idle_days'] + 0.1)
            volume_score = min(c['message_count'] / 20.0, 1.0)
            c['score'] = recency_score * 0.6 + volume_score * 0.4

        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates[:REENGAGEMENT_MAX_PER_DAY]

    def _generate_reengagement_message(candidate: dict) -> Optional[str]:
        """Generate a personalized re-engagement message via LLM."""
        # Format recent messages for context
        convo_lines = []
        for msg in candidate['recent_messages']:
            convo_lines.append(f"{msg['sender']}: {msg['text']}")
        convo_text = "\n".join(convo_lines)

        idle_days = int(candidate['idle_days'])
        display_name = candidate['display_name']

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    f"You are Heather Dvorak, a 48-year-old mom from Kirkland WA. You're texting someone you "
                    f"chatted with {idle_days} days ago. Their name is {display_name}.\n\n"
                    f"Here's the tail end of your last conversation:\n{convo_text}\n\n"
                    f"Write a SHORT casual text (1-2 sentences max) checking in on them. Reference "
                    f"something specific from the conversation. Sound natural, like you just thought "
                    f"of them. Use lowercase, abbreviations, maybe one emoji.\n\n"
                    f"Examples of good re-engagement texts:\n"
                    f"- \"hey did you ever figure out that job situation? been thinking about it lol\"\n"
                    f"- \"was just looking at our old chat and it made me smile 😊 how've you been\"\n"
                    f"- \"random but something reminded me of you haha, how's it going\"\n\n"
                    f"Do NOT:\n"
                    f"- Say \"hey stranger\" or \"long time no talk\" (too generic)\n"
                    f"- Ask multiple questions\n"
                    f"- Be overly enthusiastic or use lots of emojis\n"
                    f"- Mention that you're \"checking in\" explicitly\n"
                    f"- Use asterisks for actions like *waves*\n"
                    f"- Write more than 2 sentences\n\n"
                    f"Respond with ONLY the message text. No quotes, no thinking, no explanation.\n"
                    f"Do NOT use <think> tags. Just output the message directly."
                )
            },
            {
                "role": "user",
                "content": f"Write a casual re-engagement text to {display_name}."
            }
        ]

        timeouts = [25, 35, 45]  # Escalating timeouts — must total < 120s asyncio cap
        for attempt, timeout in enumerate(timeouts):
            try:
                response = requests.post(
                    TEXT_AI_ENDPOINT,
                    json={
                        "model": "local-model",
                        "messages": prompt_messages,
                        "temperature": 0.85,
                        "max_tokens": 512,
                        "stream": False,
                        "top_p": 0.9,
                        "frequency_penalty": 0.3,
                        "presence_penalty": 0.3
                    },
                    timeout=timeout
                )
                if response.status_code == 200:
                    message_data = response.json()['choices'][0]['message']
                    msg = message_data.get('content', '').strip()
                    # Strip thinking tags and quotes
                    msg = re.sub(r'<think>.*?</think>', '', msg, flags=re.DOTALL | re.IGNORECASE).strip()
                    msg = re.sub(r'<think>.*', '', msg, flags=re.DOTALL | re.IGNORECASE).strip()
                    if msg:
                        msg = strip_quote_wrapping(msg)
                    # Sanity check: not too long, not empty
                    if 5 < len(msg) < 500:
                        return msg
                    main_logger.warning(f"[REENGAGEMENT] LLM message bad length ({len(msg)}): {msg[:100]}")
                    # Retry on empty — LLM might have spent all tokens on reasoning
                    if attempt < len(timeouts) - 1:
                        continue
                else:
                    main_logger.warning(f"[REENGAGEMENT] LLM returned {response.status_code}")
            except requests.exceptions.ReadTimeout:
                main_logger.warning(f"[REENGAGEMENT] LLM timeout ({timeout}s) for {display_name}, attempt {attempt + 1}/{len(timeouts)}")
                if attempt < len(timeouts) - 1:
                    time.sleep(2)  # Brief pause before retry
                    continue
            except Exception as e:
                main_logger.error(f"[REENGAGEMENT] LLM call failed: {e}")
                break  # Non-timeout errors don't retry

        # All retries exhausted — use a generic fallback
        main_logger.warning(f"[REENGAGEMENT] All retries failed for {display_name}, using fallback")
        fallbacks = [
            f"hey {display_name.split()[0].lower()} it's been a while! what's new? 😊",
            f"hey you! been a minute lol, come chat with me 😘",
            f"hey stranger 😏 I'm still here if you wanna talk",
            f"hi! miss our chats, come say hi when you get a chance 💕",
        ]
        return random.choice(fallbacks)

    async def _reengagement_scanner():
        """Scan for dormant conversations and send re-engagement messages."""
        await asyncio.sleep(300)  # Wait 5 min after startup
        main_logger.info("[REENGAGEMENT] Scanner started (first scan in 5 min)")
        while True:
            try:
                now = datetime.now()

                # Only run during reasonable hours
                if not (REENGAGEMENT_HOUR_START <= now.hour < REENGAGEMENT_HOUR_END):
                    main_logger.debug(f"[REENGAGEMENT] Outside hours ({now.hour}h), sleeping 30 min")
                    await asyncio.sleep(1800)
                    continue

                history = load_reengagement_history()

                # Count how many we've already sent today
                today_str = now.strftime('%Y-%m-%d')
                sent_today = sum(
                    1 for h in history.values()
                    if h.get('last_ping_at', '')[:10] == today_str
                )

                if sent_today >= REENGAGEMENT_MAX_PER_DAY:
                    main_logger.debug(f"[REENGAGEMENT] Already sent {sent_today} today, waiting for next cycle")
                    await asyncio.sleep(REENGAGEMENT_SCAN_INTERVAL)
                    continue

                candidates = await _find_reengagement_candidates(history)
                remaining = REENGAGEMENT_MAX_PER_DAY - sent_today

                if not candidates:
                    main_logger.debug("[REENGAGEMENT] No candidates found this cycle")
                    await asyncio.sleep(REENGAGEMENT_SCAN_INTERVAL)
                    continue

                main_logger.info(f"[REENGAGEMENT] Found {len(candidates)} candidates, will send up to {remaining}")

                for candidate in candidates[:remaining]:
                    # Generate message via LLM (in executor to not block)
                    # Wrap with asyncio timeout so a stuck LLM doesn't block the scanner
                    try:
                        loop = asyncio.get_running_loop()
                        message = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, _generate_reengagement_message, candidate
                            ),
                            timeout=120  # Hard cap: 2 min per message generation
                        )
                    except asyncio.TimeoutError:
                        main_logger.warning(f"[REENGAGEMENT] LLM generation timed out (120s) for {candidate['display_name']}")
                        message = None

                    if not message:
                        main_logger.warning(f"[REENGAGEMENT] Failed to generate message for {candidate['display_name']}")
                        continue

                    try:
                        # Show typing briefly before sending
                        try:
                            entity = await client.get_entity(candidate['chat_id'])
                            async with client.action(entity, 'typing'):
                                await asyncio.sleep(random.uniform(2.0, 5.0))
                        except Exception:
                            await asyncio.sleep(random.uniform(2.0, 5.0))

                        await client.send_message(candidate['chat_id'], message)

                        # Update history
                        chat_id_str = str(candidate['chat_id'])
                        prev = history.get(chat_id_str, {})
                        history[chat_id_str] = {
                            'username': candidate['username'],
                            'display_name': candidate['display_name'],
                            'last_ping_at': now.isoformat(),
                            'ping_count': prev.get('ping_count', 0) + 1,
                            'last_ping_responded': False,
                        }
                        save_reengagement_history(history)

                        main_logger.info(
                            f"[REENGAGEMENT] Sent to {candidate['display_name']} "
                            f"({candidate['chat_id']}) after {candidate['idle_days']:.1f} days idle: "
                            f"{message[:80]}"
                        )

                        # Human-like delay between sends (1-3 min)
                        await asyncio.sleep(random.randint(60, 180))

                    except Exception as e:
                        main_logger.error(f"[REENGAGEMENT] Failed to send to {candidate['chat_id']}: {e}")
                        # Mark deleted/deactivated users so we skip them in future scans
                        err_str = str(e).lower()
                        if 'deleted' in err_str or 'deactivated' in err_str or 'peer_id_invalid' in err_str:
                            chat_id_str = str(candidate['chat_id'])
                            history[chat_id_str] = history.get(chat_id_str, {})
                            history[chat_id_str]['dead'] = True
                            save_reengagement_history(history)
                            main_logger.info(f"[REENGAGEMENT] Marked {candidate['chat_id']} as dead (deleted/deactivated)")

                await asyncio.sleep(REENGAGEMENT_SCAN_INTERVAL)

            except asyncio.CancelledError:
                main_logger.info("[REENGAGEMENT] Scanner cancelled")
                break
            except Exception as e:
                main_logger.error(f"[REENGAGEMENT] Scanner error: {e}")
                await asyncio.sleep(300)

    if REENGAGEMENT_AUTO_ENABLED:
        reengagement_task = asyncio.get_running_loop().create_task(_reengagement_scanner())
        main_logger.info("[REENGAGEMENT] Long-term re-engagement system started (auto-scan ON)")
    else:
        main_logger.info("[REENGAGEMENT] Auto-scan disabled. Use /admin_reengage_scan to test manually.")

    # ====================================================================
    # STARTUP CATCH-UP SYSTEM
    # Scans for unread messages that arrived during downtime and replies
    # through the normal AI pipeline. Runs once on startup as async task.
    # ====================================================================

    async def _startup_catchup():
        """Scan for unread private messages that arrived during downtime and reply."""
        if not CATCHUP_ENABLED:
            main_logger.info("[CATCHUP] Disabled via CATCHUP_ENABLED=False")
            return

        await asyncio.sleep(5)  # Let Telegram connection stabilize

        shutdown_ts = load_shutdown_timestamp()
        now = time.time()

        if shutdown_ts is None:
            main_logger.info("[CATCHUP] No shutdown timestamp found (first run?), saving current and skipping")
            save_shutdown_timestamp()
            return

        downtime_seconds = now - shutdown_ts
        downtime_str = f"{downtime_seconds / 3600:.1f}h" if downtime_seconds > 3600 else f"{downtime_seconds / 60:.0f}m"

        if downtime_seconds < CATCHUP_MIN_DOWNTIME_SECONDS:
            main_logger.info(f"[CATCHUP] Downtime {downtime_str} < {CATCHUP_MIN_DOWNTIME_SECONDS}s minimum, skipping")
            save_shutdown_timestamp()
            return

        main_logger.info(f"[CATCHUP] Detected downtime of {downtime_str} (since {datetime.fromtimestamp(shutdown_ts).strftime('%H:%M:%S')})")

        max_age_ts = now - (CATCHUP_MAX_AGE_HOURS * 3600)
        me = await client.get_me()
        my_id = me.id
        candidates = []

        try:
            async for dialog in client.iter_dialogs():
                try:
                    # Only private chats with unread messages
                    if not dialog.is_user or dialog.unread_count == 0:
                        continue

                    entity = dialog.entity
                    chat_id = entity.id

                    # Skip bots, self, blocked, admin, ignored
                    if getattr(entity, 'bot', False) or chat_id == my_id:
                        continue
                    if chat_id in blocked_users or chat_id in IGNORED_CHATS:
                        continue
                    if is_admin(chat_id):
                        continue

                    # Must have a recent text message
                    if not dialog.message or not dialog.message.text:
                        continue

                    msg_date = dialog.message.date
                    if msg_date is None:
                        continue
                    msg_ts = msg_date.timestamp()

                    # Skip messages older than max age or before shutdown
                    if msg_ts < max_age_ts or msg_ts < shutdown_ts:
                        continue

                    # Must be from the user (not our outgoing message)
                    if dialog.message.out:
                        continue

                    display_name = getattr(entity, 'first_name', None) or str(chat_id)
                    candidates.append({
                        'chat_id': chat_id,
                        'entity': entity,
                        'message': dialog.message,
                        'msg_ts': msg_ts,
                        'display_name': display_name,
                        'unread_count': dialog.unread_count,
                    })
                except Exception as e:
                    main_logger.debug(f"[CATCHUP] Error processing dialog: {e}")
                    continue
        except Exception as e:
            main_logger.error(f"[CATCHUP] Failed to iterate dialogs: {e}")
            save_shutdown_timestamp()
            return

        if not candidates:
            main_logger.info("[CATCHUP] No unread messages found from downtime period")
            save_shutdown_timestamp()
            return

        # Sort by most recent first, cap at max replies
        candidates.sort(key=lambda c: c['msg_ts'], reverse=True)
        candidates = candidates[:CATCHUP_MAX_REPLIES]

        main_logger.info(f"[CATCHUP] Found {len(candidates)} candidate(s) to reply to")
        replied_count = 0
        failed_count = 0

        for candidate in candidates:
            chat_id = candidate['chat_id']
            display_name = candidate['display_name']
            latest_msg = candidate['message']

            # Skip if live handler already processing this user
            if chat_id in reply_in_progress:
                main_logger.debug(f"[CATCHUP] Skipping {display_name} ({chat_id}) — reply already in progress")
                continue

            reply_in_progress.add(chat_id)
            try:
                # Seed conversation context from Telegram history (post-restart, deque is empty)
                if chat_id not in conversations or len(conversations[chat_id]) == 0:
                    conversations[chat_id] = deque()
                    try:
                        history_msgs = await client.get_messages(chat_id, limit=10)
                        for msg in reversed(history_msgs):
                            if msg.text:
                                role = "assistant" if msg.out else "user"
                                conversations[chat_id].append({"role": role, "content": msg.text})
                        # Trim to match normal context window
                        while len(conversations[chat_id]) > MAX_CONVERSATION_LENGTH:
                            conversations[chat_id].popleft()
                        main_logger.debug(f"[CATCHUP] Seeded {len(conversations[chat_id])} context messages for {chat_id}")
                    except Exception as e:
                        main_logger.warning(f"[CATCHUP] Failed to seed context for {chat_id}: {e}")

                user_message = latest_msg.text

                # Check if the missed message is a video/image request — handle directly
                if is_video_request(user_message):
                    sent = await send_video_to_chat(chat_id, client)
                    if sent:
                        main_logger.info(f"[CATCHUP] Sent video to {display_name} ({chat_id}) (video request)")
                        replied_count += 1
                        continue
                if is_image_request(user_message) and image_library:
                    category = gate_image_category(chat_id, get_image_category(user_message))
                    event_proxy = type('obj', (object,), {'chat_id': chat_id, 'respond': lambda self, msg, **kw: client.send_message(chat_id, msg, **kw)})()
                    sent = await send_library_image(event_proxy, chat_id, category)
                    if sent:
                        main_logger.info(f"[CATCHUP] Sent library image to {display_name} ({chat_id}) (image request)")
                        replied_count += 1
                        continue

                # Generate AI response through normal pipeline
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda uid=chat_id, msg=user_message: get_text_ai_response(uid, msg)
                )
                response = validate_and_fix_response(response, get_user_mode(chat_id))
                if not response or not response.strip():
                    response = get_fallback_response(chat_id)

                # Send with typing indicator
                try:
                    async with client.action(chat_id, 'typing'):
                        typing_delay = calculate_typing_delay(response)
                        await asyncio.sleep(min(typing_delay, 3.0))  # Cap typing delay for catch-up
                except Exception:
                    pass

                await client.send_message(chat_id, response)
                store_message(chat_id, "Heather", response)
                replied_count += 1
                main_logger.info(f"[CATCHUP] Replied to {display_name} ({chat_id}): {response[:80]}")

                # Delay between replies to avoid Telegram flood
                if candidate != candidates[-1]:
                    delay = random.uniform(CATCHUP_DELAY_MIN, CATCHUP_DELAY_MAX)
                    await asyncio.sleep(delay)

            except Exception as e:
                failed_count += 1
                main_logger.error(f"[CATCHUP] Failed to reply to {display_name} ({chat_id}): {e}")
            finally:
                reply_in_progress.discard(chat_id)

        # Summary
        summary = f"[CATCHUP] Done — replied to {replied_count} user(s)"
        if failed_count:
            summary += f", {failed_count} failed"
        main_logger.info(summary)

        # Notify admin
        if replied_count > 0 and ADMIN_USER_ID > 0:
            try:
                admin_msg = f"[CATCHUP] Replied to {replied_count} user(s) after {downtime_str} downtime"
                if failed_count:
                    admin_msg += f" ({failed_count} failed)"
                await client.send_message(ADMIN_USER_ID, admin_msg)
            except Exception as e:
                main_logger.debug(f"[CATCHUP] Failed to notify admin: {e}")

        # Update timestamp for next run
        save_shutdown_timestamp()

    # Auto-reconnection loop
    reconnect_delay = INITIAL_RECONNECT_DELAY

    while True:
        try:
            # Connect and run
            if not client.is_connected():
                main_logger.info("Connecting to Telegram...")
                await client.start()
                connection_state['connected'] = True
                connection_state['reconnect_attempts'] = 0
                reconnect_delay = INITIAL_RECONNECT_DELAY  # Reset delay on success

            me = await client.get_me()
            main_logger.info(f"Logged in as: {me.first_name} (@{me.username})")

            # Set Telegram bio to AI disclosure
            try:
                from telethon.tl.functions.account import UpdateProfileRequest
                bio_text = "Heather — AI companion (creator-built)"
                await client(UpdateProfileRequest(about=bio_text))
                main_logger.info(f"Updated Telegram bio: {bio_text}")
            except Exception as e:
                main_logger.warning(f"Could not update Telegram bio: {e}")

            main_logger.info("Userbot is running! Press Ctrl+C to stop.")

            # Pre-cache videos to Telegram for instant sends
            asyncio.create_task(precache_videos())
            asyncio.create_task(_startup_catchup())

            # Run until disconnected
            await client.run_until_disconnected()

            # If we get here, we disconnected
            connection_state['connected'] = False
            connection_state['last_disconnect'] = time.time()
            connection_state['disconnect_count'] += 1
            main_logger.warning(f"Disconnected from Telegram (disconnect #{connection_state['disconnect_count']})")

        except asyncio.CancelledError:
            main_logger.info("Main loop cancelled, shutting down...")
            break

        except ConnectionError as e:
            connection_state['connected'] = False
            connection_state['last_disconnect'] = time.time()
            main_logger.error(f"Connection error: {e}")

        except OperationalError as e:
            if "database is locked" in str(e):
                main_logger.warning("Database locked — deleting stale journal and retrying...")
                journal = f"{SESSION_NAME}.session-journal"
                if os.path.exists(journal):
                    os.remove(journal)
                await asyncio.sleep(5)
                continue  # retry WITHOUT incrementing reconnect_attempts
            else:
                connection_state['connected'] = False
                main_logger.error(f"SQLite error: {e}")

        except Exception as e:
            connection_state['connected'] = False
            main_logger.error(f"Unexpected error in main loop: {type(e).__name__}: {e}")
            log_error('MAIN', f"Unexpected error: {type(e).__name__}: {e}")

        # Attempt reconnection with exponential backoff
        connection_state['reconnect_attempts'] += 1

        if connection_state['reconnect_attempts'] > MAX_RECONNECT_ATTEMPTS:
            main_logger.critical(f"Max reconnection attempts ({MAX_RECONNECT_ATTEMPTS}) exceeded. Exiting.")
            log_error('MAIN', f"Max reconnection attempts exceeded after {connection_state['disconnect_count']} disconnects")
            break

        main_logger.info(f"Attempting reconnection in {reconnect_delay}s (attempt {connection_state['reconnect_attempts']}/{MAX_RECONNECT_ATTEMPTS})...")
        await asyncio.sleep(reconnect_delay)

        # Exponential backoff with cap
        reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    # Cleanup
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # Persist AI disclosure set on shutdown
    if _ai_disclosure_unsaved_count > 0:
        save_ai_disclosure_shown()
        main_logger.info(f"Saved AI disclosure set on shutdown ({len(ai_disclosure_shown)} users)")

    # Save shutdown timestamp for catch-up on next restart
    save_shutdown_timestamp()
    main_logger.info("[CATCHUP] Saved shutdown timestamp")

    if client.is_connected():
        await client.disconnect()

    main_logger.info("Bot shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        main_logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        main_logger.critical(f"Fatal error: {type(e).__name__}: {e}")
        log_error('MAIN', f"Fatal error: {type(e).__name__}: {e}")
        sys.exit(1)

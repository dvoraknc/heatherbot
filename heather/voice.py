"""
heather.voice — Text-to-Speech Generation
===========================================
ElevenLabs primary + Coqui XTTS local fallback for voice note generation.
Transport-agnostic: generates audio bytes, does NOT send via Telegram.

Replaces: heather_telegram_bot.py
  - _convert_mp3_to_ogg: lines 8414-8433
  - _generate_tts_elevenlabs: lines 8436-8468
  - _generate_tts_coqui: lines 8471-8491
  - generate_tts_audio: lines 8494-8509
  - check_tts_status: lines 6700-6709
  - is_voice_request: lines 5412-5415
  - should_nudge_voice: lines 5364-5376
  - VOICE_REQUEST_TRIGGERS: lines 660-669
  - VOICE_FLIRTY_TEXTS: lines 671-677
  - VOICE_TTS_FAIL_RESPONSES: lines 679-683
  - VOICE_NUDGE_MESSAGES: lines 754-758
  - ELEVENLABS constants: lines 8409-8411

Dependencies: heather.config, heather.logging_setup, heather.service_health
Used by: heather_telegram_bot.py (voice sending, handlers, proactive voice)
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from typing import Dict, Optional

import requests

from heather import config
from heather.logging_setup import main_logger, PerformanceTimer
from heather.service_health import tts_health


# ============================================================================
# CONSTANTS
# ============================================================================

ELEVENLABS_MODEL = "eleven_flash_v2_5"

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
    "Hey you, I've been thinking about you all day",
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

VOICE_NUDGE_MESSAGES = [
    "btw you can hear my actual voice if you type /voice_on 😏",
    "you know I can send voice notes right? type /voice_on if you wanna hear me",
    "have you tried /voice_on yet? I sound even better than I text 😘",
]


# ============================================================================
# MODULE STATE
# ============================================================================

stats = {
    'tts_failures': 0,
    'voice_messages': 0,
}


# ============================================================================
# HEALTH CHECK
# ============================================================================

def check_tts_status() -> tuple:
    """Check if Coqui TTS service is available.

    Returns:
        (online: bool, status_message: str)
    """
    if tts_health.circuit_open and not tts_health.is_available():
        return False, f"Circuit open ({tts_health.consecutive_failures} failures)"
    try:
        response = requests.get(f"{config.TTS_ENDPOINT}/health", timeout=5)
        if response.status_code == 200:
            return True, "Online"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"


# ============================================================================
# REQUEST DETECTION
# ============================================================================

def is_voice_request(message: str) -> bool:
    """Check if message is asking for a voice note."""
    message_lower = message.lower()
    return any(trigger in message_lower for trigger in VOICE_REQUEST_TRIGGERS)


def should_nudge_voice(
    chat_id: int,
    voice_mode_users: set,
    conversation_turn_count: dict,
    voice_nudge_sent_today: dict,
    warmth_tier_fn=None,
) -> bool:
    """Check if we should suggest /voice_on to this user.

    Args:
        chat_id: Telegram chat ID.
        voice_mode_users: Set of chat IDs with voice mode enabled.
        conversation_turn_count: Dict of chat_id -> turn count.
        voice_nudge_sent_today: Dict of chat_id -> date string.
        warmth_tier_fn: Callable(chat_id) -> str returning warmth tier.

    Returns:
        True if user qualifies for voice nudge.
    """
    if chat_id in voice_mode_users:
        return False
    turns = conversation_turn_count.get(chat_id, 0)
    if turns < config.VOICE_NUDGE_MIN_TURNS:
        return False
    if warmth_tier_fn and warmth_tier_fn(chat_id) != "WARM":
        return False
    today = datetime.now().strftime('%Y-%m-%d')
    if voice_nudge_sent_today.get(chat_id) == today:
        return False
    return random.random() < config.VOICE_NUDGE_CHANCE


# ============================================================================
# AUDIO CONVERSION
# ============================================================================

def _convert_mp3_to_ogg(mp3_data: bytes) -> Optional[bytes]:
    """Convert MP3 audio to OGG Opus for Telegram voice notes.

    Uses ffmpeg CLI. Returns None on failure.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as mp3_f:
            mp3_f.write(mp3_data)
            mp3_path = mp3_f.name
        ogg_path = mp3_path.replace('.mp3', '.ogg')
        subprocess.run(
            ['ffmpeg', '-y', '-i', mp3_path, '-c:a', 'libopus', '-b:a', '64k', '-vn', ogg_path],
            capture_output=True, timeout=15,
        )
        with open(ogg_path, 'rb') as f:
            ogg_data = f.read()
        os.unlink(mp3_path)
        os.unlink(ogg_path)
        return ogg_data if len(ogg_data) > 100 else None
    except Exception as e:
        main_logger.warning(f"MP3->OGG conversion failed: {e}")
        return None


# ============================================================================
# TTS GENERATION
# ============================================================================

def _generate_tts_elevenlabs(text: str) -> Optional[bytes]:
    """Generate TTS via ElevenLabs API. Returns OGG audio bytes or None."""
    if not config.ELEVENLABS_API_KEY:
        return None
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}"
        payload = json.dumps({
            "text": text,
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.85,
                "style": 0.6,
                "use_speaker_boost": True,
            },
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("xi-api-key", config.ELEVENLABS_API_KEY)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "audio/mpeg")
        resp = urllib.request.urlopen(req, timeout=10)
        mp3_data = resp.read()
        if len(mp3_data) < 500:
            return None
        ogg_data = _convert_mp3_to_ogg(mp3_data)
        if ogg_data:
            main_logger.info(f"[TTS] ElevenLabs: {len(text)} chars -> {len(ogg_data)//1024}KB OGG ({len(mp3_data)//1024}KB MP3)")
        return ogg_data
    except Exception as e:
        main_logger.warning(f"[TTS] ElevenLabs error: {e}")
        return None


def _generate_tts_coqui(text: str) -> Optional[bytes]:
    """Generate TTS via local Coqui XTTS service. Returns OGG audio bytes or None."""
    if not tts_health.is_available():
        return None
    try:
        with PerformanceTimer('TTS', 'generate', f"len={len(text)}"):
            response = requests.post(
                f"{config.TTS_ENDPOINT}/tts",
                json={"text": text},
                timeout=config.TTS_TIMEOUT
            )
        if response.status_code == 200:
            tts_health.record_success()
            return response.content
        else:
            tts_health.record_failure()
            return None
    except Exception as e:
        main_logger.warning(f"[TTS] Coqui error: {e}")
        tts_health.record_failure()
        return None


def generate_tts_audio(text: str) -> Optional[bytes]:
    """Generate TTS audio. Tries ElevenLabs first (fast, high quality), falls back to Coqui.

    Args:
        text: Text to convert to speech.

    Returns:
        OGG audio bytes, or None if both services fail.
    """
    # Primary: ElevenLabs (~0.5-1s, high quality)
    audio = _generate_tts_elevenlabs(text)
    if audio:
        return audio

    # Fallback: Local Coqui XTTS (~15-20s, decent quality)
    main_logger.info("[TTS] ElevenLabs unavailable, falling back to Coqui")
    audio = _generate_tts_coqui(text)
    if audio:
        return audio

    main_logger.warning("[TTS] Both ElevenLabs and Coqui failed")
    stats['tts_failures'] += 1
    return None

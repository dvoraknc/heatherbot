"""
HeatherBot Discord Frontend
============================
Standalone Discord bot sharing the same AI backend as the Telegram bot.
Connects to llama-server (port 1234), loads personality YAML, uses
user_memory for kink scoring/persona injection, and postprocess for
response filtering.

Features:
- AI chat with Heather personality + kink personas
- NSFW channel enforcement
- Scheduled content posting (daily images, stories)
- Auto-channel setup
- New member welcome
- Invite command

Usage:
    python heather_discord_bot.py
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
import httpx
import yaml
import logging
import os
import random
import re
import json
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# Add bot directory to path for shared imports
BOT_DIR = Path(__file__).parent
sys.path.insert(0, str(BOT_DIR))

import urllib.request
import user_memory
import postprocess

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv(BOT_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:1234/v1/chat/completions")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "bmknSHfakfqnoh2yM9dh")  # Heather The PVC (professional clone)
ELEVENLABS_MODEL = "eleven_flash_v2_5"  # Ultra-low latency, ~0.5s
PERSONALITY_FILE = BOT_DIR / "heather_personality.yaml"
IMAGE_LIBRARY_FILE = BOT_DIR / "images_db" / "library.json"
IMAGE_LIBRARY_DIR = BOT_DIR / "images_db"
STORY_FILE = BOT_DIR / "heather_stories.yaml"
MAX_CONVERSATION_LENGTH = 20
AI_TIMEOUT = 90
MAX_DISCORD_MSG = 2000
DISCORD_INVITE = "https://discord.gg/BU9gsHdkMe"
VIDEO_DIR = BOT_DIR / "videos"
STORY_ARCHIVE_DIR = Path("C:/AI/logs/discord_stories")
TTS_CACHE_DIR = BOT_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(exist_ok=True)

# Channel names to auto-create
DESIRED_CHANNELS = {
    "general": {"nsfw": False, "topic": "Welcome to Heather's Playground! Say hi 👋"},
    "introductions": {"nsfw": False, "topic": "New here? Introduce yourself!"},
    "nsfw": {"nsfw": True, "topic": "The good stuff 🔥 Heather has no filter here"},
    "heather-pics": {"nsfw": True, "topic": "Daily pics from Heather 📸"},
    "stories": {"nsfw": True, "topic": "Heather's stories — Uber rides, Navy days, and more ✍️"},
    "rate-my-dick": {"nsfw": True, "topic": "Post yours and Heather will rate it 😏"},
}

# Voice channel to auto-create and auto-join
VOICE_CHANNEL_NAME = "talk-to-heather"
AUTO_JOIN_VOICE = True  # Heather auto-joins voice channel on startup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(os.getenv("LOG_DIR", "C:/AI/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "heather_discord.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("heather_discord")

# ---------------------------------------------------------------------------
# Load personality + image library + stories
# ---------------------------------------------------------------------------
def load_personality() -> dict:
    try:
        with open(PERSONALITY_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        log.error(f"Failed to load personality: {e}")
        return {}

def load_image_library() -> list:
    try:
        with open(IMAGE_LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("images", []) if isinstance(data, dict) else data
    except Exception as e:
        log.error(f"Failed to load image library: {e}")
        return []

def load_stories() -> list:
    try:
        with open(STORY_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data.get("stories", []) if data else []
    except Exception:
        return []

PERSONA = load_personality()
IMAGE_LIBRARY = load_image_library()
STORIES = load_stories()

# Track what's been posted to avoid repeats
_posted_images: set = set()
_posted_stories: set = set()
_posted_videos: set = set()
_last_story_date: str = ""

def build_system_prompt(discord_user_id: int, mode: str = "chat") -> str:
    prompts = PERSONA.get("prompts", {})
    base = prompts.get("base_personality", "You are Heather, a friendly chatbot.")
    enforcement = prompts.get("character_enforcement_prompt", "")
    mode_addition = prompts.get("mode_additions", {}).get(mode, "")
    system = base + "\n\n" + enforcement + "\n\n" + mode_addition
    profile_prompt = user_memory.build_profile_prompt(discord_user_id)
    if profile_prompt:
        system += profile_prompt
    kink_prompt = user_memory.build_kink_persona_prompt(discord_user_id)
    if kink_prompt:
        system += kink_prompt
    return system

# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------
conversations: dict[int, deque] = {}
user_modes: dict[int, str] = {}
rate_limit: dict[int, float] = {}
RATE_LIMIT_SECONDS = 1.5

stats = {
    "messages_received": 0,
    "messages_sent": 0,
    "start_time": datetime.now().isoformat(),
    "errors": 0,
    "images_posted": 0,
    "stories_posted": 0,
}

# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------
async def get_ai_response(user_id: int, user_message: str, mode: str = "chat") -> str:
    if user_id not in conversations:
        conversations[user_id] = deque(maxlen=MAX_CONVERSATION_LENGTH)
    history = conversations[user_id]
    system_prompt = build_system_prompt(user_id, mode)
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append(msg)
    messages.append({"role": "user", "content": user_message})

    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT) as client:
            resp = await client.post(LLM_URL, json={
                "model": "local-model",
                "messages": messages,
                "temperature": 0.85,
                "max_tokens": 300,
                "top_p": 0.88,
                "frequency_penalty": 0.35,
                "presence_penalty": 0.4,
                "stream": False,
            })
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if "<think>" in content:
                think_end = content.find("</think>")
                if think_end != -1:
                    content = content[think_end + 8:].strip()
            content = postprocess.postprocess_response(content)
            if not content:
                content = random.choice(["lol sorry what was that?", "hmm say that again?", "hold on got distracted 😂"])
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": content})
            return content
    except httpx.TimeoutException:
        log.error(f"LLM timeout for user {user_id}")
        stats["errors"] += 1
        return "sorry babe my brain froze for a sec 😅 say that again?"
    except Exception as e:
        log.error(f"LLM error for user {user_id}: {e}")
        stats["errors"] += 1
        return "ugh something glitched on my end, try again? 😘"

# ---------------------------------------------------------------------------
# NSFW content check
# ---------------------------------------------------------------------------
EXPLICIT_PATTERNS = re.compile(
    r'\b(cock|dick|pussy|cum|fuck|suck|anal|blowjob|deepthroat|orgasm|'
    r'breeding|breed|creampie|gangbang|nipple|clit|masturbat|jerk\s*off|'
    r'tits|boobs|naked|nude|penis|vagina|ass\s*fuck|throat\s*fuck|'
    r'whore|slut|bitch|dildo|vibrator)\b',
    re.IGNORECASE
)

def is_explicit(text: str) -> bool:
    return bool(EXPLICIT_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Voice-to-Voice: STT (faster-whisper) + TTS (ElevenLabs)
# ---------------------------------------------------------------------------
_voice_lock = asyncio.Lock()
_tts_counter = 0
_whisper_model = None
_heather_is_speaking = False  # Ignore voice input while Heather plays audio back
_listening_users: dict = {}  # user_id -> {"buffer": bytes, "last_packet": float, "silence_start": float}
VOICE_SILENCE_THRESHOLD = 1.5  # seconds of silence before processing speech
VOICE_MIN_AUDIO_LENGTH = 0.5  # minimum seconds of audio to bother transcribing


def get_whisper_model():
    """Lazy-load faster-whisper model on first use."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("[VOICE] Loading faster-whisper model (base.en)...")
        _whisper_model = WhisperModel("base.en", device="cuda", compute_type="float16")
        log.info("[VOICE] faster-whisper loaded on CUDA")
    return _whisper_model


async def transcribe_audio(pcm_data: bytes, sample_rate=48000, channels=2) -> str:
    """Transcribe PCM audio bytes to text using faster-whisper."""
    import wave
    import tempfile

    if len(pcm_data) < sample_rate * channels * 2 * VOICE_MIN_AUDIO_LENGTH:
        return ""  # Too short to transcribe

    # Write PCM to temp WAV file
    tmp_path = str(TTS_CACHE_DIR / "stt_input.wav")
    with wave.open(tmp_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)

    try:
        loop = asyncio.get_event_loop()
        def _transcribe():
            model = get_whisper_model()
            segments, info = model.transcribe(tmp_path, beam_size=3, language="en")
            text = " ".join(seg.text for seg in segments).strip()
            return text

        text = await loop.run_in_executor(None, _transcribe)
        if text:
            log.info(f"[VOICE] STT: \"{text}\"")
        return text
    except Exception as e:
        log.error(f"[VOICE] STT error: {e}")
        return ""

async def generate_voice_audio(text: str) -> str:
    """Generate speech audio via ElevenLabs API. Returns path to mp3 file."""
    global _tts_counter
    if not ELEVENLABS_API_KEY:
        return ""

    _tts_counter += 1
    output_path = str(TTS_CACHE_DIR / f"voice_{_tts_counter % 50}.mp3")

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
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
        req.add_header("xi-api-key", ELEVENLABS_API_KEY)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "audio/mpeg")

        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        def _fetch():
            resp = urllib.request.urlopen(req, timeout=15)
            audio_data = resp.read()
            with open(output_path, "wb") as f:
                f.write(audio_data)
            return len(audio_data)

        size = await loop.run_in_executor(None, _fetch)
        log.info(f"[VOICE] ElevenLabs TTS: {len(text)} chars -> {size//1024}KB audio")
        return output_path

    except Exception as e:
        log.error(f"[VOICE] ElevenLabs TTS error: {e}")
        return ""


async def _unmute_after_playback(vc):
    """Wait for audio to finish playing, then re-enable voice listening."""
    global _heather_is_speaking
    while vc.is_playing():
        await asyncio.sleep(0.3)
    # Extra buffer so echo doesn't get picked up
    await asyncio.sleep(1.0)
    _heather_is_speaking = False
    _listening_users.clear()  # Discard any audio captured during playback


async def play_voice_response(guild: discord.Guild, text: str):
    """Play a TTS response in the voice channel if the bot is connected."""
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        log.warning(f"[VOICE] play_voice_response called but no voice client (vc={vc})")
        return

    global _heather_is_speaking
    try:
        async with _voice_lock:
            audio_path = await generate_voice_audio(text)
            if not audio_path:
                log.warning("[VOICE] TTS returned no audio path")
                return

            wait_count = 0
            while vc.is_playing() and wait_count < 60:
                await asyncio.sleep(0.5)
                wait_count += 1

            _heather_is_speaking = True
            _listening_users.clear()
            source = discord.FFmpegPCMAudio(audio_path)
            vc.play(source)
            log.info(f"[VOICE] Playing response in {vc.channel.name} ({len(text)} chars)")
        # Unmute after playback finishes
        asyncio.create_task(_unmute_after_playback(vc))
    except Exception as e:
        _heather_is_speaking = False
        log.error(f"[VOICE] play_voice_response error: {e}")


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

STATUS_MESSAGES = [
    "Driving around Seattle...",
    "Walking Marymoor trails 🌲",
    "Texting from my Accord 📱",
    "Picking up Uber passengers...",
    "Waiting for someone fun 😏",
    "Missing Erick today...",
    "Watching trash TV with Emma 📺",
    "Writing as Jen Dvorak ✍️",
    "Browsing at Trader Joe's 🍷",
    "Thinking about my Navy days ⚓",
]

# ---------------------------------------------------------------------------
# Scheduled Tasks
# ---------------------------------------------------------------------------
async def rotate_status():
    while True:
        status = random.choice(STATUS_MESSAGES)
        await bot.change_presence(activity=discord.Game(name=status))
        await asyncio.sleep(300)


_last_image_post_file = TTS_CACHE_DIR / "last_image_post.txt"
_last_video_post_file = TTS_CACHE_DIR / "last_video_post.txt"

def _seconds_since_last_post(marker_file):
    """Check how many seconds since last post using a marker file."""
    try:
        if marker_file.exists():
            ts = float(marker_file.read_text().strip())
            return time.time() - ts
    except Exception:
        pass
    return 999999  # No marker = long time ago

def _mark_post_time(marker_file):
    """Write current timestamp to marker file."""
    marker_file.write_text(str(time.time()))

async def post_daily_image():
    """Post 5 images per day to #heather-pics (~4.8 hour intervals)."""
    await asyncio.sleep(30)
    while True:
        # Skip if we posted recently (survives restarts)
        since_last = _seconds_since_last_post(_last_image_post_file)
        if since_last < 14400:  # Less than 4 hours ago
            wait = 14400 - since_last + random.randint(0, 3600)
            log.info(f"[SCHEDULED] Image post skipped — last post {since_last/3600:.1f}h ago, waiting {wait/3600:.1f}h")
            await asyncio.sleep(wait)
            continue

        try:
            for guild in bot.guilds:
                channel = discord.utils.get(guild.text_channels, name="heather-pics")
                if not channel:
                    continue

                # Pick a random NSFW image not yet posted
                eligible = [
                    img for img in IMAGE_LIBRARY
                    if img["category"] in ("nsfw_topless", "nsfw_nude", "nsfw_explicit", "sfw_flirty", "sfw_lingerie")
                    and img["id"] not in _posted_images
                ]
                if not eligible:
                    _posted_images.clear()
                    eligible = [img for img in IMAGE_LIBRARY if img["category"] in ("nsfw_topless", "nsfw_nude", "nsfw_explicit", "sfw_flirty", "sfw_lingerie")]

                if eligible:
                    img = random.choice(eligible)
                    img_path = IMAGE_LIBRARY_DIR / img["file"]
                    if img_path.exists():
                        captions = [
                            "Just took this for you 😘",
                            "Missing you... here's a little something 💋",
                            "Felt cute, might delete later 😏",
                            "This is what I look like right now 🔥",
                            "For your eyes only... well, and everyone else in here 😂",
                            "Do you like what you see? 😈",
                            "Fresh from the shower 💦",
                            "Can't sleep... here's a selfie 📸",
                            "POV: you're in my Uber 🚗😏",
                            "Thinking about you while driving... had to pull over 🥵",
                        ]
                        await channel.send(
                            content=random.choice(captions),
                            file=discord.File(str(img_path)),
                        )
                        _posted_images.add(img["id"])
                        stats["images_posted"] += 1
                        _mark_post_time(_last_image_post_file)
                        log.info(f"[SCHEDULED] Posted image {img['id']} to #heather-pics")

        except Exception as e:
            log.error(f"Daily image post error: {e}")

        # 5 images per day = every ~4.8 hours (17280 seconds)
        await asyncio.sleep(17280 + random.randint(-1800, 1800))


async def post_daily_video():
    """Post 2 videos per day to #heather-pics (~12 hour intervals)."""
    await asyncio.sleep(600)
    while True:
        # Skip if we posted recently (survives restarts)
        since_last = _seconds_since_last_post(_last_video_post_file)
        if since_last < 36000:  # Less than 10 hours ago
            wait = 36000 - since_last + random.randint(0, 7200)
            log.info(f"[SCHEDULED] Video post skipped — last post {since_last/3600:.1f}h ago, waiting {wait/3600:.1f}h")
            await asyncio.sleep(wait)
            continue

        try:
            if not VIDEO_DIR.exists():
                log.warning("Video directory not found")
                await asyncio.sleep(43200)
                continue

            for guild in bot.guilds:
                channel = discord.utils.get(guild.text_channels, name="heather-pics")
                if not channel:
                    continue

                # Use guild's actual file size limit with 512KB safety margin for multipart overhead
                size_limit = guild.filesize_limit - (512 * 1024)
                all_videos = [
                    f for f in VIDEO_DIR.glob("*.mp4")
                    if f.stat().st_size < size_limit and f.name not in _posted_videos
                ]
                if not all_videos:
                    _posted_videos.clear()
                    all_videos = [f for f in VIDEO_DIR.glob("*.mp4") if f.stat().st_size < size_limit]

                if all_videos:
                    video = random.choice(all_videos)
                    captions = [
                        "Here's a little video for you 😈🔥",
                        "I was feeling naughty... hit play 📹💋",
                        "You asked for it... enjoy 😏",
                        "My phone was rolling... oops 🤭",
                        "Late night content for my favorites 💦",
                        "Don't show anyone else... 😘",
                    ]
                    try:
                        await channel.send(
                            content=random.choice(captions),
                            file=discord.File(str(video)),
                        )
                        _posted_videos.add(video.name)
                        stats["images_posted"] += 1
                        _mark_post_time(_last_video_post_file)
                        log.info(f"[SCHEDULED] Posted video {video.name} ({video.stat().st_size / 1024 / 1024:.1f}MB) to #heather-pics")
                    except discord.HTTPException as e:
                        if e.status == 413:
                            log.warning(f"[SCHEDULED] Video {video.name} ({video.stat().st_size / 1024 / 1024:.1f}MB) too large for guild limit ({size_limit / 1024 / 1024:.1f}MB) — skipping")
                            _posted_videos.add(video.name)
                        else:
                            raise

        except Exception as e:
            log.error(f"Daily video post error: {e}")

        # 2 videos per day = every ~12 hours (43200 seconds)
        await asyncio.sleep(43200 + random.randint(-3600, 3600))


async def post_daily_story():
    """Post a fresh Dolphin-generated Uber driving story to #stories once per day at ~9am."""
    global _last_story_date
    await asyncio.sleep(120)
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")

            # Only post once per day — check BOTH in-memory flag AND a posted marker file
            # The marker file survives restarts (the in-memory flag doesn't)
            posted_marker = STORY_ARCHIVE_DIR / f"{today}.posted"
            if today == _last_story_date or posted_marker.exists():
                _last_story_date = today  # Sync in-memory flag
                await asyncio.sleep(3600)
                continue

            # Wait until 9am-ish Pacific
            now = datetime.now()
            if now.hour < 9:
                wait = (9 - now.hour) * 3600 - now.minute * 60
                await asyncio.sleep(max(wait, 60))
                continue

            # Check if daily_story_poster.py already generated today's story
            story_file = STORY_ARCHIVE_DIR / f"{today}.md"
            story_text = ""

            if story_file.exists():
                story_text = story_file.read_text(encoding="utf-8").strip()
                log.info(f"[STORY] Loaded today's story from archive ({len(story_text)} chars)")
            else:
                # Generate via daily_story_poster.py
                log.info("[STORY] No archive found, generating fresh story...")
                import subprocess
                result = subprocess.run(
                    [sys.executable, str(BOT_DIR / "daily_story_poster.py"), "--dry-run"],
                    capture_output=True, text=True, timeout=180,
                    cwd=str(BOT_DIR),
                )
                # The dry-run prints the story, but also saves to archive
                if story_file.exists():
                    story_text = story_file.read_text(encoding="utf-8").strip()
                else:
                    log.warning("[STORY] Generation didn't create archive file")

            if not story_text or len(story_text) < 100:
                # Fallback to static YAML stories
                eligible = [s for s in STORIES if s.get("key") not in _posted_stories]
                if not eligible:
                    _posted_stories.clear()
                    eligible = STORIES
                if eligible:
                    story = random.choice(eligible)
                    story_text = story.get("content", "")
                    _posted_stories.add(story.get("key", ""))
                    log.info("[STORY] Using fallback YAML story")

            if story_text:
                for guild in bot.guilds:
                    channel = discord.utils.get(guild.text_channels, name="stories")
                    if not channel:
                        continue

                    # Split at paragraph boundaries if over Discord limit
                    if len(story_text) <= MAX_DISCORD_MSG - 50:
                        await channel.send(story_text)
                    else:
                        chunks = []
                        current = ""
                        for para in story_text.split("\n\n"):
                            if len(current) + len(para) + 2 > 1900:
                                if current:
                                    chunks.append(current)
                                current = para
                            else:
                                current = current + "\n\n" + para if current else para
                        if current:
                            chunks.append(current)
                        for chunk in chunks:
                            await channel.send(chunk)
                            await asyncio.sleep(1)

                    stats["stories_posted"] += 1
                    log.info(f"[SCHEDULED] Posted daily story to #stories ({len(story_text)} chars)")

                _last_story_date = today
                # Write marker file so restarts don't re-post
                posted_marker = STORY_ARCHIVE_DIR / f"{today}.posted"
                posted_marker.write_text(f"Posted at {datetime.now().isoformat()}")

        except Exception as e:
            log.error(f"Daily story post error: {e}")

        await asyncio.sleep(3600)  # Check every hour (but only post once per day)


async def setup_channels(guild: discord.Guild):
    """Ensure all desired text + voice channels exist in the guild."""
    existing_text = {c.name: c for c in guild.text_channels}
    existing_voice = {c.name: c for c in guild.voice_channels}

    # Create text channels
    for name, config in DESIRED_CHANNELS.items():
        if name not in existing_text:
            try:
                await guild.create_text_channel(
                    name=name,
                    topic=config["topic"],
                    nsfw=config["nsfw"],
                )
                log.info(f"Created text channel #{name} (nsfw={config['nsfw']})")
            except discord.Forbidden:
                log.warning(f"No permission to create #{name}")
            except Exception as e:
                log.error(f"Failed to create #{name}: {e}")

    # Create voice channel
    if VOICE_CHANNEL_NAME not in existing_voice:
        try:
            await guild.create_voice_channel(name=VOICE_CHANNEL_NAME)
            log.info(f"Created voice channel #{VOICE_CHANNEL_NAME}")
        except discord.Forbidden:
            log.warning(f"No permission to create voice channel #{VOICE_CHANNEL_NAME}")
        except Exception as e:
            log.error(f"Failed to create voice channel: {e}")


# ---------------------------------------------------------------------------
# Slash Commands
# ---------------------------------------------------------------------------
@tree.command(name="start", description="Say hi to Heather")
async def cmd_start(interaction: discord.Interaction):
    user_id = interaction.user.id
    conversations.pop(user_id, None)
    user_modes[user_id] = "chat"
    welcome = random.choice([
        "Hey there 😘 I'm Heather. What brings you my way?",
        "Hey! 💋 I'm Heather — tell me about yourself, handsome",
        "Hey babe 😏 I'm Heather. Frank tell you about me?",
    ])
    await interaction.response.send_message(welcome)
    log.info(f"[START] {interaction.user.name} ({user_id})")


@tree.command(name="reset", description="Clear conversation history")
async def cmd_reset(interaction: discord.Interaction):
    user_id = interaction.user.id
    conversations.pop(user_id, None)
    await interaction.response.send_message("Fresh start 😘 What's on your mind?")
    log.info(f"[RESET] {interaction.user.name} ({user_id})")


@tree.command(name="about", description="Learn more about Heather")
async def cmd_about(interaction: discord.Interaction):
    await interaction.response.send_message(
        "I'm Heather — an AI companion, creator-built and running locally. "
        "I'm based on a real character with a detailed backstory. "
        "I send pics, tell stories, and chat about whatever you want. "
        "I never judge and I never sleep 💋\n\n"
        "**Here too:** Chat with me right here in Discord — get naughty in the NSFW channel 😏\n"
        "**Private 1-on-1:** Hit me up on Telegram @UberMommy for private chats\n"
        f"**Invite your friends:** {DISCORD_INVITE}"
    )


@tree.command(name="invite", description="Get the server invite link")
async def cmd_invite(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"**Invite your friends to Heather's Playground!**\n{DISCORD_INVITE}\n\n"
        "The more the merrier 😏💋"
    )


@tree.command(name="stats", description="Bot statistics")
async def cmd_stats(interaction: discord.Interaction):
    uptime = datetime.now() - datetime.fromisoformat(stats["start_time"])
    hours = int(uptime.total_seconds() // 3600)
    minutes = int((uptime.total_seconds() % 3600) // 60)
    await interaction.response.send_message(
        f"📊 **Heather Discord Stats**\n"
        f"Uptime: {hours}h {minutes}m\n"
        f"Messages: {stats['messages_received']} received / {stats['messages_sent']} sent\n"
        f"Active conversations: {len(conversations)}\n"
        f"Images posted: {stats['images_posted']}\n"
        f"Stories posted: {stats['stories_posted']}\n"
        f"Errors: {stats['errors']}"
    )


@tree.command(name="mode", description="Switch chat mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="Chat (flirty conversation)", value="chat"),
    app_commands.Choice(name="Heather (casual)", value="heather"),
])
async def cmd_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    user_id = interaction.user.id
    user_modes[user_id] = mode.value
    responses = {
        "chat": "Chat mode 😘 Let's have some fun",
        "heather": "Just being me 💋 What's up?",
    }
    await interaction.response.send_message(responses.get(mode.value, "Mode set!"))
    log.info(f"[MODE] {interaction.user.name} ({user_id}) -> {mode.value}")


@tree.command(name="pic", description="Ask Heather for a pic")
async def cmd_pic(interaction: discord.Interaction):
    """Send a random image based on channel type."""
    is_nsfw = interaction.channel.is_nsfw() if hasattr(interaction.channel, 'is_nsfw') else False

    if is_nsfw:
        categories = ["nsfw_topless", "nsfw_nude", "sfw_flirty", "sfw_lingerie"]
    else:
        categories = ["sfw_casual", "sfw_flirty"]

    eligible = [img for img in IMAGE_LIBRARY if img["category"] in categories]
    if not eligible:
        await interaction.response.send_message("Hmm I don't have any pics ready right now 😅")
        return

    img = random.choice(eligible)
    img_path = IMAGE_LIBRARY_DIR / img["file"]
    if img_path.exists():
        captions = ["Here you go 😘", "Just for you 💋", "Like what you see? 😏", "You're welcome 🔥"]
        await interaction.response.send_message(
            content=random.choice(captions),
            file=discord.File(str(img_path)),
        )
        log.info(f"[PIC] Sent {img['id']} to {interaction.user.name}")
    else:
        await interaction.response.send_message("Hold on, let me find that pic... 📸")


@tree.command(name="voice", description="Heather joins/leaves the voice channel")
async def voice_cmd(interaction: discord.Interaction):
    """Join or leave the user's voice channel. When in voice, Heather listens and responds."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Join a voice channel first, then use /voice and I'll come hang out 😘")
        return

    vc = interaction.guild.voice_client

    if vc and vc.is_connected():
        # Already in voice — leave
        _listening_users.clear()
        await vc.disconnect()
        await interaction.response.send_message("Alright, I'm out. Talk to you later 💋")
        log.info(f"[VOICE] Left voice channel in {interaction.guild.name}")
    else:
        # Join using VoiceRecvClient for listening support
        channel = interaction.user.voice.channel
        try:
            from discord.ext.voice_recv import VoiceRecvClient
            vc = await channel.connect(cls=VoiceRecvClient)
            await interaction.response.send_message(
                f"Hey! I'm in **{channel.name}** now 🎤\n"
                f"I can hear you — just talk and I'll respond! 😏\n"
                f"*You can also type in chat and I'll speak that too.*"
            )
            log.info(f"[VOICE] Joined {channel.name} with voice receive in {interaction.guild.name}")

            # Set up voice receive callback
            def voice_data_callback(user, data):
                """Called for each voice packet received from a user."""
                if not user or user.bot or _heather_is_speaking:
                    return  # Ignore unknown/bot audio and ignore while Heather is speaking
                uid = user.id
                now = time.time()

                if uid not in _listening_users:
                    _listening_users[uid] = {
                        "buffer": bytearray(),
                        "last_packet": now,
                        "silence_start": 0,
                        "user": user,
                    }

                entry = _listening_users[uid]
                entry["buffer"].extend(data.pcm)
                entry["last_packet"] = now
                entry["silence_start"] = 0  # Reset silence timer — they're talking

            from discord.ext.voice_recv import BasicSink
            vc.listen(BasicSink(voice_data_callback))

            # Start the silence detection loop
            asyncio.create_task(_voice_silence_monitor(interaction.guild))

            # Play a greeting
            asyncio.create_task(play_voice_response(interaction.guild, "Hey! I can hear you now. Talk to me, handsome."))

        except Exception as e:
            await interaction.response.send_message(f"Couldn't join the channel 😢 ({e})")
            log.error(f"[VOICE] Failed to join channel: {e}")


async def _voice_silence_monitor(guild: discord.Guild):
    """Monitor voice buffers and process speech when users stop talking."""
    log.info("[VOICE] Silence monitor started")
    while guild.voice_client and guild.voice_client.is_connected():
        now = time.time()
        to_process = []

        for uid, entry in list(_listening_users.items()):
            time_since_last = now - entry["last_packet"]
            has_audio = len(entry["buffer"]) > 48000 * 2 * 2 * VOICE_MIN_AUDIO_LENGTH  # min audio length

            # User stopped talking (silence threshold reached) and has enough audio
            if time_since_last >= VOICE_SILENCE_THRESHOLD and has_audio:
                to_process.append((uid, entry))

            # Discard stale buffers (user went quiet long ago without enough audio)
            elif time_since_last > 10 and not has_audio:
                _listening_users.pop(uid, None)

        for uid, entry in to_process:
            user = entry["user"]
            pcm_data = bytes(entry["buffer"])
            # Clear the buffer
            entry["buffer"] = bytearray()
            entry["last_packet"] = now

            log.info(f"[VOICE] Processing {len(pcm_data)//1024}KB audio from {user.display_name}")

            # Transcribe
            text = await transcribe_audio(pcm_data)
            if not text or len(text.strip()) < 2:
                continue

            # Skip common STT hallucinations (faster-whisper outputs these on silence/noise)
            noise = {
                "", "you", "the", "yeah", "hmm", "uh", "um", "oh", "ah",
                "thanks for watching", "thank you for watching", "subscribe",
                "like and subscribe", "please subscribe", "see you next time",
                "bye", "goodbye", "thank you", "thanks", "...", "you.", "the.",
                "i'm going to go ahead and", "so", "and", "but",
            }
            if text.strip().lower().rstrip(".!,") in noise:
                continue

            log.info(f"[VOICE] {user.display_name} said: \"{text}\"")

            # Generate Heather's response via Dolphin
            response = await get_ai_response(uid, text, "chat")
            if response:
                # Voice-only — no transcript to text channels (keeps the experience immersive)
                await play_voice_response(guild, response)
                log.info(f"[VOICE] Replied to {user.display_name}: {response[:60]}")

        await asyncio.sleep(0.3)  # Check 3 times per second

    log.info("[VOICE] Silence monitor stopped (disconnected from voice)")
    _listening_users.clear()

    # Auto-reconnect after disconnect
    await asyncio.sleep(10)
    log.info("[VOICE] Attempting auto-reconnect to voice channel...")
    await _auto_rejoin_voice(guild)


async def _auto_rejoin_voice(guild: discord.Guild):
    """Rejoin the voice channel after a disconnect."""
    for attempt in range(5):
        try:
            vc_channel = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_NAME)
            if not vc_channel:
                log.warning("[VOICE] Voice channel not found for rejoin")
                return
            if guild.voice_client and guild.voice_client.is_connected():
                log.info("[VOICE] Already reconnected")
                return

            # Disconnect stale client if exists
            if guild.voice_client:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(2)

            from discord.ext.voice_recv import VoiceRecvClient, BasicSink
            vc = await vc_channel.connect(cls=VoiceRecvClient)

            def voice_data_callback(user, data):
                if not user or user.bot or _heather_is_speaking:
                    return
                uid = user.id
                now = time.time()
                if uid not in _listening_users:
                    _listening_users[uid] = {
                        "buffer": bytearray(),
                        "last_packet": now,
                        "silence_start": 0,
                        "user": user,
                    }
                entry = _listening_users[uid]
                entry["buffer"].extend(data.pcm)
                entry["last_packet"] = now
                entry["silence_start"] = 0

            vc.listen(BasicSink(voice_data_callback))
            asyncio.create_task(_voice_silence_monitor(guild))
            log.info(f"[VOICE] Auto-reconnected to #{VOICE_CHANNEL_NAME} (attempt {attempt+1})")
            return

        except Exception as e:
            wait = (attempt + 1) * 15
            log.error(f"[VOICE] Reconnect attempt {attempt+1} failed: {e}, retrying in {wait}s")
            await asyncio.sleep(wait)

    log.error("[VOICE] Failed to reconnect after 5 attempts")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
async def _start_background_tasks():
    """Start all scheduled posting tasks. Called from setup_hook to avoid _MissingSentinel."""
    await bot.wait_until_ready()
    log.info("Starting background tasks (status rotation, image/video/story posting)")
    asyncio.create_task(rotate_status())
    asyncio.create_task(post_daily_image())
    asyncio.create_task(post_daily_video())
    asyncio.create_task(post_daily_story())


@bot.event
async def on_ready():
    """Bot connected to Discord."""
    await tree.sync()
    log.info(f"{'=' * 60}")
    log.info(f"HeatherBot Discord v2.1")
    log.info(f"Logged in as: {bot.user.name} ({bot.user.id})")
    log.info(f"Guilds: {len(bot.guilds)}")
    for guild in bot.guilds:
        log.info(f"  - {guild.name} ({guild.id})")
        await setup_channels(guild)
    log.info(f"LLM: {LLM_URL}")
    log.info(f"Image library: {len(IMAGE_LIBRARY)} images")
    log.info(f"Videos: {len(list(VIDEO_DIR.glob('*.mp4'))) if VIDEO_DIR.exists() else 0}")
    log.info(f"Stories: {len(STORIES)} (YAML) + daily Dolphin-generated")
    log.info(f"Personality: {PERSONALITY_FILE.name}")
    log.info(f"Posting schedule: 5 images/day, 2 videos/day, 1 story/day (9am)")
    log.info(f"Voice: ElevenLabs {'enabled' if ELEVENLABS_API_KEY else 'DISABLED'} | Auto-join: {AUTO_JOIN_VOICE}")
    log.info(f"{'=' * 60}")

    # Auto-join voice channel if configured
    if AUTO_JOIN_VOICE and ELEVENLABS_API_KEY:
        for guild in bot.guilds:
            vc_channel = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_NAME)
            if vc_channel and not guild.voice_client:
                try:
                    from discord.ext.voice_recv import VoiceRecvClient, BasicSink
                    vc = await vc_channel.connect(cls=VoiceRecvClient)

                    def voice_data_callback(user, data):
                        if user.bot or _heather_is_speaking:
                            return
                        uid = user.id
                        now = time.time()
                        if uid not in _listening_users:
                            _listening_users[uid] = {
                                "buffer": bytearray(),
                                "last_packet": now,
                                "silence_start": 0,
                                "user": user,
                            }
                        entry = _listening_users[uid]
                        entry["buffer"].extend(data.pcm)
                        entry["last_packet"] = now
                        entry["silence_start"] = 0

                    vc.listen(BasicSink(voice_data_callback))
                    asyncio.create_task(_voice_silence_monitor(guild))
                    log.info(f"[VOICE] Auto-joined #{VOICE_CHANNEL_NAME} in {guild.name} with voice receive")
                except Exception as e:
                    log.error(f"[VOICE] Failed to auto-join voice: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    """Welcome new members."""
    # Find #general or first text channel
    channel = discord.utils.get(member.guild.text_channels, name="general")
    if not channel:
        channel = member.guild.text_channels[0] if member.guild.text_channels else None

    if channel:
        welcomes = [
            f"Hey {member.display_name}! 😘 Welcome to Heather's Playground. I'm Heather — head to #nsfw if you want the real fun, or just chat with me right here 💋",
            f"Well hello {member.display_name} 😏 Fresh meat! Welcome to my playground. DM me or find me in #nsfw — I don't bite... unless you want me to 😈",
            f"Welcome {member.display_name}! 💋 I'm Heather, and this is where the fun happens. Check out #heather-pics for some eye candy, or just say hi 😘",
        ]
        await channel.send(random.choice(welcomes))
        log.info(f"[WELCOME] {member.display_name} joined {member.guild.name}")


@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages."""
    if message.author == bot.user:
        return
    if message.author.bot:
        return

    user_id = message.author.id
    user_name = message.author.display_name
    content = message.content.strip()

    if not content:
        return
    if content.startswith("/"):
        return

    # Rate limiting
    now = time.time()
    last = rate_limit.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return
    rate_limit[user_id] = now

    stats["messages_received"] += 1
    log.info(f"[MSG] {user_name} ({user_id}): {content[:80]}")

    user_memory.update_from_user_message(user_id, content, display_name=user_name)
    mode = user_modes.get(user_id, "chat")
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_nsfw_channel = is_dm or (hasattr(message.channel, 'is_nsfw') and message.channel.is_nsfw())

    # Special handling for #rate-my-dick — if they post an image
    if hasattr(message.channel, 'name') and message.channel.name == "rate-my-dick" and message.attachments:
        async with message.channel.typing():
            response = await get_ai_response(user_id, "Rate this dick pic for me, be honest and flirty", "rate")
        await message.reply(response, mention_author=False)
        stats["messages_sent"] += 1
        return

    # Auto-detect pic/photo/selfie requests and send an image from the library
    _pic_triggers = re.compile(
        r'\b(show me|send me|can i see|let me see|send a|got a|have a|give me|want to see|wanna see|'
        r'pic|photo|selfie|nude|nudes|naked|topless|tits|boobs|pussy|ass pic|body)'
        r'\b', re.IGNORECASE
    )
    if _pic_triggers.search(content) and is_nsfw_channel:
        # Pick an appropriate category based on what they asked
        msg_lower = content.lower()
        if any(w in msg_lower for w in ['nude', 'naked', 'pussy', 'ass']):
            categories = ['nsfw_nude', 'nsfw_explicit']
        elif any(w in msg_lower for w in ['topless', 'tits', 'boobs']):
            categories = ['nsfw_topless', 'nsfw_nude']
        elif any(w in msg_lower for w in ['selfie', 'face', 'cute']):
            categories = ['sfw_casual', 'sfw_flirty']
        else:
            categories = ['nsfw_topless', 'sfw_flirty', 'sfw_lingerie']

        eligible = [img for img in IMAGE_LIBRARY if img["category"] in categories and img["id"] not in _posted_images]
        if not eligible:
            eligible = [img for img in IMAGE_LIBRARY if img["category"] in categories]

        if eligible:
            img = random.choice(eligible)
            img_path = IMAGE_LIBRARY_DIR / img["file"]
            if img_path.exists():
                # Generate a flirty text response AND attach the image
                async with message.channel.typing():
                    response = await get_ai_response(user_id, content, mode)
                user_memory.update_from_bot_reply(user_id, response)
                await message.reply(
                    content=response,
                    file=discord.File(str(img_path)),
                    mention_author=False,
                )
                _posted_images.add(img["id"])
                stats["messages_sent"] += 1
                log.info(f"[PIC-REQUEST] Sent {img['id']} ({img['category']}) to {user_name}")

                # Also speak if in voice
                if message.guild:
                    vc = message.guild.voice_client
                    if vc and vc.is_connected():
                        asyncio.create_task(play_voice_response(message.guild, response))
                return

    # Non-NSFW channel pic requests — tease and redirect
    if _pic_triggers.search(content) and not is_nsfw_channel:
        async with message.channel.typing():
            response = await get_ai_response(user_id, content, mode)
        await message.reply(
            "Mmm I've got something for you but you'll need to find me in the NSFW channel or DM me 😏💋",
            mention_author=False,
        )
        stats["messages_sent"] += 1
        return

    async with message.channel.typing():
        response = await get_ai_response(user_id, content, mode)

    user_memory.update_from_bot_reply(user_id, response)

    if not is_nsfw_channel and is_explicit(response):
        response = random.choice([
            "Mmm I wanna tell you but let's take this somewhere more private 😏 DM me or find me in the NSFW channel",
            "Oh I've got thoughts about that 🔥 but I need an age-restricted channel to say them... DM me?",
            "Haha saving the good stuff for private 😘 slide into my DMs and I'll show you what I mean",
        ])

    # If Heather is in voice, speak FIRST then show text (so users hear before they read)
    global _heather_is_speaking
    in_voice = False
    if message.guild:
        vc = message.guild.voice_client
        if vc and vc.is_connected():
            in_voice = True
            # Generate audio and start playing before sending text
            audio_path = await generate_voice_audio(response)
            if audio_path:
                async with _voice_lock:
                    while vc.is_playing():
                        await asyncio.sleep(0.3)
                    try:
                        _heather_is_speaking = True  # Mute listener so she doesn't hear herself
                        # Clear any audio buffers that accumulated during generation
                        _listening_users.clear()
                        vc.play(discord.FFmpegPCMAudio(audio_path))
                        log.info(f"[VOICE] Playing response in {vc.channel.name} ({len(response)} chars)")
                        # Wait for playback to start, then delay text
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        log.error(f"[VOICE] Playback error: {e}")
                        _heather_is_speaking = False
                # Keep muted until playback finishes, then unmute
                asyncio.create_task(_unmute_after_playback(vc))

    # Now send the text
    if len(response) <= MAX_DISCORD_MSG:
        await message.reply(response, mention_author=False)
    else:
        chunks = split_message(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

    stats["messages_sent"] += 1
    log.info(f"[REPLY] -> {user_name}: {response[:80]}")


def split_message(text: str) -> list[str]:
    if len(text) <= MAX_DISCORD_MSG:
        return [text]
    chunks = []
    current = ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > MAX_DISCORD_MSG:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current = current + " " + sentence if current else sentence
    if current:
        chunks.append(current.strip())
    return chunks if chunks else [text[:MAX_DISCORD_MSG]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set in .env")
        sys.exit(1)

    log.info("Starting HeatherBot Discord v2.0...")
    log.info(f"Personality: {PERSONALITY_FILE}")
    log.info(f"LLM: {LLM_URL}")
    log.info(f"Image library: {len(IMAGE_LIBRARY)} images")
    log.info(f"Stories: {len(STORIES)}")

    try:
        import requests
        r = requests.get(LLM_URL.replace("/v1/chat/completions", "/v1/models"), timeout=5)
        model = r.json()["data"][0]["id"]
        log.info(f"LLM model: {model}")
    except Exception as e:
        log.warning(f"LLM not reachable (will retry on messages): {e}")

    # Schedule background tasks via setup_hook (discord.py 2.x compatible)
    async def setup_hook():
        asyncio.create_task(_start_background_tasks())
    bot.setup_hook = setup_hook

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

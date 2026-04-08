# HeatherBot

**A fully local AI companion chatbot ecosystem for Telegram — with multi-platform funnel automation.**

HeatherBot is a Telegram userbot (MTProto via Telethon) that runs entirely on your own hardware. No cloud APIs, no OpenAI, no subscriptions. Just a local LLM, a persona YAML file, and a stack of services that deliver a transparent AI companion experience.

The bot is a **transparent digital twin** — based on a real person, upfront about being AI from the first message, with zero content restrictions. No pretending to be human, no catfish angle. Users know what they're talking to and engage more freely because of it.

The ecosystem includes the core Telegram companion, a Discord community bot, ElevenLabs voice cloning (real voice, not robotic TTS), SVD/Wan video animation, and multi-platform outreach — all running locally on consumer GPUs.

The thesis: a 24B parameter model with detailed persona engineering, adaptive kink personas, professional voice cloning, and honest AI disclosure delivers a better companion experience than cloud services — while keeping everything private, uncensored, and under your control.

## Architecture

```
  +-------------------+     +-------------------+     +-------------------+
  |   llama-server    |     |      Ollama       |     |     ComfyUI       |
  |  (Text AI, 12B)   |     |  (Image Analysis) |     | (Image Generation)|
  |    port 1234      |     |    port 11434     |     |    port 8188      |
  +--------+----------+     +--------+----------+     +--------+----------+
           |                         |                         |
           +------------+------------+------------+------------+
                        |                         |
               +--------+----------+     +--------+----------+
               |  HeatherBot Core  |     |   Chatterbox TTS  |
               |   (Telethon)      |     |    port 5001      |
               |  + Flask monitor  |     +-------------------+
               |    port 8888      |
               +--------+----------+
                        |
           +-----+------+------+------------+
           |            |                    |
  +--------+-------+  +-+-------------+  +--+--------------+
  | Frank Dashboard |  | Discord Bot   |  | OpenClaw MGMT   |
  | (Reddit/FetLife)|  | (discord.py)  |  | (Autonomous Agt)|
  |   port 8080     |  +---------------+  +-----------------+
  +--------+-------+
           |
  +--------+-------+
  | Twitter/X Bot   |
  | (Tweepy API)    |
  +----------------+
```

| Service | Port | Purpose |
|---------|------|---------|
| llama-server | 1234 | Text generation (llama.cpp with any GGUF model) |
| Ollama | 11434 | Image analysis (LLaVA or similar vision model) |
| ComfyUI | 8188 | Image generation (FLUX.1 dev FP8 or SDXL) with face-swap workflows |
| ElevenLabs / Coqui TTS | 5001 | Voice synthesis (ElevenLabs primary, Coqui fallback) |
| Bot Monitor | 8888 | Web dashboard for analytics and admin |
| Frank Dashboard | 8080 | Multi-platform outreach (Reddit + FetLife auto-funnel) |
| Discord Bot | — | Community server with scheduled posting and auto-welcome |
| Twitter/X | — | Automated tweeting via Tweepy (Jen Dvorak persona) |

## Features

### Core Bot (Telegram)
- **Persona YAML system** — Define your character's identity, backstory, personality, communication style, and sexual boundaries in a single YAML file. Swap personas by pointing to a different file.
- **MTProto userbot** — Appears as a real Telegram user, not a bot. No "bot" label, no command menus.
- **Adaptive kink personas** — 17 kink-specific personality variants (breeding, CNC, BBC, MILF, anal, domme, etc.) that auto-detect each user's primary kink and shift the character's emphasis to match.
- **Per-user memory system** — 4-layer personalization: kink scoring (21 categories), memorable moments, LLM session summaries, and callback prompts. Each user gets a persistent profile that grows over time.
- **Image generation** — FLUX.1 dev FP8 via ComfyUI with runtime LoRA injection, face-swap, and body-accurate NSFW prompts with negative prompt support.
- **Image analysis** — Receives and analyzes user photos via Ollama vision models (LLaVA).
- **Voice messages** — ElevenLabs professional voice cloning (sub-1s latency) with Coqui TTS fallback. Her real voice, not a robot. Users type `/voice_on` to switch to voice mode.
- **Story system** — Pre-written story bank (YAML) with 60/40 banked/LLM-generated split, per-user rotation.
- **Video delivery** — Pre-cached video library with offer-and-deliver flow.
- **SVD/Wan animation** — Animate static images with breathing, movement via Stable Video Diffusion or Wan 2.1 Image-to-Video. Script: `animate_breathing.py`.
- **Content tier system** — FREE → FAN (50 stars) → VIP (unrestricted, 200 stars). Telegram Stars integration. Currently running full VIP for all users during transparency trial.
- **Post-processing pipeline** — 7-stage filter: strips thinking tags, asterisk actions, bracketed metadata, model artifacts, and quote wrapping. Optional human imperfections (typos, abbreviations) at 12% chance for realism.
- **Monitoring dashboard** — Real-time Flask dashboard with user analytics, conversation logs, and conversion funnels.
- **Content safety** — CSAM flag-and-review system, blocked user management, admin alerts, gender violation detection, prompt injection detection.
- **Transparent digital twin** — Upfront AI disclosure on first message. Bot is honest about being AI, frames it as a feature (24/7, no limits, remembers everything). Never denies being AI, never pretends to be human.
- **Hostile exit detection** — Graceful one-message exit when users are angry/frustrated, then silence. No clingy "but wait" behavior.
- **Re-engagement** — Automatic outreach to inactive users with configurable timing and personalized callbacks based on conversation history.
- **Breeding/CNC injection** — Contextual breeding and CNC fantasy prompts that inject based on keyword detection or conversation energy level.
- **Domme mode** — Detects humiliation/degradation requests and switches to dominant personality overlay.
- **Arousal tracking** — Detects climax/heated/afterglow states and adjusts responses accordingly.
- **Single-char spam detection** — Catches rapid single-character message spam and responds with canned messages instead of burning LLM tokens.
- **Meetup/verification deflection** — Persistent deflection for meetup requests and verification demands, carries across multiple messages.

### Kink Persona System
- **17 adaptive personas** — breeding, MILF/age-gap, CNC/rough, cuckold, BBC/size, GFE/intimate, deepthroat/oral, gangbang, voyeur/exhib, domme/mommy, anal, free-use, forced-bi, body-worship, findom, stepfamily, uber-slut
- **Automatic detection** — 21 keyword categories score every user message in real-time
- **Phased injection** — Messages 1-3: warmth (no persona). Messages 4-9: light hints. Messages 10+: full persona injection with specific phrases, session flow, and cuckold integration.
- **Per-user tracking** — Active persona stored in each user's profile JSON with kink name and score
- **Persona definitions** in `heather_kink_personas.yaml` — each persona includes core traits, verbal responses, physical details, session flow, and cuckold/Frank integration

### Frank Dashboard (Multi-Platform Outreach)
- **Reddit + FetLife automation** — Playwright browser manages Reddit conversations; OpenClaw managed browser handles FetLife messaging and content posting
- **Stage-aware Frank AI** — Dolphin 12B generates responses as "Frank" (cuckold husband character) with 3-stage conversation strategy: rapport building → Telegram pitch → post-pitch follow-up
- **Auto-send** — Replies are automatically queued with 2-8 minute random delays (human texting pace). Hold keywords flag risky messages for manual review.
- **Reddit chat layers** — Handles standard chats, regular Requests, and NSFW Additional Requests (all three layers automated)
- **Catch-up replier** — Background task scans database every 2 minutes for conversations where the last message is inbound and auto-replies
- **Unified dashboard** — Web UI at port 8080 with platform badges (R/FL), conversation management, AI suggestion approval, and metrics
- **Frank Content API** — Shared content generation endpoint for consistent Frank voice across all platforms
- **Self-healing watchdog** — Monitors dashboard health, browser status, and message flow. Auto-fixes common failures and escalates to Claude Code CLI when self-healing fails.

### Discord Bot
- **Auto-channel creation** — Sets up introductions, heather-pics, stories, rate-my-dick channels on join
- **Scheduled image posting** — Posts character images every 4 hours
- **Daily story posting** — Publishes LLM-generated stories once per day
- **New member welcome** — Automatic greeting with character introduction
- **Commands** — `/pic` (send image), `/invite` (generate invite link)

### Twitter/X Bot (Jen Dvorak Persona)
- **Separate character** — Jen Dvorak (@UberSlutty), an Uber-driving slut mom persona defined in `jen_twitter_persona.yaml`
- **Automated tweeting** — 3 posts/day via Tweepy (morning, afternoon, evening)
- **Content mix** — 8 tweet types: uber stories, mom contrast, body confidence, horniness, tip humor, morning/evening shift, funnel
- **Curated video library** — Video classification pipeline using llama3.2-vision (86% accuracy) identifies car-interior content for on-brand tweets
- **Funnel integration** — 1 in 5 tweets soft-plugs Telegram/Discord for conversion

### Autonomous Management (OpenClaw MGMT)
- **Heartbeat monitoring** — 30-minute cycle checks all services, error rates, engagement metrics
- **Scheduling ownership** — All recurring tasks (Twitter posting, FetLife replier, dashboard watchdog, video classification) run from OpenClaw heartbeat
- **Dashboard monitoring** — Confirms Frank Dashboard health, reports metrics every 4 hours
- **Kink persona reporting** — Scans user profiles for persona distribution, reports trends
- **Personality tuning** — Authorized to edit persona YAML directly for prompt tweaks
- **User engagement research** — Analyzes bot logs for satisfaction patterns, builds optimization briefs
- **Dashboard restart** — Authorized to restart the Frank Dashboard (but NOT HeatherBot)
- **Delegation model** — OpenClaw diagnoses and recommends; Claude Code implements all code changes

## Hardware Requirements

**Minimum:**
- 1x GPU with 24GB VRAM (RTX 3090, RTX 4090, etc.)
- 32GB system RAM
- Python 3.10+

**Recommended:**
- 2x GPUs (one for text, one for image generation)
- 64GB+ system RAM
- SSD for model storage

The bot is designed for consumer hardware. A single RTX 3090 can run the text model, image analysis, and TTS simultaneously. Image generation benefits from a second GPU.

## Prerequisites

Install these before setting up the bot:

1. **[llama.cpp](https://github.com/ggerganov/llama.cpp)** — Download a GGUF model (12B+ recommended) and the `llama-server` binary
2. **[Ollama](https://ollama.ai)** — Install and pull a vision model: `ollama pull llava`
3. **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** — For image generation (optional)
4. **[ElevenLabs](https://elevenlabs.io)** — For voice cloning ($99/mo Pro plan, optional — falls back to Coqui TTS locally)
5. **[Coqui TTS](https://github.com/coqui-ai/TTS)** — Free local voice synthesis fallback (15-20s latency vs ElevenLabs' sub-1s)
6. **Python 3.10+**
7. **Telegram account** — Register API credentials at [my.telegram.org](https://my.telegram.org)

## Installation

```bash
# Clone the repo
git clone https://github.com/youruser/heatherbot.git
cd heatherbot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Telegram API credentials and admin user ID
```

## Configuration

### 1. Environment Variables

Edit `.env` with your credentials (see `.env.example` for all options):

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
ADMIN_USER_ID=your_telegram_user_id
```

### 2. Persona YAML

The bot ships with `persona_example.yaml` — a complete template with a fictional character. To create your own:

1. Copy `persona_example.yaml` to `my_persona.yaml`
2. Edit every section with your character's details
3. Run with `--personality my_persona.yaml`

Key sections:
- **identity** — Name, age, location, occupation, relationships
- **personality** — Traits, humor, flaws
- **communication** — Voice, phrases, flirting patterns
- **sexual** — Preferences, boundaries, breeding/CNC fantasies
- **ai_behavior** — Rules, guardrails, mode behaviors
- **prompts** — The system prompts sent to the LLM (most important section)

### 3. Kink Personas (Optional)

The bot ships with `heather_kink_personas.yaml` containing 17 kink-specific personality overlays. These are automatically applied based on user behavior — no configuration needed.

To add a custom kink persona:
1. Add a new entry in `heather_kink_personas.yaml`
2. Add keyword triggers in `user_memory.py` → `KINK_KEYWORDS`
3. Add the mapping in `user_memory.py` → `KINK_TO_PERSONA`

### 4. Story Bank (Optional)

Create a `heather_stories.yaml` (or whatever you name it) with pre-written stories:

```yaml
stories:
  - key: "story_beach_001"
    kinks: ["romance", "outdoor"]
    content: |
      Your story text here...
```

## Usage

### Starting Services

Start your backend services first:

```bash
# Text AI (llama-server)
llama-server -m /path/to/model.gguf --host 0.0.0.0 --port 1234 -ngl 99 -c 32768

# Image analysis (Ollama)
ollama serve  # Usually auto-starts

# Image generation (ComfyUI — optional)
cd /path/to/ComfyUI && python main.py

# Voice (Chatterbox TTS — optional)
python heather_tts_service.py
```

### Running the Bot

```bash
# Basic (text-only, no dashboard)
python heather_telegram_bot.py

# Full setup (monitoring + optimized for 12B models)
python heather_telegram_bot.py --monitoring --small-model

# Custom persona
python heather_telegram_bot.py --personality my_persona.yaml --monitoring

# Full production setup
python heather_telegram_bot.py --monitoring --small-model --personality heather_personality.yaml --log-dir /path/to/logs
```

First run will prompt for your Telegram phone number and verification code. Subsequent runs use the saved session file.

### Running the Frank Dashboard (Optional)

```bash
cd heather-reddit
python -m uvicorn app:app --host 127.0.0.1 --port 8080
```

Opens two browser windows (Reddit + FetLife) and starts autonomous conversation management.

### CLI Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--personality` | `persona_example.yaml` | Persona YAML file path |
| `--monitoring` | off | Enable web dashboard on port 8888 |
| `--small-model` | off | Optimized prompts for 12B models |
| `--text-port` | 1234 | llama-server port |
| `--image-port` | 11434 | Ollama port |
| `--tts-port` | 5001 | TTS service port |
| `--log-dir` | `logs/` | Log directory |
| `--debug` | off | Verbose logging |
| `--unfiltered` | off | Disable content filters |
| `--session` | `heather_session` | Telethon session file name |

### Admin Commands (in Telegram)

Send these in your Saved Messages or any chat while logged in as the admin:

- `/stats` — User statistics
- `/admin_flags` — Review CSAM flags
- `/block <user_id>` — Block a user
- `/unblock <user_id>` — Unblock a user
- `/takeover <user_id>` — Pause bot for a user (you reply manually)
- `/botreturn <user_id>` — Resume bot for a user
- `/stories` — List story bank
- `/stories reload` — Hot-reload stories YAML
- `/menu` — Display interactive menu

## White-Labeling

HeatherBot is designed to be re-skinned. To create a completely different character:

1. **Create a new persona YAML** — Copy `persona_example.yaml`, change everything
2. **Create kink personas** — Copy `heather_kink_personas.yaml`, customize for your character
3. **Provide your own media** — Photos in `images_db/`, videos in `videos/`
4. **Clone a voice** — Use ElevenLabs to professionally clone your character's voice (or Coqui TTS for free local alternative)
5. **Set up face-swap** — Place your character's face source image for ComfyUI
6. **Update `.env`** — Set `PAYMENT_BOT_USERNAME` to your payment bot's username

The bot code is character-agnostic. All personality comes from the YAML files and media assets.

## Windows Service Manager

On Windows, use the included PowerShell service manager:

```powershell
# Interactive menu
.\heather_services.ps1

# Start all services
.\heather_services.ps1 startall

# Check status
.\heather_services.ps1 status
```

## Project Structure

```
heather-bot/                    # Core Telegram bot
  heather_telegram_bot.py       # Main bot (~10K lines)
  heather_personality.yaml      # Character persona definition
  heather_kink_personas.yaml    # 17 adaptive kink persona overlays
  user_memory.py                # Per-user memory + kink scoring
  postprocess.py                # 7-stage response filter + human imperfections
  heather_discord_bot.py        # Discord community bot
  jen_twitter_persona.yaml      # Twitter/X persona definition
  generate_batch_sdxl.py        # SDXL batch image generator with LoRA stacking
  faceswap_batch.py             # ReActor face swap batch processor
  classify_car_v2.py            # Video classifier (llama3.2-vision)
  hourly_memory_check.py        # Memory consolidation checker
  generate_pose_skeletons.py    # OpenPose skeleton generator for ControlNet
  user_profiles/                # Per-user JSON profiles (auto-generated)

heather-reddit/                 # Multi-platform outreach dashboard
  app.py                        # FastAPI + background tasks + Frank Content API
  ai_frank.py                   # Stage-aware Frank AI with safety filters
  frank_content_api.py          # Frank + Jen content generation via Dolphin
  twitter_poster.py             # Twitter/X posting via Tweepy
  reddit_monitor.py             # Reddit Playwright automation
  fetlife_replier.py            # FetLife inbox replier via OpenClaw browser
  dashboard_watchdog.py         # Self-healing watchdog with Claude Code escalation
  database.py                   # SQLite with platform support
  static/                       # Dashboard frontend
```

## Known Limitations

- **Single-session**: Telethon userbot can only have one active session per account. Running the bot locks out other Telethon scripts using the same session.
- **LLM hallucinations**: Small models (7B-12B) occasionally invent backstory details not in the persona YAML. The post-processing pipeline catches some of these but not all.
- **Conversation-end detection**: Goodbye loop detection stops replying after 2+ goodbyes. Hostile exit detection sends one graceful exit then goes silent for 1 hour.
- **Image generation speed**: ComfyUI FLUX.1 takes 10-30 seconds per image depending on GPU.
- **FLUX body accuracy ceiling**: ~70% match to reference photos via text prompts alone. Custom LoRA or img2img needed for better body accuracy.
- **Voice quality**: TTS quality varies. Short utterances work best.

## Disclaimer

This software is intended for creating AI companion chatbots. It can be configured for adult content.

- **You are responsible** for complying with the laws of your jurisdiction regarding adult content, AI-generated media, and automated messaging.
- **AI disclosure is built in** — the bot automatically discloses its AI nature to new users. Do not disable this.
- **Content safety systems** (CSAM flagging, blocked user management) are included and should remain active.
- **This is a local tool** — no data leaves your machine unless you configure it to.

Use responsibly.

## License

MIT License. See [LICENSE](LICENSE) for details.

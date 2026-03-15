# HeatherBot

**A fully local AI companion chatbot ecosystem for Telegram — with multi-platform funnel automation.**

HeatherBot is a Telegram userbot (MTProto via Telethon) that runs entirely on your own hardware. No cloud APIs, no OpenAI, no subscriptions. Just a local LLM, a persona YAML file, and a stack of services that make the bot feel like a real person texting.

The ecosystem includes the core Telegram companion bot, a multi-platform outreach dashboard (Reddit + FetLife), and an autonomous management agent — all running locally.

The thesis: a well-scaffolded 12B parameter model with the right persona engineering, adaptive kink personas, content pipeline, and post-processing can deliver a compelling companion experience that rivals cloud-hosted solutions — while keeping everything private and under your control.

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
           +------------+------------+
           |                         |
  +--------+----------+     +--------+----------+
  |  Frank Dashboard  |     |   OpenClaw MGMT   |
  | (Reddit+FetLife)  |     | (Autonomous Agent)|
  |    port 8080      |     |  @HeatherMGMTBot  |
  +-------------------+     +-------------------+
```

| Service | Port | Purpose |
|---------|------|---------|
| llama-server | 1234 | Text generation (llama.cpp with any GGUF model) |
| Ollama | 11434 | Image analysis (LLaVA or similar vision model) |
| ComfyUI | 8188 | Image generation (FLUX.1 dev FP8) with face-swap workflows |
| Chatterbox TTS | 5001 | Voice synthesis (voice cloning) |
| Bot Monitor | 8888 | Web dashboard for analytics and admin |
| Frank Dashboard | 8080 | Multi-platform outreach (Reddit + FetLife auto-funnel) |

## Features

### Core Bot (Telegram)
- **Persona YAML system** — Define your character's identity, backstory, personality, communication style, and sexual boundaries in a single YAML file. Swap personas by pointing to a different file.
- **MTProto userbot** — Appears as a real Telegram user, not a bot. No "bot" label, no command menus.
- **Adaptive kink personas** — 17 kink-specific personality variants (breeding, CNC, BBC, MILF, anal, domme, etc.) that auto-detect each user's primary kink and shift the character's emphasis to match.
- **Per-user memory system** — 4-layer personalization: kink scoring (21 categories), memorable moments, LLM session summaries, and callback prompts. Each user gets a persistent profile that grows over time.
- **Image generation** — FLUX.1 dev FP8 via ComfyUI with runtime LoRA injection, face-swap, and body-accurate NSFW prompts with negative prompt support.
- **Image analysis** — Receives and analyzes user photos via Ollama vision models (LLaVA).
- **Voice messages** — Chatterbox TTS voice cloning for sending voice notes that sound like the character.
- **Story system** — Pre-written story bank (YAML) with 60/40 banked/LLM-generated split, per-user rotation.
- **Video delivery** — Pre-cached video library with offer-and-deliver flow.
- **Tipping system** — Telegram Stars integration via a companion BotFather bot. Tiered access (free/paid/VIP).
- **Post-processing pipeline** — 7-stage filter: strips thinking tags, asterisk actions, bracketed metadata, GLM artifacts, AI denial claims, unprompted AI self-identification, and quote wrapping.
- **Monitoring dashboard** — Real-time Flask dashboard with user analytics, conversation logs, and conversion funnels.
- **Content safety** — CSAM flag-and-review system, blocked user management, admin alerts, gender violation detection.
- **AI disclosure** — Automatic first-message disclosure, bio tag, reality-check responses that own the AI status without breaking character.
- **Re-engagement** — Automatic outreach to inactive users with configurable timing and personalized callbacks based on conversation history.
- **Breeding/CNC injection** — Contextual breeding and CNC fantasy prompts that inject based on keyword detection or conversation energy level.
- **Domme mode** — Detects humiliation/degradation requests and switches to dominant personality overlay.
- **Arousal tracking** — Detects climax/heated/afterglow states and adjusts responses accordingly.

### Kink Persona System
- **17 adaptive personas** — breeding, MILF/age-gap, CNC/rough, cuckold, BBC/size, GFE/intimate, deepthroat/oral, gangbang, voyeur/exhib, domme/mommy, anal, free-use, forced-bi, body-worship, findom, stepfamily, uber-slut
- **Automatic detection** — 21 keyword categories score every user message in real-time
- **Phased injection** — Messages 1-3: warmth (no persona). Messages 4-9: light hints. Messages 10+: full persona injection with specific phrases, session flow, and cuckold integration.
- **Per-user tracking** — Active persona stored in each user's profile JSON with kink name and score
- **Persona definitions** in `heather_kink_personas.yaml` — each persona includes core traits, verbal responses, physical details, session flow, and cuckold/Frank integration

### Frank Dashboard (Multi-Platform Outreach)
- **Reddit + FetLife automation** — Two Playwright browsers manage conversations on both platforms simultaneously
- **Stage-aware Frank AI** — Dolphin 12B generates responses as "Frank" (cuckold husband character) with 3-stage conversation strategy: rapport building → Telegram pitch → post-pitch follow-up
- **Auto-send** — Replies are automatically queued with 2-8 minute random delays (human texting pace). Hold keywords flag risky messages for manual review.
- **Reddit chat layers** — Handles standard chats, regular Requests, and NSFW Additional Requests (all three layers automated)
- **Catch-up replier** — Background task scans database every 2 minutes for conversations where the last message is inbound and auto-replies
- **Unified dashboard** — Web UI at port 8080 with platform badges (R/FL), conversation management, AI suggestion approval, and metrics
- **Transparent digital twin pitch** — Frank sells both "live Heather on Telegram" and "filthy digital twin with zero guardrails" as VIP access

### Autonomous Management (OpenClaw MGMT)
- **Heartbeat monitoring** — 30-minute cycle checks all services, error rates, engagement metrics
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
4. **[Chatterbox TTS](https://github.com/resemble-ai/chatterbox)** — For voice messages (optional)
5. **[Playwright](https://playwright.dev/)** — For Reddit/FetLife dashboard: `playwright install chromium`
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
4. **Clone a voice** — Use Chatterbox TTS to clone your character's voice
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
  postprocess.py                # 7-stage response filter
  user_profiles/                # Per-user JSON profiles (auto-generated)

heather-reddit/                 # Multi-platform outreach dashboard
  app.py                        # FastAPI + background tasks
  ai_frank.py                   # Stage-aware Frank AI
  reddit_monitor.py             # Reddit Playwright automation
  fetlife_monitor.py            # FetLife Playwright automation
  database.py                   # SQLite with platform support
  static/                       # Dashboard frontend
```

## Known Limitations

- **Single-session**: Telethon userbot can only have one active session per account. Running the bot locks out other Telethon scripts using the same session.
- **LLM hallucinations**: Small models (7B-12B) occasionally invent backstory details not in the persona YAML. The post-processing pipeline catches some of these but not all.
- **No conversation-end detection**: The bot doesn't detect when a user has ended the conversation (repeated goodbyes). It will keep replying.
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

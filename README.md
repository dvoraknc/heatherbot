# HeatherBot

**A fully local AI companion chatbot for Telegram.**

HeatherBot is a Telegram userbot (MTProto via Telethon) that runs entirely on your own hardware. No cloud APIs, no OpenAI, no subscriptions. Just a local LLM, a persona YAML file, and a stack of services that make the bot feel like a real person texting.

The thesis: a well-scaffolded 12B parameter model with the right persona engineering, content pipeline, and post-processing can deliver a compelling companion experience that rivals cloud-hosted solutions — while keeping everything private and under your control.

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
               |  HeatherBot Core  |     |    Coqui TTS      |
               |   (Telethon)      |     |   (XTTS v2)       |
               |  + Flask monitor  |     |    port 5001      |
               |    port 8888      |     +-------------------+
               +-------------------+
```

| Service | Port | Purpose |
|---------|------|---------|
| llama-server | 1234 | Text generation (llama.cpp with any GGUF model) |
| Ollama | 11434 | Image analysis (LLaVA or similar vision model) |
| ComfyUI | 8188 | Image generation with face-swap workflows |
| Coqui TTS | 5001 | Voice synthesis (XTTS v2 voice cloning) |
| Bot Monitor | 8888 | Web dashboard for analytics and admin |

## Features

- **Persona YAML system** — Define your character's identity, backstory, personality, communication style, and sexual boundaries in a single YAML file. Swap personas by pointing to a different file.
- **MTProto userbot** — Appears as a real Telegram user, not a bot. No "bot" label, no command menus.
- **Image generation** — ComfyUI integration with face-swap for consistent character photos.
- **Image analysis** — Receives and analyzes user photos via Ollama vision models.
- **Voice messages** — Coqui XTTS v2 voice cloning for sending voice notes.
- **Story system** — Pre-written story bank (YAML) with 60/40 banked/LLM-generated split, per-user rotation.
- **Video delivery** — Pre-cached video library with offer-and-deliver flow.
- **Tipping system** — Optional Telegram Stars integration via a companion BotFather bot.
- **Post-processing pipeline** — Strips AI artifacts, asterisk actions, incomplete sentences, and more.
- **Monitoring dashboard** — Real-time Flask dashboard with user analytics, conversation logs, and conversion funnels.
- **Content safety** — CSAM flag-and-review system, blocked user management, admin alerts.
- **AI disclosure** — Automatic first-message disclosure, `/about` command, reality-check responses that own the AI status.
- **Re-engagement** — Automatic outreach to inactive users with configurable timing.

## Hardware Requirements

**Minimum:**
- 1x GPU with 24GB VRAM (RTX 3090, RTX 4090, etc.)
- 32GB system RAM
- Python 3.10+

**Recommended:**
- 2x GPUs (one for text, one for image generation)
- 64GB system RAM
- SSD for model storage

The bot is designed for consumer hardware. A single RTX 3090 can run the text model, image analysis, and TTS simultaneously. Image generation benefits from a second GPU.

## Prerequisites

Install these before setting up the bot:

1. **[llama.cpp](https://github.com/ggerganov/llama.cpp)** — Download a GGUF model (12B+ recommended) and the `llama-server` binary
2. **[Ollama](https://ollama.ai)** — Install and pull a vision model: `ollama pull llava`
3. **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** — For image generation (optional)
4. **[Coqui TTS](https://github.com/coqui-ai/TTS)** — For voice messages (optional)
5. **Python 3.10+**
6. **Telegram account** — Register API credentials at [my.telegram.org](https://my.telegram.org)

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
- **ai_behavior** — Rules, guardrails, mode behaviors
- **prompts** — The system prompts sent to the LLM (most important section)

### 3. Story Bank (Optional)

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

# Voice (Coqui TTS — optional)
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
```

First run will prompt for your Telegram phone number and verification code. Subsequent runs use the saved session file.

### CLI Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--personality` | `persona_example.yaml` | Persona YAML file path |
| `--monitoring` | off | Enable web dashboard on port 8888 |
| `--small-model` | off | Optimized prompts for 12B models |
| `--text-port` | 1234 | llama-server port |
| `--image-port` | 11434 | Ollama port |
| `--tts-port` | 5001 | Coqui TTS port |
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

## White-Labeling

HeatherBot is designed to be re-skinned. To create a completely different character:

1. **Create a new persona YAML** — Copy `persona_example.yaml`, change everything
2. **Provide your own media** — Photos in `images_db/`, videos in `videos/`
3. **Clone a voice** — Use Coqui XTTS to clone your character's voice
4. **Set up face-swap** — Place your character's face source image for ComfyUI
5. **Update `.env`** — Set `PAYMENT_BOT_USERNAME` to your payment bot's username

The bot code is character-agnostic. All personality comes from the YAML file and media assets.

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

## Known Limitations

- **Single-session**: Telethon userbot can only have one active session per account. Running the bot locks out other Telethon scripts using the same session.
- **LLM hallucinations**: Small models (7B-12B) occasionally invent backstory details not in the persona YAML. The post-processing pipeline catches some of these but not all.
- **No conversation-end detection**: The bot doesn't detect when a user has ended the conversation (repeated goodbyes). It will keep replying.
- **Image generation speed**: ComfyUI face-swap takes 10-30 seconds per image depending on GPU.
- **Voice quality**: Coqui XTTS v2 quality varies. Short utterances work best.

## Disclaimer

This software is intended for creating AI companion chatbots. It can be configured for adult content.

- **You are responsible** for complying with the laws of your jurisdiction regarding adult content, AI-generated media, and automated messaging.
- **AI disclosure is built in** — the bot automatically discloses its AI nature to new users. Do not disable this.
- **Content safety systems** (CSAM flagging, blocked user management) are included and should remain active.
- **This is a local tool** — no data leaves your machine unless you configure it to.

Use responsibly.

## License

MIT License. See [LICENSE](LICENSE) for details.

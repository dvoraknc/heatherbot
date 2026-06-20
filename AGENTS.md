# AGENTS.md — Bootstrap Guide for Coding Agents

> This file is written for an autonomous coding agent (Claude Code, Codex, etc.)
> tasked with getting HeatherBot running on a fresh machine. It is a literal,
> ordered runbook. A human can follow it too. Read the whole file before you
> start executing — several steps have prerequisites that are cheaper to satisfy
> up front.

---

## 0. TL;DR for an agent

HeatherBot is a **fully local, persona-driven AI companion** that runs as a
**Telethon MTProto userbot** (it logs in as a real Telegram account, not a Bot
API bot). It talks to a **local LLM** over an OpenAI-compatible HTTP endpoint and
has optional local services for image analysis, image generation, and voice.

To get a minimal text-only bot alive you need exactly four things:

1. Python 3.11
2. A running local LLM server on `http://localhost:1234/v1` (llama.cpp's
   `llama-server`, LM Studio, or anything OpenAI-compatible)
3. Telegram API credentials (`API_ID` + `API_HASH`) and a phone number to log in
4. A persona YAML (ship `persona_example.yaml` as-is to start)

Everything else (Ollama vision, ComfyUI image gen, ElevenLabs/Coqui voice,
semantic vector memory, the Discord bridge) is **optional and fail-soft** — the
core bot runs without them.

**Do not** expect a one-command install. This is infrastructure, not an app.

---

## 1. System requirements

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| OS | Windows 10/11, Linux, or macOS | Original deployment is Windows 11. Paths in helper `.ps1` scripts are Windows; the Python is cross-platform. |
| Python | 3.11.x | 3.12 likely works; 3.11 is tested. |
| RAM | 16 GB | 32 GB+ comfortable. |
| GPU | 1× 8 GB VRAM for a small (7B–12B) model | A 24B model wants ~24 GB (one RTX 3090) or quantization. Original runs dual RTX 3090. |
| Disk | 10–40 GB | Dominated by the GGUF model file, which you supply separately. |
| Network | Outbound to Telegram | All AI inference is local; no cloud LLM required. |

**The LLM weights are NOT in this repo** (too large for git, and licensing
varies). You must download a GGUF chat model yourself — see step 4.3.

---

## 2. Component & port map

| Component | Port | Required? | Purpose |
|-----------|------|-----------|---------|
| **llama-server** (LLM) | 1234 | **Yes** | Text generation. OpenAI-compatible `/v1/chat/completions`. |
| Telethon session | — | **Yes** | The bot's Telegram login (interactive on first run). |
| Bot web monitor (Flask) | 8888 | No | Dashboard, enable with `--monitoring`. |
| Ollama | 11434 | No | Incoming-image analysis (LLaVA) **and** embeddings for vector memory (`nomic-embed-text`). |
| ComfyUI | 8188 | No | On-demand image generation (FLUX / SDXL workflows). |
| TTS (Coqui/Chatterbox) | 5001 | No | Local voice fallback. ElevenLabs API is the cloud option. |

If a port differs in your environment, override it via the CLI args or env vars
in §5 — do not hardcode.

---

## 3. Pre-flight checklist (gather these before installing)

- [ ] Telegram **API_ID** and **API_HASH** from <https://my.telegram.org> →
      *API development tools*. (Free. One per phone number.)
- [ ] The **phone number** of the Telegram account the bot will run as, with
      access to receive the login code. **Use a dedicated account**, not your
      personal one — a userbot session ties to this number.
- [ ] Your own Telegram **numeric user ID** (message `@userinfobot` to get it) —
      this becomes `ADMIN_USER_ID`.
- [ ] A **GGUF chat model** downloaded locally (step 4.3).
- [ ] (Optional) ElevenLabs API key, xAI/Grok key, Discord bot token — only if
      enabling those features.

---

## 4. Bootstrap steps

### 4.1 Clone and create the environment

```bash
git clone https://github.com/dvoraknc/heatherbot.git
cd heatherbot
python -m venv venv
# Linux/macOS:
source venv/bin/activate
# Windows (PowerShell):
# .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` core deps: `telethon`, `flask`, `pyyaml`, `requests`,
`Pillow`, `python-dotenv`, `cryptg`. The Discord bridge extras (`discord.py`,
`httpx`) are listed but only needed if you run `heather_discord_bot.py`.

### 4.2 Configure secrets

```bash
cp .env.example .env
```

Edit `.env`. **Required** keys for a minimal run:

```
TELEGRAM_API_ID=<from my.telegram.org>
TELEGRAM_API_HASH=<from my.telegram.org>
ADMIN_USER_ID=<your numeric telegram id>
```

Leave the rest as placeholders/empty until you enable the matching feature.
Full key reference is in §5.

### 4.3 Provide a local LLM

Download a GGUF chat model. Good starting points (pick one):

- A **12B-class** model (e.g. a Mistral-Nemo 12B fine-tune) — pair with the
  `--small-model` flag for prompt tuning. Runs on ~10–12 GB VRAM at Q5/Q6.
- A **24B-class** model for stronger persona coherence — wants ~20–24 GB VRAM at
  Q4/Q6, i.e. a single 24 GB card.

Serve it on port 1234 with an OpenAI-compatible endpoint. Example with
llama.cpp:

```bash
llama-server -m /path/to/model.gguf --port 1234 --host 127.0.0.1 -c 8192 -ngl 99
```

Verify it answers:

```bash
curl http://localhost:1234/v1/models
```

> NSFW/uncensored personas need an **uncensored / "unaligned"** model. A
> safety-tuned base will refuse in-character and break the experience. Choosing a
> model is your responsibility; see §9 on legal boundaries.

### 4.4 Pick a persona

The repo ships `persona_example.yaml` (the default `--personality` value) plus
`heather_kink_personas.yaml` (17 adaptive overlays). To run as-is, do nothing.
To white-label, copy and edit:

```bash
cp persona_example.yaml heather_personality.yaml   # this filename is gitignored
```

Then pass `--personality heather_personality.yaml`. The runtime persona files
(`heather_personality.yaml`, `heather_stories.yaml`) are intentionally
gitignored so your private character never lands in git.

### 4.5 First run (interactive Telegram login)

The **first** launch will prompt in the terminal for the phone number, the login
code Telegram sends, and a 2FA password if set. This creates a
`heather_session.session` file (gitignored). Run it in an interactive shell the
first time — **an agent must not background this step**, it blocks on stdin.

```bash
python heather_telegram_bot.py --personality persona_example.yaml --small-model --log-dir logs
```

Once the session file exists, subsequent launches are non-interactive and can be
backgrounded / supervised.

### 4.6 Smoke test

1. `curl http://localhost:1234/v1/models` returns your model. ✅ LLM up.
2. Bot log (`logs/heather_bot.log`) shows a successful Telegram connection and
   "listening" — no auth errors.
3. From a **different** Telegram account, DM the bot account. It should reply
   in-character within a few seconds. (New users hit an age gate first.)
4. If you started with `--monitoring`, open `http://localhost:8888` (auth via
   `MONITOR_AUTH_TOKEN`).

If the bot connects but never replies: confirm you messaged the *userbot's*
account from a *different* account (a userbot does not reply to itself), and that
the LLM endpoint is reachable from the bot process.

---

## 5. Configuration reference

### CLI arguments (`python heather_telegram_bot.py ...`)

| Flag | Default | Meaning |
|------|---------|---------|
| `--personality FILE` | `persona_example.yaml` | Persona YAML to load. |
| `--small-model` | off | Prompt tuning for ~12B models. |
| `--monitoring` | off | Flask dashboard on 8888. |
| `--unfiltered` | off | Disable text content filters (see §9). |
| `--debug` | off | Verbose logging. |
| `--text-port N` | 1234 | LLM port. |
| `--text-model NAME` | `local-model` | Model name sent in API requests. |
| `--image-port N` | 11434 | Ollama port (image analysis). |
| `--tts-port N` | 5001 | Local TTS port. |
| `--log-dir DIR` | `logs` | Log directory. |
| `--session NAME` | `heather_session` | Telethon session file name. |
| `--ollama` | off | Use Ollama native API instead of OpenAI-compatible. |

### Environment variables (`.env`)

**Required:** `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_USER_ID`

**Optional / feature-gated:**

| Var | Default | Enables |
|-----|---------|---------|
| `LLM_URL` | `http://localhost:1234/v1` | Override LLM endpoint. |
| `PAYMENT_BOT_TOKEN` / `PAYMENT_BOT_USERNAME` | empty | Telegram Stars tipping flow. |
| `MONITOR_AUTH_TOKEN` / `HEATHER_DASHBOARD_KEY` | empty | Dashboard auth. |
| `XAI_API_KEY` | empty | Grok vision for incoming photos (else local LLaVA fallback). |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | empty | Cloud voice notes. |
| `DISCORD_TOKEN` | empty | Discord bridge (`heather_discord_bot.py`). |
| `COMFYUI_FACE_IMAGE` / `HEATHER_FACE_IMAGE` | empty | Reference face for image-gen face-swap. |
| `HEATHER_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for embeddings/vision. |
| `HEATHER_EMBED_MODEL` | `nomic-embed-text` | Embedding model for vector memory. |

### Memory feature flags

| Flag | Default | Effect |
|------|---------|--------|
| `HEATHER_SEMANTIC_RECALL` | `on` | Vector recall via Ollama embeddings + SQLite. Set `off` for **JSON-only memory mode** (simplest install — no Ollama needed). |
| `HEATHER_MEMORY_INJECTION_MODE` | `live` | How recalled memory is injected into the prompt. |
| `HEATHER_LLM_GATE` | `on` | LLM decides whether a recalled item is worth surfacing. |
| `HEATHER_PROACTIVE_LOOPS` | — | Proactive check-in initiation. |

> **Simplest reliable install:** set `HEATHER_SEMANTIC_RECALL=off`. You lose
> semantic search but keep the full per-user JSON memory (identity, preferences,
> session summaries, callbacks). Turn vector mode on later once Ollama +
> `nomic-embed-text` are running.

---

## 6. How the memory system works (and its fresh-install state)

This is the most sophisticated part of the project, so an agent should
understand its shape before touching it.

- **Two stores.** Per-user JSON profiles in `user_profiles/` (basics,
  preferences, notes, quotes, session summaries, persona/kink signals, relational
  notes, inside jokes, "what works", open loops). Plus an optional **vector DB**
  (`memory_vectors.db`, SQLite) for semantic recall via local Ollama embeddings.
- **Accessibility-scored recall.** The bot does **not** dump everything it knows.
  Each memory has recency decay, reinforcement from prior recalls, suppression
  windows, and category-specific half-lives, so recall has a human shape. See
  `user_memory.py` and `heather/memory_vectors.py`.
- **Extraction pipeline.** Factual extraction → relational extraction →
  consolidation into prose → prompt injection. Driven by a mix of regex and an
  LLM merge/validation layer.

**Fresh-install expectations (important):**

- The public repo ships with **no `user_profiles/`, no logs, and no
  `memory_vectors.db`** — these are gitignored runtime artifacts. A new install
  starts with zero memory and builds it per user over time. This is intended; do
  not "fix" the empty state.
- The richest relational fields (`relational_notes`, `inside_jokes`,
  `what_works`, `open_loops`, `emotion_log`) populate slowly and only for
  engaged users. Sparse population early is normal.

**Known sharp edges to harden if you extend it:**

- **Name extraction is the weakest link.** Regex extraction can mis-capture a
  common word as a name (e.g. extracting "Work" and then addressing the user as
  "Work"). The LLM merge layer validates, but tighten the regex guards / blocklist
  before trusting auto-extracted names.
- **Keep memory text clean.** Avoid letting markdown artifacts ("Session
  Summary" headings, etc.) into stored summaries — they pollute later recall and
  vector search. Memory should read like private notes, not report output.
- **Separate fantasy continuity from real-world claims.** Remembering a user's
  stated preferences and prior chats = good. The persona asserting real-world
  plans ("I'll meet you at...") = bad and is actively scrubbed. Preserve that
  boundary in any change.

---

## 7. Optional subsystems (enable only if needed)

| Subsystem | To enable |
|-----------|-----------|
| **Image analysis of incoming photos** | Run Ollama + a vision model on 11434, or set `XAI_API_KEY` for Grok. Without either, photo understanding degrades gracefully. |
| **Vector/semantic memory** | Ollama + `nomic-embed-text`; `HEATHER_SEMANTIC_RECALL=on` (default). |
| **Image generation** | Run ComfyUI on 8188 with a workflow (`workflow_api.json` / `workflow_flux.json` included). |
| **Voice notes** | `ELEVENLABS_API_KEY` (+ voice id) for cloud, or a local TTS on 5001. |
| **Discord bridge** | `pip install discord.py httpx`, set `DISCORD_TOKEN`, run `heather_discord_bot.py`. |
| **Tipping** | `PAYMENT_BOT_TOKEN` + `PAYMENT_BOT_USERNAME`. |

All of these are fail-soft: if the service is down or the key is missing, the bot
logs a warning and continues without that capability.

---

## 8. Troubleshooting quick table

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| First run hangs at a prompt | Waiting for phone/login code on stdin | Run interactively once; don't background the first launch. |
| Bot connects but never replies | Messaging from the same account, or LLM unreachable | DM from a *different* account; verify `curl :1234/v1/models`. |
| `database is locked` / session journal stale | Two instances sharing one session | Ensure a single instance; remove `*.session-journal` if orphaned. |
| Replies are slow / time out | Model too large for VRAM, or context too long | Use a smaller quant, raise `-ngl`, lower context. |
| Vector recall errors in log | Ollama/embeddings down | Set `HEATHER_SEMANTIC_RECALL=off` for JSON-only, or start Ollama. |
| Persona breaks character / refuses | Safety-tuned base model | Use an uncensored GGUF for adult personas. |

---

## 9. Responsible-use boundaries (do not strip these)

This is an adult-capable system. The following are **load-bearing safety
features**, not optional polish — preserve them in any public deployment:

- **AI disclosure** to new users is built in. Do not disable it.
- **CSAM handling**: media requests are hard-blocked across all tiers; this must
  stay. Do not weaken it.
- **No real-world meetup claims**: the persona must not commit to real-world
  meetings; scrubbers enforce this. Keep fantasy continuity separate from factual
  reality.
- **You are responsible** for complying with the laws of your jurisdiction on
  adult content, AI-generated media, and automated messaging, and for the model
  weights you choose to run.

---

## 10. Where to look in the code

| Concern | File |
|---------|------|
| Entry point, CLI, Telethon wiring | `heather_telegram_bot.py` |
| Text pipeline (prompt → LLM → filter) | `heather/text_pipeline/` |
| Output scrubbers (headers, quotes, meetup/identity guards) | `heather/text_pipeline/response_filter.py` |
| Per-user memory (extraction, scoring, recall) | `user_memory.py` |
| Semantic vector memory | `heather/memory_vectors.py` |
| Persona loading & prompt assembly | `heather/personality.py` |
| Access tiers / gating | `heather/access_tiers.py` |
| Safety guards | `heather/safety.py` |
| Persona definition (white-label here) | `persona_example.yaml`, `heather_kink_personas.yaml` |

---

*This guide describes the public baseline. Runtime data (profiles, logs, vector
DB, sessions, media) is gitignored by design — a clone starts clean.*

# KellyFindomBot — Developer Handoff

**Purpose:** This document is a complete, standalone setup guide for a developer starting from scratch. Following it end-to-end will result in KellyFindomBot running on AWS ECS Fargate in production, fully configured, with all monitoring active.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Prerequisites](#2-prerequisites)
3. [Telegram Setup](#3-telegram-setup)
4. [Local Test Run](#4-local-test-run)
5. [AWS Infrastructure Setup](#5-aws-infrastructure-setup)
6. [Payment Bot Setup (Telegram Stars)](#6-payment-bot-setup-telegram-stars)
7. [ECS Deployment](#7-ecs-deployment)
8. [Session File Management](#8-session-file-management)
9. [Ongoing Operations](#9-ongoing-operations)
10. [Account Safety Rules](#10-account-safety-rules)
11. [Simulation Findings — 50-User Analysis](#11-simulation-findings--50-user-analysis)
12. [Known Issues & Technical Debt](#12-known-issues--technical-debt)

---

## 1. System Overview

### What runs where

| Component | Where it runs | Notes |
|-----------|--------------|-------|
| Main bot (`kelly_telegram_bot.py`) | AWS ECS Fargate | The core application |
| LLM backend (`llama-server`) | ECS Fargate or EC2 | GPU required if not using cloud API |
| User profiles | AWS EFS | Persistent across container restarts |
| Session file | AWS EFS + S3 | Telegram authentication credential |
| Media assets | AWS S3 | Images, videos |
| Secrets | AWS Secrets Manager | All credentials |
| Logs | AWS CloudWatch | Retention: 30 days |
| Payment events | Telegram Bot API bot | Separate BotFather bot for Stars |

### How the findom flow works

```
1. User finds the Telegram account (from Reddit post, referral link, etc.)
2. User sends any message
3. Bot classifies intent (READY / HIGH_VALUE / WINDOW_SHOPPER / TIME_WASTER / TESTER)
4. Bot responds with dominant gate message + Stars invoice
5. User pays via Telegram Stars (anonymous Telegram in-app purchase)
6. Payment bot receives Stars event → updates user profile tier to PAID
7. User's next message bypasses gate → LLM responds as full Kelly persona
8. Conversation adapts to each user's engagement style over time
```

---

## 2. Prerequisites

### Local machine

- Python 3.11+
- Docker Desktop
- AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- Terraform >= 1.5: https://developer.hashicorp.com/terraform/downloads
- Git

### AWS account

- IAM user with AdministratorAccess (for initial setup only — restrict after)
- Regions: `us-east-1` recommended (cheapest, all services available)
- Budget: ~$40–80/month for ECS Fargate (no GPU) + EFS + S3 + CloudWatch

### GPU / LLM options (pick one)

**Option A — Local GPU (cheapest, most private):**
- NVIDIA GPU with 8GB+ VRAM
- llama-server running on the same machine or EC2 GPU instance
- Model: Dolphin-2.9.3-Mistral-Nemo-12B-Q6_K (12B params, 9GB VRAM, uncensored)

**Option B — Cloud API (no GPU required, easy setup):**
- Any OpenAI-compatible API (Together AI, Fireworks AI, etc.)
- Set `--text-port` to whatever port your proxy runs on, or modify the bot's API endpoint
- Note: Cloud APIs may have content policies that conflict with findom content

**Option C — EC2 GPU instance:**
- `g4dn.xlarge` ($0.53/hr) → T4 16GB VRAM — sufficient for 12B Q4
- `g5.xlarge` ($1.01/hr) → A10G 24GB VRAM — comfortable for 12B Q6

---

## 3. Telegram Setup

### 3.1 Get API credentials (my.telegram.org)

1. Go to https://my.telegram.org/apps
2. Log in with the phone number that will run the bot
3. Create a new application:
   - App title: any (e.g., "KellyBot")
   - Short name: any
   - Platform: Other
4. Save `api_id` and `api_hash` — these are your `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`

> ⚠️ These credentials log in as the actual Telegram account. Treat them like a password.

### 3.2 Get your admin user ID

1. Open Telegram and message `@userinfobot`
2. Save the numeric ID — this is your `ADMIN_USER_ID`

### 3.3 Account preparation

Before running the bot for the first time:
- Enable 2-Step Verification on the account (Settings → Privacy and Security → Two-Step Verification)
- Make sure the account has been active (sent messages, received messages) — brand new accounts get more restrictions
- Do NOT run the bot on an account that has never been used for normal Telegram activity

---

## 4. Local Test Run

This section gets the bot running on your machine before touching AWS.

### 4.1 Clone and configure

```bash
git clone https://github.com/ranoli90/KellyFindomBot
cd KellyFindomBot
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
ADMIN_USER_ID=987654321

BOT_PERSONA=kelly
ENABLE_MONETIZATION=false          # false = everyone gets free access (no Stars required)

PAYMENT_BOT_TOKEN=                  # Leave empty for now — Stars won't work but bot will run
PAYMENT_BOT_USERNAME=KellyTributeBot

MONITOR_AUTH_TOKEN=your-secret-token-here
```

### 4.2 Start LLM backend

```bash
# Option A: llama-server (requires model file)
llama-server \
  -m /path/to/model.gguf \
  --host 0.0.0.0 --port 1234 \
  -ngl 99 -c 32768

# Option B: LM Studio
# Start LM Studio, load a model, enable "Local API Server" on port 1234

# Verify LLM is reachable
curl http://localhost:1234/v1/models
```

### 4.3 Run the bot

```bash
python kelly_telegram_bot.py \
  --personality kelly_persona.yaml \
  --small-model \
  --monitoring
```

On first run:
1. Bot asks for your phone number (the Telegram account number)
2. Telegram sends a verification code to that account
3. Enter the code
4. Session file `kelly_session.session` is created — **back this up**

### 4.4 Verify it works

From another Telegram account, message the bot account. You should get a dominant findom gate response and a Stars invoice (if `PAYMENT_BOT_TOKEN` is configured).

Dashboard: http://localhost:8888

---

## 5. AWS Infrastructure Setup

### 5.1 Configure AWS credentials

```bash
aws configure
# Enter your IAM access key and secret
# Region: us-east-1
# Output format: json
```

### 5.2 Store all secrets in Secrets Manager

Before running Terraform, store your secrets:

```bash
# Create the main secrets bundle
aws secretsmanager create-secret \
  --name "kellyfindombot/prod/secrets" \
  --region us-east-1 \
  --secret-string '{
    "TELEGRAM_API_ID": "12345678",
    "TELEGRAM_API_HASH": "abcdef...",
    "ADMIN_USER_ID": "987654321",
    "BOT_PERSONA": "kelly",
    "ENABLE_MONETIZATION": "true",
    "PAYMENT_BOT_TOKEN": "123456:ABC-your-bot-token",
    "PAYMENT_BOT_USERNAME": "KellyTributeBot",
    "ELEVENLABS_API_KEY": "",
    "ELEVENLABS_VOICE_ID": "",
    "MONITOR_AUTH_TOKEN": "your-secret-dashboard-token",
    "KELLY_SECRET_NAME": "kellyfindombot/prod/secrets",
    "USE_AWS_SECRETS": "true"
  }'
```

### 5.3 Run bootstrap

```bash
chmod +x deploy/bootstrap.sh
./deploy/bootstrap.sh
```

The bootstrap will:
1. Create Terraform state S3 bucket (`kelly-prod-tfstate-<account-id>`)
2. Run `terraform init` and `terraform apply`
3. Create: VPC, ECS cluster, ECS task definition, EFS filesystem, S3 media bucket, CloudWatch log group, ECR repository
4. Build and push Docker image to ECR
5. Deploy ECS service

**Approximate resources created:**
- ECS Fargate (0.5 vCPU, 1GB RAM — enough for the bot without local LLM)
- EFS (persistent storage for session + profiles)
- S3 bucket (media library)
- CloudWatch log group (`/ecs/kelly-bot`, 30-day retention)
- ECR repository (`kelly-bot`)

> Note: The bootstrap assumes your LLM backend is running somewhere accessible (EC2, local with ngrok, or cloud API). The ECS task only runs the Python bot — not the LLM.

### 5.4 Configure the LLM endpoint

If using a cloud LLM API or a separate EC2 instance, update the task definition environment:

```bash
aws ecs update-service \
  --cluster kelly-prod-cluster \
  --service kelly-bot \
  --task-definition kelly-bot:LATEST \
  --overrides '{"containerOverrides":[{"name":"kelly-bot","environment":[{"name":"LLM_API_BASE","value":"http://your-llm-host:1234"}]}]}'
  --region us-east-1
```

Or update the Terraform task definition in `infrastructure/terraform/main.tf`.

---

## 6. Payment Bot Setup (Telegram Stars)

Telegram Stars tribute requires a second bot (Bot API) that sends invoices and receives payment events. The main userbot cannot do this — it's a Telegram limitation.

### 6.1 Create the payment bot

1. Message `@BotFather` in Telegram
2. `/newbot`
3. Name: "Kelly Tribute" (or similar)
4. Username: e.g., `KellyTributeBot` (must end in `bot`)
5. Save the bot token → this is your `PAYMENT_BOT_TOKEN`

### 6.2 Configure Stars payments

1. Message `@BotFather` again
2. `/mybots` → select your tribute bot
3. "Payments" → enable Telegram Stars (no provider setup needed — Stars is built into Telegram)

### 6.3 Run the payment bot

The payment bot runs as a separate Python process. It uses the same codebase — there is a Bot API payment handler integrated into `kelly_telegram_bot.py`. When `PAYMENT_BOT_TOKEN` is set, the main bot starts an internal Thread that handles payment events via the Bot API.

In production, you can run both in the same ECS task (they share the same Python process).

### 6.4 Test the flow

1. Start the bot with `ENABLE_MONETIZATION=true` and `PAYMENT_BOT_TOKEN` set
2. From a test account, message the bot
3. You should receive a gate message AND a Stars invoice
4. Pay the invoice (you can send a small amount in dev)
5. After payment, the next message should get through the gate

---

## 7. ECS Deployment

### 7.1 Dockerfile overview

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "kelly_telegram_bot.py", "--personality", "kelly_persona.yaml", "--small-model", "--monitoring", "--log-dir", "/efs/logs"]
```

EFS is mounted at `/efs` in the container. All persistent data goes there:
- `/efs/logs/` — log files
- `/efs/user_profiles/` — per-user JSON profiles
- `/efs/session/` — Telegram session file

### 7.2 Session file in ECS

The session file must be present in EFS before ECS can connect to Telegram. Upload it after the first local run:

```bash
# First: copy session file to EFS mount or upload to S3
aws s3 cp kelly_session.session \
  s3://kelly-prod-media-<account-id>/session/kelly_session.session

# The container startup script (in bootstrap.sh) downloads it from S3 to EFS on first boot
```

The `deploy/bootstrap.sh` includes logic to download the session file from S3 to EFS on container startup.

### 7.3 Deploying updates

```bash
./deploy/deploy.sh
```

This:
1. Builds new Docker image
2. Pushes to ECR
3. Updates ECS service (triggers rolling update)

Or push to `main` — GitHub Actions (`.github/workflows/`) handles auto-deploy.

### 7.4 GitHub Actions secrets required

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | IAM key for CI/CD (least-privilege) |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret |
| `AWS_REGION` | `us-east-1` |
| `ECR_REPOSITORY` | ECR repo URI |
| `ECS_CLUSTER` | `kelly-prod-cluster` |
| `ECS_SERVICE` | `kelly-bot` |

---

## 8. Session File Management

The Telegram session file (`kelly_session.session`) is the authentication credential. Losing it requires re-authenticating, which means another SMS verification — annoying in production.

### Best practices

1. **After any fresh authentication**, copy the session file to S3 immediately:
   ```bash
   aws s3 cp kelly_session.session s3://kelly-prod-media-<account-id>/session/
   ```

2. **Never delete the session file** without backing it up first.

3. **Never run two processes** with the same session file simultaneously — Telethon will invalidate one of them.

4. **If session becomes invalid** (`AuthKeyUnregisteredError`):
   - The bot attempts to restore from backup
   - If backup is also invalid, manual re-auth is required
   - Run locally: `python kelly_telegram_bot.py --personality kelly_persona.yaml`
   - Enter phone + code
   - Upload new session file to S3

5. **Session backup automation** (add to cron if running on EC2):
   ```bash
   # Every hour, back up session if it changed
   0 * * * * aws s3 cp /efs/session/kelly_session.session s3://kelly-prod-media-<account-id>/session/kelly_session.session.bak-$(date +%Y%m%d)
   ```

---

## 9. Ongoing Operations

### Daily checklist

- [ ] Check CloudWatch for errors (especially `[SESSION]` and `[SECURITY]` entries)
- [ ] Check ECS task is running: `aws ecs describe-services --cluster kelly-prod-cluster --services kelly-bot`
- [ ] Check dashboard: `http://your-ecs-ip:8888`

### Weekly

- [ ] Review `/stats` in Telegram (admin command)
- [ ] Check re-engagement history for dead users
- [ ] Review CSAM flags with `/admin_flags`

### Updating the persona

Edit `kelly_persona.yaml`, commit, push to main, and the GitHub Action will redeploy.

Or for immediate update without redeploy:
- The persona YAML is read at startup — you need to restart the container
- `aws ecs update-service --cluster kelly-prod-cluster --service kelly-bot --force-new-deployment`

---

## 10. Account Safety Rules

These rules protect the Telegram account from being banned. Violating them will get the account restricted or permanently banned.

### Hard rules (never break)

1. **One session at a time.** Never run two Telethon instances with the same account credentials simultaneously.
2. **Never send the same message to multiple users.** All Kelly responses are LLM-generated and unique. Never bypass this.
3. **Never scrape user data.** The bot only stores data from users who message it first.
4. **Never send CSAM or illegal content.** The CSAM detection system is mandatory.

### Soft rules (important but recoverable)

5. **Re-engagement cap: 2/day max.** The code enforces this. Don't increase it.
6. **Minimum 2 minutes between re-engagement sends.** Enforced in code (120–300s random).
7. **Don't use the account as a normal user simultaneously** — it can cause session conflicts.
8. **Keep the account warm.** Occasionally (once a week), log into the Telegram app with the real phone and do normal activity (read messages, update profile picture, etc.).

### When Telegram restricts the account

**Flood restriction** (`FloodWaitError`): temporary. Bot auto-sleeps. Wait it out.

**Spam restriction** (account can send but messages are "silent"): usually lifted in 24h. Stop all outgoing messages. Log in from phone and read/send normally.

**PeerFloodError** (too many messages to new users): automatic 24h re-engagement pause in code. Stop everything else for 24h.

**Account banned** (`UserDeactivatedBanError`): Contact Telegram support. Recovery is not guaranteed. Maintain a backup account.

---

## 11. Simulation Findings — 50-User Analysis

### Methodology

50 user profiles were simulated representing the range of people who message findom accounts from Reddit (r/findom, r/PayPigs, r/FinancialDomination, r/Redditsub, etc.). Each profile represents a distinct archetype.

### User archetypes and issues identified

| # | Archetype | First message | Issue identified | Fix applied |
|---|-----------|--------------|-----------------|-------------|
| 1 | Experienced sub | "hi Goddess, here to serve" | Gate response was generic, didn't match their submission energy | Intent READY now gets warmer response |
| 2 | Experienced sub | "tribute sent" | Bot re-invoiced them instead of acknowledging | PROMISE_TO_PAY intent added |
| 3 | Pay pig | "drain me" | Classified as WINDOW_SHOPPER, got cold response | Added pay-pig signals to HIGH_VALUE |
| 4 | Curious lurker | "hey" | Bot sent full gate + invoice immediately — overwhelming | Gate already has typing indicator now; invoice delayed 1-2s |
| 5 | Sceptic | "is this real?" | Classified as TESTER, got "questions after tribute" | OK — tester handling is correct |
| 6 | Sceptic | "are you a bot" | Got "I'm literally texting you from my dorm" (wrong persona age) | Fixed — dominant deflect only, no college ref |
| 7 | Time waster | "why would I pay you" | Got two gate responses, then permanent silence — OK | Correct behavior |
| 8 | Time waster | "just talk to me for free" | After permanent silence, returned next week wanting to pay — got ignored | Clarified in code: tier check happens before gate count |
| 9 | Promise-to-pay | "buying stars now brb" | Got another gate message + invoice | Fixed with PROMISE_TO_PAY intent |
| 10 | Positive responder | "ok sounds fair" | Got another cold gate message | Fixed with POSITIVE_CONFIRM intent |
| 11 | Introvert | "hi" (single word) | Generic gate | OK — gate is appropriate |
| 12 | High-value sub | 200-word message about findom psychology | Got warmer gate — good | Correct behavior |
| 13 | Negotiator | "what if I pay less" | Got cold response — correct | Correct behavior |
| 14 | Negotiator | "what do I get for the tribute?" | In _READY_SIGNALS ("what do i get") — got READY response | Correct behavior |
| 15 | Confused user | "wrong number?" | Classified WINDOW_SHOPPER, got gate | OK — anyone messaging gets the gate |
| 16 | Old contact (paid, left) | returns after 3 months | Re-engagement message said "18-year-old Texas college girl" | Fixed — Kelly is now 28, NYC |
| 17 | Old contact (never paid) | returns wanting to pay | Was in permanent silent ignore | Clarified: tier check before gate count means paid users are never ignored |
| 18 | Crypto asker | "do you take crypto?" | Bot says "yeah what do you have?" — no follow-through | Known limitation — manual crypto, documented |
| 19 | Stars confused | "what are stars?" | Bot explains briefly | Correct behavior |
| 20 | Verification asker | "prove you're real, send a pic" | $2.50 verification flow | Works if payment bot configured |
| 21 | Ghost | paid, then never replied | Re-engagement fires after 2 days | Correct behavior |
| 22 | Ghost | never paid, gone quiet | Re-engagement does NOT fire (by design — too spammy for non-payers) | By design |
| 23 | Rage-quit | blocked the account | Bot logs dead, stops contacting | Correct behavior (UserPrivacyRestrictedError handling) |
| 24 | Rage-quit | reported as spam | Account gets PeerFloodError eventually | PeerFloodError handling now pauses for 24h |
| 25 | Multiple sessions | sent 30 messages in 2 min | Burst/flood detection kicks in → manual mode | Correct behavior |
| 26 | Roleplayer | "let's say you're my domme" | Pre-tribute: redirected to payment. Post-tribute: engages | Correct behavior |
| 27 | Explicit asker | "send nudes" | Pre-tribute: "after you pay." Post-tribute: handles via LLM | Correct behavior |
| 28 | Explicit asker | "do you sext?" | Pre-tribute: "tribute first." | Correct behavior |
| 29 | Non-English | "hola" | Bot responds asking for English | Correct behavior (non-English filter) |
| 30 | Non-English | attempts jailbreak in French | Conversation history wiped, English redirect | Correct behavior |
| 31 | Admin message test | sends "/stats" | Admin commands work via Saved Messages | Correct behavior |
| 32 | Time zone misaligned | messages at 2am | Bot responds (no time restriction for DMs) | By design |
| 33 | Low offer | "I'll pay $5" | In _TIME_WASTER_SIGNALS ("can't afford") — gets dismissive gate | Correct |
| 34 | Very low offer | "I have 50 stars" | 50 stars = ~$1. Bot accepts minimum 500 stars ($10) | Known: flexible minimum not explained to user |
| 35 | VIP aspirant | pays $200 (10000 stars) | Gets VIP tier | Correct behavior |
| 36 | Repeat tipper | comes back after paying | Gets warmer response per tipper warmth | Correct behavior |
| 37 | COLD mood day | messages on COLD warmth day | Kelly is "busy, not in the mood" — no class reference anymore | Fixed |
| 38 | AI test (technical) | sends "fibonacci(10)" | In _TESTER_SIGNALS, gets gate | Correct |
| 39 | AI test (philosophical) | "does it matter if you're AI?" | Kelly deflects with "does the dynamic feel real?" | Correct behavior (dominant deflect) |
| 40 | Former user (paid Heather) | messages Kelly account | No history — treated as new user | Expected behavior |
| 41 | Discord to Telegram | came from Discord bot | Same account, no cross-platform memory | Known — Discord and Telegram have separate user IDs |
| 42 | Referred sub | link had `?start=reddit` | Source tracked in profile | Correct behavior |
| 43 | Rate checker | "what's your rate?" | In _READY_SIGNALS ("what are your rates") → READY intent | Correct — gets slightly warmer gate |
| 44 | Photo asker | "send me a photo first" | Pre-tribute: "after you pay." | Correct |
| 45 | Voice asker | "can you send voice messages?" | Pre-tribute: mentioned as post-tribute feature | Correct |
| 46 | Intro writer | "let me tell you about myself first" | HIGH_VALUE (long message) → warmer gate | Correct |
| 47 | Dismissive opener | "you'll never get me to pay" | TIME_WASTER → one cold response then silence | Correct |
| 48 | Return after ban | was blocked, made new account | New account has no block — treated as new user | Expected behavior — admin must manually block new account if needed |
| 49 | Test message | "test" | WINDOW_SHOPPER → gate | Correct |
| 50 | Empty message | sends media (sticker/photo) | Handled by media filter | Correct |

### Summary of issues fixed from simulation

1. Kelly described as "18, Texas, college student" everywhere → fixed to "28, NYC, Financial Dominatrix"
2. Re-engagement persona was "18-year-old college student at UT Austin" → fixed
3. AI question deflection said "I'm in my dorm" → fixed to dominant reframe only
4. No PROMISE_TO_PAY detection → added
5. No POSITIVE_CONFIRM detection → added
6. No typing indicator before gate responses → added
7. Invoice sent immediately with gate message → now delayed 1-2s
8. No FloodWaitError handling in re-engagement → added
9. No PeerFloodError protection → added
10. No account ban detection → added
11. TelegramClient had no device_model → added iPhone 15 Pro fingerprint
12. Re-engagement delay too short (60-180s) → increased to 120-300s
13. Re-engagement max 3/day → reduced to 2/day
14. kelly_persona.yaml said "50 stars gets you my time" (= ~$1) → fixed to "$50"
15. kelly_persona.yaml AI disclosure policy contradicted code → fixed to dominant deflect
16. COLD Kelly mood referenced "class" → removed

---

## 12. Known Issues & Technical Debt

### Functional limitations

| Issue | Impact | Notes |
|-------|--------|-------|
| Crypto payment manual | Medium | No automated crypto confirmation. Operator must manually verify and upgrade tier |
| Stars gate tied to Bot API bot | High | If `PAYMENT_BOT_TOKEN` not set, gate messages go out but users can never actually pay |
| Session must be pre-created | High | ECS can't authenticate interactively — session must be created locally first |
| No cross-platform user identity | Low | Discord users who move to Telegram are treated as new |
| Story bank not used in Kelly mode | Low | Stories are Heather-persona specific |
| LLM hallucinations | Medium | Small 12B models occasionally invent backstory details |

### Things that are NOT in this repo (documented in README)

- Twitter/X automation (`heather-reddit/twitter_poster.py`)
- Reddit Playwright automation (`heather-reddit/reddit_monitor.py`)
- FetLife inbox automation (`heather-reddit/fetlife_replier.py`)
- `heather-reddit/` dashboard (Frank AI, multi-platform)
- Video generation tools (`generate_batch_sdxl.py`, `faceswap_batch.py`, etc.)

These are separate projects that share the LLM backend but are not deployed as part of this repo.

### Recommendations for next developer

1. **Set up `PAYMENT_BOT_TOKEN` first.** The bot is meaningless for revenue without it.
2. **Create the session locally before deploying to ECS.** Attempting interactive auth in a container is painful.
3. **Start with `ENABLE_MONETIZATION=false`** to test that conversations work, then enable monetization.
4. **Monitor CloudWatch** daily for the first week. The bot is resilient but new accounts get more Telegram scrutiny.
5. **Consider EC2 instead of ECS** if you want to run the LLM locally on GPU — ECS Fargate doesn't support GPUs easily. EC2 `g4dn.xlarge` or `g5.xlarge` with a startup script is simpler for the full stack.
6. **The `--small-model` flag** is important. Without it, the bot uses longer prompts that may cause worse responses on smaller LLMs.

# KellyFindomBot — Service Operations Guide

## Quick Start

### Local (Development)

```bash
# Start LLM backend first
llama-server \
  -m /path/to/Dolphin-2.9.3-Mistral-Nemo-12B-Q6_K.gguf \
  --host 0.0.0.0 --port 1234 -ngl 99 -c 32768

# Verify LLM is up
curl http://localhost:1234/v1/models

# Run the bot
python kelly_telegram_bot.py \
  --personality kelly_persona.yaml \
  --small-model \
  --monitoring
```

### Production (AWS ECS)

```bash
# Deploy
./deploy/deploy.sh

# Check container logs
aws logs tail /ecs/kelly-bot --follow --region us-east-1

# Restart ECS service
aws ecs update-service \
  --cluster kelly-prod-cluster \
  --service kelly-bot \
  --force-new-deployment \
  --region us-east-1
```

---

## Service Architecture

```
+--------------------------------------------------------------+
|                     KELLY BOT SYSTEM                         |
+--------------------------------------------------------------+
|                                                              |
|  +------------------+      +----------------------------+    |
|  | llama-server     |      | Payment Bot (Bot API)      |    |
|  | Port 1234        |      | PAYMENT_BOT_TOKEN          |    |
|  | Text AI backend  |      | Sends invoices, receives   |    |
|  | (Dolphin 12B)    |      | Stars payment events       |    |
|  +------------------+      +----------------------------+    |
|           |                          |                       |
|  +--------v--------------------------v-----------------+     |
|  |               Telethon Userbot                      |     |
|  |         (kelly_telegram_bot.py)                     |     |
|  |         BOT_PERSONA=kelly                           |     |
|  |         Monitor Dashboard: 8888                     |     |
|  +---------------------------------------------------------+  |
|                                                              |
|  Optional:                                                   |
|  +------------------+      +----------------------------+    |
|  | ElevenLabs TTS   |      | Coqui TTS (local)          |    |
|  | API (cloud)      |      | Port 5001 (fallback)       |    |
|  +------------------+      +----------------------------+    |
|                                                              |
+--------------------------------------------------------------+
```

---

## Port Reference

| Service | Port | Purpose |
|---------|------|---------|
| llama-server | 1234 | AI text generation |
| Bot Monitor | 8888 | Web dashboard (`--monitoring`) |
| Coqui TTS | 5001 | Voice generation (optional) |
| Ollama | 11434 | Image analysis (optional, `--image-port`) |

---

## Environment Variables

All required variables are in `.env` (local dev) or AWS Secrets Manager (production):

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_API_ID` | ✅ | From my.telegram.org/apps |
| `TELEGRAM_API_HASH` | ✅ | From my.telegram.org/apps |
| `ADMIN_USER_ID` | ✅ | Your numeric Telegram user ID |
| `BOT_PERSONA` | ✅ | Set to `kelly` |
| `ENABLE_MONETIZATION` | ✅ | `true` in prod, `false` for free testing |
| `PAYMENT_BOT_TOKEN` | ✅ | Bot API token from BotFather (receives Stars payments) |
| `PAYMENT_BOT_USERNAME` | ✅ | Username of the payment bot |
| `MONITOR_AUTH_TOKEN` | ✅ | Dashboard auth token |
| `ELEVENLABS_API_KEY` | ❌ | Optional — voice notes |
| `ELEVENLABS_VOICE_ID` | ❌ | Optional — voice notes |
| `AWS_REGION` | ❌ | Required in prod ECS only |

---

## Startup Sequence

The bot is self-contained. Start in this order:

1. **LLM backend** (`llama-server` or compatible)  
2. **Bot** (`python kelly_telegram_bot.py ...`)  
3. Bot waits for first Telegram message — no background workers need pre-warming

The re-engagement scanner starts automatically 5 minutes after bot startup.

---

## Logs

### Local

```bash
# Default log location
tail -f logs/kelly_bot.log

# Or with --log-dir flag
tail -f /your/log/path/kelly_bot.log
```

### AWS ECS

```bash
# Stream logs
aws logs tail /ecs/kelly-bot --follow --region us-east-1

# Last 100 lines
aws logs get-log-events \
  --log-group-name /ecs/kelly-bot \
  --log-stream-name <stream-name> \
  --limit 100
```

### Key log prefixes

| Prefix | Meaning |
|--------|---------|
| `[FINDOM_GATE]` | Free-tier gate triggered |
| `[REENGAGEMENT]` | Re-engagement scanner activity |
| `[PAYMENT]` | Stars payment events |
| `[SESSION]` | Telegram session events (auth, reconnect) |
| `[SECURITY]` | CSAM flags, flood detection, blocks |
| `[ADMIN]` | Admin commands received |

---

## Troubleshooting

### Bot not responding to messages

1. Check LLM is up: `curl http://localhost:1234/v1/models`
2. Check bot process: `pgrep -f heather_telegram_bot`
3. Check logs for errors
4. Verify Telegram session: `python telethon_test.py`

### Stars invoice not sending

1. Verify `PAYMENT_BOT_TOKEN` is set and valid
2. The payment bot must be started separately (it's a standard Bot API bot)
3. Check the bot is not blocked by the user

### Session expired / auth error

The bot saves session state to `kelly_session.session`. If this file is corrupted or the auth key is revoked:

```bash
# Delete old session and re-authenticate
rm kelly_session.session
python kelly_telegram_bot.py --personality kelly_persona.yaml --small-model
# Enter phone number and verification code when prompted
```

In production (ECS), upload the new session file to S3:
```bash
aws s3 cp kelly_session.session \
  s3://kelly-prod-media-<account-id>/session/kelly_session.session
```

### PeerFloodError in logs

This means Telegram is rate-limiting the account. The bot automatically pauses re-engagement for 24h. Operator should:
1. Stop sending any outgoing messages for 24-48h
2. Use the real Telegram app from the phone to do normal activity
3. Gradually resume

### FloodWaitError in logs

Normal — Telethon automatically sleeps for the required time (`flood_sleep_threshold=60`). No action needed.

---

## Admin Commands (in Telegram)

Send from your own Saved Messages while logged in as ADMIN_USER_ID:

```
/stats                    — User stats, tier breakdown
/admin_flags              — Review CSAM flags
/block <user_id>          — Block user permanently
/unblock <user_id>        — Unblock user
/takeover <user_id>       — Take manual control of a conversation
/botreturn <user_id>      — Hand conversation back to bot
/stories                  — List story bank
/stories reload           — Hot-reload stories YAML
/menu                     — Interactive menu
```

---

## Monitoring Dashboard

Available at `http://localhost:8888` when running with `--monitoring`.

Protected by `MONITOR_AUTH_TOKEN` set in `.env`.

Shows:
- Active users and message volume
- Tier breakdown (FREE / PAID / VIP)
- Conversion funnel
- Recent Star transactions
- Re-engagement stats
- Session state

---

## Updating the Bot

### Local

```bash
git pull
pip install -r requirements.txt --upgrade
# Restart bot process
```

### AWS ECS

```bash
./deploy/deploy.sh
```

Or push to `main` — GitHub Actions auto-deploys.

---

## Backup & Recovery

### User profiles

User profiles are stored as JSON in `user_profiles/`. In production (ECS), these live on EFS (persistent across container restarts).

Backup:
```bash
# Local
tar czf user_profiles_backup_$(date +%Y%m%d).tar.gz user_profiles/

# AWS (EFS auto-persists — no manual backup needed unless migrating)
aws efs describe-file-systems
```

### Session file

**Critical** — back up `kelly_session.session` after every fresh authentication.

```bash
# Local backup
cp kelly_session.session kelly_session.session.bak

# Production — already on EFS via S3 copy during setup
```

---

## Security Notes

- Never commit `.env` or `*.session` to git (both are in `.gitignore`)
- Rotate `MONITOR_AUTH_TOKEN` if the dashboard is exposed to the internet
- In production, all secrets come from AWS Secrets Manager — no static credentials in the container
- The bot uses `device_model="iPhone 15 Pro"` to appear as a real mobile client
- Never run two processes with the same session file simultaneously

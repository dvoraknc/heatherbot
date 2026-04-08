# Heather Bot - Service Management Guide

## Quick Start Options

### Option 1: Double-Click (Easiest)
```
C:\Users\groot\heather-bot\
  START_HEATHER.bat     -- Double-click to start everything
  STOP_HEATHER.bat      -- Double-click to stop everything
  HEATHER_MANAGER.bat   -- Double-click for full control menu
```

### Option 2: PowerShell Commands
```powershell
cd C:\Users\groot\heather-bot

# Interactive menu
.\heather_services.ps1

# Direct commands
.\heather_services.ps1 -Action startall
.\heather_services.ps1 -Action stopall
.\heather_services.ps1 -Action status

# Individual services (llama, ollama, bot, comfyui, tts)
.\heather_services.ps1 -Action start -Service llama
.\heather_services.ps1 -Action start -Service bot
.\heather_services.ps1 -Action restart -Service tts
```

### Option 3: Clean Bot Restart
```powershell
.\restart_bot.ps1
```
Kills existing bot process, cleans stale Telethon session journal, starts fresh.

## Service Architecture

```
+--------------------------------------------------------------+
|                    HEATHER BOT SYSTEM                        |
+--------------------------------------------------------------+
|  +--------------+  +--------------+  +--------------+        |
|  |   Ollama     |  | llama-server |  |   ComfyUI    |        |
|  |  (LLaVA)     |  |  (Dolphin    |  |  (RealVisXL  |        |
|  | Port 11434   |  |   2.9.3)     |  |   Lightning) |        |
|  | Image AI     |  | Port 1234    |  | Port 8188    |        |
|  | (fallback)   |  | Text AI      |  | Image Gen    |        |
|  +--------------+  +--------------+  +--------------+        |
|                          |                                    |
|  +--------------+        |          +--------------+          |
|  | Falconsai    |        |          |  Coqui TTS   |          |
|  | ViT NSFW     |        |          | Port 5001    |          |
|  +--------------+        |          +--------------+          |
|                          |                                    |
|         +----------------v-----------------+                  |
|         |         Heather Bot              |                  |
|         |    (Python / Telethon userbot)   |                  |
|         |    Monitor dashboard: 8888       |                  |
|         +----------------------------------+                  |
+--------------------------------------------------------------+
```

## Port Reference

| Service | Port | URL | Purpose |
|---------|------|-----|---------|
| llama-server | 1234 | http://localhost:1234 | AI text generation (Dolphin 2.9.3) |
| Coqui TTS | 5001 | http://localhost:5001 | Voice note generation |
| Reddit Dashboard | 8080 | http://localhost:8080 | Reddit/FetLife chat UI |
| ComfyUI | 8188 | http://localhost:8188 | AI image generation |
| Bot Monitor | 8888 | http://localhost:8888 | Bot web dashboard |
| Ollama | 11434 | http://localhost:11434 | Image analysis (fallback) |
| Openclaw | 18789 | http://localhost:18789 | Agent gateway |

## Startup Order (Handled Automatically)

1. **Ollama** - Image analysis service (auto-starts via startup entry)
2. **llama-server** - Text AI model (loads Dolphin 2.9.3, ~10GB VRAM, takes ~30s)
3. **Heather Bot** - Main application (waits for AI services)
4. **Coqui TTS** - Voice note generation (loads XTTS model, takes ~30s)
5. **ComfyUI** - Image generation (when enabled)

## Remote Access Watchdog

A scheduled task (`HeatherBot-RemoteAccessWatchdog`) runs every 5 minutes as SYSTEM to ensure remote access stays available:

- Checks and restarts RDP + SSH services
- Verifies ports 3389 and 22 are listening
- Monitors Tailscale VPN tunnel
- Network connectivity checks (8.8.8.8 / 1.1.1.1)
- Disk space monitoring (auto-cleans at <5GB)
- GPU health via nvidia-smi (temperature + TDR detection)
- Sleep/hibernate prevention
- Windows Update reboot detection

Log: `C:\AI\logs\watchdog.log`

## GPU Notes

- **Dual RTX 3090** (24GB each)
- **Driver**: 595.79 (March 2026)
- **TDR Timeout**: 8 seconds (prevents false GPU timeout kills during LLM inference)
- **LM Studio**: Auto-start DISABLED - was causing GPU TDR crashes by competing with llama-server for GPU resources. Do NOT re-enable.

## Troubleshooting

### Service won't start
```powershell
# Check what's using the port
netstat -ano | findstr :1234
netstat -ano | findstr :5001
netstat -ano | findstr :8888

# Kill process by PID
taskkill /PID <pid> /F
```

### llama-server slow to load
- Normal first load: ~30 seconds for Dolphin 12B Q6_K
- Check GPU memory: `nvidia-smi`
- Model location: `C:\Models\Dolphin-2.9.3-Mistral-Nemo-12B\`

### Bot not responding
1. Check bot process: `Get-Process python`
2. Check text AI: `curl http://localhost:1234/v1/models`
3. Check logs: `Get-Content C:\AI\logs\heather_bot.log -Tail 20`
4. Clean restart: `.\restart_bot.ps1`

### GPU issues
```powershell
# Check GPU status
nvidia-smi

# Check for recent TDR events
Get-WinEvent -LogName System -MaxEvents 20 | Where-Object { $_.ProviderName -eq "nvlddmkm" }

# Check watchdog log
Get-Content C:\AI\logs\watchdog.log -Tail 20
```

### Reset everything
```powershell
.\heather_services.ps1 -Action stopall
Start-Sleep 10
.\heather_services.ps1 -Action startall
```

## Logs

| Log | Location | Purpose |
|-----|----------|---------|
| Bot (primary) | `C:\AI\logs\heather_bot.log` | Main bot activity |
| Bot (fallback) | `C:\Users\groot\heather-bot\logs\heather_bot.log` | When started without --log-dir |
| TTS | `C:\Users\groot\heather-bot\logs\tts.log` | Voice note generation |
| Watchdog | `C:\AI\logs\watchdog.log` | Remote access monitoring |
| Auto-changes | `C:\AI\logs\auto_changes.log` | Automated code changes |
| Reports | `C:\AI\logs\reports\` | Periodic bot activity reports |

## Configuration

- `.env` - Telegram token, feature flags
- `heather_personality.yaml` - Character definition
- `heather_kink_personas.yaml` - Kink persona variants
- `heather_stories.yaml` - Pre-written story pool
- `heather_services.ps1` - Service paths and startup args

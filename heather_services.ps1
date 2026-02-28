#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Heather Bot Service Manager - The Beast Edition (v2.5 with llama-server support)

.DESCRIPTION
    Manages all Heather Bot services including:
    - llama-server (llama.cpp - primary text backend)
    - LM Studio (alternate text backend)
    - Ollama (Image analysis)
    - ComfyUI (Image generation with face swap)
    - Coqui TTS (Voice synthesis for Heather)
    - Heather Telegram Bot (with YAML personality config)
    - Support for 12B model optimized prompts

.PARAMETER Action
    start, stop, status, restart, menu, startall, stopall, autostart

.PARAMETER Service
    all, text, llama, lmstudio, ollama, bot, comfyui, tts

.EXAMPLE
    # Interactive menu (default)
    .\heather_services.ps1

.EXAMPLE
    # Fully automated startup for Task Scheduler
    .\heather_services.ps1 autostart

.EXAMPLE
    # Start individual service
    .\heather_services.ps1 start text

.EXAMPLE
    # Stop all services
    .\heather_services.ps1 stop all
#>

param(
    [Parameter(Position=0)]
    [ValidateSet('start', 'stop', 'status', 'restart', 'menu', 'startall', 'stopall', 'autostart')]
    [string]$Action = 'menu',
    
    [Parameter(Position=1)]
    [ValidateSet('all', 'text', 'llama', 'lmstudio', 'ollama', 'bot', 'comfyui', 'tts')]
    [string]$Service = 'all'
)

# Fix console encoding for box characters
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ============================================================================
# CONFIGURATION - The Beast (groot@TheBeast)
# ============================================================================

$BaseDir = $PSScriptRoot
$ConfigFile = Join-Path $BaseDir "config\services.json"

# Default configuration for The Beast
$Config = @{
    # Text backend: "llama" (llama-server) or "lmstudio" (LM Studio)
    TextBackend = "llama"

    # llama-server (llama.cpp)
    LlamaServerExe = "C:\llama-cpp\llama-server.exe"
    LlamaServerModel = "C:\Models\your-model-file.gguf"
    LlamaServerGPULayers = 99
    LlamaServerContext = 32768

    # LM Studio (alternate backend)
    LMStudioDir = "$env:LOCALAPPDATA\Programs\LM Studio"
    LMStudioExe = "LM Studio.exe"
    TextGenPort = 1234  # Shared port for whichever text backend is active
    LMStudioModel = "hermes-4-70b"  # Model to auto-load
    
    # Ollama
    OllamaPort = 11434
    
    # Bot
    BotScript = Join-Path $BaseDir "heather_telegram_bot.py"
    BotMonitorPort = 8888
    
    # NEW: Personality YAML file
    PersonalityFile = Join-Path $BaseDir "persona_example.yaml"
    
    # NEW: Small Model Mode (for 12B models like SingularitySynth)
    # Set to $true when using smaller models that need condensed prompts
    SmallModelMode = $true  # Set to $false for 70B models like Hermes
    
    # ComfyUI
    ComfyUIDir = "C:\ComfyUI"
    ComfyUIScript = "main.py"
    ComfyUIPort = 8188
    
    # Coqui TTS
    TTSScript = "C:\AI\heather_tts_service.py"
    TTSPort = 5001
    TTSPythonEnv = "C:\AI\coqui_tts\Scripts\python.exe"
    
    # Logs
    LogDir = Join-Path $BaseDir "logs"
}

# Load config overrides from services.json if it exists
if (Test-Path $ConfigFile) {
    try {
        $jsonConfig = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        Write-Host "Loaded config from services.json" -ForegroundColor Gray
        
        if ($jsonConfig.services.text_generation.port) {
            $Config.TextGenPort = $jsonConfig.services.text_generation.port
        }
        if ($jsonConfig.services.text_generation.model) {
            $Config.LMStudioModel = $jsonConfig.services.text_generation.model
        }
        if ($jsonConfig.services.ollama.port) {
            $Config.OllamaPort = $jsonConfig.services.ollama.port
        }
        if ($jsonConfig.services.bot.monitoring_port) {
            $Config.BotMonitorPort = $jsonConfig.services.bot.monitoring_port
        }
        if ($jsonConfig.services.bot.personality_file) {
            $Config.PersonalityFile = $jsonConfig.services.bot.personality_file
        }
        if ($jsonConfig.services.comfyui.port) {
            $Config.ComfyUIPort = $jsonConfig.services.comfyui.port
        }
        if ($jsonConfig.services.comfyui.directory) {
            $Config.ComfyUIDir = $jsonConfig.services.comfyui.directory
        }
        if ($jsonConfig.services.tts.port) {
            $Config.TTSPort = $jsonConfig.services.tts.port
        }
        if ($jsonConfig.services.tts.script) {
            $Config.TTSScript = $jsonConfig.services.tts.script
        }
        if ($jsonConfig.services.tts.python_env) {
            $Config.TTSPythonEnv = $jsonConfig.services.tts.python_env
        }
    } catch {
        Write-Host "Could not parse services.json, using defaults" -ForegroundColor Yellow
    }
}

# Ensure log directory exists
if (!(Test-Path $Config.LogDir)) {
    New-Item -ItemType Directory -Path $Config.LogDir -Force | Out-Null
}

# Auto-start log file
$AutoStartLog = Join-Path $Config.LogDir "autostart.log"

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

function Write-Status {
    param([string]$Message, [string]$Status)
    $statusColors = @{
        "RUNNING" = "Green"; "STOPPED" = "Red"; "STARTING" = "Yellow"
        "STOPPING" = "Yellow"; "ERROR" = "Red"; "OK" = "Green"; "INFO" = "Cyan"
    }
    $color = if ($statusColors[$Status]) { $statusColors[$Status] } else { "White" }
    Write-Host "[$Status]" -ForegroundColor $color -NoNewline
    Write-Host " $Message"
}

function Write-AutoLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logLine = "$timestamp - $Message"
    Write-Host $logLine
    Add-Content -Path $AutoStartLog -Value $logLine -Encoding UTF8
}

function Test-PortInUse {
    param([int]$Port)
    try {
        $connection = New-Object System.Net.Sockets.TcpClient
        $connection.Connect("127.0.0.1", $Port)
        $connection.Close()
        return $true
    } catch {
        return $false
    }
}

function Stop-ServiceByPort {
    param([int]$Port, [string]$ServiceName)
    
    $connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($connections) {
        $processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -gt 4 }
        
        if ($processIds) {
            foreach ($procId in $processIds) {
                $process = Get-Process -Id $procId -ErrorAction SilentlyContinue
                if ($process) {
                    Write-Status "Stopping $ServiceName (PID: $procId, Process: $($process.ProcessName))..." "STOPPING"
                    try {
                        Stop-Process -Id $procId -Force -ErrorAction Stop
                    } catch {
                        Write-Status "Could not stop PID $procId : $_" "ERROR"
                    }
                }
            }
            Start-Sleep -Seconds 2
            Write-Status "$ServiceName stopped" "OK"
            return $true
        }
    }
    Write-Status "$ServiceName was not running on port $Port" "INFO"
    return $false
}

function Test-LMSCliAvailable {
    try {
        $result = & lms status 2>&1
        return $true
    } catch {
        return $false
    }
}

# ============================================================================
# BOT FUNCTIONS - UPDATED FOR YAML PERSONALITY
# ============================================================================

function Start-Bot {
    $modeLabel = if ($Config.SmallModelMode) { "12B Optimized" } else { "Standard (70B)" }
    Write-Status "Starting Heather Bot v2.6 ($modeLabel Mode)..." "STARTING"
    
    if (Test-PortInUse -Port $Config.BotMonitorPort) {
        Write-Status "Bot already running (monitoring on port $($Config.BotMonitorPort))" "RUNNING"
        return
    }
    
    # Check script exists
    $botScript = $Config.BotScript
    if (!(Test-Path $botScript)) {
        Write-Status "Bot script not found at $botScript" "ERROR"
        return
    }
    
    # Check personality file exists
    $personalityExists = Test-Path $Config.PersonalityFile
    if ($personalityExists) {
        Write-Status "Using personality: $($Config.PersonalityFile)" "INFO"
    } else {
        Write-Status "Personality file not found - using defaults" "INFO"
    }
    
    # Build arguments - NOW INCLUDES --personality AND --small-model
    $botArgs = @($botScript, "--monitoring", "--unfiltered", "--log-dir", $Config.LogDir)
    if ($personalityExists) {
        $botArgs += "--personality"
        $botArgs += $Config.PersonalityFile
    }

    # Add small model flag if configured
    if ($Config.SmallModelMode) {
        $botArgs += "--small-model"
        Write-Status "Using optimized 12B prompt (--small-model)" "INFO"
    }
    
    $process = Start-Process -FilePath "python" -ArgumentList $botArgs -WorkingDirectory $BaseDir -WindowStyle Minimized -PassThru
    
    Start-Sleep -Seconds 3
    
    if (Test-PortInUse -Port $Config.BotMonitorPort) {
        Write-Status "Bot ready - Monitor at http://localhost:$($Config.BotMonitorPort)" "RUNNING"
    } else {
        Write-Status "Bot starting (PID: $($process.Id))..." "INFO"
    }
}

function Stop-Bot {
    Write-Status "Stopping Heather Bot..." "STOPPING"
    $stopped = $false
    
    # Method 1: Stop by port
    $portStopped = Stop-ServiceByPort -Port $Config.BotMonitorPort -ServiceName "Bot (port $($Config.BotMonitorPort))"
    if ($portStopped) { $stopped = $true }
    
    # Method 2: Find and kill python processes running the bot script
    $botScriptName = [System.IO.Path]::GetFileName($Config.BotScript)
    $pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
    
    foreach ($proc in $pythonProcesses) {
        try {
            # Get command line to check if it's the bot
            $wmiProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
            if ($wmiProc -and $wmiProc.CommandLine -match "heather.*bot|$botScriptName") {
                Write-Status "Killing bot process (PID: $($proc.Id))..." "STOPPING"
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        } catch {
            Write-Status "Could not inspect process $($proc.Id): $_" "INFO"
        }
    }
    
    # Verify
    Start-Sleep -Seconds 1
    if (Test-PortInUse -Port $Config.BotMonitorPort) {
        Write-Status "WARNING: Port $($Config.BotMonitorPort) still in use!" "ERROR"
    } else {
        if ($stopped) {
            Write-Status "Heather Bot stopped successfully" "OK"
        } else {
            Write-Status "Heather Bot was not running" "INFO"
        }
    }
}


# ============================================================================
# LM STUDIO FUNCTIONS
# ============================================================================

function Start-TextGenCLI {
    Write-Status "Starting LM Studio via CLI..." "STARTING"
    
    if (Test-PortInUse -Port $Config.TextGenPort) {
        $psResult = & lms ps 2>&1
        if ($psResult -match $Config.LMStudioModel) {
            Write-Status "LM Studio already running with model loaded" "RUNNING"
            return $true
        }
    }
    
    if (-not (Test-LMSCliAvailable)) {
        Write-Status "LMS CLI not available - falling back to GUI mode" "INFO"
        Start-TextGen
        return $false
    }
    
    $serverStatus = & lms server status 2>&1
    if ($serverStatus -notmatch "ON") {
        Write-Status "Starting LM Studio server..." "INFO"
        & lms server start 2>&1 | Out-Null
        Start-Sleep -Seconds 5
    }
    
    $psResult = & lms ps 2>&1
    if ($psResult -match $Config.LMStudioModel) {
        Write-Status "Model already loaded" "RUNNING"
        return $true
    }
    
    Write-Status "Loading model: $($Config.LMStudioModel) (this takes ~60 seconds)..." "INFO"
    $loadStart = Get-Date
    
    try {
        $loadResult = & lms load $Config.LMStudioModel --yes 2>&1
        $loadTime = ((Get-Date) - $loadStart).TotalSeconds
        Write-Status "Model loaded in $([math]::Round($loadTime, 1)) seconds" "OK"
    } catch {
        Write-Status "Failed to load model: $_" "ERROR"
        return $false
    }
    
    Start-Sleep -Seconds 5
    if (Test-PortInUse -Port $Config.TextGenPort) {
        Write-Status "LM Studio ready on port $($Config.TextGenPort)" "RUNNING"
        return $true
    } else {
        Write-Status "LM Studio port not responding" "ERROR"
        return $false
    }
}

function Start-TextGen {
    Write-Status "Starting LM Studio (GUI mode)..." "STARTING"
    
    if (Test-PortInUse -Port $Config.TextGenPort) {
        Write-Status "LM Studio server already running on port $($Config.TextGenPort)" "RUNNING"
        return
    }
    
    $lmStudioPath = Join-Path $Config.LMStudioDir $Config.LMStudioExe
    if (!(Test-Path $lmStudioPath)) {
        $lmStudioPath = Join-Path $env:LOCALAPPDATA "Programs\LM Studio\LM Studio.exe"
    }
    
    if (!(Test-Path $lmStudioPath)) {
        Write-Status "LM Studio not found at: $lmStudioPath" "ERROR"
        Write-Status "Please start LM Studio manually and load a model" "INFO"
        return
    }
    
    $process = Start-Process -FilePath $lmStudioPath -PassThru
    
    Write-Status "LM Studio starting (PID: $($process.Id))..." "INFO"
    Write-Status "IMPORTANT: Load a model and start the server on port $($Config.TextGenPort)" "INFO"
    Write-Host ""
    Write-Host "  In LM Studio:" -ForegroundColor Yellow
    Write-Host "  1. Load: $($Config.LMStudioModel)" -ForegroundColor Yellow  
    Write-Host "  2. Go to Local Server tab" -ForegroundColor Yellow
    Write-Host "  3. Set port to $($Config.TextGenPort)" -ForegroundColor Yellow
    Write-Host "  4. Click 'Start Server'" -ForegroundColor Yellow
    Write-Host ""
    
    $timeout = 180
    $elapsed = 0
    while (-not (Test-PortInUse -Port $Config.TextGenPort) -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 5
        $elapsed += 5
        Write-Host "." -NoNewline
    }
    Write-Host ""
    
    if (Test-PortInUse -Port $Config.TextGenPort) {
        Write-Status "LM Studio server ready on port $($Config.TextGenPort)" "RUNNING"
    } else {
        Write-Status "Waiting for LM Studio server... start it manually in the app" "INFO"
    }
}

function Stop-TextGen {
    if (Test-LMSCliAvailable) {
        try {
            & lms unload --all 2>&1 | Out-Null
            Write-Status "Unloaded models via CLI" "INFO"
        } catch {}
    }
    
    $lmProcesses = Get-Process -Name "LM Studio" -ErrorAction SilentlyContinue
    if ($lmProcesses) {
        foreach ($proc in $lmProcesses) {
            Write-Status "Stopping LM Studio (PID: $($proc.Id))..." "STOPPING"
            Stop-Process -Id $proc.Id -Force
        }
        Write-Status "LM Studio stopped" "OK"
    } else {
        Stop-ServiceByPort -Port $Config.TextGenPort -ServiceName "LM Studio"
    }
}

# ============================================================================
# LLAMA-SERVER FUNCTIONS
# ============================================================================

function Start-LlamaServer {
    Write-Status "Starting llama-server..." "STARTING"

    if (Test-PortInUse -Port $Config.TextGenPort) {
        Write-Status "Text backend already running on port $($Config.TextGenPort)" "RUNNING"
        return
    }

    if (!(Test-Path $Config.LlamaServerExe)) {
        Write-Status "llama-server not found at: $($Config.LlamaServerExe)" "ERROR"
        return
    }

    if (!(Test-Path $Config.LlamaServerModel)) {
        Write-Status "Model not found at: $($Config.LlamaServerModel)" "ERROR"
        return
    }

    $modelName = [System.IO.Path]::GetFileName($Config.LlamaServerModel)
    Write-Status "Loading model: $modelName" "INFO"

    $llamaArgs = @(
        "-m", $Config.LlamaServerModel,
        "--host", "0.0.0.0",
        "--port", $Config.TextGenPort,
        "-ngl", $Config.LlamaServerGPULayers,
        "-c", $Config.LlamaServerContext
    )

    $process = Start-Process -FilePath $Config.LlamaServerExe `
        -ArgumentList $llamaArgs `
        -WindowStyle Minimized `
        -PassThru

    Write-Status "llama-server starting (PID: $($process.Id))..." "INFO"

    $timeout = 120
    $elapsed = 0
    while (-not (Test-PortInUse -Port $Config.TextGenPort) -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 3
        $elapsed += 3
        Write-Host "." -NoNewline
    }
    Write-Host ""

    if (Test-PortInUse -Port $Config.TextGenPort) {
        Write-Status "llama-server ready on port $($Config.TextGenPort) ($modelName)" "RUNNING"
    } else {
        Write-Status "llama-server may still be loading - check the window" "INFO"
    }
}

function Stop-LlamaServer {
    Write-Status "Stopping llama-server..." "STOPPING"
    $stopped = $false

    $llamaProcesses = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
    if ($llamaProcesses) {
        foreach ($proc in $llamaProcesses) {
            Write-Status "Stopping llama-server (PID: $($proc.Id))..." "STOPPING"
            Stop-Process -Id $proc.Id -Force
        }
        Start-Sleep -Seconds 2
        Write-Status "llama-server stopped" "OK"
        $stopped = $true
    }

    if (-not $stopped) {
        $portStopped = Stop-ServiceByPort -Port $Config.TextGenPort -ServiceName "llama-server"
        if (-not $portStopped) {
            Write-Status "llama-server was not running" "INFO"
        }
    }
}

# Unified text backend start/stop (routes based on TextBackend config)
function Start-TextBackend {
    if ($Config.TextBackend -eq "llama") {
        Start-LlamaServer
    } else {
        Start-TextGenCLI
    }
}

function Stop-TextBackend {
    if ($Config.TextBackend -eq "llama") {
        Stop-LlamaServer
    } else {
        Stop-TextGen
    }
}

# ============================================================================
# OLLAMA FUNCTIONS
# ============================================================================

function Start-Ollama {
    Write-Status "Starting Ollama..." "STARTING"
    
    if (Test-PortInUse -Port $Config.OllamaPort) {
        Write-Status "Ollama already running on port $($Config.OllamaPort)" "RUNNING"
        return
    }
    
    $process = Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Minimized -PassThru
    
    Start-Sleep -Seconds 3
    
    if (Test-PortInUse -Port $Config.OllamaPort) {
        Write-Status "Ollama ready on port $($Config.OllamaPort)" "RUNNING"
    } else {
        Write-Status "Ollama starting (PID: $($process.Id))..." "INFO"
    }
}

function Stop-Ollama {
    $ollamaProcesses = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
    if ($ollamaProcesses) {
        foreach ($proc in $ollamaProcesses) {
            Write-Status "Stopping Ollama (PID: $($proc.Id))..." "STOPPING"
            Stop-Process -Id $proc.Id -Force
        }
        Write-Status "Ollama stopped" "OK"
    } else {
        Stop-ServiceByPort -Port $Config.OllamaPort -ServiceName "Ollama"
    }
}

# ============================================================================
# COMFYUI FUNCTIONS
# ============================================================================

function Start-ComfyUI {
    Write-Status "Starting ComfyUI (Image Generation)..." "STARTING"
    
    if (Test-PortInUse -Port $Config.ComfyUIPort) {
        Write-Status "ComfyUI already running on port $($Config.ComfyUIPort)" "RUNNING"
        return
    }
    
    if (!(Test-Path $Config.ComfyUIDir)) {
        Write-Status "ComfyUI directory not found: $($Config.ComfyUIDir)" "ERROR"
        return
    }
    
    $comfyScript = Join-Path $Config.ComfyUIDir $Config.ComfyUIScript
    if (!(Test-Path $comfyScript)) {
        Write-Status "ComfyUI script not found: $comfyScript" "ERROR"
        return
    }
    
    $process = Start-Process -FilePath "python" `
        -ArgumentList $Config.ComfyUIScript `
        -WorkingDirectory $Config.ComfyUIDir `
        -WindowStyle Minimized `
        -PassThru
    
    Write-Status "ComfyUI starting (PID: $($process.Id))..." "INFO"
    Write-Status "Loading models, please wait..." "INFO"
    
    $timeout = 90
    $elapsed = 0
    while (-not (Test-PortInUse -Port $Config.ComfyUIPort) -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 3
        $elapsed += 3
        Write-Host "." -NoNewline
    }
    Write-Host ""
    
    if (Test-PortInUse -Port $Config.ComfyUIPort) {
        Write-Status "ComfyUI ready at http://localhost:$($Config.ComfyUIPort)" "RUNNING"
    } else {
        Write-Status "ComfyUI may still be loading - check the minimized window" "INFO"
    }
}

function Stop-ComfyUI {
    Write-Status "Stopping ComfyUI..." "STOPPING"
    $stopped = $false
    
    $portStopped = Stop-ServiceByPort -Port $Config.ComfyUIPort -ServiceName "ComfyUI (port $($Config.ComfyUIPort))"
    if ($portStopped) { $stopped = $true }
    
    $pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
    
    foreach ($proc in $pythonProcesses) {
        try {
            $wmiProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
            if ($wmiProc -and $wmiProc.CommandLine -match "ComfyUI|main\.py.*ComfyUI") {
                Write-Status "Killing ComfyUI process (PID: $($proc.Id))..." "STOPPING"
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        } catch {
            Write-Status "Could not inspect process $($proc.Id): $_" "INFO"
        }
    }
    
    Start-Sleep -Seconds 1
    if (Test-PortInUse -Port $Config.ComfyUIPort) {
        Write-Status "WARNING: Port $($Config.ComfyUIPort) still in use!" "ERROR"
    } else {
        if ($stopped) {
            Write-Status "ComfyUI stopped successfully" "OK"
        } else {
            Write-Status "ComfyUI was not running" "INFO"
        }
    }
}

# ============================================================================
# TTS SERVICE FUNCTIONS
# ============================================================================

function Start-TTS {
    Write-Status "Starting Coqui TTS Service..." "STARTING"
    
    if (Test-PortInUse -Port $Config.TTSPort) {
        Write-Status "TTS already running on port $($Config.TTSPort)" "RUNNING"
        return
    }
    
    if (!(Test-Path $Config.TTSScript)) {
        Write-Status "TTS script not found at $($Config.TTSScript)" "ERROR"
        return
    }
    
    $pythonExe = if (Test-Path $Config.TTSPythonEnv) { 
        $Config.TTSPythonEnv 
    } else { 
        "python" 
    }
    
    $ttsDir = [System.IO.Path]::GetDirectoryName($Config.TTSScript)
    
    $process = Start-Process -FilePath $pythonExe `
        -ArgumentList $Config.TTSScript `
        -WorkingDirectory $ttsDir `
        -WindowStyle Minimized `
        -PassThru
    
    Write-Status "TTS Service starting (PID: $($process.Id))..." "INFO"
    Write-Status "Loading XTTS model, please wait (~30 seconds)..." "INFO"
    
    $timeout = 60
    $elapsed = 0
    while (-not (Test-PortInUse -Port $Config.TTSPort) -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 3
        $elapsed += 3
        Write-Host "." -NoNewline
    }
    Write-Host ""
    
    if (Test-PortInUse -Port $Config.TTSPort) {
        Write-Status "TTS Service ready on port $($Config.TTSPort)" "RUNNING"
        
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:$($Config.TTSPort)/health" -TimeoutSec 5
            if ($health.status -eq "healthy") {
                Write-Status "TTS health check passed (model: $($health.model))" "OK"
            }
        } catch {
            Write-Status "TTS running but health check failed" "INFO"
        }
    } else {
        Write-Status "TTS Service may still be loading - check the window" "INFO"
    }
}

function Stop-TTS {
    Write-Status "Stopping Coqui TTS Service..." "STOPPING"
    $stopped = $false
    
    $portStopped = Stop-ServiceByPort -Port $Config.TTSPort -ServiceName "TTS (port $($Config.TTSPort))"
    if ($portStopped) { $stopped = $true }
    
    $ttsScriptName = [System.IO.Path]::GetFileName($Config.TTSScript)
    $pythonProcesses = Get-Process -Name python -ErrorAction SilentlyContinue
    
    foreach ($proc in $pythonProcesses) {
        try {
            $wmiProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
            if ($wmiProc -and $wmiProc.CommandLine -match "tts_service|$ttsScriptName|coqui") {
                Write-Status "Killing TTS process (PID: $($proc.Id))..." "STOPPING"
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        } catch {
            Write-Status "Could not inspect process $($proc.Id): $_" "INFO"
        }
    }
    
    Start-Sleep -Seconds 1
    if (Test-PortInUse -Port $Config.TTSPort) {
        Write-Status "WARNING: Port $($Config.TTSPort) still in use!" "ERROR"
    } else {
        if ($stopped) {
            Write-Status "TTS Service stopped successfully" "OK"
        } else {
            Write-Status "TTS Service was not running" "INFO"
        }
    }
}

# ============================================================================
# AGGREGATE FUNCTIONS
# ============================================================================

function Start-AllServices {
    Write-Host ""
    Write-Host "Starting all Heather services on The Beast..." -ForegroundColor Cyan
    Write-Host ""
    
    Start-Ollama
    Start-ComfyUI
    Start-TTS
    Start-TextBackend

    Write-Status "Waiting for AI services to initialize..." "INFO"
    $maxWait = 90
    $waited = 0
    while ($waited -lt $maxWait) {
        $textReady = Test-PortInUse -Port $Config.TextGenPort
        $ollamaReady = Test-PortInUse -Port $Config.OllamaPort
        
        if ($textReady -and $ollamaReady) {
            break
        }
        Start-Sleep -Seconds 5
        $waited += 5
        Write-Host "." -NoNewline
    }
    Write-Host ""
    
    Start-Bot
    
    Write-Host ""
    Write-Host "All services started!" -ForegroundColor Green
    Show-Status
}

function Start-AllServicesAuto {
    Write-AutoLog "=========================================="
    Write-AutoLog "HEATHER BOT AUTO-START INITIATED (v2.3)"
    Write-AutoLog "=========================================="
    
    Write-AutoLog "Waiting 30 seconds for system to settle..."
    Start-Sleep -Seconds 30
    
    Write-AutoLog "Starting Ollama..."
    Start-Ollama
    Start-Sleep -Seconds 5
    
    Write-AutoLog "Starting ComfyUI..."
    Start-ComfyUI
    Start-Sleep -Seconds 5
    
    Write-AutoLog "Starting TTS Service..."
    Start-TTS
    Start-Sleep -Seconds 5
    
    $backendName = if ($Config.TextBackend -eq "llama") { "llama-server" } else { "LM Studio" }
    Write-AutoLog "Starting $backendName..."
    Start-TextBackend
    
    Write-AutoLog "Waiting for AI services to be ready..."
    $maxWait = 120
    $waited = 0
    while ($waited -lt $maxWait) {
        $textReady = Test-PortInUse -Port $Config.TextGenPort
        $ollamaReady = Test-PortInUse -Port $Config.OllamaPort
        
        if ($textReady -and $ollamaReady) {
            Write-AutoLog "AI services ready!"
            break
        }
        Start-Sleep -Seconds 5
        $waited += 5
    }
    
    if ($waited -ge $maxWait) {
        Write-AutoLog "WARNING: Timeout waiting for AI services"
    }
    
    Write-AutoLog "Starting Heather Bot..."
    Start-Bot
    Start-Sleep -Seconds 5
    
    Write-AutoLog "=========================================="
    Write-AutoLog "AUTO-START COMPLETE - Final Status:"
    Write-AutoLog "=========================================="
    
    $textBackendName = if ($Config.TextBackend -eq "llama") { "llama-server" } else { "LM Studio" }
    $services = @(
        @{ Name = "Ollama"; Port = $Config.OllamaPort }
        @{ Name = "$textBackendName (Chat AI)"; Port = $Config.TextGenPort }
        @{ Name = "ComfyUI"; Port = $Config.ComfyUIPort }
        @{ Name = "TTS Service"; Port = $Config.TTSPort }
        @{ Name = "Heather Bot"; Port = $Config.BotMonitorPort }
    )

    foreach ($svc in $services) {
        $status = if (Test-PortInUse -Port $svc.Port) { "RUNNING" } else { "NOT RUNNING" }
        Write-AutoLog "$($svc.Name): $status (port $($svc.Port))"
    }
    
    # Log personality file status
    if (Test-Path $Config.PersonalityFile) {
        Write-AutoLog "Personality: $($Config.PersonalityFile)"
    } else {
        Write-AutoLog "Personality: Using defaults (YAML not found)"
    }
    
    Write-AutoLog "=========================================="
}

function Stop-AllServices {
    Write-Host ""
    Write-Host "Stopping all Heather services..." -ForegroundColor Yellow
    Write-Host ""
    
    Stop-Bot
    Stop-TTS
    Stop-ComfyUI
    Stop-TextBackend
    Stop-Ollama
    
    Write-Host ""
    Write-Host "Verifying services stopped..." -ForegroundColor Cyan
    Start-Sleep -Seconds 2
    
    $services = @(
        @{ Name = "Heather Bot"; Port = $Config.BotMonitorPort }
        @{ Name = "TTS Service"; Port = $Config.TTSPort }
        @{ Name = "ComfyUI"; Port = $Config.ComfyUIPort }
        @{ Name = "LM Studio"; Port = $Config.TextGenPort }
        @{ Name = "Ollama"; Port = $Config.OllamaPort }
    )
    
    $allStopped = $true
    foreach ($svc in $services) {
        if (Test-PortInUse -Port $svc.Port) {
            Write-Status "$($svc.Name) still running on port $($svc.Port)!" "ERROR"
            $allStopped = $false
        }
    }
    
    $pythonProcs = Get-Process -Name python -ErrorAction SilentlyContinue
    if ($pythonProcs) {
        Write-Host ""
        Write-Host "WARNING: $($pythonProcs.Count) Python process(es) still running:" -ForegroundColor Yellow
        foreach ($proc in $pythonProcs) {
            try {
                $wmiProc = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
                $cmdLine = if ($wmiProc) { $wmiProc.CommandLine.Substring(0, [Math]::Min(80, $wmiProc.CommandLine.Length)) } else { "Unknown" }
                Write-Host "  PID $($proc.Id): $cmdLine..." -ForegroundColor Gray
            } catch {
                Write-Host "  PID $($proc.Id): (could not get details)" -ForegroundColor Gray
            }
        }
        Write-Host ""
        Write-Host "Use 'Get-Process python | Stop-Process -Force' to kill all Python processes" -ForegroundColor Yellow
    }
    
    Write-Host ""
    if ($allStopped -and -not $pythonProcs) {
        Write-Host "All services stopped successfully!" -ForegroundColor Green
    } else {
        Write-Host "Some services may still be running - check above" -ForegroundColor Yellow
    }
}

function Show-Status {
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  HEATHER BOT - The Beast - Service Status" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
    
    # Show model mode
    $modelModeDisplay = if ($Config.SmallModelMode) { "12B OPTIMIZED" } else { "STANDARD (70B)" }
    $modeColor = if ($Config.SmallModelMode) { "Yellow" } else { "Green" }
    Write-Host "  Model Mode: " -NoNewline
    Write-Host $modelModeDisplay -ForegroundColor $modeColor
    
    # Show personality file status
    $personalityStatus = if (Test-Path $Config.PersonalityFile) { 
        "Loaded" 
    } else { 
        "Not Found (using defaults)" 
    }
    Write-Host "  Personality: $personalityStatus" -ForegroundColor Magenta
    if (Test-Path $Config.PersonalityFile) {
        Write-Host "    File: $($Config.PersonalityFile)" -ForegroundColor Gray
    }
    
    $textBackendName = if ($Config.TextBackend -eq "llama") { "llama-server" } else { "LM Studio" }
    $textBackendDesc = if ($Config.TextBackend -eq "llama") {
        $modelFile = [System.IO.Path]::GetFileNameWithoutExtension($Config.LlamaServerModel)
        "llama.cpp - $modelFile"
    } else {
        "$($Config.LMStudioModel) via LM Studio"
    }
    $services = @(
        @{ Name = "Ollama (Image Analysis)"; Port = $Config.OllamaPort; Desc = "LLaVA for photo analysis" }
        @{ Name = "$textBackendName (Chat AI)"; Port = $Config.TextGenPort; Desc = $textBackendDesc }
        @{ Name = "ComfyUI (Image Generation)"; Port = $Config.ComfyUIPort; Desc = "Generates Heather selfies" }
        @{ Name = "Coqui TTS (Voice)"; Port = $Config.TTSPort; Desc = "XTTS voice synthesis" }
        @{ Name = "Heather Bot (Telegram)"; Port = $Config.BotMonitorPort; Desc = "Main bot + monitoring" }
    )
    
    foreach ($svc in $services) {
        $running = Test-PortInUse -Port $svc.Port
        $status = if ($running) { "RUNNING" } else { "STOPPED" }
        $statusColor = if ($running) { "Green" } else { "Red" }
        
        Write-Host ""
        Write-Host "  $($svc.Name)" -ForegroundColor White
        Write-Host "    Status: " -NoNewline
        Write-Host $status -ForegroundColor $statusColor
        Write-Host "    Port:   $($svc.Port)"
        Write-Host "    Info:   $($svc.Desc)" -ForegroundColor Gray
        if ($running) {
            Write-Host "    URL:    http://localhost:$($svc.Port)" -ForegroundColor Gray
        }
    }
    
    if ($Config.TextBackend -eq "lmstudio" -and (Test-LMSCliAvailable)) {
        Write-Host ""
        Write-Host "  LMS CLI Status:" -ForegroundColor White
        $lmsStatus = & lms status 2>&1
        Write-Host "    $lmsStatus" -ForegroundColor Gray
        $loadedModels = & lms ps 2>&1
        if ($loadedModels -and $loadedModels -notmatch "No Models Loaded") {
            Write-Host "    Loaded: $loadedModels" -ForegroundColor Gray
        }
    }
    
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host ""
}

# ============================================================================
# MENU
# ============================================================================

function Show-Menu {
    Clear-Host
    $modelModeDisplay = if ($Config.SmallModelMode) { "12B OPTIMIZED" } else { "STANDARD (70B)" }
    $modelModeColor = if ($Config.SmallModelMode) { "Yellow" } else { "Green" }
    $backendDisplay = if ($Config.TextBackend -eq "llama") { "llama-server" } else { "LM Studio" }
    $backendColor = if ($Config.TextBackend -eq "llama") { "Cyan" } else { "Magenta" }
    Write-Host @"
+---------------------------------------------------------------+
|      HEATHER BOT - The Beast Service Manager v2.5             |
|   Dual RTX 3090 + ComfyUI + TTS + YAML Personality            |
+---------------------------------------------------------------+
"@ -ForegroundColor Cyan
    Write-Host "|   Model Mode: " -NoNewline -ForegroundColor Cyan
    Write-Host "$modelModeDisplay" -NoNewline -ForegroundColor $modelModeColor
    Write-Host "  |  Backend: " -NoNewline -ForegroundColor Cyan
    Write-Host "$backendDisplay" -NoNewline -ForegroundColor $backendColor
    Write-Host "          |" -ForegroundColor Cyan
    Write-Host @"
+---------------------------------------------------------------+
|                                                               |
|   [1]  Start ALL Services (interactive)                       |
|   [2]  Stop ALL Services                                      |
|   [3]  Restart ALL Services                                   |
|   [A]  AUTO-START (CLI mode - for scheduled tasks)            |
|                                                               |
|   ---------------------------------------------------------   |
|   Individual Service Control:                                 |
|   ---------------------------------------------------------   |
|                                                               |
|   [4]  Start/Stop Text AI           (active backend)          |
|   [5]  Start/Stop Ollama            (Image Analysis - LLaVA)  |
|   [6]  Start/Stop Heather Bot       (Telegram Bot)            |
|   [7]  Start/Stop ComfyUI           (Image Generation)        |
|   [8]  Start/Stop TTS Service       (Coqui XTTS Voice)        |
|   [9]  Start/Stop LM Studio         (alternate backend)       |
|                                                               |
|   ---------------------------------------------------------   |
|   Quick Access:                                               |
|   ---------------------------------------------------------   |
|                                                               |
|   [S]  Show Status                                            |
|   [L]  Open Logs Folder                                       |
|   [M]  Open Bot Monitoring Dashboard                          |
|   [C]  Open ComfyUI Web Interface                             |
|   [V]  Open in VS Code                                        |
|   [P]  Check Personality File                                 |
|   [T]  Toggle Model Mode (12B/70B)                            |
|   [B]  Toggle Text Backend (llama/lmstudio)                   |
|                                                               |
|   [Q]  Quit                                                   |
|                                                               |
+---------------------------------------------------------------+
"@ -ForegroundColor Cyan

    Show-Status
    
    $choice = Read-Host "Enter choice"
    
    switch ($choice.ToUpper()) {
        "1" { Start-AllServices; Pause }
        "2" { Stop-AllServices; Pause }
        "3" { Stop-AllServices; Start-Sleep -Seconds 3; Start-AllServices; Pause }
        "A" { Start-AllServicesAuto; Pause }
        "4" {
            if (Test-PortInUse -Port $Config.TextGenPort) { Stop-TextBackend } else { Start-TextBackend }
            Pause
        }
        "5" {
            if (Test-PortInUse -Port $Config.OllamaPort) { Stop-Ollama } else { Start-Ollama }
            Pause
        }
        "6" {
            if (Test-PortInUse -Port $Config.BotMonitorPort) { Stop-Bot } else { Start-Bot }
            Pause
        }
        "7" {
            if (Test-PortInUse -Port $Config.ComfyUIPort) { Stop-ComfyUI } else { Start-ComfyUI }
            Pause
        }
        "8" {
            if (Test-PortInUse -Port $Config.TTSPort) { Stop-TTS } else { Start-TTS }
            Pause
        }
        "9" {
            if (Test-PortInUse -Port $Config.TextGenPort) { Stop-TextGen } else { Start-TextGenCLI }
            Pause
        }
        "S" { Show-Status; Pause }
        "L" { Start-Process "explorer.exe" -ArgumentList $Config.LogDir }
        "M" { 
            if (Test-PortInUse -Port $Config.BotMonitorPort) {
                Start-Process "http://localhost:$($Config.BotMonitorPort)"
            } else {
                Write-Host ""
                Write-Host "Bot not running!" -ForegroundColor Red
                Pause
            }
        }
        "C" {
            if (Test-PortInUse -Port $Config.ComfyUIPort) {
                Start-Process "http://localhost:$($Config.ComfyUIPort)"
            } else {
                Write-Host ""
                Write-Host "ComfyUI not running!" -ForegroundColor Red
                Pause
            }
        }
        "V" { Start-Process "code" -ArgumentList $BaseDir }
        "P" {
            Write-Host ""
            Write-Host "Personality Configuration:" -ForegroundColor Cyan
            Write-Host "  File: $($Config.PersonalityFile)" -ForegroundColor White
            if (Test-Path $Config.PersonalityFile) {
                Write-Host "  Status: " -NoNewline
                Write-Host "FOUND" -ForegroundColor Green
                $fileInfo = Get-Item $Config.PersonalityFile
                Write-Host "  Size: $($fileInfo.Length) bytes"
                Write-Host "  Modified: $($fileInfo.LastWriteTime)"
            } else {
                Write-Host "  Status: " -NoNewline
                Write-Host "NOT FOUND" -ForegroundColor Red
                Write-Host "  Bot will use hardcoded defaults" -ForegroundColor Yellow
            }
            Write-Host ""
            Pause
        }
        "T" {
            Write-Host ""
            $Config.SmallModelMode = -not $Config.SmallModelMode
            $newMode = if ($Config.SmallModelMode) { "12B OPTIMIZED" } else { "STANDARD (70B)" }
            $modeColor = if ($Config.SmallModelMode) { "Yellow" } else { "Green" }
            Write-Host "Model mode changed to: " -NoNewline
            Write-Host $newMode -ForegroundColor $modeColor
            Write-Host ""
            Write-Host "NOTE: This change is temporary for this session." -ForegroundColor Gray
            Write-Host "To make permanent, edit SmallModelMode in heather_services.ps1" -ForegroundColor Gray
            Write-Host ""
            if (Test-PortInUse -Port $Config.BotMonitorPort) {
                Write-Host "Bot is currently running. Restart it (option 6 twice) to apply." -ForegroundColor Yellow
            }
            Write-Host ""
            Pause
        }
        "B" {
            Write-Host ""
            $Config.TextBackend = if ($Config.TextBackend -eq "llama") { "lmstudio" } else { "llama" }
            $newBackend = if ($Config.TextBackend -eq "llama") { "llama-server" } else { "LM Studio" }
            $bColor = if ($Config.TextBackend -eq "llama") { "Cyan" } else { "Magenta" }
            Write-Host "Text backend changed to: " -NoNewline
            Write-Host $newBackend -ForegroundColor $bColor
            Write-Host ""
            Write-Host "NOTE: This change is temporary for this session." -ForegroundColor Gray
            Write-Host "To make permanent, edit TextBackend in heather_services.ps1" -ForegroundColor Gray
            Write-Host ""
            if (Test-PortInUse -Port $Config.TextGenPort) {
                Write-Host "A text service is currently running on port $($Config.TextGenPort)." -ForegroundColor Yellow
                Write-Host "Stop it first, then start the new backend." -ForegroundColor Yellow
            }
            Write-Host ""
            Pause
        }
        "Q" { return }
        default { Write-Host "Invalid choice" -ForegroundColor Red; Start-Sleep -Seconds 1 }
    }
    
    if ($choice.ToUpper() -ne "Q") {
        Show-Menu
    }
}

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

switch ($Action.ToLower()) {
    "start" {
        switch ($Service.ToLower()) {
            "all" { Start-AllServices }
            "text" { Start-TextBackend }
            "llama" { Start-LlamaServer }
            "lmstudio" { Start-TextGenCLI }
            "ollama" { Start-Ollama }
            "bot" { Start-Bot }
            "comfyui" { Start-ComfyUI }
            "tts" { Start-TTS }
        }
    }
    "stop" {
        switch ($Service.ToLower()) {
            "all" { Stop-AllServices }
            "text" { Stop-TextBackend }
            "llama" { Stop-LlamaServer }
            "lmstudio" { Stop-TextGen }
            "ollama" { Stop-Ollama }
            "bot" { Stop-Bot }
            "comfyui" { Stop-ComfyUI }
            "tts" { Stop-TTS }
        }
    }
    "status" { Show-Status }
    "restart" {
        switch ($Service.ToLower()) {
            "all" { Stop-AllServices; Start-Sleep -Seconds 3; Start-AllServices }
            "text" { Stop-TextBackend; Start-Sleep -Seconds 2; Start-TextBackend }
            "llama" { Stop-LlamaServer; Start-Sleep -Seconds 2; Start-LlamaServer }
            "lmstudio" { Stop-TextGen; Start-Sleep -Seconds 2; Start-TextGenCLI }
            "ollama" { Stop-Ollama; Start-Sleep -Seconds 2; Start-Ollama }
            "bot" { Stop-Bot; Start-Sleep -Seconds 2; Start-Bot }
            "comfyui" { Stop-ComfyUI; Start-Sleep -Seconds 2; Start-ComfyUI }
            "tts" { Stop-TTS; Start-Sleep -Seconds 2; Start-TTS }
        }
    }
    "startall" { Start-AllServices }
    "stopall" { Stop-AllServices }
    "autostart" { Start-AllServicesAuto }
    "menu" { Show-Menu }
    default { Show-Menu }
}

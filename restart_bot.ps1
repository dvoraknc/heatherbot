# Heather Bot - Aggressive Restart Script
# Waits for confirmed process exit before launching new instance

$ErrorActionPreference = 'Continue'
$botDir = 'C:\Users\groot\heather-bot'
$maxKillWaitSec = 15
$maxStartWaitSec = 60
$monitorPort = 8888

function Get-BotProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'heather_telegram' }
}

function Get-PortListeners {
    param([int]$Port)
    netstat -ano | Select-String ":$Port\s" | Select-String 'LISTENING'
}

# -- Phase 1: Kill existing bot processes --
$procs = Get-BotProcesses
if ($procs) {
    Write-Host "Found $(@($procs).Count) bot process(es) to kill:"
    foreach ($p in $procs) {
        Write-Host "  PID $($p.ProcessId) - $($p.Name)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }

    # Wait for confirmed exit with polling
    $elapsed = 0
    while ($elapsed -lt $maxKillWaitSec) {
        Start-Sleep -Seconds 1
        $elapsed++
        $remaining = Get-BotProcesses
        if (-not $remaining) {
            Write-Host "All bot processes exited after ${elapsed}s"
            break
        }
        Write-Host "  Waiting for exit... (${elapsed}s) - $(@($remaining).Count) still alive"

        # Escalate at 5s: try taskkill /T (kill process tree)
        if ($elapsed -eq 5) {
            Write-Host "  Escalating: taskkill /F /T"
            foreach ($r in $remaining) {
                & taskkill /F /T /PID $r.ProcessId 2>$null
            }
        }

        # Escalate at 10s: kill any python process holding our port
        if ($elapsed -eq 10) {
            Write-Host "  Escalating: killing any process on port $monitorPort"
            $portLines = netstat -ano | Select-String ":${monitorPort}\s" | Select-String 'LISTENING'
            foreach ($line in $portLines) {
                if ($line -match '\s(\d+)\s*$') {
                    $pid = [int]$Matches[1]
                    Write-Host "    Killing port holder PID $pid"
                    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }

    # Final check
    $zombies = Get-BotProcesses
    if ($zombies) {
        Write-Host "WARNING: $(@($zombies).Count) process(es) refused to die after ${maxKillWaitSec}s!"
        foreach ($z in $zombies) {
            Write-Host "  Zombie PID $($z.ProcessId) - attempting final taskkill"
            & taskkill /F /PID $z.ProcessId 2>$null
        }
        Start-Sleep -Seconds 2
        $stillAlive = Get-BotProcesses
        if ($stillAlive) {
            Write-Host "FATAL: Cannot kill bot processes. Aborting restart."
            exit 1
        }
    }
} else {
    Write-Host "No existing bot processes found"
}

# -- Phase 2: Ensure port is free --
$portCheck = Get-PortListeners -Port $monitorPort
if ($portCheck) {
    Write-Host "Port $monitorPort still in use, waiting for release..."
    $portWait = 0
    while ($portWait -lt 10) {
        Start-Sleep -Seconds 1
        $portWait++
        $portCheck = Get-PortListeners -Port $monitorPort
        if (-not $portCheck) {
            Write-Host "Port $monitorPort freed after ${portWait}s"
            break
        }
    }
    if ($portCheck) {
        # Force kill whatever is on the port
        foreach ($line in $portCheck) {
            if ($line -match '\s(\d+)\s*$') {
                $pid = [int]$Matches[1]
                Write-Host "Force killing PID $pid holding port $monitorPort"
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 2
    }
}

# -- Phase 2.5: Clean up stale session journal --
$journalFile = Join-Path $botDir 'heather_session.session-journal'
if (Test-Path $journalFile) {
    Write-Host "Removing stale session journal..."
    try {
        Remove-Item $journalFile -Force -ErrorAction Stop
        Write-Host "Session journal cleaned up"
    } catch {
        Write-Host "WARNING: Could not remove session journal (may still be locked): $_"
        Start-Sleep -Seconds 3
        try {
            Remove-Item $journalFile -Force -ErrorAction Stop
            Write-Host "Session journal cleaned up on retry"
        } catch {
            Write-Host "WARNING: Journal still locked - bot will handle on startup"
        }
    }
}

# -- Phase 3: Start new bot instance --
Write-Host "Starting bot..."
Start-Process -FilePath 'python' -ArgumentList 'heather_telegram_bot.py','--monitoring','--small-model' -WorkingDirectory $botDir -WindowStyle Hidden

# -- Phase 4: Poll for readiness instead of fixed sleep --
$startElapsed = 0
$botReady = $false
while ($startElapsed -lt $maxStartWaitSec) {
    Start-Sleep -Seconds 3
    $startElapsed += 3

    # Check if process is running
    $newProc = Get-BotProcesses
    if (-not $newProc) {
        Write-Host "Bot process died during startup after ${startElapsed}s!"
        exit 1
    }

    # Check if monitoring port is listening
    $listening = Get-PortListeners -Port $monitorPort
    if ($listening) {
        $botReady = $true
        Write-Host "Bot is UP on port $monitorPort after ${startElapsed}s"
        Write-Host $listening
        break
    }

    Write-Host "  Waiting for bot startup... (${startElapsed}s)"
}

if (-not $botReady) {
    Write-Host "WARNING: Bot did not bind to port $monitorPort within ${maxStartWaitSec}s"
    $proc = Get-BotProcesses
    if ($proc) {
        Write-Host "  Process is running (PID $($proc[0].ProcessId)) but port not ready yet"
        Write-Host "  The NSFW classifier may still be loading - check manually"
    } else {
        Write-Host "  No bot process found - startup failed!"
        exit 1
    }
}

# ExecRelay — main entry point. Builds and starts the full stack (nats,
# ml-predictor, ingress, bridge, ea-shim, telegram-ingest, telegram-forwarder,
# trade-dashboard), prints the public TradingView webhook URL, the Rey Capital
# trade dashboard link and the Telegram bot link, then STAYS ATTACHED,
# streaming every service's log to the console (color-coded per service,
# errors in red) until Ctrl+C — which stops the whole stack.
#
# If the stack is already running, it does not double-start; it just attaches
# to the logs.
#
#   .\run.ps1                     # start + follow logs; Ctrl+C stops the stack
#   .\run.ps1 -LocalOnly          # ...without public-exposure verification
#   .\run.ps1 -NoFollow           # start, print links, and return (old behavior)
#   .\run.ps1 -AllLogs            # don't filter the log stream (raw firehose)
#   .\run.ps1 -StopOnExit:$false  # Ctrl+C detaches but leaves services running
#   .\stop.ps1                    # stop everything from another window
#
# The console stream is FILTERED by default to what an operator actually
# needs: warnings, errors, and business events (signals, orders, fills,
# relays). Health-check request logs, DEBUG chatter and over-long lines are
# dropped or truncated -- a single library exception can otherwise carry
# megabytes of binary. The log FILES always keep everything; -AllLogs shows
# the unfiltered stream.
#
# Implementation lives in scripts\local-stack.ps1 (start/stop/status).

param(
    [switch]$LocalOnly,
    [switch]$NoFollow,
    [switch]$AllLogs,
    [bool]$StopOnExit = $true
)

$APP_NAME = "ExecRelay"

$stack = Join-Path $PSScriptRoot "scripts\local-stack.ps1"
$logDir = Join-Path $PSScriptRoot ".local-stack\logs"
$pidDir = Join-Path $PSScriptRoot ".local-stack\pids"

# Name the console window so several stacks running side by side are
# tellable apart at a glance (taskbar included).
function Set-WindowTitle { param([string]$State) $Host.UI.RawUI.WindowTitle = "$APP_NAME - $State" }
Set-WindowTitle "starting..."

# --- console log filter ----------------------------------------------------
$MaxLineChars = 400
# Dropped outright: per-request health-check logs (every few seconds from the
# supervisor's own probes) and DEBUG-level lines.
$DropPatterns = @(
    '"path":"/health(z)?"',
    '"path":"/readyz"',
    '"level":"DEBUG"',
    'GET /health',
    'GET /readyz'
)
# Always shown, whatever else matches: real problems and business events.
$KeepPatterns = @(
    'ERROR', 'WARN', 'FATAL', 'Traceback', 'Exception', 'error=',
    'signal', 'order', 'fill', 'relayed', 'REJECT', 'risk sizing',
    'REGISTERED', 'healthy', 'listening', 'watching', 'attached'
)

function Test-ShowLine {
    param([string]$Line, [bool]$IsErr)
    if ($AllLogs -or $IsErr) { return $true }
    foreach ($p in $KeepPatterns) { if ($Line -match [regex]::Escape($p)) { return $true } }
    foreach ($p in $DropPatterns) { if ($Line -match $p) { return $false } }
    return $true
}

function Format-LogLine {
    param([string]$Line)
    $clean = $Line -replace '[^	 -￿]', ''   # strip control bytes
    if ($clean.Length -gt $MaxLineChars) {
        $dropped = $clean.Length - $MaxLineChars
        $clean = $clean.Substring(0, $MaxLineChars) + " ...(+$dropped chars, see log file)"
    }
    return $clean
}

# Seed log offsets with the files' current sizes so the follower only prints
# lines written from this moment on (the day's files are append-only).
$offsets = @{}
Get-ChildItem -Path $logDir -File -Filter "*.log" -ErrorAction SilentlyContinue |
    ForEach-Object { $offsets[$_.FullName] = $_.Length }

# Single-instance guard, two layers. Duplicate starts are dangerous here:
# Windows lets duplicate services share the same ports (SO_REUSEADDR), so a
# second stack doesn't fail cleanly -- and duplicate ea-shims would place
# duplicate trades.
#  1. A named mutex held for this runner's lifetime: a second run.ps1, even
#     launched in the same second, sees it immediately and attaches instead.
#  2. A port probe, for a stack started by other means (pid files can go
#     stale and pids get recycled; listening ports are the ground truth).
$runMutex = New-Object System.Threading.Mutex($false, "Global\ExecRelayRunPs1")
$ownsMutex = $false
try { $ownsMutex = $runMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }

function Test-StackUp {
    foreach ($port in 4222, 8081) {
        $client = New-Object Net.Sockets.TcpClient
        try {
            if ($client.ConnectAsync("127.0.0.1", $port).Wait(400) -and $client.Connected) { return $true }
        } catch {} finally { $client.Close() }
    }
    return $false
}

if (-not $ownsMutex) {
    Write-Host "another run.ps1 is active -- attaching to logs only" -ForegroundColor Yellow
} elseif (Test-StackUp) {
    Write-Host "stack already running -- attaching to logs (use .\stop.ps1 for a fresh start)" -ForegroundColor Yellow
} else {
    & $stack -Command start -Public:(-not $LocalOnly)
}

if ($NoFollow) { return }

$palette = @("Cyan", "Green", "Yellow", "Magenta", "Blue", "DarkCyan", "DarkYellow", "DarkGreen", "Gray", "White")
$colors = @{}
$colorIdx = 0

Write-Host ""
$filterNote = if ($AllLogs) { "unfiltered" } else { "filtered to warnings, errors and trade events -- use -AllLogs for everything" }
if ($StopOnExit) {
    Write-Host "following logs ($filterNote) -- Ctrl+C stops the stack" -ForegroundColor DarkGray
} else {
    Write-Host "following logs ($filterNote) -- Ctrl+C detaches (services keep running)" -ForegroundColor DarkGray
}
Write-Host ""

$hidden = 0
$nextTitle = Get-Date
Set-WindowTitle "running"

try {
    while ($true) {
        $today = Get-Date -Format "yyyy-MM-dd"
        $files = Get-ChildItem -Path $logDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*-$today*.log" }
        foreach ($f in $files) {
            $name = $f.Name -replace "-$today(\.err)?\.log$", ""
            $isErr = $f.Name -like "*.err.log"
            if (-not $colors.ContainsKey($name)) {
                $colors[$name] = $palette[$colorIdx % $palette.Count]
                $colorIdx++
            }
            if (-not $offsets.ContainsKey($f.FullName)) { $offsets[$f.FullName] = 0 }
            if ($f.Length -le $offsets[$f.FullName]) { continue }

            # Services hold these files open for writing -- open shared.
            $fs = [IO.File]::Open($f.FullName, "Open", "Read", "ReadWrite")
            try {
                $fs.Seek($offsets[$f.FullName], "Begin") | Out-Null
                $sr = New-Object IO.StreamReader($fs)
                while ($null -ne ($line = $sr.ReadLine())) {
                    if (-not $line.Trim()) { continue }
                    if (-not (Test-ShowLine -Line $line -IsErr $isErr)) { $hidden++; continue }
                    $fg = if ($isErr) { "Red" } else { $colors[$name] }
                    Write-Host ("{0,-20}" -f "[$name]") -ForegroundColor $fg -NoNewline
                    Write-Host " $(Format-LogLine $line)"
                }
                $offsets[$f.FullName] = $fs.Position
            } finally {
                $fs.Close()
            }
        }
        # Keep the title useful at a glance without repainting every tick.
        if ((Get-Date) -ge $nextTitle) {
            Set-WindowTitle "running - $(Get-Date -Format 'HH:mm')$(if ($hidden) { " - $hidden routine lines hidden" })"
            $nextTitle = (Get-Date).AddSeconds(30)
        }
        Start-Sleep -Milliseconds 700
    }
} finally {
    # Runs on Ctrl+C: take the whole stack down with the console session.
    if ($StopOnExit -and $ownsMutex) {
        Write-Host ""
        Write-Host "shutting down stack..." -ForegroundColor Yellow
        Set-WindowTitle "stopping..."
        & $stack -Command stop
        Set-WindowTitle "stopped"
    } else {
        Set-WindowTitle "detached (services still running)"
    }
    if ($ownsMutex) { try { $runMutex.ReleaseMutex() } catch {} }
    $runMutex.Dispose()
}

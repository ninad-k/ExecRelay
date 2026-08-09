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
#   .\run.ps1 -StopOnExit:$false  # Ctrl+C detaches but leaves services running
#   .\stop.ps1                    # stop everything from another window
#
# Implementation lives in scripts\local-stack.ps1 (start/stop/status).

param(
    [switch]$LocalOnly,
    [switch]$NoFollow,
    [bool]$StopOnExit = $true
)

$stack = Join-Path $PSScriptRoot "scripts\local-stack.ps1"
$logDir = Join-Path $PSScriptRoot ".local-stack\logs"
$pidDir = Join-Path $PSScriptRoot ".local-stack\pids"

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
if ($StopOnExit) {
    Write-Host "following logs -- Ctrl+C stops the stack" -ForegroundColor DarkGray
} else {
    Write-Host "following logs -- Ctrl+C detaches (services keep running)" -ForegroundColor DarkGray
}
Write-Host ""

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
                    $fg = if ($isErr) { "Red" } else { $colors[$name] }
                    Write-Host ("{0,-20}" -f "[$name]") -ForegroundColor $fg -NoNewline
                    Write-Host " $line"
                }
                $offsets[$f.FullName] = $fs.Position
            } finally {
                $fs.Close()
            }
        }
        Start-Sleep -Milliseconds 700
    }
} finally {
    # Runs on Ctrl+C: take the whole stack down with the console session.
    if ($StopOnExit -and $ownsMutex) {
        Write-Host ""
        Write-Host "shutting down stack..." -ForegroundColor Yellow
        & $stack -Command stop
    }
    if ($ownsMutex) { try { $runMutex.ReleaseMutex() } catch {} }
    $runMutex.Dispose()
}

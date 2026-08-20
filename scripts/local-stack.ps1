# ExecRelay local stack -- PowerShell start/end scripts (Windows-native
# counterpart to scripts/local-stack.sh, extended to also cover the Python
# execution shim, i.e. the full stack used for local demo-account testing
# against a running MT5 terminal).
#
#   .\run.ps1 (repo root)                    # the usual entry point: start -Public
#   scripts\local-stack.ps1 start            # build + start everything, wait healthy
#   scripts\local-stack.ps1 start -Public    # ...and print/verify the public webhook URL
#                                             #    (direct hosting on this machine's public
#                                             #    IP -- no tunnel service) so TradingView
#                                             #    can reach ingress. -Tunnel is a
#                                             #    deprecated alias for -Public.
#   scripts\local-stack.ps1 stop             # stop everything (or .\stop.ps1)
#   scripts\local-stack.ps1 status           # health-check each component
#
# Full guide: docs/development/windows-local-stack.md
#
# Services: nats (4222), ml-predictor (8080), ingress (8081), bridge (8082),
# ea-shim (MT5 execution, no HTTP port),
# trade-dashboard (8090, localhost-only trade summary UI -- now also serves
# management reporting: channel scorecard, risk/exposure panel, monthly P/L
# calendar, and a weekly XLSX export backed by .local-stack\execrelay.db,
# see docs/development/windows-local-stack.md "Management reporting").
# The XLSX export needs the openpyxl package (not required for the rest of
# the dashboard): python -m pip install openpyxl
#
# Public hosting (-Public): TradingView only delivers webhooks to ports 80 and
# 443, so ingress (8081) is reached through a Windows portproxy on port 80.
# One-time setup, run as Administrator (persists across reboots):
#   netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=80 connectaddress=127.0.0.1 connectport=8081
#   New-NetFirewallRule -DisplayName "ExecRelay ingress 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
# plus an inbound TCP 80 rule in the cloud firewall (e.g. the AWS security
# group). -Public verifies the portproxy and prints these commands if missing.
#
# Every service's stdout/stderr is written to a date-stamped file under
# .local-stack\logs (e.g. bridge-2026-08-05.log) and files older than
# RetentionDays are purged on every start -- this is per-start rotation, not
# live intra-run rollover: a service left running past midnight keeps
# writing to the day it started on until next restart. Fine for the
# start/stop/test-session workflow this harness is built for; the
# transactions\ subdirectory (scripts/_txnlog.py) rotates independently at
# actual UTC midnight since it's Python-native.
#
# Requires: the portable Go toolchain in .local-stack\go, a Python 3.11+
# interpreter with MetaTrader5/websockets installed for ea-shim, a running
# demo-account-logged-in MT5 terminal for ea-shim to attach to, and (for
# -Public) the one-time port-80 portproxy/firewall setup described above.

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Command = "status",
    [switch]$Public,
    [switch]$Tunnel,  # deprecated alias for -Public (kept so old invocations keep working)
    # Reuse .local-stack\bin\{ingress,bridge}.exe as-is instead of rebuilding.
    # Used by the SYSTEM-context scheduled tasks (ExecRelay-Stack-Boot/
    # -Watchdog, see docs/development/windows-local-stack.md "Reliability")
    # so a periodic watchdog run never has to invoke `go build` under an
    # account whose Go build cache/network context differs from the
    # interactive admin session that normally builds these -- a cold/stuck
    # SYSTEM-context build is exactly what caused the 2026-08-19 incident
    # this flag exists to avoid repeating. Falls back to building anyway if
    # the binary is simply missing (nothing to skip to).
    [switch]$SkipBuild
)
if ($Tunnel) { $Public = $true }

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Dir = Join-Path $Root ".local-stack"
$Bin = Join-Path $Dir "bin"
$Logs = Join-Path $Dir "logs"
$Pids = Join-Path $Dir "pids"
$PublicLinkPath = Join-Path $Dir "Public-Ingress-Link.url"

$NatsPort = 4222
$PredictorPort = 8080
$IngressPort = 8081
$BridgePort = 8082
$DashboardPort = 8090
$PublicPort = 80   # TradingView only posts webhooks to ports 80/443
$RetentionDays = 7

function Resolve-PythonExe {
    # $env:LOCALAPPDATA is per-account -- it points at a different (usually
    # empty) profile when this script runs as SYSTEM (e.g. from a Task
    # Scheduler task set to "run whether user is logged on or not", the
    # mechanism that lets the stack survive an RDP disconnect) rather than
    # interactively as the admin user who actually has Python installed. The
    # C:\Users\*\... glob finds it either way, so the same script works
    # unchanged both interactively and as a SYSTEM-run scheduled task.
    $candidates = @(
        "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe",
        (Get-ChildItem "C:\Users\*\AppData\Local\Python\pythoncore-*\python.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName),
        (Get-Command python -ErrorAction SilentlyContinue).Source,
        (Get-Command py -ErrorAction SilentlyContinue).Source
    ) | Where-Object { $_ -and (Test-Path $_) }
    if (-not $candidates) {
        throw "no python interpreter found; install Python 3.11+ and put it on PATH"
    }
    return $candidates[0]
}

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        [Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
}

function Format-CmdArg {
    param([string]$Value)
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Start-Tracked {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [hashtable]$Env = @{},
        [string]$WorkDir = $Root
    )
    $today = Get-Date -Format "yyyy-MM-dd"
    $stdout = Join-Path $Logs "$Name-$today.log"
    $stderr = Join-Path $Logs "$Name-$today.err.log"
    foreach ($k in $Env.Keys) { [Environment]::SetEnvironmentVariable($k, [string]$Env[$k], "Process") }

    # Start-Process's own -RedirectStandardOutput/-Error TRUNCATE the target
    # file on every launch -- Windows PowerShell 5.1 has no append mode for
    # it -- so restarting the stack mid-day (e.g. to deploy a fix) silently
    # wiped that day's log history each time. Routed through cmd.exe's `>>`
    # instead: real OS-level append, and (unlike a pipe + PowerShell event
    # reader) the file handle stays valid even after this launcher script's
    # own process exits, which every service here depends on since they're
    # meant to keep running detached long after `start` returns.
    #
    # cmd.exe stays alive as the real service's parent for its whole
    # lifetime (`cmd /c` waits on the child), so $proc.Id here is cmd's PID,
    # not the service's -- Invoke-Stop kills by PID with no tree-kill, so
    # tracking cmd's PID would leave the actual service orphaned on `stop`.
    # Poll for the real child PID instead and track that; once it's killed,
    # cmd.exe exits on its own with nothing left behind.
    $quotedInner = (@(Format-CmdArg $FilePath) + ($ArgumentList | ForEach-Object { Format-CmdArg $_ })) -join ' '
    $cmdLine = "$quotedInner >> `"$stdout`" 2>> `"$stderr`""
    $spArgs = @{
        FilePath                = "cmd.exe"
        ArgumentList             = @("/c", $cmdLine)
        WorkingDirectory         = $WorkDir
        WindowStyle              = "Hidden"
        PassThru                 = $true
    }
    $wrapper = Start-Process @spArgs

    # cmd.exe (run WindowStyle Hidden) spawns TWO children under its PID:
    # conhost.exe (console host, created for the hidden console) and the
    # actual target executable -- Win32_Process doesn't guarantee which one
    # WMI enumerates first, so filtering on ParentProcessId alone can (and
    # did, in testing) grab conhost.exe by mistake. Match on the target's own
    # leaf filename to get the real one.
    $exeLeaf = Split-Path $FilePath -Leaf
    $childId = $null
    for ($i = 0; $i -lt 30 -and -not $childId; $i++) {
        Start-Sleep -Milliseconds 100
        $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($wrapper.Id)" -ErrorAction SilentlyContinue
        $match = $kids | Where-Object { $_.Name -eq $exeLeaf } | Select-Object -First 1
        if ($match) { $childId = $match.ProcessId }
    }
    if (-not $childId) {
        Write-Warning "$Name`: could not resolve the real child PID under cmd.exe (pid $($wrapper.Id)); tracking the wrapper instead -- 'stop' may leave $Name running"
        $childId = $wrapper.Id
    }

    $childId | Out-File -FilePath (Join-Path $Pids "$Name.pid") -Encoding ascii
    Write-Host "  $Name`: pid $childId (log: $stdout)"
    return $childId
}

function Wait-Http {
    param([string]$Name, [string]$Url, [int]$Tries = 30)
    for ($i = 0; $i -lt $Tries; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) {
                Write-Host "  $Name`: healthy ($Url)"
                return
            }
        } catch { Start-Sleep -Seconds 1 }
    }
    Write-Warning "$Name`: FAILED to become healthy ($Url) -- check logs in $Logs"
}

function Wait-Tcp {
    param([string]$Name, [int]$Port, [int]$Tries = 30)
    for ($i = 0; $i -lt $Tries; $i++) {
        $ok = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue -InformationLevel Quiet
        if ($ok) {
            Write-Host "  $Name`: accepting connections (:$Port)"
            return
        }
        Start-Sleep -Seconds 1
    }
    Write-Warning "$Name`: FAILED to accept connections (:$Port) -- check logs in $Logs"
}

function Test-Health {
    param([string]$Name, [string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { Write-Host "$Name`: up ($Url)"; return }
    } catch {}
    Write-Host "$Name`: DOWN"
}

function Remove-OldLogs {
    $cutoff = (Get-Date).AddDays(-$RetentionDays)
    Get-ChildItem -Path $Logs -File -Filter "*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object {
            Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
            Write-Host "  purged old log: $($_.Name)"
        }
}

# The instance's internet-facing IP: EC2 instance metadata first (IMDSv2,
# link-local, no external dependency), then a generic echo service for
# non-AWS hosts. Returns $null when neither answers (fully offline box).
function Get-PublicIP {
    try {
        $token = Invoke-RestMethod -Method Put -Uri "http://169.254.169.254/latest/api/token" `
            -Headers @{ "X-aws-ec2-metadata-token-ttl-seconds" = "60" } -TimeoutSec 2
        return (Invoke-RestMethod -Uri "http://169.254.169.254/latest/meta-data/public-ipv4" `
            -Headers @{ "X-aws-ec2-metadata-token" = $token } -TimeoutSec 2)
    } catch {}
    try { return (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 5) } catch {}
    return $null
}

# True when a portproxy already forwards public port $Port to somewhere local.
# The forward itself is one-time admin setup (see header) -- this only checks.
function Test-PublicPortForward {
    param([int]$Port)
    $rules = netsh interface portproxy show v4tov4 2>$null
    return [bool]($rules | Select-String -Pattern "(0\.0\.0\.0|\*)\s+$Port\s")
}

# Writes a double-clickable Windows internet shortcut pointing at the current
# public webhook URL, or removes it when the stack has no public exposure so
# a leftover shortcut never points at a dead endpoint.
function Set-PublicLinkFile {
    param([string]$Path, [string]$Url)
    if ($Url) {
        $noBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($Path, "[InternetShortcut]`nURL=$Url`n", $noBom)
    } elseif (Test-Path $Path) {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
    }
}

function Get-NatsExe {
    $natsDir = Get-ChildItem -Path $Dir -Directory -Filter "nats-server-*-windows-amd64" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($natsDir) { return Join-Path $natsDir.FullName "nats-server.exe" }

    $version = "2.12.1"
    $zipUrl = "https://github.com/nats-io/nats-server/releases/download/v$version/nats-server-v$version-windows-amd64.zip"
    $zipPath = Join-Path $Dir "nats.zip"
    Write-Host "downloading nats-server v$version..."
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $Dir -Force
    Remove-Item $zipPath
    return Join-Path $Dir "nats-server-v$version-windows-amd64\nats-server.exe"
}

function Invoke-Start {
    # Refuse to double-start. Duplicate services don't fail cleanly on
    # Windows (SO_REUSEADDR lets them share ports), and a duplicate ea-shim
    # would execute every signal twice.
    try {
        $probe = Invoke-WebRequest -Uri "http://127.0.0.1:$IngressPort/health" -TimeoutSec 2 -UseBasicParsing
        if ($probe.StatusCode -eq 200) {
            Write-Warning "stack already running (ingress healthy on :$IngressPort) -- refusing to start twice. Run 'stop' first, or use .\run.ps1 to attach to its logs."
            return
        }
    } catch {}

    # Second guard, independent of the health check above: two overlapping
    # `start` calls (e.g. a manual run landing in the same window as the
    # 5-minute watchdog task) can BOTH observe ingress as unhealthy and race
    # past the check above before either finishes -- this is what let stuck
    # SYSTEM-context runs pile up on 2026-08-19 instead of the second one
    # backing off. A lock file closes that race regardless of what made the
    # first run slow. A lock older than 5 minutes is treated as stale (a
    # crashed run that never reached the `finally` below) rather than
    # honored forever.
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $lockFile = Join-Path $Dir "start.lock"
    if (Test-Path $lockFile) {
        $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
        if ($age.TotalMinutes -lt 5) {
            Write-Warning "another 'start' is already in progress (lock is $([int]$age.TotalSeconds)s old) -- refusing to start concurrently."
            return
        }
        Write-Warning "found a stale start.lock (>5 min old, so treated as a crashed prior run, not an active one) -- proceeding."
    }
    New-Item -ItemType File -Force -Path $lockFile | Out-Null
    try {
        New-Item -ItemType Directory -Force -Path $Bin, $Logs, $Pids | Out-Null
        Write-Host "purging logs older than $RetentionDays days..."
        Remove-OldLogs
        Import-DotEnv (Join-Path $Root ".env")

        if (-not $env:EXECRELAY_LICENSES) { $env:EXECRELAY_LICENSES = "60000000001:test-secret::test-instance:mt5" }
        if (-not $env:BRIDGE_AUTH_TOKEN) { $env:BRIDGE_AUTH_TOKEN = "test-bridge-token" }
        if (-not $env:ML_ENFORCE) { $env:ML_ENFORCE = "false" }
        if (-not $env:ML_THRESHOLD) { $env:ML_THRESHOLD = "0.50" }

        $goBin = Join-Path $Dir "go\bin"
        $env:PATH = "$goBin;$env:PATH"
        $python = Resolve-PythonExe
        Write-Host "using python: $python"

        $ingressExe = Join-Path $Bin "ingress.exe"
        $bridgeExe = Join-Path $Bin "bridge.exe"
        $haveBinaries = (Test-Path $ingressExe) -and (Test-Path $bridgeExe)
        if ($SkipBuild -and $haveBinaries) {
            Write-Host "skipping build, reusing existing $ingressExe / $bridgeExe"
        } else {
            if ($SkipBuild) {
                Write-Warning "-SkipBuild set but a binary is missing -- building anyway (nothing to reuse yet)"
            }
            Write-Host "building services..."
            & (Join-Path $goBin "go.exe") build -o $ingressExe "./apps/ingress/cmd/ingress"
            & (Join-Path $goBin "go.exe") build -o $bridgeExe "./apps/bridge/cmd/bridge"
        }

        Write-Host "starting stack..."
        $nats = Get-NatsExe
        Start-Tracked -Name "nats" -FilePath $nats -ArgumentList @("-js", "-p", "$NatsPort") | Out-Null
        Wait-Tcp -Name "nats" -Port $NatsPort

        Start-Tracked -Name "ml-predictor" -FilePath $python -ArgumentList @("app.py") `
            -WorkDir (Join-Path $Root "apps\ml-predictor") -Env @{
            HTTP_PORT = $PredictorPort; DEBUG = "false"
        } | Out-Null

        Start-Tracked -Name "ingress" -FilePath $ingressExe -ArgumentList @() -Env @{
            HTTP_ADDR = ":$IngressPort"
            NATS_URL = "nats://127.0.0.1:$NatsPort"
            WEBHOOK_RATE_LIMIT = "0"
            WEBHOOK_TIMESTAMP_WINDOW_SECS = "0"
            ML_PREDICTOR_URL = "http://127.0.0.1:$PredictorPort"
        } | Out-Null

        Start-Tracked -Name "bridge" -FilePath $bridgeExe -ArgumentList @() -Env @{
            HTTP_ADDR = ":$BridgePort"
            NATS_URL = "nats://127.0.0.1:$NatsPort"
        } | Out-Null

        Write-Host "waiting for health..."
        Wait-Http -Name "ml-predictor" -Url "http://127.0.0.1:$PredictorPort/healthz"
        Wait-Http -Name "ingress" -Url "http://127.0.0.1:$IngressPort/health"
        Wait-Http -Name "bridge" -Url "http://127.0.0.1:$BridgePort/health"

        Start-Tracked -Name "ea-shim" -FilePath $python -ArgumentList @("scripts\ea_shim.py") | Out-Null

        # Localhost-only (shows account balances; optional bearer-token auth via
        # DASHBOARD_TOKEN in .env) -- keep it off the public interface even
        # though ingress itself is exposed.
        Start-Tracked -Name "trade-dashboard" -FilePath $python `
            -ArgumentList @("scripts\trade_dashboard.py") -Env @{
            DASHBOARD_ADDR = "127.0.0.1:$DashboardPort"
        } | Out-Null
        Wait-Http -Name "trade-dashboard" -Url "http://127.0.0.1:$DashboardPort/health"

        if ($Public) {
            $ip = Get-PublicIP
            if (-not $ip) {
                Write-Warning "could not determine this machine's public IP -- no public webhook URL available"
            } else {
                if (-not (Test-PublicPortForward -Port $PublicPort)) {
                    Write-Warning ("public port $PublicPort is not forwarded to ingress yet. One-time setup, run as Administrator:`n" +
                        "  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$PublicPort connectaddress=127.0.0.1 connectport=$IngressPort`n" +
                        "  New-NetFirewallRule -DisplayName 'ExecRelay ingress $PublicPort' -Direction Inbound -Protocol TCP -LocalPort $PublicPort -Action Allow`n" +
                        "and allow inbound TCP $PublicPort in the cloud firewall (AWS security group) for this instance.")
                }
                $url = "http://$ip"
                Set-PublicLinkFile -Path $PublicLinkPath -Url $url
                Write-Host "  public ingress URL: $url"
                Write-Host "  webhook endpoints:  $url/webhook  and  $url/webhook/ml  (TradingView needs port 80/443)"
                Write-Warning "reachable from the public internet while port $PublicPort stays open -- still requires a valid license/secret to place a trade, but treat the URL as sensitive."
            }
        }

        $dashboardUrl = "http://127.0.0.1:$DashboardPort"
        if ($env:DASHBOARD_TOKEN) { $dashboardUrl += "?token=$($env:DASHBOARD_TOKEN)" }
        Write-Host ""
        Write-Host "  trade dashboard:  $dashboardUrl  (local only)"
        Write-Host ""
        Write-Host "stack is up. EA connects to 127.0.0.1:$BridgePort (instance: test-instance)."
    } finally {
        Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Stop {
    $pidFiles = Get-ChildItem -Path $Pids -Filter "*.pid" -ErrorAction SilentlyContinue
    if (-not $pidFiles) {
        Write-Host "nothing to stop (no pid files in $Pids)"
        return
    }
    foreach ($f in $pidFiles) {
        $name = $f.BaseName
        $procId = Get-Content $f.FullName | Select-Object -First 1
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  stopped $name (pid $procId)"
        } catch {
            Write-Host "  $name (pid $procId) already gone"
        }
        Remove-Item $f.FullName -Force
    }
    # Ingress is down, so the public webhook URL no longer answers.
    Set-PublicLinkFile -Path $PublicLinkPath -Url $null
}

function Invoke-Status {
    Test-Health -Name "ml-predictor" -Url "http://127.0.0.1:$PredictorPort/readyz"
    Test-Health -Name "ingress" -Url "http://127.0.0.1:$IngressPort/health"
    Test-Health -Name "bridge" -Url "http://127.0.0.1:$BridgePort/health"
    Test-Health -Name "trade-dashboard" -Url "http://127.0.0.1:$DashboardPort/health"
    foreach ($name in @("ea-shim")) {
        $pidFile = Join-Path $Pids "$name.pid"
        if (Test-Path $pidFile) {
            $procId = Get-Content $pidFile | Select-Object -First 1
            if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                Write-Host "$name`: running (pid $procId)"
            } else {
                Write-Host "$name`: DOWN (stale pid file)"
            }
        } else {
            Write-Host "$name`: not started"
        }
    }
    if (Test-Path $PublicLinkPath) {
        $line = Get-Content $PublicLinkPath | Where-Object { $_ -like "URL=*" } | Select-Object -First 1
        Write-Host "public webhook: $($line -replace '^URL=', '')/webhook"
    } else {
        Write-Host "public webhook: not exposed (start with -Public)"
    }
}

switch ($Command) {
    "start" { Invoke-Start }
    "stop" { Invoke-Stop }
    "status" { Invoke-Status }
}

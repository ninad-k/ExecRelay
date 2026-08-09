# ExecRelay local stack -- PowerShell start/end scripts (Windows-native
# counterpart to scripts/local-stack.sh, extended to also cover the Python
# execution shim and telegram-ingest, i.e. the full stack used for local
# demo-account testing against a running MT5 terminal).
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
# ea-shim (MT5 execution, no HTTP port), telegram-ingest (8089),
# telegram-forwarder (personal-account channel relay, no HTTP port),
# trade-dashboard (8090, localhost-only trade summary UI).
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
    [switch]$Tunnel  # deprecated alias for -Public (kept so old invocations keep working)
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
$TelegramIngestPort = 8089
$DashboardPort = 8090
$PublicPort = 80   # TradingView only posts webhooks to ports 80/443
$RetentionDays = 7

function Resolve-PythonExe {
    $candidates = @(
        "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe",
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
    $spArgs = @{
        FilePath                = $FilePath
        WorkingDirectory         = $WorkDir
        WindowStyle              = "Hidden"
        PassThru                 = $true
        RedirectStandardOutput   = $stdout
        RedirectStandardError    = $stderr
    }
    if ($ArgumentList.Count -gt 0) { $spArgs["ArgumentList"] = $ArgumentList }
    $proc = Start-Process @spArgs
    $proc.Id | Out-File -FilePath (Join-Path $Pids "$Name.pid") -Encoding ascii
    Write-Host "  $Name`: pid $($proc.Id) (log: $stdout)"
    return $proc
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

    Write-Host "building services..."
    & (Join-Path $goBin "go.exe") build -o (Join-Path $Bin "ingress.exe") "./apps/ingress/cmd/ingress"
    & (Join-Path $goBin "go.exe") build -o (Join-Path $Bin "bridge.exe") "./apps/bridge/cmd/bridge"

    Write-Host "starting stack..."
    $nats = Get-NatsExe
    Start-Tracked -Name "nats" -FilePath $nats -ArgumentList @("-js", "-p", "$NatsPort") | Out-Null
    Wait-Tcp -Name "nats" -Port $NatsPort

    Start-Tracked -Name "ml-predictor" -FilePath $python -ArgumentList @("app.py") `
        -WorkDir (Join-Path $Root "apps\ml-predictor") -Env @{
        HTTP_PORT = $PredictorPort; DEBUG = "false"
    } | Out-Null

    Start-Tracked -Name "ingress" -FilePath (Join-Path $Bin "ingress.exe") -ArgumentList @() -Env @{
        HTTP_ADDR = ":$IngressPort"
        NATS_URL = "nats://127.0.0.1:$NatsPort"
        WEBHOOK_RATE_LIMIT = "0"
        WEBHOOK_TIMESTAMP_WINDOW_SECS = "0"
        ML_PREDICTOR_URL = "http://127.0.0.1:$PredictorPort"
    } | Out-Null

    Start-Tracked -Name "bridge" -FilePath (Join-Path $Bin "bridge.exe") -ArgumentList @() -Env @{
        HTTP_ADDR = ":$BridgePort"
        NATS_URL = "nats://127.0.0.1:$NatsPort"
    } | Out-Null

    Write-Host "waiting for health..."
    Wait-Http -Name "ml-predictor" -Url "http://127.0.0.1:$PredictorPort/healthz"
    Wait-Http -Name "ingress" -Url "http://127.0.0.1:$IngressPort/health"
    Wait-Http -Name "bridge" -Url "http://127.0.0.1:$BridgePort/health"

    Start-Tracked -Name "ea-shim" -FilePath $python -ArgumentList @("scripts\ea_shim.py") | Out-Null

    if ($env:TELEGRAM_INGEST_BOT_TOKEN -and $env:TELEGRAM_INGEST_ALLOWED_CHAT_IDS) {
        # When the perimeter gate is on, every webhook caller (including the
        # internal ingest module) must carry ?token=<value>.
        $webhookUrl = "http://127.0.0.1:$IngressPort/webhook"
        if ($env:INGRESS_PERIMETER_TOKEN) { $webhookUrl += "?token=$($env:INGRESS_PERIMETER_TOKEN)" }
        Start-Tracked -Name "telegram-ingest" -FilePath $python -ArgumentList @("apps\telegram-ingest\app.py") -Env @{
            HTTP_ADDR = "0.0.0.0:$TelegramIngestPort"
            TELEGRAM_INGEST_WEBHOOK_URL = $webhookUrl
        } | Out-Null
        Start-Sleep -Seconds 2
        Wait-Http -Name "telegram-ingest" -Url "http://127.0.0.1:$TelegramIngestPort/health"
    } else {
        Write-Warning "skipping telegram-ingest: TELEGRAM_INGEST_BOT_TOKEN / TELEGRAM_INGEST_ALLOWED_CHAT_IDS not set in .env"
    }

    # Personal-account channel relay. Needs a one-time interactive login
    # (python scripts\telegram_user_forwarder.py login) done by the account
    # owner beforehand; if the session isn't authorized the process exits and
    # the error lands in telegram-forwarder-<date>.err.log.
    if ($env:TG_FORWARDER_API_ID -and $env:TG_FORWARDER_SOURCE_CHAT -and $env:TG_FORWARDER_TARGET_CHAT) {
        Start-Tracked -Name "telegram-forwarder" -FilePath $python `
            -ArgumentList @("scripts\telegram_user_forwarder.py", "run") | Out-Null
    } else {
        Write-Warning "skipping telegram-forwarder: TG_FORWARDER_SOURCE_CHAT / TG_FORWARDER_TARGET_CHAT (or API_ID) not set in .env -- Telegram signals will only flow if posted directly to the ingest bot's chat"
    }

    # Localhost-only (shows account balances, has no auth) -- keep it off the
    # public interface even though ingress itself is exposed.
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

    $botUser = if ($env:TELEGRAM_BOT_USERNAME) { $env:TELEGRAM_BOT_USERNAME } else { "TeleGoldSignalsBot" }
    Write-Host ""
    Write-Host "  trade dashboard:  http://127.0.0.1:$DashboardPort  (local only)"
    Write-Host "  telegram bot:     https://t.me/$botUser  (order + open/close notifications)"
    Write-Host ""
    Write-Host "stack is up. EA connects to 127.0.0.1:$BridgePort (instance: test-instance)."
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
    Test-Health -Name "telegram-ingest" -Url "http://127.0.0.1:$TelegramIngestPort/health"
    Test-Health -Name "trade-dashboard" -Url "http://127.0.0.1:$DashboardPort/health"
    foreach ($name in @("ea-shim", "telegram-forwarder")) {
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

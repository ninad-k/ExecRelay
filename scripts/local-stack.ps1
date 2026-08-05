# ExecRelay local stack -- PowerShell start/end scripts (Windows-native
# counterpart to scripts/local-stack.sh, extended to also cover the Python
# execution shim and telegram-ingest, i.e. the full stack used for local
# demo-account testing against a running MT5 terminal).
#
#   scripts\local-stack.ps1 start    # build + start everything, wait healthy
#   scripts\local-stack.ps1 stop     # stop everything started by this script
#   scripts\local-stack.ps1 status   # health-check each component
#
# Services: nats (4222), ml-predictor (8080), ingress (8081), bridge (8082),
# ea-shim (MT5 execution, no HTTP port), telegram-ingest (8089).
#
# Requires: the portable Go toolchain in .local-stack\go, a Python 3.11+
# interpreter with MetaTrader5/websockets installed for ea-shim, and a
# running, demo-account-logged-in MT5 terminal for ea-shim to attach to.

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Dir = Join-Path $Root ".local-stack"
$Bin = Join-Path $Dir "bin"
$Logs = Join-Path $Dir "logs"
$Pids = Join-Path $Dir "pids"

$NatsPort = 4222
$PredictorPort = 8080
$IngressPort = 8081
$BridgePort = 8082
$TelegramIngestPort = 8089

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
    $stdout = Join-Path $Logs "$Name.log"
    $stderr = Join-Path $Logs "$Name.err.log"
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
        Start-Tracked -Name "telegram-ingest" -FilePath $python -ArgumentList @("apps\telegram-ingest\app.py") -Env @{
            HTTP_ADDR = "0.0.0.0:$TelegramIngestPort"
            TELEGRAM_INGEST_WEBHOOK_URL = "http://127.0.0.1:$IngressPort/webhook"
        } | Out-Null
        Start-Sleep -Seconds 2
        Wait-Http -Name "telegram-ingest" -Url "http://127.0.0.1:$TelegramIngestPort/health"
    } else {
        Write-Warning "skipping telegram-ingest: TELEGRAM_INGEST_BOT_TOKEN / TELEGRAM_INGEST_ALLOWED_CHAT_IDS not set in .env"
    }

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
}

function Invoke-Status {
    Test-Health -Name "ml-predictor" -Url "http://127.0.0.1:$PredictorPort/readyz"
    Test-Health -Name "ingress" -Url "http://127.0.0.1:$IngressPort/health"
    Test-Health -Name "bridge" -Url "http://127.0.0.1:$BridgePort/health"
    Test-Health -Name "telegram-ingest" -Url "http://127.0.0.1:$TelegramIngestPort/health"
    $shimPidFile = Join-Path $Pids "ea-shim.pid"
    if (Test-Path $shimPidFile) {
        $procId = Get-Content $shimPidFile | Select-Object -First 1
        if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
            Write-Host "ea-shim: running (pid $procId)"
        } else {
            Write-Host "ea-shim: DOWN (stale pid file)"
        }
    } else {
        Write-Host "ea-shim: not started"
    }
}

switch ($Command) {
    "start" { Invoke-Start }
    "stop" { Invoke-Stop }
    "status" { Invoke-Status }
}

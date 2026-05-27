<#
.SYNOPSIS
    Production hardening for ExecRelay on Windows Server 2022.

.DESCRIPTION
    Prerequisite: scripts/install.ps1 has been run successfully and the stack
    is up inside WSL.

    This script:
      1. Installs Caddy as a native Windows binary via winget.
      2. Writes C:\caddy\Caddyfile from infra/caddy/Caddyfile.template,
         substituting Domain and a freshly-generated Grafana admin bcrypt.
      3. Registers Caddy as a Windows Service (auto-start) that reverse-
         proxies to the WSL-hosted services on localhost.
      4. Configures Windows Firewall: allow 80/443 inbound on Public; deny
         everything else inbound. App service ports (8081-8088, 3000, etc.)
         are already only reachable via WSL2 mirrored networking on
         localhost, so external traffic must go through Caddy.
      5. Registers a Scheduled Task that runs at startup to launch WSL +
         start the docker-compose stack (systemd inside WSL handles the
         per-service unit lifecycle).
      6. Reminds you to point DNS A records at this server.

.PARAMETER Domain
    Base domain. Caddy serves: Domain, api.Domain, hook.Domain, admin.Domain.

.PARAMETER Email
    Email passed to Let's Encrypt for cert renewal notices.

.PARAMETER WslDistro
    WSL distro name (default Ubuntu-22.04, must match install.ps1).

.EXAMPLE
    .\configure-prod.ps1 -Domain execrelay.example.com -Email ops@example.com

.NOTES
    Idempotent — safe to re-run after editing the Caddyfile template or
    rotating the Grafana password.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Domain,
    [Parameter(Mandatory=$true)][string]$Email,
    [string]$WslDistro = 'Ubuntu-22.04'
)

. "$PSScriptRoot\lib.ps1"

Assert-Administrator
Assert-WindowsServer2022

$repoOnWindows = (Get-Item -Path "$PSScriptRoot\..").FullName
$caddyDir = 'C:\caddy'
$caddyExe = Join-Path $caddyDir 'caddy.exe'
$caddyFile = Join-Path $caddyDir 'Caddyfile'

# ---- 1. Install Caddy --------------------------------------------------------
function Install-Caddy {
    if (Test-Path $caddyExe) {
        Write-Ok "Caddy already present at $caddyExe"
        return
    }
    Write-Log "installing Caddy via winget"
    & winget install --id CaddyServer.Caddy --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { Stop-WithError "winget install Caddy failed (exit $LASTEXITCODE)" }

    # winget puts caddy.exe somewhere under Program Files; copy to C:\caddy so
    # everything Caddy needs is in one predictable location.
    New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null
    $found = Get-Command caddy -ErrorAction SilentlyContinue
    if (-not $found) {
        Stop-WithError "winget reported success but 'caddy' is not on PATH; investigate winget install output"
    }
    Copy-Item -Path $found.Source -Destination $caddyExe -Force
    Write-Ok "Caddy installed at $caddyExe"
}

# ---- 2. Write Caddyfile ------------------------------------------------------
function Write-Caddyfile {
    Write-Log "writing $caddyFile for $Domain"

    # Generate a Grafana admin password + bcrypt for basic-auth at admin.$Domain.
    $pwFile      = Join-Path $caddyDir 'admin_password.txt'
    $bcryptFile  = Join-Path $caddyDir 'admin_bcrypt.txt'
    if (-not (Test-Path $bcryptFile) -or ((Get-Item $bcryptFile).Length -eq 0)) {
        $pw = New-Secret -Length 24
        $pw | Out-File -FilePath $pwFile -Encoding ascii -Force
        # Caddy reads stdin for hash-password
        $bcrypt = ($pw | & $caddyExe hash-password) 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $bcrypt) {
            Stop-WithError "caddy hash-password failed; cannot generate Grafana auth"
        }
        $bcrypt | Out-File -FilePath $bcryptFile -Encoding ascii -Force
        Write-Warn "Grafana admin password for admin.$Domain : $pw"
        Write-Warn "  (also saved to $pwFile — delete after the password is stored somewhere safe)"
    }
    $bcrypt = (Get-Content $bcryptFile -Raw).Trim()

    $template = Join-Path $repoOnWindows 'infra\caddy\Caddyfile.template'
    if (-not (Test-Path $template)) { Stop-WithError "$template missing" }

    $content = Get-Content $template -Raw
    # .Replace() = literal substitution. -replace = regex, which would treat
    # '$' as a backreference and corrupt bcrypt values (they contain literal
    # '$2a$10$...' separators).
    $content = $content.Replace('{{DOMAIN}}',       $Domain)
    $content = $content.Replace('{{ADMIN_BCRYPT}}', $bcrypt)
    # Prepend a global block with the LE contact email
    $globalBlock = "{`n`temail $Email`n}`n`n"
    if ($content -notmatch 'email\s+' ) {
        $content = $globalBlock + $content
    }
    $content | Out-File -FilePath $caddyFile -Encoding utf8 -Force

    & $caddyExe validate --config $caddyFile
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Caddyfile failed validation" }
    Write-Ok "wrote and validated $caddyFile"
}

# ---- 3. Caddy as Windows Service ---------------------------------------------
function Register-CaddyService {
    $svcName = 'ExecRelay-Caddy'
    $existing = Get-Service -Name $svcName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Log "restarting existing $svcName service"
        Restart-Service -Name $svcName
        return
    }
    Write-Log "registering $svcName as a Windows Service"
    # Caddy v2 has built-in run; wrap with sc.exe (simpler than nssm dependency).
    # Use --config so the service knows the file; --adapter caddyfile is default.
    $binPath = '"' + $caddyExe + '" run --config "' + $caddyFile + '" --adapter caddyfile'
    & sc.exe create $svcName binPath= $binPath start= auto DisplayName= "ExecRelay Caddy reverse proxy" | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-WithError "sc.exe create $svcName failed" }
    & sc.exe description $svcName "Caddy reverse proxy + Let's Encrypt TLS in front of the ExecRelay WSL stack." | Out-Null
    Start-Service -Name $svcName
    Write-Ok "$svcName started"
}

# ---- 4. Windows Firewall -----------------------------------------------------
function Set-Firewall {
    Write-Log "configuring Windows Firewall (allow 80, 443; deny app service ports inbound)"
    # Allow HTTP/HTTPS inbound for Caddy
    foreach ($p in 80,443) {
        $name = "ExecRelay-Allow-$p"
        if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow -Profile Any | Out-Null
            Write-Ok "added firewall rule $name"
        }
    }
    # Block direct access to app service ports from outside the host. WSL2
    # mirrored networking exposes them on localhost; this rule ensures no
    # external interface can reach them even if mirrored mode is later disabled.
    $blockPorts = @(3000,3001,3200,3306,4222,5432,6379,8081,8082,8083,8084,8085,8086,8087,8088,9000,9001,9090,9093,9187,9121)
    $blockName = 'ExecRelay-Block-App-Ports'
    if (-not (Get-NetFirewallRule -DisplayName $blockName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $blockName -Direction Inbound -Protocol TCP `
            -LocalPort $blockPorts -Action Block -Profile Public,Domain `
            -Description "Deny direct access to ExecRelay app service ports from any network — Caddy on 80/443 is the only entry point." | Out-Null
        Write-Ok "added firewall block rule $blockName"
    }
}

# ---- 5. Scheduled Task: start stack on boot ----------------------------------
function Register-StartupTask {
    $taskName = 'ExecRelay-Stack-Startup'
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Write-Log "scheduled task $taskName already exists; updating"
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Log "registering scheduled task: $taskName"

    # The task boots WSL and lets systemd inside WSL handle service start.
    # We also explicitly bring the apps profile up in case the user removed
    # any per-service unit.
    $cmd = "wsl.exe -d $WslDistro --user root -- bash -lc 'cd /root/ExecRelay && docker compose --profile apps up -d'"
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Boot WSL and bring the ExecRelay docker compose stack up on Windows startup." | Out-Null
    Write-Ok "scheduled task $taskName registered (runs as SYSTEM at boot)"
}

# ---- 6. Restart the stack ----------------------------------------------------
function Restart-Stack {
    Write-Log "restarting stack so the new configuration takes effect"
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command 'cd /root/ExecRelay && docker compose --profile apps up -d --force-recreate'
}

# ---- main --------------------------------------------------------------------
Write-Log "configuring production for $Domain"
Install-Caddy
Write-Caddyfile
Register-CaddyService
Set-Firewall
Register-StartupTask
Restart-Stack

$pw = if (Test-Path (Join-Path $caddyDir 'admin_password.txt')) { (Get-Content (Join-Path $caddyDir 'admin_password.txt') -Raw).Trim() } else { '(see C:\caddy\admin_password.txt)' }
Write-Ok @"

Production hardening complete.

  DNS — point these A records at this server's public IP:
    $Domain
    api.$Domain
    hook.$Domain
    admin.$Domain

  Caddy requests Let's Encrypt certificates on first request to each domain.
  Tail the service:  Get-Service ExecRelay-Caddy | Stop/Start-Service
  Inspect logs:      Get-EventLog -LogName Application -Source ExecRelay-Caddy

  Verify firewall:   Get-NetFirewallRule -DisplayName 'ExecRelay-*'
  Verify task:       Get-ScheduledTask -TaskName 'ExecRelay-Stack-Startup'
  Verify stack:      wsl -d $WslDistro -- bash -lc 'docker compose --profile apps ps'

  Grafana admin password (write down NOW):  $pw
"@

<#
.SYNOPSIS
    Single-server installer for ExecRelay on Windows Server 2022.

.DESCRIPTION
    Bootstraps the full ExecRelay stack on a fresh Windows Server 2022 host by:

      1. Enabling the WSL and VirtualMachinePlatform Windows features.
      2. Installing/updating the WSL2 kernel.
      3. Installing Ubuntu 22.04 as the default WSL distro.
      4. Enabling systemd inside WSL (required for the Linux scripts).
      5. Configuring WSL2 mirrored networking so services bound inside WSL
         are reachable as localhost on the Windows host.
      6. Cloning the ExecRelay repo inside WSL (in the Linux filesystem for
         speed) and running scripts/install.sh — which installs Docker
         Engine, generates secrets, applies DB migrations, and brings up
         the apps profile.

    On the first run a REBOOT is required after the Windows features are
    enabled. Re-run this script after the reboot — it's idempotent and
    will skip steps already completed.

.PARAMETER RepoUrl
    Git URL to clone inside WSL. Defaults to the public repo.

.PARAMETER WslDistro
    Name of the WSL distro to use. Defaults to Ubuntu-22.04.

.EXAMPLE
    # First run (will reboot):
    .\install.ps1

    # Custom repo / fork:
    .\install.ps1 -RepoUrl https://github.com/yourorg/ExecRelay.git

.NOTES
    Run as Administrator from an elevated PowerShell prompt.
    After this script finishes, run configure-prod.ps1 to add Caddy +
    Windows Firewall + auto-start Scheduled Task.
#>
[CmdletBinding()]
param(
    [string]$RepoUrl   = 'https://github.com/ninad-k/ExecRelay.git',
    [string]$WslDistro = 'Ubuntu-22.04'
)

. "$PSScriptRoot\lib.ps1"

Assert-Administrator
Assert-WindowsServer2022
Test-Virtualization

# ---- 1. Windows features ------------------------------------------------------
function Enable-WslFeatures {
    $needsReboot = $false
    $features = 'Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform'
    foreach ($f in $features) {
        $state = (Get-WindowsOptionalFeature -Online -FeatureName $f).State
        if ($state -ne 'Enabled') {
            Write-Log "enabling Windows feature: $f"
            $r = Enable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart -All
            if ($r.RestartNeeded) { $needsReboot = $true }
        } else {
            Write-Ok "$f already enabled"
        }
    }
    if ($needsReboot) {
        Write-Warn "Windows features enabled — REBOOT REQUIRED before WSL2 can run."
        Write-Warn "Reboot, then re-run this script. It's idempotent and will resume."
        Restart-Computer -Confirm
        exit 0
    }
}

# ---- 2. WSL kernel + distro ---------------------------------------------------
function Install-WslKernel {
    Write-Log "ensuring WSL2 kernel is up to date"
    & wsl.exe --update --web-download
    if ($LASTEXITCODE -ne 0) { Stop-WithError "wsl --update failed" }
    & wsl.exe --set-default-version 2 | Out-Null
}

function Install-UbuntuDistro {
    $installed = & wsl.exe --list --quiet 2>$null | ForEach-Object { ($_ -replace "`0","").Trim() } | Where-Object { $_ }
    if ($installed -contains $WslDistro) {
        Write-Ok "$WslDistro already installed"
        return
    }
    Write-Log "installing $WslDistro (this can take several minutes)"
    & wsl.exe --install -d $WslDistro --no-launch
    if ($LASTEXITCODE -ne 0) { Stop-WithError "wsl --install -d $WslDistro failed" }

    # First boot of the distro creates the default user account interactively.
    # Skip that with --user root for the bootstrap; the user can create accounts later.
    Write-Log "initializing $WslDistro (root-only first boot)"
    & wsl.exe -d $WslDistro --user root -- bash -lc 'echo wsl-init-ok' | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-WithError "$WslDistro failed to boot" }
}

# ---- 3. systemd + mirrored networking -----------------------------------------
function Set-WslSystemd {
    Write-Log "enabling systemd inside $WslDistro (required for service auto-start)"
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command @"
cat > /etc/wsl.conf <<EOF
[boot]
systemd=true

[network]
generateResolvConf=true
EOF
"@
}

function Set-WslMirroredNetworking {
    $cfgPath = Join-Path $env:USERPROFILE '.wslconfig'
    Write-Log "writing $cfgPath for mirrored networking"
    @'
[wsl2]
networkingMode=mirrored
firewall=true
dnsTunneling=true

[experimental]
hostAddressLoopback=true
'@ | Out-File -FilePath $cfgPath -Encoding ascii -Force
    Write-Ok "wrote $cfgPath"
}

function Restart-Wsl {
    Write-Log "restarting WSL so the new config takes effect"
    & wsl.exe --shutdown
    Start-Sleep -Seconds 5
    # Boot the distro again
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command 'systemctl is-system-running || true' | Out-Null
}

# ---- 4. Run the existing bash installer inside WSL ----------------------------
function Initialize-Stack {
    # Clone the repo INSIDE the WSL native filesystem (much faster than /mnt/c).
    Write-Log "cloning $RepoUrl into ~/ExecRelay inside $WslDistro"
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command @"
set -e
apt-get update -qq
apt-get install -y -qq git ca-certificates
if [ -d /root/ExecRelay/.git ]; then
  cd /root/ExecRelay && git pull --ff-only
else
  git clone --depth 50 $RepoUrl /root/ExecRelay
fi
"@

    Write-Log "running scripts/install.sh inside $WslDistro (Docker install + .env + migrations + up)"
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command 'cd /root/ExecRelay && bash scripts/install.sh'
}

# ---- main ---------------------------------------------------------------------
Write-Log "ExecRelay Windows Server installer (WSL2 + Ubuntu $WslDistro)"
Enable-WslFeatures
Install-WslKernel
Install-UbuntuDistro
Set-WslSystemd
Set-WslMirroredNetworking
Restart-Wsl
Initialize-Stack

Write-Ok @"

ExecRelay is running inside WSL on this host.

  The stack listens on localhost ports (via WSL2 mirrored networking):
    Portal web   →  http://localhost:3001
    Portal API   →  http://localhost:8085
    Ingress      →  http://localhost:8081/webhook
    Grafana      →  http://localhost:3000   (admin / admin)
    Prometheus   →  http://localhost:9090

  Next steps:
    1. Production hardening (Caddy + TLS + Firewall + auto-start):
         .\configure-prod.ps1 -Domain execrelay.example.com -Email you@example.com
    2. Nightly Postgres backups:
         .\install-backups.ps1

  Useful WSL commands:
    wsl -d $WslDistro -- bash -lc 'docker compose --profile apps ps'
    wsl -d $WslDistro -- bash -lc 'docker compose logs -f ingress'
    wsl --shutdown   # restart WSL entirely
"@

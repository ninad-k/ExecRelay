# scripts/lib.ps1 — common helpers for the Windows installer scripts.
# Dot-source from other scripts:  . "$PSScriptRoot\lib.ps1"

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log  { param([string]$m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Stop-WithError  { param([string]$m) Write-Host "[x] $m" -ForegroundColor Red; throw $m }

function Assert-Administrator {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Stop-WithError "This script must be run as Administrator. Re-launch PowerShell with 'Run as Administrator'."
    }
}

function Assert-WindowsServer2022 {
    $os = Get-CimInstance Win32_OperatingSystem
    if ($os.Caption -notmatch 'Windows Server') {
        Write-Warn "This script targets Windows Server 2022. Detected: $($os.Caption). Proceed at your own risk."
        return
    }
    # 2022 build numbers are 20348.*; 2025 is 26100.*.
    $build = [int]$os.BuildNumber
    if ($build -lt 20348) {
        Stop-WithError "Windows Server 2022 or newer required (need build >= 20348; have $build). WSL2 support on Server 2019 is too limited."
    }
    if ($build -ge 26100) {
        Write-Log "Detected Windows Server 2025 (build $build) — same flow as 2022."
    }
}

function Test-Virtualization {
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    if (-not $cpu.VirtualizationFirmwareEnabled) {
        Stop-WithError "Hardware virtualization is not enabled in BIOS/UEFI. WSL2 requires it. Enable VT-x (Intel) or AMD-V (AMD) and reboot."
    }
}

# Generate a URL-safe random secret of the given length.
function New-Secret {
    param([int]$Length = 32)
    $bytes = New-Object byte[] $Length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    # base64url, trimmed to requested length
    ([Convert]::ToBase64String($bytes) -replace '\+','-' -replace '/','_' -replace '=','').Substring(0, $Length)
}

# Run a command inside the named WSL distro as root, fail on non-zero exit.
function Invoke-Wsl {
    param(
        [Parameter(Mandatory=$true)][string]$Distro,
        [Parameter(Mandatory=$true)][string]$Command,
        [switch]$AsRoot
    )
    $userArg = if ($AsRoot) { @('--user','root') } else { @() }
    & wsl.exe -d $Distro @userArg -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "WSL command failed (exit $LASTEXITCODE): $Command"
    }
}

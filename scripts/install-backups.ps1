<#
.SYNOPSIS
    Register the nightly Postgres backup as a Windows Scheduled Task.

.DESCRIPTION
    Wraps the existing Linux scripts/backup.sh (running inside WSL) in a
    Scheduled Task that fires at 03:15 local time daily. The actual dump
    is taken by docker exec inside WSL — same script the Ubuntu installer
    uses — so the on-disk layout under /var/backups/execrelay (inside WSL)
    is identical between the two platforms.

    To also keep a copy on the Windows side, the script optionally creates
    a symlink in C:\backups\execrelay pointing into the WSL filesystem
    (\\wsl$\Ubuntu-22.04\var\backups\execrelay) so the backups are visible
    from Windows tools (Veeam, Robocopy, OneDrive sync, etc.).

.PARAMETER WslDistro
    WSL distro name (default Ubuntu-22.04, must match install.ps1).

.PARAMETER RunHour / RunMinute
    Local time to run the backup. Defaults to 03:15.

.EXAMPLE
    .\install-backups.ps1
    .\install-backups.ps1 -RunHour 2 -RunMinute 30
#>
[CmdletBinding()]
param(
    [string]$WslDistro = 'Ubuntu-22.04',
    [int]$RunHour      = 3,
    [int]$RunMinute    = 15
)

. "$PSScriptRoot\lib.ps1"

Assert-Administrator
Assert-WindowsServer2022

# ---- 1. Make sure the Linux side is ready ------------------------------------
function Initialize-LinuxBackup {
    Write-Log "running scripts/install-backups.sh inside $WslDistro to enable the Linux systemd timer too"
    # We run the Linux script as well so backups exist even if the Windows
    # scheduled task is ever deleted. Defense in depth — both timers will fire,
    # but the backup script is idempotent and writes a uniquely-named file
    # per run, so duplicates don't conflict.
    Invoke-Wsl -Distro $WslDistro -AsRoot -Command 'cd /root/ExecRelay && bash scripts/install-backups.sh'
}

# ---- 2. Windows Scheduled Task ------------------------------------------------
function Register-BackupTask {
    $taskName = 'ExecRelay-Postgres-Backup'
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Write-Log "scheduled task $taskName already exists; updating"
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Log "registering scheduled task: $taskName ($('{0:D2}:{1:D2}' -f $RunHour, $RunMinute) daily)"

    $cmd = "wsl.exe -d $WslDistro --user root -- bash -lc 'cd /root/ExecRelay && bash scripts/backup.sh'"
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($RunHour).AddMinutes($RunMinute))
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Nightly pg_dump of ExecRelay Postgres via WSL." | Out-Null
    Write-Ok "scheduled task $taskName registered"
}

# ---- 3. Symlink for Windows-side visibility ----------------------------------
function New-WindowsSymlink {
    $linkPath = 'C:\backups\execrelay'
    $target   = "\\wsl$\$WslDistro\var\backups\execrelay"
    if (Test-Path $linkPath) {
        Write-Ok "$linkPath already exists; leaving it alone"
        return
    }
    New-Item -ItemType Directory -Force -Path 'C:\backups' | Out-Null
    try {
        New-Item -ItemType SymbolicLink -Path $linkPath -Target $target -ErrorAction Stop | Out-Null
        Write-Ok "created symlink $linkPath -> $target"
    } catch {
        Write-Warn "could not create symbolic link (needs Developer Mode or admin token; skipping)"
        Write-Warn "  Manually access backups from Explorer at: $target"
    }
}

# ---- main --------------------------------------------------------------------
Write-Log "installing ExecRelay backup scheduling"
Initialize-LinuxBackup
Register-BackupTask
New-WindowsSymlink

Write-Ok @"

Backups are configured. They will fire from two places (idempotent — each
run writes a uniquely timestamped file):

  1. Windows Scheduled Task 'ExecRelay-Postgres-Backup' at $('{0:D2}:{1:D2}' -f $RunHour, $RunMinute) local time.
  2. Linux systemd timer 'execrelay-backup.timer' at 03:15 UTC (inside WSL).

  Test the Windows task now:
    Start-ScheduledTask -TaskName ExecRelay-Postgres-Backup
    Get-ScheduledTaskInfo -TaskName ExecRelay-Postgres-Backup

  View the resulting dumps from Windows:
    dir C:\backups\execrelay\daily

  Or from WSL:
    wsl -d $WslDistro -- bash -lc 'ls -la /var/backups/execrelay/daily'

  To enable S3 upload, set BACKUP_S3_BUCKET as a per-user env var inside WSL
  (or edit the systemd service drop-in described in scripts/install-backups.sh).
"@

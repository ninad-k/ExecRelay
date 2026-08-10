# Companion to run.ps1 — stops every service that the stack started.
#
#   .\stop.ps1

$Host.UI.RawUI.WindowTitle = "ExecRelay - stopping..."
& "$PSScriptRoot\scripts\local-stack.ps1" -Command stop

$Host.UI.RawUI.WindowTitle = "ExecRelay - stopped"

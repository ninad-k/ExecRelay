# Companion to run.ps1 — stops every service that the stack started.
#
#   .\stop.ps1

& "$PSScriptRoot\scripts\local-stack.ps1" -Command stop

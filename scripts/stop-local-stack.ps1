# Companion to start-local-stack.ps1 -- stops every service (and the
# Cloudflare tunnel, if one was started) that local-stack.ps1 started.
#
#   scripts\stop-local-stack.ps1

& "$PSScriptRoot\local-stack.ps1" -Command stop

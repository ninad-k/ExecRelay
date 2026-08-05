# Thin start/stop pair mirroring TeleTrader's start_teletrader.bat /
# stop_teletrader.bat naming. Behavior lives in local-stack.ps1 (start/stop/
# status subcommands) so there's one implementation to maintain; these just
# forward to it.
#
#   scripts\start-local-stack.ps1            # start everything
#   scripts\start-local-stack.ps1 -Tunnel    # ...and expose ingress publicly

param([switch]$Tunnel)

& "$PSScriptRoot\local-stack.ps1" -Command start -Tunnel:$Tunnel

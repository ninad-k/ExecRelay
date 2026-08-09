# ExecRelay — main entry point. Builds and starts the full local stack
# (nats, ml-predictor, ingress, bridge, ea-shim, telegram-ingest,
# telegram-forwarder, trade-dashboard), waits for health, and prints the
# public TradingView webhook URL, the Rey Capital trade dashboard link, and
# the Telegram bot link. Behavior lives in scripts\local-stack.ps1
# (start/stop/status subcommands); this is a thin forwarder.
#
#   .\run.ps1              # start everything + public webhook (default)
#   .\run.ps1 -LocalOnly   # start without verifying/printing public exposure
#   .\stop.ps1             # stop everything
#   scripts\local-stack.ps1 status   # health-check each component

param([switch]$LocalOnly)

& "$PSScriptRoot\scripts\local-stack.ps1" -Command start -Public:(-not $LocalOnly)

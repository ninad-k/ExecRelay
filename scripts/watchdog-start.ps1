# Invoked by the ExecRelay-Stack-Boot and ExecRelay-Stack-Watchdog scheduled
# tasks (both run as SYSTEM, independent of any RDP session). local-stack.ps1
# start is already idempotent -- it no-ops with a warning if ingress is
# already healthy -- so calling this on a timer is a safe self-healing
# watchdog, not just a one-shot boot hook.
#
# -SkipBuild: `go build` under SYSTEM hung on 2026-08-19 (its build
# cache/network context differs from the interactive admin session that
# normally builds these), and every 5-minute retrigger piled another stuck
# process on top before the first ever finished. The binaries only change
# when this code is edited -- which happens interactively, where `start`
# still rebuilds by default -- so the watchdog has no reason to ever invoke
# go.exe itself; it just launches whatever's already in .local-stack\bin.
& "C:\ExecRelay\scripts\local-stack.ps1" start -Public -SkipBuild *>> "C:\ExecRelay\.local-stack\logs\watchdog.log"

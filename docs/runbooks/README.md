# Runbooks

When an alert fires, find the matching runbook here and follow it. Every
runbook has the same shape:

1. **Symptom** — what the alert / page actually says
2. **Triage** — first 60 seconds of investigation
3. **Diagnosis** — narrowing to the root cause
4. **Mitigation** — the immediate fix to restore service
5. **Root cause checklist** — what to investigate after service is restored
6. **Postmortem prompts** — questions for the followup writeup

Runbooks are **opinionated**, not exhaustive. If you find yourself doing
something this doc doesn't cover, update the runbook in the same PR as
the fix.

## Index

| Runbook | When to use |
|---|---|
| [`ingress-5xx.md`](ingress-5xx.md) | Ingress is returning 5xx, or the `IngressHighErrorRate` alert fired |
| [`postgres-down.md`](postgres-down.md) | `PostgresDown` alert; cold-path services failing |
| [`kill-switch-tripped.md`](kill-switch-tripped.md) | `TradingHalted` alert; was it intentional? |
| [`fills-not-arriving.md`](fills-not-arriving.md) | Signals are accepted but fills aren't landing |
| [`license-misconfigured.md`](license-misconfigured.md) | `ingress_license_config_warnings{issue="no_auth"}` is 1 |

## See also

- [`docs/observability.md`](../observability.md) — what every metric and
  alert means
- [`docs/disaster-recovery.md`](../disaster-recovery.md) — for total host
  loss / data corruption scenarios that exceed normal runbook scope

# Runbook: Ingress is returning 5xx

## Symptom

- `IngressHighErrorRate` alert: `rate(ingress_webhook_requests_total{status=~"5.."}[5m]) > 1`
- Customer complaints: "my TradingView alert says 503"
- `ingress_webhook_requests_total{status="503"}` rising in Grafana

## Triage (first 60 seconds)

```sh
# 1. Is ingress alive at all?
docker compose ps ingress           # Should be "running (healthy)"
curl -sf http://localhost:8081/health || echo DOWN

# 2. What status codes are we returning?
curl -s http://localhost:8081/metrics | grep ^ingress_webhook_requests_total

# 3. What rejection reason is spiking?
curl -s http://localhost:8081/metrics | grep ^ingress_rejections_total
```

The combination of (1)+(2)+(3) tells you whether ingress is **down**,
**up but failing**, or **up and intentionally rejecting**.

## Diagnosis tree

### If `/health` returns 5xx or hangs

Ingress is broken. Skip to **Mitigation → restart**.

### If `/health` is OK and `ingress_webhook_requests_total{status="503"}` is the spike

Look at `ingress_rejections_total`:

| Top reason | Meaning |
|---|---|
| `publish_failed` | NATS is down or unreachable. **Go to [`postgres-down.md`-style NATS investigation](#nats-publish-failures)** |
| `trading_halted` | Kill switch is on. **Go to [`kill-switch-tripped.md`](kill-switch-tripped.md)** |
| (rare) `encode_failed` | Internal bug; ingress couldn't marshal protobuf |

### If `ingress_webhook_requests_total{status="401"}` or `{status="403"}` is the spike

Not a 5xx, but worth checking — usually a customer-side misconfig:

| Top reason | Likely cause |
|---|---|
| `signature_rejected` | Customer rotated a key without confirming, or HMAC bug in their producer |
| `license_rejected` | License toggled inactive in portal; or wrong UUID being sent |
| `perimeter_rejected` | Wrong / missing `?token=` query param |

## NATS publish failures

```sh
# Is NATS up?
docker compose ps nats
curl -sf http://localhost:8222/healthz || echo NATS_DOWN

# Active connections / streams
curl -s http://localhost:8222/connz?subs=1 | head -50
curl -s http://localhost:8222/jsz | head -30
```

If NATS is down: restart it (`docker compose restart nats`). Ingress will
start succeeding again within seconds. **Any signals that hit ingress
during the outage were rejected with 503 — they are gone.** TradingView
usually retries once.

If NATS is up but ingress still can't publish: check ingress logs for
the actual NATS error:

```sh
docker compose logs ingress --tail=200 | grep -i 'publish\|nats'
```

Common: NATS user/pass mismatch between `ingress` env and `nats` config.

## Mitigation

### Restart ingress

```sh
docker compose restart ingress
# Watch it recover
docker compose logs -f ingress | head -50
```

### Roll back recent deploys

```sh
git log --oneline -10                # Find the last good commit
git checkout <good-commit>
docker compose --profile apps up -d --build ingress
```

### Halt trading while investigating

If something is dangerously wrong, halt the whole webhook layer rather
than serve broken responses:

```sh
curl -X POST "https://hook.example.com/admin/kill-switch?token=$TOKEN&state=on"
```

This returns 503 `trading_halted` to all callers — explicit, expected,
and rejected before any downstream consequence.

## Root cause checklist

Once service is restored, before closing the incident:

- [ ] Was there a deploy in the previous hour? (`git log --since=1.hour`)
- [ ] Was there a config change? (`.env`, license update via portal)
- [ ] Did NATS or Postgres flap? Check their logs for the same window.
- [ ] Was the host under CPU/memory pressure? (`docker stats`)
- [ ] Were there infrastructure changes (network, DNS)?
- [ ] Is `bridge_consumer_lag_pending` elevated? Maybe the slowdown is
      downstream and ingress 503s are NATS backpressure.

## Postmortem prompts

- What was the customer-visible duration?
- How many trades were lost (not just attempted)?
- Did the kill switch get used? Should it have?
- What metric or alert *should* have caught this earlier?
- Is there a unit / integration test that should have caught this in CI?

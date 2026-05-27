# Runbook: Kill switch tripped

## Symptom

- `TradingHalted` alert: `ingress_trading_halted == 1` for 5m
- Customers reporting `503 trading_halted` on every webhook
- Ingress `ingress_rejections_total{reason="trading_halted"}` rising

## First question: was it intentional?

The kill switch is exactly one of:

| Source | How to verify |
|---|---|
| **Operator (you/colleague)** via `POST /admin/kill-switch?state=on` | `docker compose logs ingress \| grep 'kill switch toggled'` — includes client IP |
| **Operator** via `INGRESS_TRADING_HALTED=true` in `.env` at start | `grep TRADING_HALTED .env` |
| **Nobody** — it's been on since a restart for some other reason | Same env check |

```sh
# Find who toggled and when
docker compose logs ingress --since 24h | grep 'kill switch toggled'
# Sample line:
# {"level":"WARN","msg":"kill switch toggled","client":"203.0.113.42","halted":true,"previous":false}
```

If the toggle was intentional and recent, you should already know why
(deploy, incident, scheduled maintenance). Skip to **resume trading**
when ready.

If the toggle was **NOT intentional** — somebody else has your perimeter
token. **Treat this as a security incident.** Skip to **security
incident** below.

## Resume trading

```sh
curl -X POST "https://hook.example.com/admin/kill-switch?token=$TOKEN&state=off"
# {"halted":"false","previous":"true"}

# Verify:
curl "https://hook.example.com/admin/kill-switch?token=$TOKEN"
# {"halted":"false"}

curl -s http://localhost:8081/metrics | grep ingress_trading_halted
# ingress_trading_halted 0
```

Customers' next webhook attempts will succeed. TradingView usually
doesn't retry beyond a small window, so signals that were rejected
during the halt window are gone — communicate this clearly to customers.

## What customers experienced

Every `/webhook` POST during the halt got a `503 trading_halted`
response **before any signal was published to NATS**. Downstream
(bridge, EA, broker) never saw the trade. From the trader's perspective:

- The alert appears to "fail" in TradingView.
- TradingView retries N times (depends on plan / setup) then gives up.
- No fill record exists in `fills` for these attempts.
- Customer should manually verify their broker positions match
  expectations.

For an analysis of what was *attempted* during the halt:

```sh
docker compose exec postgres psql -U execrelay -d execrelay -c "
  SELECT count(*), MIN(created_at), MAX(created_at)
  FROM audit_rejections
  WHERE reason_code = 'trading_halted'
    AND created_at > now() - INTERVAL '24 hours';
"
```

## Security incident — unintended toggle

If the kill-switch toggle event in the logs has a `client` IP you don't
recognise, **rotate the perimeter token immediately**:

```sh
# 1. Generate a new perimeter token
NEW_TOKEN=$(openssl rand -base64 36 | tr -dc 'A-Za-z0-9_-' | head -c 48)
echo "$NEW_TOKEN"  # save this

# 2. Update .env
sed -i "s|^INGRESS_PERIMETER_TOKEN=.*|INGRESS_PERIMETER_TOKEN=$NEW_TOKEN|" .env

# 3. Restart ingress to pick up the new token
docker compose restart ingress

# 4. Resume trading with the new token
curl -X POST "https://hook.example.com/admin/kill-switch?token=$NEW_TOKEN&state=off"

# 5. Investigate: who had the old token, and how did they get it?
#    Check:
#      - Last 90 days of pull requests / config changes
#      - Last successful uses of the old token (greppable in logs)
#      - Anyone with access to .env (operations team, deploy systems)
```

Then file a security incident per [`SECURITY.md`](../../SECURITY.md).

## Root cause checklist

- [ ] Was a deploy in progress when the switch was tripped?
- [ ] If intentional, is the original incident resolved?
- [ ] Did the alert fire too quickly / slowly? (5m is the default.)
- [ ] Were any customers actively in trades that needed managing? Did
      the halt cause them to miss a close / SL adjustment?
- [ ] If the toggle wasn't intentional, who had the perimeter token?
- [ ] Should the toggle endpoint require an additional check (e.g.,
      OTP via portal-api)? File an issue if so.

## Postmortem prompts

- How long was the halt window?
- How many signals were rejected during the halt?
- How many customers were affected?
- Could the underlying problem have been solved without using the kill
  switch? If yes, document the cheaper path.
- If the toggle was unintended, treat the followup as a security review.

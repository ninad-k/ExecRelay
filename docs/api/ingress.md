# Ingress reference

`ingress` is the public-facing Go service that receives webhook alerts
(typically from TradingView) and publishes them to NATS for downstream
routing.

- **Base URL** (local): `http://localhost:8081`
- **Base URL** (prod, via Caddy): `https://hook.<your-domain>`

For the customer-facing integration guide ("how do I wire up a TradingView
alert?") see [`docs/customer/webhook-integration.md`](../customer/webhook-integration.md).

---

## Endpoints

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/webhook` | Receive a trade signal | Per-license (HMAC + secret) + optional perimeter token |
| `GET` | `/health` | Liveness | None |
| `GET` | `/metrics` | Prometheus metrics | None (firewall this in prod) |
| `GET` | `/admin/kill-switch` | Read current kill-switch state | Perimeter token |
| `POST` | `/admin/kill-switch?state=on\|off` | Toggle kill switch | Perimeter token |

---

## `POST /webhook`

Receives the trade signal. The handler runs every check in order; the
first failure short-circuits and returns the corresponding error code.

### Request

| Element | Required | Notes |
|---|---|---|
| Method | yes | `POST` only — `405 method_not_allowed` otherwise |
| `?token=<value>` query param | iff `INGRESS_PERIMETER_TOKEN` is set | Defense in depth in front of per-license auth |
| `X-ExecRelay-Timestamp` header | iff `WEBHOOK_TIMESTAMP_WINDOW_SECS > 0` | Unix seconds; must be within the window of current time |
| `X-ExecRelay-Signature` (or `X-Signature` / `X-Hub-Signature-256`) header | iff license has `hmac_secret` configured | `sha256=<hex hmac of body>` |
| Body | yes | Plain-text PineConnector-compatible alert (see below) |

### Body format

The parser is at [`packages/parser-go/`](../../packages/parser-go/). Format:

```
licenseID,command,symbol,key1=value1,key2=value2,...
```

Example:
```
60123456789,BUY,EURUSD,vol_lots=0.10,sl_pips=20,tp_pips=40,secret=alert-secret
```

| Field | Required | Notes |
|---|---|---|
| `licenseID` | yes | First field, no key. Must match a `licenses.id` UUID |
| `command` | yes | Second field, no key. `BUY`, `SELL`, `CLOSE`, `CLOSEALL`, etc. |
| `symbol` | yes | Third field, no key. `EURUSD`, `BTCUSDT`, etc. |
| `secret=<value>` | iff license has `secret` configured | Body-embedded password (Tradingview's only built-in auth mechanism) |
| `vol_lots=<float>` | depends on command | Lot size for opening trades |
| `sl_pips`, `tp_pips`, `entry`, `comment`, ... | optional | Anything the EA understands |

### Response

| HTTP | JSON | Meaning |
|---|---|---|
| `200` | `{"status":"accepted","trace_id":"..."}` | Published to NATS; trace_id is your audit handle |
| `400` | `{"error":"parse_error","reason":"...","field":"..."}` | Body didn't parse |
| `400` | `{"error":"invalid_route_token"}` | License/instance ID has non-allowed characters |
| `401` | `{"error":"license_rejected"}` | Unknown license |
| `401` | `{"error":"secret_rejected"}` | Wrong body-embedded secret |
| `401` | `{"error":"signature_rejected"}` | HMAC didn't verify |
| `401` | `{"error":"timestamp_rejected"}` | Outside replay window |
| `401` | `{"error":"perimeter_rejected"}` | Wrong/missing perimeter token |
| `403` | `{"error":"license_rejected"}` | License exists but `active=false` |
| `403` | `{"error":"ip_not_allowed"}` | Client IP not in `WEBHOOK_ALLOWED_CIDRS` |
| `429` | `{"error":"rate_limit_exceeded"}` | Per-IP token bucket exhausted |
| `429` | `{"error":"plan_limit_exceeded"}` | License hit `max_signals_per_day` |
| `429` | `{"error":"exposure_limit_exceeded","reason":"..."}` | Risk-limit breach |
| `413` | `{"error":"body_too_large"}` | Body exceeded `MAX_BODY_BYTES` |
| `503` | `{"error":"trading_halted"}` | Kill switch is on |
| `503` | `{"error":"publish_failed"}` | NATS publish failed — client should retry |
| `500` | `{"error":"encode_failed"}` | Internal — should never happen |

The 200 response includes `X-ExecRelay-Trace-ID` as a header.

### HMAC computation

Compute `HMAC-SHA256(body_bytes, license.hmac_secret)` and send as
hex-encoded (lowercase) string. The header value can optionally be
prefixed with `sha256=` (GitHub-webhook style); both are accepted.

Reference implementation (Python):

```python
import hmac, hashlib
sig = hmac.new(hmac_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
headers["X-ExecRelay-Signature"] = f"sha256={sig}"
```

---

## `GET /health`

Liveness check. Returns `200 {"service":"ingress","status":"ok"}` if the
HTTP server is up. Does not check downstream (NATS, DB) — use this for
load-balancer health checks, not for "is the system working?" alerts.

---

## `GET /metrics`

Prometheus exposition format. Documented in
[`docs/observability.md`](../observability.md#metric-catalog--go-services).

> **Security note**: this endpoint reveals service internals (request
> rates, license counts, etc.). Firewall it from the public internet in
> production — the included Caddyfile + UFW config does this. The Helm
> chart's NetworkPolicy similarly restricts it to the Prometheus pod.

---

## `GET /admin/kill-switch`

Read the current kill-switch state.

```sh
curl 'https://hook.example.com/admin/kill-switch?token=PERIMETER_TOKEN'
# {"halted":"false"}
```

Returns `503 kill_switch_disabled` if `INGRESS_PERIMETER_TOKEN` is not
configured. The endpoint refuses to act without perimeter auth — a
wide-open `/admin/kill-switch` would be a self-inflicted DoS vector.

---

## `POST /admin/kill-switch?state=<on|off>`

Toggle the kill switch.

```sh
# Halt all trading immediately:
curl -X POST 'https://hook.example.com/admin/kill-switch?token=PERIMETER_TOKEN&state=on'
# {"halted":"true","previous":"false"}

# Resume:
curl -X POST 'https://hook.example.com/admin/kill-switch?token=PERIMETER_TOKEN&state=off'
# {"halted":"false","previous":"true"}
```

Accepted `state` values:
- **On**: `on`, `halt`, `halted`, `true`, `1`
- **Off**: `off`, `resume`, `false`, `0`

While halted, every `/webhook` POST returns `503 trading_halted` *before*
any signal is published. No downstream service (bridge, dxtrade, persist)
sees the rejected signals.

Toggle events are logged with the client IP for audit and emit the
`ingress_trading_halted` Prometheus gauge (0 or 1). The token value
itself is **never** logged.

---

## Idempotency

ExecRelay deduplicates by `(license_id, body_sha256)` via the
`signal_fingerprints` table. If you retry the exact same body, you'll
get a `200 accepted` but downstream will skip the duplicate dispatch.

This is intentional — TradingView retries automatically on 5xx, and you
don't want a single network blip to fire the same trade twice.

---

## Rate limits

| Layer | Default | Configured by |
|---|---|---|
| Per-IP request rate | 1000/minute (token bucket) | `WEBHOOK_RATE_LIMIT` env |
| Per-license daily quota | unlimited | `licenses.max_signals_per_day` column |
| Body size | 4096 bytes | `MAX_BODY_BYTES` env |
| Replay window | 60 s | `WEBHOOK_TIMESTAMP_WINDOW_SECS` env |

For higher per-IP throughput, raise `WEBHOOK_RATE_LIMIT`. The bucket is
**per-pod**, not cluster-wide — if you horizontally scale ingress, the
effective limit is `WEBHOOK_RATE_LIMIT × pod_count`.

---

## See also

- [`docs/customer/webhook-integration.md`](../customer/webhook-integration.md) — end-to-end customer setup
- [`docs/observability.md`](../observability.md) — metrics + alerts
- [`apps/ingress/internal/ingress/handler.go`](../../apps/ingress/internal/ingress/handler.go) — source of truth
- [`apps/ingress/internal/ingress/handler_test.go`](../../apps/ingress/internal/ingress/handler_test.go) — every behavior here is covered by a test

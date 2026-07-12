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
| `POST` | `/webhook/ml` | Receive an ML-filtered trade signal (opt-in, JSON) | Same as `/webhook` |
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

## `POST /webhook/ml`

**ADR:** [`docs/adr/0008-opt-in-json-ml-webhook-path.md`](../adr/0008-opt-in-json-ml-webhook-path.md).

Opt-in JSON path that scores the request through `ml-predictor` before
publishing. Reuses the *exact* gating + per-license auth chain as `/webhook`
(perimeter token, kill-switch, per-IP rate limit, CIDR allowlist, timestamp
window, license lookup, secret, HMAC-over-raw-body, daily quota, exposure
limits) via a shared internal helper — anything that rejects `/webhook`
rejects `/webhook/ml` identically. The flat `/webhook` path and its 95 ms
latency budget are untouched; this route makes a **synchronous** call to
`ml-predictor`, so its latency profile is deliberately looser.

### Request

Same auth requirements as `/webhook` (`?token=`, `X-ExecRelay-Timestamp`,
`X-ExecRelay-Signature`), but the body is JSON, not the flat wire format:

```json
{
  "license_id": "60123456789",
  "secret": "alert-secret",
  "action": "buy",
  "symbol": "EURUSD",
  "volume": 0.1,
  "sl": 0,
  "tp": 0,
  "comment": "AlgoCombo",
  "current_position": "LONG",
  "features": { "rsi_14": 55.5, "adx_14": 21.3, "...": "...35 features total, per apps/ml-predictor/model/feature_order.txt minus direction" }
}
```

| Field | Required | Notes |
|---|---|---|
| `license_id` | yes | Must match a `licenses.id` |
| `secret` | iff license has `secret` configured | Body-embedded password, same check as `/webhook` |
| `action` | yes | `"buy"` or `"sell"` — anything else is `400 invalid_action` |
| `symbol` | yes | |
| `volume`, `sl`, `tp`, `comment` | optional | Carried onto the published `Signal` exactly like the flat path's `vol_lots`/`sl`/`tp`/`comment` |
| `current_position` | optional | `"LONG"` \| `"SHORT"` \| `null`. Caller value always wins; if omitted, ingress falls back to a snapshot read of `account_positions` (license + symbol); if that's unavailable or has no row, position is treated as unknown/flat |
| `features` | yes (for scoring) | Passed through verbatim to `ml-predictor`. Missing/incomplete features cause the predictor call to error, which **fails open** (see below) rather than rejecting the webhook |

### Scoring and shadow mode

`direction` is derived from `action` (`buy`→`1`, `sell`→`-1`) and POSTed to
`ml-predictor`'s `/predict` with `features` and the resolved
`current_position`. The predictor's `action_summary` maps to an ExecRelay
command:

| `action_summary` | Command | Published? |
|---|---|---|
| `OPEN_LONG` | `buy` | yes |
| `OPEN_SHORT` | `sell` | yes |
| `FLIP_LONG` | `closeshortopenlong` | yes |
| `FLIP_SHORT` | `closelongopenshort` | yes |
| `CLOSE_ONLY` (position was LONG) | `closelong` | yes |
| `CLOSE_ONLY` (position was SHORT) | `closeshort` | yes |
| `NOTHING` | — | no → `200 {"status":"skipped"}` |

**`ML_ENFORCE`** (env, default `false`) controls whether that mapped command
is actually what gets published:

- **Shadow mode (`ML_ENFORCE=false`, the default).** Every request is still
  authenticated, scored, and audited, but ingress **always publishes the
  caller's original `action`** (`buy`→`buy`, `sell`→`sell`), regardless of
  what the model recommends. The response reports what the model *would*
  have done.
- **Enforce mode (`ML_ENFORCE=true`).** Ingress publishes the mapped command
  from the table above. `NOTHING` publishes nothing and responds
  `200 {"status":"skipped"}`.

**Predictor unreachable, erroring, or timing out always fails open**: the
original `action` is published (as in shadow mode), the response's `ml.error`
field is set, and the `ingress_ml_predictor_errors_total` counter increments.
`/webhook/ml` never rejects a trade because the ML filter is down.

### Response

```json
{
  "status": "accepted",
  "trace_id": "...",
  "ml": {
    "action_summary": "OPEN_LONG",
    "prob_win": 0.63,
    "threshold": 0.5,
    "model_version": "xgb-v3",
    "enforced": false
  }
}
```

| Field | Notes |
|---|---|
| `status` | `"accepted"` (published) or `"skipped"` (enforce mode, `NOTHING`) |
| `ml.action_summary` | Raw predictor decision |
| `ml.prob_win`, `ml.threshold` | Model win probability and the pass/fail threshold |
| `ml.model_version` | Omitted/empty if the predictor doesn't send it |
| `ml.enforced` | Reflects `ML_ENFORCE` at request time |
| `ml.error` | Present only on the fail-open path (predictor down/erroring) |

All the same rejection codes as `/webhook` apply for auth failures (see the
table above), plus:

| HTTP | JSON | Meaning |
|---|---|---|
| `400` | `{"error":"parse_error","reason":"..."}` | Body wasn't valid JSON |
| `400` | `{"error":"invalid_action","reason":"..."}` | `action` wasn't `"buy"`/`"sell"` |
| `400` | `{"error":"missing_field","reason":"..."}` | `license_id` or `symbol` missing |

### Audit trail

Every `/webhook/ml` request writes one row to `ml_decisions`
(`infra/migrations/000006_ml_decisions.up.sql`): trace id, license, symbol,
action, `prob_win`/`threshold`/`action_summary`, the published command (if
any), `enforced`, `model_version`, `position_source` (`caller`/`db`/`unknown`),
and any predictor error. The insert is best-effort and non-blocking — it never
slows down or fails the webhook response, and is skipped entirely when no DB
is configured.

### New metrics

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `ingress_ml_webhook_requests_total` | counter | `outcome` (`accepted`\|`skipped`\|`fail_open`\|`rejected`) | `/webhook/ml` requests by outcome |
| `ingress_ml_webhook_duration_seconds` | histogram | — | `/webhook/ml` latency, including the synchronous predictor call |
| `ingress_ml_predictor_errors_total` | counter | — | Errors calling `ml-predictor` `/predict` (timeout, connection refused, non-2xx, malformed response) |

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

## `/webhook/ml` configuration

| Env var | Default | Notes |
|---|---|---|
| `ML_PREDICTOR_URL` | `http://ml-predictor:8080` | Base URL; ingress POSTs to `<url>/predict` |
| `ML_PREDICT_TIMEOUT_MS` | `2000` | Timeout for the synchronous predictor call |
| `ML_ENFORCE` | `false` | `false` = shadow mode (always publish the caller's original action); `true` = publish the model's mapped command |

---

## See also

- [`docs/customer/webhook-integration.md`](../customer/webhook-integration.md) — end-to-end customer setup
- [`docs/observability.md`](../observability.md) — metrics + alerts
- [`apps/ingress/internal/ingress/handler.go`](../../apps/ingress/internal/ingress/handler.go) — source of truth
- [`apps/ingress/internal/ingress/handler_test.go`](../../apps/ingress/internal/ingress/handler_test.go) — every behavior here is covered by a test

# Portal API reference

`portal-api` is the FastAPI backend for `portal-web` and for any
programmatic clients that want to manage licenses, query signals/fills,
or export journal data.

- **Base URL** (local): `http://localhost:8085`
- **Base URL** (prod, via Caddy): `https://api.<your-domain>`
- **Format**: JSON request bodies, JSON responses (CSV for journal export).
- **Live OpenAPI spec**: `GET /docs` — FastAPI auto-generates a Swagger UI.
  This document is the human-friendly reference; the OpenAPI spec is
  authoritative for field-level types.

For a domain glossary (License, Instance, Trace ID, etc.) see
[`docs/glossary.md`](../glossary.md).

---

## Authentication

The portal uses **bearer JWT tokens**, HMAC-SHA256 signed with
`JWT_SECRET`.

```sh
# Register a new account
curl -X POST https://api.example.com/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"choose-a-strong-one"}'
# Response: {"token":"eyJ...","expires_at":"..."}

# Or log in to an existing account
curl -X POST https://api.example.com/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"your-password"}'
# Response: {"token":"eyJ...","expires_at":"..."}

# Use the token on every subsequent request:
curl https://api.example.com/licenses \
  -H 'Authorization: Bearer eyJ...'
```

Rate limits: login is capped at **20 attempts per minute per IP**.

### Roles

| Role | What they can do |
|---|---|
| `user` | Manage their own licenses, instances, signals, fills, exports. Default for new registrations. |
| `support` | Read-only access across users. |
| `super_admin` | Full read/write across users; can change roles. |

Every privileged action by `support` or `super_admin` is recorded in
`admin_audit_log`.

---

## Endpoints

### Health

`GET /health` — Liveness check, no auth required. Returns
`{"service":"portal-api","status":"ok"}`.

### Auth

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/auth/register` | `{"email", "password"}` | `{"token","expires_at"}` |
| `POST` | `/auth/login` | `{"email", "password"}` | `{"token","expires_at"}` |

### Licenses

A license is the credential the ingress webhook uses to authenticate
trades. See [`docs/glossary.md#license`](../glossary.md#license).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/licenses` | List my licenses |
| `POST` | `/licenses` | Create a new license — server generates UUID + secrets |
| `PATCH` | `/licenses/{license_id}` | Update fields (e.g., toggle `active`, change `max_signals_per_day`) |
| `GET` | `/licenses/{license_id}/config` | Export the EXECRELAY_LICENSES env line for this license |
| `POST` | `/licenses/{license_id}/rotate-hmac` | Begin HMAC rotation — generates a new `pending_hmac_secret` |
| `POST` | `/licenses/{license_id}/confirm-rotation` | Promote pending HMAC to primary; clear pending |

#### Create license

```sh
curl -X POST https://api.example.com/licenses \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"license_key":"my-prod-account","max_signals_per_day":1000}'
# Response includes the generated secret + hmac_secret — capture them
# immediately, they are only returned on creation.
```

#### Rotate HMAC (zero-downtime)

```sh
# Step 1: generate a pending HMAC. Both old and new are accepted by ingress.
curl -X POST https://api.example.com/licenses/$LID/rotate-hmac \
  -H 'Authorization: Bearer $TOKEN'
# Response: {"pending_hmac_secret":"..."}

# Step 2: update your TradingView alert (or whatever's signing) to use
# the new secret. Wait until all in-flight alerts are signed with the new key.

# Step 3: confirm — promote pending → primary, clear pending.
curl -X POST https://api.example.com/licenses/$LID/confirm-rotation \
  -H 'Authorization: Bearer $TOKEN'
```

### Instances

An instance is a broker terminal (one MT4/MT5/DXTrade account).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/licenses/{license_id}/instances` | List instances on this license |
| `POST` | `/licenses/{license_id}/instances` | Create a new instance |
| `PATCH` | `/licenses/{license_id}/instances/{instance_id}` | Update instance |
| `GET` | `/licenses/{license_id}/instances/{instance_id}/fills` | Recent fills for this instance |

### Signals & fills

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/licenses/{license_id}/signals` | Recent signals for this license |
| `GET` | `/traces/{trace_id}` | Full lifecycle of one trade: signal → routing → fill |
| `POST` | `/signals/{signal_id}/replay` | Re-publish a signal to NATS (replay; useful for testing or recovering missed fills) |
| `POST` | `/licenses/{license_id}/test-signal` | Send a synthetic signal end-to-end as a smoke test |

### Risk & analytics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/licenses/{license_id}/portfolio-exposure` | Current exposure across instances/symbols |
| `GET` | `/licenses/{license_id}/risk-metrics` | Drawdown, daily P&L, breach history |
| `POST` | `/licenses/{license_id}/signals/correlate` | Run correlation analysis on a window of signals |
| `POST` | `/licenses/{license_id}/signal-groups` | Group related signals (multi-leg trades) |

### Backtesting

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/backtest` | Submit a backtest job; runs in `backtester` service |

### Journal export

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/journal/export?from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv\|json` | Stream all fills in the date range. See [`apps/portal-api/app.py`](../../apps/portal-api/app.py) for shape. |

Example: download the last quarter for accounting:

```sh
curl 'https://api.example.com/journal/export?from=2026-01-01&to=2026-04-01&format=csv' \
  -H 'Authorization: Bearer $TOKEN' \
  -o execrelay-Q1-2026.csv
```

Hard cap: 200,000 rows per request. For larger exports, page by date range.

---

## Error responses

All error responses use FastAPI's default envelope:

```json
{"detail":"<error message>"}
```

| HTTP | Meaning |
|---|---|
| `400` | Malformed input — missing field, invalid date format |
| `401` | Bad / missing bearer token |
| `403` | Authenticated but unauthorised (e.g., trying to access another user's license) |
| `404` | License / instance / trace ID not found |
| `409` | Conflict (e.g., duplicate `license_key`) |
| `429` | Rate limit exceeded |
| `5xx` | Internal — check container logs for the trace |

---

## Quick start (programmatic)

```python
import httpx

BASE = "https://api.example.com"

# 1. Get a token
r = httpx.post(f"{BASE}/auth/login", json={"email": "...", "password": "..."})
token = r.json()["token"]
h = {"Authorization": f"Bearer {token}"}

# 2. Create a license
r = httpx.post(f"{BASE}/licenses", json={"license_key": "auto"}, headers=h)
lic = r.json()
print("Use this in your TradingView alert body:")
print(f"  {lic['id']},BUY,EURUSD,vol_lots=0.1,secret={lic['secret']}")

# 3. Send a test signal end-to-end
r = httpx.post(f"{BASE}/licenses/{lic['id']}/test-signal", headers=h)
trace_id = r.json()["trace_id"]

# 4. Watch it land
import time; time.sleep(2)
r = httpx.get(f"{BASE}/traces/{trace_id}", headers=h)
print(r.json())
```

---

## See also

- [`docs/api/ingress.md`](ingress.md) — webhook + admin endpoints on ingress
- [`docs/customer/webhook-integration.md`](../customer/webhook-integration.md) — end-to-end customer flow
- [`docs/glossary.md`](../glossary.md) — domain vocabulary
- [`apps/portal-api/app.py`](../../apps/portal-api/app.py) — the source of truth

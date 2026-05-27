# Glossary

Domain vocabulary used across code, docs, support tickets, and customer
conversations. When a word means something specific in ExecRelay, look here.

---

### Alert

A message produced by a charting platform (TradingView, MotiveWave,
custom). In ExecRelay, an "alert" arrives at ingress as an HTTP POST and
becomes a [Signal](#signal) once parsed.

### Audit log

Two distinct tables:
- `admin_audit_log` — privileged actions (promote user, change a license,
  override a limit) in portal-api.
- `audit_rejections` — every signal ingress *rejected*, with reason code
  and body SHA256.

### Bridge

The Go service (`apps/bridge/`) that holds persistent WebSocket connections
to MT4/MT5 [EAs](#ea) and dispatches signals from NATS to the right EA
by [Instance ID](#instance). Also receives [Fill](#fill) reports back from
EAs and publishes them on the NATS `fills` stream.

### Cold path

Everything that's not on the [Hot path](#hot-path). Cold-path services
(persist, portal-api, analytics, reports, tasks) consume from NATS and
write to TimescaleDB. They can fail or restart without affecting trade
execution latency.

### DXTrade

A broker platform. ExecRelay's `dxtrade` adapter speaks DXTrade's REST API
to place orders, an alternative to MT4/MT5's EA model.

### EA (Expert Advisor)

The plugin running inside the trader's MetaTrader terminal. In ExecRelay,
the EA is the [execution authority](#execution-authority) — it owns the
broker connection and is the source of truth for broker-side positions.
- MT5 EA: `ea/mt5/ExecRelay.mq5`, native socket support.
- MT4 EA: `ea/mt4/ExecRelay.mq4`, requires `ExecRelayWS.dll` for sockets.

### Execution authority

The component that actually places trades with the broker. In ExecRelay,
this is always the EA (for MT4/MT5) or the DXTrade adapter (for DXTrade).
**Bridge never owns broker position state.** It routes and records.

### Fill

A confirmation from the broker that a trade was executed (or rejected).
Stored in the `fills` table with `status` = `filled`, `partially_filled`,
`rejected`, or `error`. Tied back to the originating signal by [trace ID](#trace-id).

### HMAC

Hash-based Message Authentication Code. ExecRelay uses HMAC-SHA256 to
sign webhook bodies — the customer's HMAC secret is shared between the
alert producer and the per-[license](#license) `hmac_secret` column.

### Hot path

The end-to-end signal flow from TradingView POST to broker `OrderSend`:
ingress → NATS → bridge → EA. Has a **95 ms p99 latency target**.
Hot-path code is in Go, has no DB writes, and uses no external HTTP.

### Ingress

The Go service (`apps/ingress/`) that receives webhook alerts on
`POST /webhook`. Runs every auth check
(see [SECURITY.md § layered auth](../SECURITY.md#authentication-layers-defense-in-depth)),
parses the body to a [Signal](#signal), and publishes to NATS.

### Instance

A specific broker terminal session — typically one MT4/MT5/DXTrade account
running one EA. A [License](#license) can own many instances. Identified
by `(license_id, instance_key)`.

### JetStream

NATS's durable-messaging layer. ExecRelay uses it for at-least-once
delivery from ingress to bridge / dxtrade / persist with replay on
consumer reconnect.

### Kill switch

An emergency stop on ingress. When on, every webhook is rejected with
503 `trading_halted` *before* publishing to NATS — no downstream service
sees the signal. Toggled via `INGRESS_TRADING_HALTED` env at startup or
`POST /admin/kill-switch?state=on` while running. See [SECURITY.md](../SECURITY.md).

### License

The credential a customer uses to authenticate with ingress. A license has
a UUID (sent as the first comma-separated field in the alert body), a
human-readable `license_key`, an optional body-embedded `secret`, an
optional `hmac_secret` (and `pending_hmac_secret` for rotation), an
`active` flag, and a `max_signals_per_day` quota.

### License audit

`AuditLicenses()` runs at ingress startup and on `SIGHUP` license reload,
flagging licenses with no auth configured (`no_auth`), only one auth
mechanism (`no_hmac` / `no_secret`), or an unfinished HMAC rotation
(`rotation_active`). Exposed as the `ingress_license_config_warnings`
Prometheus gauge.

### Migrate (the service)

The compose service that runs `golang-migrate up` and exits 0 when done.
All app services `depends_on` it via `service_completed_successfully`,
so they only start after schema is at the latest version.

### NATS

The message broker (`nats:2.11-alpine`). ExecRelay uses [JetStream](#jetstream)
durable subscriptions for the hot path and core NATS pub/sub for events.

### Perimeter token

An optional, single, shared secret (`INGRESS_PERIMETER_TOKEN`) checked
on every `/webhook` request as `?token=<value>`. Defense in depth in
front of per-[license](#license) auth. See [SECURITY.md](../SECURITY.md).

### Persist

The Python service (`apps/persist/`) that consumes the NATS fills stream
and writes rows to the `fills` table. The only cold-path service that
absolutely *must* keep up with bridge — backlog here means missing trade
records.

### Plan tier

The pricing tier a customer is on, defining their `max_instances`,
`max_concurrent_connections`, `max_signals_per_day`. Stored in `plan_tiers`;
the link from `users` to tier is implicit in application code.

### Portal

The user-facing web app (`apps/portal-web/`) + its backend API
(`apps/portal-api/`). Where customers manage licenses, view fills, and
download journal exports.

### Region

A deployment locality (e.g., `iad`, `sfo`, `fra`). Every signal is
stamped with `INGRESS_REGION` so analytics can break down by region.

### Replay window

The `WEBHOOK_TIMESTAMP_WINDOW_SECS` env on ingress. Requests with an
`X-ExecRelay-Timestamp` outside the window are rejected as
`timestamp_rejected` to defend against replay attacks.

### Signal

A parsed trade instruction: license ID, instance ID, command (`buy`,
`sell`, `close`, …), symbol, parameters (`vol_lots`, `sl_pips`,
`tp_pips`, …). The wire format is protobuf `Signal` in
[`packages/proto/signal.pb.go`](../packages/proto/signal.pb.go), gated by
the wire-format golden test in `packages/proto/wire_test.go`.

### Trace ID

A 16-byte hex string assigned by ingress on every accepted signal and
propagated through NATS, bridge, EA, and into the `fills` table. **The
join key for end-to-end debugging** — `docker compose logs | grep <trace_id>`
shows the full life of a single trade.

### TradingView

The most common upstream alert producer. ExecRelay accepts TradingView's
default plain-text alert format via the
[PineConnector](https://www.pineconnector.com/)-compatible parser in
[`packages/parser-go/`](../packages/parser-go/). Other producers can use
the same format.

### Trust boundary

A point where one component starts trusting input from another. See
[ARCHITECTURE.md § Trust boundaries](ARCHITECTURE.md#4-trust-boundaries)
for the diagram. Crossing one always requires explicit auth.

### WSL2

Windows Subsystem for Linux v2. The PowerShell installer runs the entire
ExecRelay stack inside WSL2 on Windows Server 2022 because all images are
Linux. See `scripts/install.ps1`.

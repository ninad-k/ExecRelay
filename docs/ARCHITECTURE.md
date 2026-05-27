# Architecture

Last reviewed: see `git log -- docs/ARCHITECTURE.md`.

This document is the orientation map for the ExecRelay system. If you're new
to the codebase, read this first — every other doc assumes you've internalised
the data flow and trust boundaries described here.

For the *why* behind specific decisions (NATS vs Kafka, hand-rolled proto, etc.)
see [`docs/adr/`](adr/).

---

## 1. System context (C4 level 1)

```
                                      ┌──────────────────────────┐
       chart-alert source             │      ExecRelay system    │
   ┌─────────────────────┐            │                          │
   │   TradingView (or   │  HTTPS     │  ┌─────────────────────┐ │
   │   any HTTPS webhook │ ───POST───►│  │  ingress + bridge + │ │
   │   producer)         │            │  │  EA layer           │ │
   └─────────────────────┘            │  └──────────┬──────────┘ │
                                      │             │            │
   ┌─────────────────────┐  HTTPS     │  ┌──────────▼──────────┐ │
   │  Portal user        │ ◄─────────►│  │  portal-web/api +   │ │
   │  (trader / admin)   │            │  │  analytics/reports  │ │
   └─────────────────────┘            │  └─────────────────────┘ │
                                      │                          │
                                      └────────────┬─────────────┘
                                                   │  WebSocket / REST
                                                   ▼
                                      ┌──────────────────────────┐
                                      │  Broker terminal         │
                                      │  (MT4 / MT5 / DXTrade)   │
                                      └──────────────────────────┘
```

External actors:

| Actor | Interaction |
|---|---|
| **TradingView (or any HTTPS POSTer)** | Sends webhook alerts to `ingress` on `/webhook` |
| **Portal user** (trader, support, super_admin) | Authenticates to `portal-api` via JWT; manages licenses, instances, views fills/reports |
| **Broker terminal** | Hosts the EA (MT4/MT5) which holds a persistent WebSocket to `bridge`, OR is reached via REST by `dxtrade` for DXTrade accounts |

---

## 2. Container view (C4 level 2)

```
                                  ┌─────────────┐
                                  │  ingress    │ Go
        TradingView ──── /webhook │  :8081      │
                                  │  - perimeter token gate
                                  │  - per-license HMAC + secret
                                  │  - timestamp window
                                  │  - IP CIDR allow
                                  │  - rate limit (per IP)
                                  │  - daily quota (per license)
                                  │  - kill switch
                                  │  - exposure-limit check
                                  └──────┬──────┘
                                         │ proto Signal
                                         ▼
                                  ┌─────────────┐
                                  │  NATS       │
                                  │  JetStream  │  signals.<platform>.<licenseID>.<instanceID>
                                  └──────┬──────┘
                                         │
                       ┌─────────────────┼─────────────────┐
                       ▼                 ▼                 ▼
              ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
              │  bridge     │    │  dxtrade    │    │  persist    │
              │  :8082  Go  │    │  :8083  Go  │    │  :8084  Py  │
              │             │    │             │    │             │
              │  WS hub for │    │  REST       │    │  NATS →     │
              │  MT4/MT5    │    │  client for │    │  Timescale  │
              │  EAs        │    │  DXTrade    │    │             │
              └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
                     │                  │                  │
              persistent WS         REST/HTTPS              │
                     │                  │                  │
                     ▼                  ▼                  ▼
              ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
              │  MT4/MT5 EA │    │  DXTrade    │    │ TimescaleDB │
              │  (broker)   │    │  cloud      │    │  (Postgres) │
              └──────┬──────┘    └──────┬──────┘    └─────────────┘
                     │                  │                  ▲
                     └────── fill ──────┴─── fill ────►NATS┘
                                                    FILLS stream

   Cold path (no SLA tied to hot path latency):
   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
   │ portal-api  │  │ analytics   │  │ reports     │  │ tasks       │
   │ :8085  Py   │  │ :8087  Py   │  │ :8088  Py   │  │ :8086  Py   │
   │             │  │             │  │             │  │             │
   │ Users,      │  │ Latency &   │  │ Daily/      │  │ Fill        │
   │ licenses,   │  │ fill-rate   │  │ weekly      │  │ timeouts,   │
   │ JWT auth,   │  │ query API   │  │ reports     │  │ retention   │
   │ journal     │  │             │  │             │  │ cleanup,    │
   │ export      │  │             │  │             │  │ pollers     │
   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
          │                │                │                │
          ▼                ▼                ▼                ▼
                         TimescaleDB
                              │
                              ▼
                      ┌─────────────┐
                      │ portal-web  │
                      │ :3001 Next  │
                      └─────────────┘

   Infrastructure tier:
     - timescaledb (Postgres 16 + TimescaleDB)
     - nats (JetStream)
     - redis
     - minio (S3-compatible blob, for backtester artifacts & MLflow)
     - prometheus + alertmanager + grafana + tempo  (observability)
     - mlflow (model registry)
     - migrate  (runs once on startup, applies golang-migrate up)
```

---

## 3. Hot path vs cold path

The single most important invariant in this system:

> **The hot path** (TradingView → broker `OrderSend`) **never blocks on the cold path.**

| | Hot path | Cold path |
|---|---|---|
| **Members** | `ingress`, NATS, `bridge` (or `dxtrade`), EA | `persist`, `portal-api`, `analytics`, `reports`, `tasks`, `portal-web` |
| **Latency target** | p99 ≤ 95 ms (same region) | seconds to minutes is fine |
| **DB writes?** | **No.** Ingress publishes to NATS and returns 200. | Yes — all persistence happens here. |
| **Failure mode** | A cold-path outage MUST NOT degrade hot path. NATS is durable; consumers replay when they come back. | A hot-path outage breaks everything; this is what the kill switch is for. |
| **Owning team concern** | Latency, throughput, auth correctness | Storage, queries, reporting correctness |

If you find yourself adding a Postgres write to the ingress hot path, stop and
ask: can this go on a NATS event consumer instead?

---

## 4. Trust boundaries

```
┌──────────── public internet ────────────┐
│                                          │
│   TradingView, customer EAs, attackers   │
│                                          │
└────────────────────┬─────────────────────┘
                     │ HTTPS (TLS via Caddy)
                     │
┌────────────────────┴─────────────────────┐
│              edge (Caddy / WAF)          │
│                                          │
│   - TLS termination                       │
│   - Optional: rate-limiting / WAF rules   │
│   - Forwards 80/443 to internal services  │
└────────────────────┬─────────────────────┘
                     │ loopback / Docker network
                     │
┌────────────────────┴─────────────────────┐
│        public-facing app tier            │
│                                          │
│   ingress (webhook)     portal-api       │
│   portal-web (static)                    │
│                                          │
│   - Per-request auth: HMAC | JWT         │
│   - Input validation                     │
│   - Rate limits, kill switch              │
└────────────────────┬─────────────────────┘
                     │ NATS (mTLS-capable; today: NATS user+pass)
                     │ Postgres (in-cluster, password auth)
                     │
┌────────────────────┴─────────────────────┐
│            internal-only tier            │
│                                          │
│   bridge, dxtrade, persist, tasks,        │
│   analytics, reports, grafana, etc.       │
│                                          │
│   No public ingress — only reachable      │
│   via Docker network / loopback.          │
└──────────────────────────────────────────┘
```

**Crossing a boundary always requires explicit auth.** Internal services trust
their network (Docker bridge or k8s service mesh) but never the data on it —
NATS message payloads are validated, Postgres queries are parameterised, and
internal HTTP endpoints (e.g. `bridge`'s EA WebSocket endpoint) require their
own tokens.

For per-service threat detail, see [`SECURITY.md`](../SECURITY.md).

---

## 5. Data flow — happy path

A typical TradingView alert from POST to broker fill:

1. **TradingView** fires the alert and POSTs the configured payload to
   `https://hook.<your-domain>/webhook?token=<perimeter>`.
2. **Caddy** terminates TLS, forwards to `ingress` on `localhost:8081`.
3. **`ingress`** runs checks in order (any failure → reject and record metric):
   1. Method = POST
   2. Perimeter token matches (if configured)
   3. Kill switch is off
   4. Per-IP rate limit hasn't been exceeded
   5. Client IP is in the allowed CIDR set (if configured)
   6. Timestamp header is within the replay window (if configured)
   7. Body parses as a PineConnector-compatible signal
   8. License exists and is active
   9. Body-embedded secret matches (if license has one configured)
   10. HMAC signature header verifies (if license has one configured)
   11. Daily quota not exceeded
   12. Exposure limits not breached (Phase 7 check, requires DB)
4. **`ingress`** marshals a protobuf `Signal` and publishes to NATS subject
   `signals.<platform>.<licenseID>.<instanceID>`. Returns `200 OK` with the
   trace ID. Total time on the hot path: typically 1–5 ms.
5. **`bridge`** (for MT4/MT5) or **`dxtrade`** (for DXTrade) is subscribed
   to the matching NATS subject. It picks up the signal.
6. **`bridge`**: looks up the connected EA's WebSocket session by instance ID
   and forwards the order command. The EA executes `OrderSend()`.
   **`dxtrade`**: makes a REST call to the DXTrade cloud API to place the order.
7. **Broker** confirms the fill (or rejection).
8. **EA / dxtrade adapter** sends a fill report back: EA → bridge → NATS
   `fills.*` subject; dxtrade → NATS `fills.*` directly.
9. **`persist`** consumes the NATS fill stream and writes a row to the
   `fills` table in TimescaleDB. This is the durable record.
10. **`portal-web`** can display the fill (via `analytics` API), `reports`
    aggregates it into daily/weekly summaries, and `tasks` may notify the
    user.

The whole journey from step 1 to step 7 typically completes in well under 100 ms
within a single region. Steps 8–10 are off the hot path and may take seconds.

---

## 6. Failure modes & guarantees

| Failure | What happens | Recovery |
|---|---|---|
| **NATS down** | `ingress` returns 503 `publish_failed`. The client should retry. **Trade is NOT placed.** | Compose restarts NATS; ingress publishes succeed again. No data loss because nothing was ever durable. |
| **Bridge down** | NATS holds messages in the JetStream durable consumer. EAs are disconnected. **New trades queue but don't execute.** | Bridge restarts, reconnects to NATS, drains the durable consumer, EAs reconnect. The kill switch should be considered if the outage is prolonged. |
| **Postgres down** | Hot path is **unaffected** (no writes). `persist` buffers messages in the NATS durable consumer; portal-api / analytics / reports return 5xx. | Postgres restarts; `persist` drains the backlog; portal services recover automatically. |
| **EA disconnects** | Bridge drops in-flight commands for that instance and emits a `bridge_ea_disconnected` event. NATS continues to queue. | EA reconnects; bridge resumes delivering from the durable consumer's last-acked position. |
| **Bad license / HMAC** | `ingress` returns 401 / 403 with a specific error code; `ingress_rejections_total{reason}` increments. | Operator fixes config; license hot-reloads via `SIGHUP` (no restart needed). |
| **Trading must be halted** | Operator POSTs `/admin/kill-switch?state=on`. Every subsequent webhook is rejected with 503 `trading_halted`. | Operator POSTs `state=off` when ready. |

The system is **fail-loud, not fail-silent.** Every rejection is a Prometheus
metric with a labelled reason; every consumer has a NATS durable subscription;
nothing is dropped silently.

---

## 7. Storage

| Store | Used by | Why |
|---|---|---|
| **TimescaleDB** (Postgres + TS extension) | persist, portal-api, tasks, analytics, reports, risk | Relational with time-series superpowers for fills/signals/audit logs |
| **NATS JetStream** | All services (transport) | Low-latency pub/sub with durable subscriptions + replay |
| **Redis** | tasks, ingress (rate limiter sharing — currently per-pod, future per-cluster) | Hot ephemeral state |
| **MinIO** (S3-compatible) | mlflow, backtester | Blob storage for model artifacts, backtest result files |
| **MLflow** | ML services | Model registry & experiment tracking |

Schema lives in [`infra/migrations/`](../infra/migrations/) — managed by
`golang-migrate`. See [`docs/data-model.md`](data-model.md) for table-level
detail.

---

## 8. Multi-region considerations

Today: single-region deployments are first-class; multi-region is **possible
but not turn-key**. Notes:

- The `INGRESS_REGION` env var stamps every signal with the producing region.
- NATS can be configured for cross-region replication (super-cluster mode);
  Postgres needs Patroni or RDS multi-AZ.
- The Helm chart at [`infra/helm/`](../infra/helm/) targets a single cluster.
- Multi-region is on the Phase 6 roadmap; see [`docs/adr/`](adr/) for any
  ADR on the design once it lands.

---

## 9. Technology choices — short version

(For full reasoning, see [`docs/adr/`](adr/).)

| Choice | Why |
|---|---|
| Go for hot-path services (ingress, bridge, dxtrade) | Single-binary, fast cold start, no GC stop-the-world surprises at our object rates, race detector in CI |
| Python (FastAPI) for cold-path services | Faster to build CRUD; first-class async; uvicorn is fast enough for the cold path |
| Next.js for portal-web | Easy SSR, server-side proxy to portal-api, no separate Node server to manage |
| NATS JetStream (not Kafka) | Lower operational cost, sufficient throughput for our workload, durable consumers, simpler ops |
| TimescaleDB | Postgres familiarity + time-series indexes for fills/signals tables |
| Caddy reverse proxy | Single binary, automatic Let's Encrypt, identical config on Linux + Windows |
| `golang-migrate` for schema | Language-agnostic plain SQL, simple CLI, version table tracking |
| Hand-rolled `signal.pb.go` | Pragmatic — wire format is stable, no `.proto` toolchain dependency. Guarded by `packages/proto/wire_test.go` golden. |

---

## 10. Where to look next

- Want to ship a feature? Read [`CONTRIBUTING.md`](../CONTRIBUTING.md) then
  pick the service under `apps/`.
- Adding a DB column? Read [`docs/data-model.md`](data-model.md) and
  [`infra/migrations/README.md`](../infra/migrations/README.md).
- Touching the auth flow? Read [`SECURITY.md`](../SECURITY.md) and
  [`apps/ingress/internal/ingress/handler.go`](../apps/ingress/internal/ingress/handler.go).
- Building dashboards / alerts? Read [`docs/observability.md`](observability.md).
- On call? Bookmark [`docs/runbooks/`](runbooks/).

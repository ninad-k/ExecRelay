# ExecRelay — Architecture & Comparison with the Photos (MT5 Autotrader) Project

> **Scope.** This document explains the ExecRelay architecture in detail and
> contrasts it with the earlier `D:\Personal\Photos` MT5 trading project — the
> single-machine Python automation system that ExecRelay supersedes.
>
> **Note.** Any machine-learning / signal-filtering capability (XGBoost
> predictor, `/webhook/ml`) is intentionally **excluded** here. That work is not
> complete and is out of scope for this architecture document. ExecRelay is the
> *execution wire*, not a signal/AI platform.

---

## 1. What ExecRelay is

**ExecRelay is low-latency execution infrastructure for automated traders.** It
relays TradingView (and TradingView-compatible) webhook alerts through a regional
ingress, a durable message bus, and a bridge to broker-side Expert Advisors (EAs)
or REST adapters.

It is deliberately **not** a signal provider, broker, copy-trading platform, or
AI trading platform. It is the wire between *your* signal source and *your*
broker account.

### Design constraints that shape everything

| Constraint | Consequence |
|---|---|
| Same-region **p99 latency target: 95 ms** (TradingView POST → broker `OrderSend`) | Hot path is *parse → auth → publish*; **no database writes on the hot path** |
| **Multi-broker** (MT4, MT5 native socket, DXTrade REST) | Routing layer abstracts platform differences |
| **Multi-tenant** (license + instance model) | Per-license HMAC, secret, rate limit, daily quota, IP allowlist |
| **Operationally serious** | Prometheus on every service, durable at-least-once delivery, TimescaleDB for fills, Grafana/Tempo/Alertmanager |
| **Deploy flexibility** | One-command Bash (Ubuntu), PowerShell (Windows Server), and Helm (Kubernetes) installers |

---

## 2. System context

```
[TradingView alert]  ── any HTTPS webhook producer
        │
        ▼
[Cloudflare edge]    ── optional WAF / DDoS shield
        │
        ▼
┌──────────────────────── ExecRelay system ─────────────────────────┐
│                                                                    │
│  ingress ──► NATS JetStream ──► bridge ──► EA / DXTrade adapter    │
│     │                              │                               │
│  portal-web / portal-api ──── analytics / reports / persist        │
│                                                                    │
└────────────────────────────────────┬──────────────────────────────┘
                                      │  WebSocket / REST
                                      ▼
                          [Broker terminal — MT4 / MT5 / DXTrade]
```

The **EA (or DXTrade adapter) is the execution authority and broker-position
source of truth.** The bridge validates, routes, and records fills, but never
owns broker position state.

---

## 3. How a trade flows (end to end)

```
[TradingView alert]
        │
        ▼
[ingress]        parse → auth → publish protobuf Signal to NATS → 200 OK
        │        (≤ 5 ms typical; NO DB writes on the hot path)
        ▼
[NATS JetStream] durable, at-least-once delivery
        │
        ▼
[bridge]         route by platform (mt5 / mt4 / dxtrade)
        │
        ▼
[EA (MT5/MT4)  OR  dxtrade adapter]   OrderSend / PositionClose
        │
        ▼
[fill report]    EA → bridge → NATS FILLS stream
        │
        ▼
[persist]        NATS → TimescaleDB
```

Key property: the **request path is publish-only**. Ingress accepts the webhook,
authenticates it, serializes a protobuf `Signal`, publishes to NATS, and returns
`200 OK` — without touching Postgres. Everything durable (fills, analytics,
retention) happens asynchronously off the bus.

---

## 4. Services at a glance

| Service | Port | Language | Purpose |
|---|---|---|---|
| `ingress` | 8081 | Go | TradingView webhook receiver (hot path) |
| `bridge` | 8082 | Go | EA WebSocket hub + NATS dispatcher |
| `dxtrade` | 8083 | Go | DXTrade REST API adapter |
| `persist` | 8084 | Python | NATS → TimescaleDB writer |
| `portal-api` | 8085 | Python | REST API for license / instance management |
| `tasks` | 8086 | Python | Background workers (fill timeouts, retention) |
| `analytics` | 8087 | Python | Latency and fill-rate query API |
| `reports` | 8088 | Python | Daily / weekly performance reports |
| `portal-web` | 3001 | Next.js | Web dashboard |

**Foundation tier (always running):** TimescaleDB, NATS JetStream, Redis, MinIO,
Tempo, Prometheus, Grafana, Alertmanager.

**Language split is deliberate:** Go on the latency-critical path (ingress,
bridge, dxtrade) where predictable low-latency and concurrency matter; Python for
control-plane / analytical services where developer velocity matters more than
microseconds.

---

## 5. Component responsibilities

- **ingress (Go).** The only public hot-path entry. Validates the perimeter
  token, resolves the license, checks HMAC / alert secret, enforces rate limit +
  daily quota + CIDR allowlist, honors the kill switch (`INGRESS_TRADING_HALTED`),
  tags the signal with its region, serializes a protobuf `Signal`, and publishes
  to NATS. No DB writes.
- **NATS JetStream.** Durable, at-least-once message bus. Decouples ingest from
  execution so a slow broker can never block a webhook response, and an ingress
  restart never drops in-flight signals.
- **bridge (Go).** Holds persistent WebSocket connections from EAs. Consumes
  signals, routes by platform, dispatches to the correct EA/instance, and
  collects fill reports back onto the FILLS stream.
- **dxtrade (Go).** REST adapter for brokers reached over HTTP rather than a
  socket EA.
- **EA (MQL5 / MQL4).** Broker-side execution authority. `ea/mt5/ExecRelay.mq5`
  uses the native MT5 socket API; `ea/mt4/ExecRelay.mq4` requires the signed
  `ExecRelayWS.dll` because MQL4 has no native TCP socket API.
- **persist / tasks / analytics / reports (Python).** Off-path workers: write
  fills to TimescaleDB, expire unfilled signals, enforce retention, and serve
  analytics/reporting queries.
- **portal-web / portal-api.** Multi-tenant management plane: licenses,
  instances, quotas, dashboards.

---

## 6. Multi-tenancy & security model

- **License + instance model.** Each tenant has a license; each broker
  connection is an instance. Configured via `EXECRELAY_LICENSES`:
  ```
  licenseID:alertSecret:hmacSecret:instanceID[:platform[:pendingHmacSecret[:maxSignalsPerDay]]]
  ```
- **Per-license controls:** HMAC signing, alert secret, rate limit, daily quota,
  IP (CIDR) allowlist.
- **Perimeter token.** Optional shared secret (`?token=…`) required on every
  webhook.
- **Kill switch.** `INGRESS_TRADING_HALTED=true` rejects execution globally.
- **Secret hygiene.** Secrets only via environment variables, never baked into
  images; `gitleaks` runs in pre-commit and CI.

---

## 7. Deployment model

- **Local dev:** `docker compose --profile apps up -d --build`.
- **Single-server prod:** Bash installer (Ubuntu 22.04/24.04) or PowerShell
  installer (Windows Server 2022), with `configure-prod` (Caddy TLS) and
  `install-backups`.
- **Kubernetes:** Helm chart under `infra/helm/`.
- **Docker contract:** every image is multi-stage, digest-pinned, non-root,
  health-checkable, OCI-labeled, env-configured.

### Repository layout

```text
apps/        Service implementations (ingress, bridge, dxtrade, persist, portal-*, …)
packages/    Shared libraries (parser-go, proto, py-shared, ts-types)
ea/          Expert Advisors (MT4, MT5, MT4 WebSocket DLL)
infra/       docker/ caddy/ systemd/ helm/ migrations/ k8s/
loadtest/    Go load test tool (targets ingress hot path)
scripts/     Single-server installers (Bash + PowerShell)
docs/        Structured documentation
.github/     CI: per-app builds, tests, shellcheck, PSScriptAnalyzer
```

---

## 8. The Photos project (what came before)

`D:\Personal\Photos` is a **single-machine Python MT5 autotrader** — a working,
single-operator system that ExecRelay re-architects for multi-tenant production.

- **Language / framework:** Python 3 + Flask, run synchronously.
- **Structure:** script-based — ~18 independent `.py` files
  (`5001.py`, `5000.py`, `webhook.py`, `Autotrader.py`,
  `mt5_active_order_management.py`, `trade_dashboard.py`, `backtest.py`, …)
  with no module/package boundaries.
- **Signal path:** TradingView → Flask webhook (port 5000/5001) → symbol mapping
  → account routing → **MetaTrader5 Python library** (`mt5.initialize/login/
  order_send`) → local MT5 terminal.
- **Execution model:** in-process. The Flask handler calls the MT5 library
  directly, serialized with a single `threading.Lock()` because the MT5 library
  is not thread-safe — so it processes **one webhook at a time**.
- **State:** ephemeral. Live orders live in an in-memory `active_orders` dict
  plus JSON files; state resets when the server restarts. No database.
- **Configuration:** JSON files — `accounts.json` (logins/passwords/servers per
  account, with **hardcoded** Windows terminal paths), `symbol_map.json`
  (per-broker symbol translation), `config.json`, `symbols.json`.
- **UI:** a Flask/Jinja web dashboard (`trade_dashboard.py`, `templates/`,
  `static/`) plus a Tkinter desktop GUI (`Autotrader.py`).
- **Deployment:** packaged with PyInstaller (`build_exe.py`,
  `ReycapitalDashboard.spec`) into a single Windows `.exe` in `dist/`, or run
  manually with `python 5001.py`. Desktop/Windows-only; requires a local MT5
  install.

It is a functional personal automation tool — not built for multiple tenants,
horizontal scale, durability, or observability.

---

## 9. ExecRelay vs Photos — side by side

| Dimension | **Photos (MT5 Autotrader)** | **ExecRelay** |
|---|---|---|
| Primary language | Python 3 (Flask) | Go on hot path; Python for control/analytics; Next.js UI |
| Architecture style | Monolithic, script-based, single machine | Multi-service, message-bus-decoupled |
| Webhook → execution | In-process: Flask handler calls MT5 lib directly | ingress publishes Signal → NATS → bridge → EA/adapter |
| Concurrency | One webhook at a time (`threading.Lock`, MT5 not thread-safe) | Concurrent ingest; durable queue absorbs bursts |
| Hot-path latency | Per-webhook MT5 `initialize/login/shutdown` (expensive) | parse → auth → publish, ≤ ~5 ms, no DB writes; **95 ms p99** target |
| Durability | None — in-memory dict + JSON, resets on restart | NATS JetStream at-least-once; fills persisted to TimescaleDB |
| State ownership | App holds order state in memory | EA/adapter is the source of truth; bridge records fills |
| Persistence | JSON files, no database | TimescaleDB (fills), Redis, MinIO, golang-migrate schema |
| Multi-tenancy | Single operator; accounts in one JSON | License + instance model, per-license quotas/secrets/allowlists |
| Security | Credentials in plaintext JSON; hardcoded paths | Per-license HMAC, perimeter token, CIDR allowlist, kill switch, env-only secrets, gitleaks |
| Brokers | MT5 only (local terminal) | MT4, MT5 (native socket), DXTrade (REST) |
| Observability | `webhook.log` file; ad-hoc | Prometheus + Grafana + Tempo + Alertmanager on every service |
| Deployment | PyInstaller `.exe`, manual run, Windows desktop | Docker Compose, Bash/PowerShell installers, Helm/Kubernetes |
| Config | Hardcoded paths + JSON, read at startup | Environment variables, 12-factor, installer-generated secrets |
| DR / ops | None | Automated backups, restore drills, RPO/RTO targets, runbooks |
| Testing / CI | Standalone backtest script; no CI | Go tests, load test, pre-commit + CI gates, per-app builds |

---

## 10. Why ExecRelay exists (the core shift)

The Photos project proves the trading logic works but is bounded by its
architecture: a single Windows machine, one webhook at a time, no durability,
secrets in plaintext, and one broker. ExecRelay keeps the same *job* — get a
TradingView alert to a broker `OrderSend` as fast and reliably as possible — and
re-engineers it for production:

1. **Decoupling.** A durable message bus (NATS JetStream) sits between ingest and
   execution, so a slow or disconnected broker never blocks the webhook response
   and no in-flight signal is lost on restart.
2. **Latency discipline.** The hot path does no database work; persistence and
   analytics run off the bus. This is what makes the 95 ms p99 target reachable.
3. **Multi-tenancy & security.** Licenses, instances, per-tenant quotas, HMAC,
   allowlists, and a kill switch replace a single plaintext `accounts.json`.
4. **Source-of-truth correctness.** The broker-side EA owns position state; the
   platform records fills rather than guessing them from an in-memory dict.
5. **Operability.** Metrics, tracing, alerting, backups, runbooks, and repeatable
   installers replace a `.log` file and a hand-run `.exe`.

In short: **Photos is a personal autotrader; ExecRelay is the same idea rebuilt
as durable, observable, multi-tenant execution infrastructure.**

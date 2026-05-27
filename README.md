# ExecRelay

**Low-latency execution infrastructure for automated traders.** ExecRelay relays
TradingView (and TradingView-compatible) webhook alerts through regional ingress,
NATS JetStream, and the ExecRelay Bridge to broker-side Expert Advisors over a
persistent WebSocket connection.

ExecRelay is **not** a signal provider, broker, copy-trading platform, or AI
trading platform. It is the wire between *your* signal source and *your* broker
account.

---

## What you get

- **Same-region p99 latency target: 95 ms** from TradingView POST to broker
  `OrderSend`. The hot path is parse → auth → publish; no Postgres writes.
- **Multi-broker.** MT4, MT5 (native socket), and DXTrade (REST) supported today.
- **Multi-tenant.** License + instance model with per-license HMAC, secret,
  rate limit, daily quota, and IP allowlist.
- **Operationally serious.** Prometheus metrics on every service, NATS JetStream
  for durable at-least-once delivery, TimescaleDB for fills, Grafana + Tempo +
  Alertmanager wired up.
- **One-command deploy.** Bash installer for Ubuntu, PowerShell installer for
  Windows Server, Helm chart for Kubernetes.

## Documentation map

| Path | What's in it |
|---|---|
| [`STANDALONE_DEPLOYMENT.md`](STANDALONE_DEPLOYMENT.md) | How to install on Ubuntu or Windows Server, ops cookbook, DR |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System overview, data flow, trust boundaries, technology choices |
| [`SECURITY.md`](SECURITY.md) | Vulnerability disclosure, threat model, security boundaries |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development workflow, branch / commit / PR conventions |
| [`docs/data-model.md`](docs/data-model.md) | Tables, columns, ER diagram, why each table exists |
| [`docs/observability.md`](docs/observability.md) | Prometheus metrics catalog, recommended alerts |
| [`docs/glossary.md`](docs/glossary.md) | License, Instance, Trace ID, Signal, Fill, EA — domain vocabulary |
| [`docs/disaster-recovery.md`](docs/disaster-recovery.md) | Backups, restore drills, RPO/RTO targets |
| [`docs/compliance.md`](docs/compliance.md) | Data retention, restricted jurisdictions, audit-log handling |
| [`docs/api/portal-api.md`](docs/api/portal-api.md) | Portal API reference, auth flow, curl examples |
| [`docs/api/ingress.md`](docs/api/ingress.md) | Webhook + admin endpoint reference for ingress |
| [`docs/customer/webhook-integration.md`](docs/customer/webhook-integration.md) | End-to-end customer guide: TradingView alert → broker fill |
| [`docs/runbooks/`](docs/runbooks/) | On-call runbooks: ingress 5xx, postgres down, kill switch tripped, fills not arriving, license misconfigured |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — why NATS, why FastAPI, why hand-rolled proto, etc. |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each release |

---

## Quick start

### Local development (any host with Docker)

```sh
cp .env.example .env
docker compose --profile apps up -d --build
```

The stack listens on:

```
Portal web   → http://localhost:3001
Portal API   → http://localhost:8085
Ingress      → http://localhost:8081/webhook
Grafana      → http://localhost:3000   (admin / admin)
```

Stop everything: `docker compose --profile apps down`.

### Single-server production install

**Ubuntu 22.04 / 24.04** — see [`STANDALONE_DEPLOYMENT.md`](STANDALONE_DEPLOYMENT.md):

```sh
sudo bash scripts/install.sh
sudo DOMAIN=execrelay.example.com EMAIL=ops@example.com \
  bash scripts/configure-prod.sh
sudo bash scripts/install-backups.sh
```

**Windows Server 2022** (PowerShell, elevated):

```powershell
.\scripts\install.ps1
.\scripts\configure-prod.ps1 -Domain execrelay.example.com -Email ops@example.com
.\scripts\install-backups.ps1
```

**Kubernetes** — see [`infra/helm/README.md`](infra/helm/README.md).

---

## How a trade actually flows

```
[TradingView alert]
        │
        ▼
[Cloudflare edge] ─── optional WAF / DDoS shield
        │
        ▼
[ingress]       parse  →  auth  →  publish protobuf Signal to NATS  →  200 OK
        │       (≤ 5 ms typical; no DB writes on the hot path)
        ▼
[NATS JetStream] durable, at-least-once delivery
        │
        ▼
[bridge]        route by platform (mt5 / mt4 / dxtrade)
        │
        ▼
[EA (MT5/MT4) OR dxtrade adapter]   OrderSend / PositionClose
        │
        ▼
[fill report]   EA → bridge → NATS FILLS stream
        │
        ▼
[persist]       NATS → TimescaleDB
```

The EA (or DXTrade adapter) is the **execution authority and broker-position
source of truth**. Bridge validates, routes, and records fills, but never owns
broker position state.

For the full architecture story, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Services at a glance

| Service | Port | Language | Purpose |
|---|---|---|---|
| `ingress` | 8081 | Go | TradingView webhook receiver |
| `bridge` | 8082 | Go | EA WebSocket hub + NATS dispatcher |
| `dxtrade` | 8083 | Go | DXTrade REST API adapter |
| `persist` | 8084 | Python | NATS → TimescaleDB writer |
| `portal-api` | 8085 | Python | REST API for license/instance management |
| `tasks` | 8086 | Python | Background workers (fill timeouts, retention) |
| `analytics` | 8087 | Python | Latency and fill-rate query API |
| `reports` | 8088 | Python | Daily/weekly performance reports |
| `portal-web` | 3001 | Next.js | Web dashboard |

**Foundation tier** (always running): TimescaleDB, NATS JetStream, Redis, MinIO,
Tempo, Prometheus, Grafana, Alertmanager, MLflow.

For per-service deep dives, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Expert Advisors

| Platform | Path | Notes |
|---|---|---|
| MT5 | `ea/mt5/ExecRelay.mq5` | Uses native MT5 socket API (build 2715+) |
| MT4 | `ea/mt4/ExecRelay.mq4` | Requires `ExecRelayWS.dll` (see `ea/mt4-ws-dll/`) |

MT4 requires a signed WebSocket DLL because MQL4 has no native TCP socket API.

---

## Configuration

All services read configuration from environment variables. Copy `.env.example`
to `.env` as a starting point. For the installer-driven flow, secrets are
generated automatically. The most important variables:

| Variable | Service | Description |
|---|---|---|
| `EXECRELAY_LICENSES` | ingress | Semicolon-separated license entries (see below) |
| `INGRESS_PERIMETER_TOKEN` | ingress | Optional shared secret required as `?token=<value>` on every webhook |
| `INGRESS_TRADING_HALTED` | ingress | `true` to start in kill-switch mode |
| `INGRESS_REGION` | ingress | Region tag included in every signal |
| `BRIDGE_REGION` | bridge | Region tag for the bridge |
| `DXTRADE_INSTANCES` | dxtrade | Semicolon-separated DXTrade credentials |
| `JWT_SECRET` | portal-api | HMAC secret for JWT tokens |
| `PORTAL_API_URL` | portal-web | URL of portal-api (server-side proxy) |
| `RETENTION_DAYS` | tasks | Days to retain signals and fills (default 90) |
| `FILL_TIMEOUT_SECS` | tasks | Seconds before unfilled signal is flagged (default 30) |
| `PAGERDUTY_INTEGRATION_KEY` | alertmanager | Optional, for critical alerts |
| `SLACK_WEBHOOK_URL` | alertmanager | Optional, for warning alerts |

`EXECRELAY_LICENSES` format:
```
licenseID:alertSecret:hmacSecret:instanceID[:platform[:pendingHmacSecret[:maxSignalsPerDay]]]
```
Multiple entries separated by `;`.

For the customer-facing flow (setting up a TradingView alert), see
[`docs/customer/webhook-integration.md`](docs/customer/webhook-integration.md).

---

## Development

```sh
make check                # tests + benchmarks + compose config validation
make test                 # Go tests
make migrate-up           # apply pending DB migrations
make install-hooks        # install pre-commit (one-time per clone)
make lint                 # run pre-commit on whole tree
```

Pre-commit hooks (`gofmt`, `ruff-format`, `gitleaks`, etc.) run automatically
on `git commit`. CI re-runs them on every PR so contributors who skipped
`make install-hooks` still get checked.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for branch/commit/PR conventions and
[`docs/development/`](docs/development/) for IDE setup and debugging guides.

---

## Repository layout

```text
apps/                Service implementations (see "Services" above)
packages/            Shared libraries (parser-go, proto, py-shared, ts-types)
ea/                  Expert Advisors (MT4, MT5, MT4 WebSocket DLL)
infra/
  docker/            NATS, Prometheus, Tempo, Alertmanager configs
  caddy/             Caddyfile template used by scripts/configure-prod.sh
  systemd/           Systemd unit templates (Linux installer)
  helm/              Kubernetes Helm chart
  migrations/        golang-migrate DB schema migrations
  k8s/               Cluster-deploy scripts (minikube, EKS)
loadtest/            Go load test tool (targets ingress hot path)
scripts/             Single-server installers (Bash + PowerShell)
docs/                All structured documentation (see "Documentation map")
.github/workflows/   CI: per-app builds, tests, shellcheck, PSScriptAnalyzer
```

---

## Docker contract

Every service image:
- Multi-stage, digest-pinned base images
- Non-root runtime user
- Health-checkable (`/health` endpoint or `--healthcheck` flag)
- OCI-labeled
- Configured only through environment variables
- Secrets never baked into images

---

## License

See [`LICENSE`](LICENSE).

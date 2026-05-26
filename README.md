# ExecRelay

Chart alerts to broker execution — fast.

ExecRelay is low-latency execution infrastructure for automated traders. It relays
TradingView webhook alerts through regional ingress, NATS JetStream, and the ExecRelay
Bridge to broker-side Expert Advisors over a persistent WebSocket connection.

ExecRelay is not a signal provider, broker, copy-trading platform, or AI trading platform.

## Execution Architecture

```text
TradingView Alert
  → Cloudflare Edge
  → Go Webhook Ingress        (parse, auth, publish protobuf Signal to NATS)
  → NATS JetStream            (durable, at-least-once delivery)
  → ExecRelay Bridge          (route by platform: mt5/mt4/dxtrade)
      → MT5/MT4 EA            (persistent WebSocket → OrderSend / PositionClose)
      → DXTrade Adapter       (REST API call)
  → Fill report               (EA → Bridge → NATS FILLS stream)
  → Persist Service           (NATS → TimescaleDB)
```

The EA (or DXTrade adapter) is the execution authority and broker-position source of
truth. The Bridge validates, routes, and records fill reports, but never owns broker
position state.

## Latency Contract

Same-region target:

```text
TradingView POST → EA OrderSend ≤ 95ms p99
```

Hot-path rules:

- Go ingress parses, validates, authenticates, publishes protobuf Signal to NATS, and
  returns 200 immediately.
- No Postgres writes on the hot path.
- Persistence, analytics, reports, and portal APIs are cold-path consumers.
- Bridge → EA WebSocket fan-out is the primary execution architecture.

## Services

| Service | Port | Language | Purpose |
|---|---|---|---|
| ingress | 8081 | Go | TradingView webhook receiver |
| bridge | 8082 | Go | EA WebSocket hub + NATS dispatcher |
| dxtrade | 8083 | Go | DXTrade REST API adapter |
| persist | 8084 | Python | NATS → TimescaleDB writer |
| portal-api | 8085 | Python | REST API for license/instance management |
| portal-web | 3001 | Next.js | Web dashboard |
| tasks | 8086 | Python | Background workers (fill timeouts, retention) |
| analytics | 8087 | Python | Latency and fill-rate query API |
| reports | 8088 | Python | Daily/weekly performance reports |

Foundation stack (always running): TimescaleDB, NATS JetStream, Redis, MinIO, Tempo,
Prometheus, Grafana, MLflow.

## Expert Advisors

| Platform | Path | Notes |
|---|---|---|
| MT5 | `ea/mt5/ExecRelay.mq5` | Uses native MT5 socket API (build 2715+) |
| MT4 | `ea/mt4/ExecRelay.mq4` | Requires `ExecRelayWS.dll` (see `ea/mt4-ws-dll/`) |

The MT4 EA requires a signed WebSocket DLL because MQL4 has no native TCP socket API.
Build instructions are in `ea/mt4-ws-dll/`.

## Quick Start

```sh
cp .env.example .env
# Edit .env — set EXECRELAY_LICENSES at minimum for the ingress to accept alerts.

make up                    # Start foundation stack
docker compose --profile apps up -d   # Start app services
```

Run all checks (tests, benchmarks, compose config validation):

```sh
make check
```

Run the load test against a live ingress (target: p99 ≤ 95 ms):

```sh
make loadtest
```

## Configuration

All services are configured via environment variables. Copy `.env.example` to `.env`
and fill in the required values.

### Key variables

| Variable | Service | Description |
|---|---|---|
| `EXECRELAY_LICENSES` | ingress | Semicolon-separated license entries |
| `INGRESS_REGION` | ingress | Region tag included in every signal |
| `BRIDGE_REGION` | bridge | Region tag for the bridge |
| `DXTRADE_INSTANCES` | dxtrade | Semicolon-separated DXTrade credentials |
| `JWT_SECRET` | portal-api | HMAC secret for JWT tokens |
| `PORTAL_API_URL` | portal-web | URL of portal-api (server-side proxy) |
| `RETENTION_DAYS` | tasks | Days to retain signals and fills (default 90) |
| `FILL_TIMEOUT_SECS` | tasks | Seconds before unfilled signal is flagged (default 30) |

`EXECRELAY_LICENSES` format:
```
licenseID:alertSecret:hmacSecret:instanceID[:platform]
```
`platform` is `mt5` (default), `mt4`, or `dxtrade`. Multiple entries are separated by `;`.

`DXTRADE_INSTANCES` format:
```
instanceID:host:username:password:account
```
Multiple entries separated by `;`.

## Repository Layout

```text
apps/
  ingress/          Go webhook ingress
  bridge/           Go WebSocket bridge
  dxtrade/          Go DXTrade adapter
  persist/          Python NATS→DB writer
  portal-api/       Python FastAPI portal backend
  portal-web/       Next.js portal frontend
  tasks/            Python background workers
  analytics/        Python analytics query API
  reports/          Python report generator
packages/
  parser-go/        Hot-path PineConnector-compatible alert parser
  proto/            Protobuf definitions (Signal, SignalParam)
ea/
  mt5/              MT5 Expert Advisor (MQL5)
  mt4/              MT4 Expert Advisor (MQL4)
  mt4-ws-dll/       WebSocket DLL for MT4 (C++/WinSock2)
infra/
  docker/           Postgres init SQL, Prometheus/Tempo config
  grafana/          Grafana provisioning
  terraform/        Infrastructure-as-code stubs
loadtest/           Go load test tool (targets ingress hot path)
.github/workflows/  CI: tests, Docker builds, Trivy, SBOM
```

## Docker Contract

Every service:
- Multi-stage, digest-pinned base images
- Non-root runtime user
- Health-checkable (`/health` endpoint or `--healthcheck` flag)
- OCI-labeled
- Configured only through environment variables
- Secrets never baked into images

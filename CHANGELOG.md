# Changelog

All notable changes to this project. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); historical
entries use the project's earlier phase-based naming and are kept
verbatim for the audit trail.

For unreleased changes, see [`git log main`](https://github.com/ninad-k/ExecRelay/commits/main).

---

## Phase 13 -- MT4 EA + WebSocket DLL + Supplementary

Status: PASS.

Completed scope:

- **MT4 WebSocket DLL** (`ea/mt4-ws-dll/`):
  - `src/ws_dll.cpp` — 560-line self-contained C++ WinSock2 DLL; no external
    dependencies beyond `ws2_32.dll`.
  - Inline SHA-1 (RFC 3174) and Base64 for the WebSocket handshake.
  - Connection pool of up to 8 handles guarded by `CRITICAL_SECTION`.
  - RFC 6455 framing: masked client frames, auto-pong on server ping,
    16-bit/64-bit extended length, clean close handling.
  - Five `__stdcall` exports matching the MQL4 `#import` declaration:
    `WsConnect`, `WsDisconnect`, `WsIsConnected`, `WsSend`, `WsRead`.
  - `DllMain` handles `WSAStartup`/`WSACleanup` and pool initialisation.
  - Build instructions: CMake + MinGW cross-compile from Linux
    (`cmake/mingw32.cmake`), MSVC on Windows (`build.bat`), or single-command
    MinGW on Windows.
  - `README.md` covers exported API, all three build paths, and installation
    path inside the MT4 data folder.

- **MT4 EA** (`ea/mt4/ExecRelay.mq4`):
  - Imports the five DLL functions via `#import "ExecRelayWS.dll"` with
    `uchar[]` array parameters matching the C `char*` ABI.
  - Reconnects every 3 s via `EventSetMillisecondTimer`; reads frames in
    a tight `while (WsRead(...) > 0)` loop per timer tick.
  - `SendRegister` reports `platform: "mt4"`, `AccountNumber()`,
    `AccountCompany()`.
  - Full command set: `buy`, `sell`, `buystop`, `sellstop`, `buylimit`,
    `selllimit`, `closebuy`, `closesell`, `closeall`, `cancel`.
  - `PipSize` normalises 3/5-digit broker quotes.
  - Pending orders use `entry` price parameter; SL/TP accept absolute price
    or pip offset (`sl_pips`, `tp_pips`).
  - `ClosePositions` iterates `OrdersTotal()` with `SELECT_BY_POS / MODE_TRADES`,
    skips pending orders and non-matching magic number.
  - `CancelPending` deletes orders with `OrderType() >= OP_BUYLIMIT`.
  - Fill reporting is identical JSON to the MT5 EA for backend compatibility.

- **DXTrade unit tests**:
  - `apps/dxtrade/internal/dxtrade/config_test.go` — 8 tests for
    `ParseInstanceConfigs`: empty, whitespace-only, single, multiple, trailing
    semicolon, wrong field count, missing instanceID, missing host,
    whitespace trimming.
  - `apps/dxtrade/internal/dxtrade/command_test.go` — 9 tests for
    `ParseCommand`: buy/sell market, all four pending types, three close
    variants (no volume required), missing volume, invalid volume string,
    zero volume, negative volume, unsupported action, extra params ignored.

- **`.env.example`** updated with all service env vars introduced in Phases
  7–12: `EXECRELAY_LICENSES`, `INGRESS_REGION`, `BRIDGE_REGION`,
  `DXTRADE_INSTANCES`, `JWT_SECRET`, `PORTAL_API_URL`, `RETENTION_DAYS`,
  `FILL_TIMEOUT_SECS`.

- **`README.md`** fully rewritten: architecture diagram covering all three
  EA paths (MT5 native socket, MT4 DLL WebSocket, DXTrade REST adapter),
  services table, Expert Advisors table with DLL requirement noted, Quick
  Start, and Configuration section documenting all key variables.

Validation:

- `go test ./...`: PASS (14 dxtrade tests, bridge/ingress suite all green).
- `go build ./...`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `ws_dll.cpp` compiles clean on macOS clang (expected missing Windows headers
  are not errors — DLL targets MinGW/MSVC on Windows only).

Notes:

- The MT4 DLL must be **32-bit**. MT4 is a 32-bit process on all platforms,
  so the MinGW cross-compile target is `i686-w64-mingw32` and MSVC build uses
  the x86 Native Tools prompt.
- SHA-1 is implemented inline (no `<openssl/sha.h>` dependency) because the DLL
  needs to run on any Windows installation without redistributable packages.
- The MT4 EA and MT5 EA produce identical JSON fill reports so the persist
  service and portal fill history work without modification.

## Phase 12 -- Load Testing

Status: PASS.

Completed scope:

- `loadtest/cmd/loadtest/main.go` — a standalone Go load test binary.
  - Sends concurrent signed POST `/webhook` requests to the ingress at a
    configurable rate (default 50 req/s) for a configurable duration (default 30s).
  - Computes and prints p50, p95, p99, min, max latency in milliseconds.
  - Exits 0 if p99 ≤ 95ms (the system latency target); exits 2 if the target is
    exceeded; exits 1 if no responses were received.
  - Flags: `-target`, `-license`, `-hmac-secret`, `-alert-secret`, `-rate`,
    `-duration`, `-workers`.
- Added `loadtest` Makefile target:
  ```
  make loadtest TARGET=http://localhost:8081/webhook RATE=200 DURATION=60s
  ```
  Requires a running stack (`make up` first).
- `go test ./...` registers the loadtest package (`[no test files]`) without errors.
- `go build -o /tmp/bin ./loadtest/cmd/loadtest`: PASS.

Notes:

- The load test measures ingress-only latency (HTTP round-trip to /webhook). End-to-end
  latency (POST → EA execution) requires a live bridge + EA and is measured separately
  by joining `accepted_signals.received_at` with `fills.created_at` via the analytics
  service latency endpoint.
- Default credentials match the ingress test fixture (`hmac-secret`, `alert-secret`).
  Override with env var overrides in `EXECRELAY_LICENSES` for a real stack.

## Phase 11 -- Reports Service

Status: PASS.

Completed scope:

- Replaced persist-copying placeholder with a real FastAPI reports service.
- `GET /health` — health check.
- `GET /reports?report_type=<type>&limit=N` — list recent report runs.
- `GET /reports/{id}` — fetch a specific report's full payload.
- `POST /reports/generate?report_type=<type>&target_date=YYYY-MM-DD` — generate and
  store a report; idempotent via `ON CONFLICT (report_type, data_as_of, content_hash)`.
- Report types: `daily_signal_summary` (signals, fills, fill rate for one calendar day),
  `weekly_performance` (7-day latency p50/p95/p99 and per-day signal counts).
- Results stored in `report_runs` table with JSONB payload and SHA-256 content hash.
- Fixed Dockerfile: copies `apps/reports/app.py` (not `apps/persist/app.py`), installs
  requirements into `/app/deps`.
- Added `apps/reports/requirements.txt` (fastapi, uvicorn, asyncpg).
- Updated `docker-compose.yml` reports service with `DATABASE_URL` and postgres healthcheck dependency.

## Phase 10 -- Analytics Service

Status: PASS.

Completed scope:

- Replaced persist-copying placeholder with a real FastAPI analytics query service.
- `GET /health` — health check.
- `GET /analytics/signals/summary?window_hours=N&license_id=<id>` — total signal count,
  breakdown by command and symbol for the given time window.
- `GET /analytics/fills/summary?window_hours=N&license_id=<id>` — fill count, fill rate
  percentage, counts by status.
- `GET /analytics/latency?window_hours=N&license_id=<id>` — signal-to-fill latency stats
  (avg, p50, p95, p99 in milliseconds) computed directly from TimescaleDB via
  `PERCENTILE_CONT`.
- Fixed Dockerfile: copies `apps/analytics/app.py`, installs requirements.
- Added `apps/analytics/requirements.txt` (fastapi, uvicorn, asyncpg).
- Updated `docker-compose.yml` analytics service with `DATABASE_URL` and postgres
  healthcheck dependency.

## Phase 9 -- Tasks Service

Status: PASS.

Completed scope:

- Replaced placeholder with a real async background worker service.
- Three periodic tasks running in an asyncio event loop:
  - `fill_timeout_check` (every `FILL_CHECK_INTERVAL` seconds, default 60): queries
    `accepted_signals` joined with `fills` to find signals older than `FILL_TIMEOUT_SECS`
    (default 30s) that have no fill; writes `fill_timeout` events to `system_events`.
  - `data_retention` (every `RETENTION_INTERVAL` seconds, default 86400): deletes fills
    older than `RETENTION_DAYS` (default 90); calls TimescaleDB `drop_chunks` on
    `accepted_signals`, falls back to plain DELETE if not available.
  - `task_processor` (every `TASK_POLL_INTERVAL` seconds, default 10): claims up to 10
    pending rows from `tasks` table using `FOR UPDATE SKIP LOCKED`; marks completed or
    failed after processing.
- HTTP health server on a daemon thread alongside the asyncio loop.
- Graceful DB-unavailability: starts with no pool and idles rather than crashing.
- Fixed Dockerfile: copies `apps/tasks/app.py`, installs asyncpg into `/app/deps`.
- Added `apps/tasks/requirements.txt` (asyncpg).
- Updated `docker-compose.yml` tasks service with `DATABASE_URL`, `RETENTION_DAYS`,
  `FILL_TIMEOUT_SECS`, and postgres healthcheck dependency.

## Phase 8 -- Portal Web

Status: PASS.

Completed scope:

- Replaced busybox httpd placeholder with a full Next.js 14 (App Router, TypeScript,
  Tailwind CSS) single-page application.
- **Pages:**
  - `/login` — sign in / register form; stores JWT in `localStorage`; redirects on
    success.
  - `/dashboard` — license list with platform badge, active/inactive toggle, create
    license form.
  - `/dashboard/licenses/[id]` — license detail page with three sections:
    - Ingress config: generated `EXECRELAY_LICENSES` string with copy-to-clipboard.
    - Instances: list existing instances with fill history table; create-instance form.
    - Signal history: paginated table of received signals (trace ID, command, symbol,
      region, timestamp).
- **Auth guard:** `DashboardLayout` checks `localStorage` on mount and redirects to
  `/login` if no token is found.
- **API proxy:** `next.config.mjs` rewrites `/api/:path*` to the `PORTAL_API_URL`
  env var (default `http://portal-api:8080`). No CORS configuration required; the
  browser always calls the same origin.
- **Health route:** `GET /health` returns `{"service":"portal-web","status":"ok"}`
  via Next.js App Router route handler. Dockerfile HEALTHCHECK uses this endpoint.
- **Build:** `output: 'standalone'` in next.config.mjs produces a minimal Node.js
  server. Three-stage Dockerfile: `deps` (npm install) → `builder` (next build) →
  `runtime` (standalone server).
- **docker-compose.yml** portal-web service: added `PORTAL_API_URL` and
  `depends_on: portal-api: service_started`.
- Removed obsolete `server.mjs` and `health.json` placeholder files.

Validation:

- `go test ./...`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- TypeScript checked via `tsc --noEmit` in build stage (`npm run type-check`).

Notes:

- The JWT token is stored in `localStorage`. For a production deployment, consider
  migrating to `HttpOnly` cookies to prevent XSS token theft.
- `PORTAL_API_URL` is a server-side runtime env var (not baked in at build time),
  so the same image can be redeployed pointing at a different API endpoint.
- For local dev without Docker Compose, set `PORTAL_API_URL=http://localhost:8085`
  and run `npm run dev` in `apps/portal-web/`.

## Phase 7 -- DXTrade Adapter + Platform Routing

Status: PASS.

Completed scope:

- **Platform-aware signal routing** in ingress: `LicenseRecord` gained a `Platform`
  field (mt4/mt5/dxtrade; defaults to `mt5`). `EXECRELAY_LICENSES` now accepts an
  optional 5th colon-separated field: `license:secret:hmac:instance[:platform]`.
  `signalSubject` now prefixes with platform: `signals.<platform>.<licenseID>.<instanceID>`.
- **Bridge updated** for dual-platform consumers: `stream.go` exports
  `SignalSubjectMT5 = "signals.mt5.>"` and `SignalSubjectMT4 = "signals.mt4.>"`.
  `Subscriber.Subscribe()` creates two durable consumers (`bridge-mt5`, `bridge-mt4`)
  and returns `[]*nats.Subscription`. After `maxDeliverBeforeTerm = 10` redeliveries,
  unroutable signals are terminated with `msg.Term()` to prevent infinite loops.
- **DXTrade adapter** (`apps/dxtrade/`):
  - `internal/dxtrade/config.go`: `InstanceConfig` with `instanceID:host:username:password:account`
    format; `DXTRADE_INSTANCES` env var; `ConfigFromEnv`.
  - `internal/dxtrade/command.go`: `Command` and `Result` types; `ParseCommand` maps
    signal fields to typed trading instructions.
  - `internal/dxtrade/client.go`: HTTP client with session management; auto-reauth on
    401; `Execute` dispatches buy/sell/stop/limit/close/cancel to DXTrade REST API
    (`/api/auth/login`, `/api/trading/accounts/{account}/orders`,
    `/api/trading/accounts/{account}/positions`).
  - `internal/dxtrade/subscriber.go`: JetStream push consumer on `signals.dxtrade.>`;
    `maxDeliverBeforeTerm = 10`; publishes fill JSON to `fills.<instanceID>.<traceID>`.
  - `cmd/dxtrade/main.go`: real wiring — NATS, JetStream, per-instance clients,
    subscriber, health endpoint, graceful shutdown.
  - `Dockerfile`: updated to copy `go.sum`, run `go mod download`, copy `packages/proto`.
- **docker-compose.yml** dxtrade service: added `NATS_URL`, `SIGNALS_STREAM`,
  `SIGNALS_CONSUMER`, `DXTRADE_INSTANCES`, and `depends_on: nats: service_healthy`.

Validation:

- `go build ./...`: PASS.
- `go test ./...`: PASS (bridge hub + WS handler + ingress webhook tests all green).
- `docker compose --profile apps config >/dev/null`: PASS.

Notes:

- DXTrade `signals.dxtrade.>` is routed exclusively to the dxtrade service; bridge
  only subscribes to `signals.mt5.>` and `signals.mt4.>`. No overlap.
- Fill reports from dxtrade are written to the `FILLS` JetStream stream (same as
  bridge fills) and persisted by the persist service.
- `DXTRADE_INSTANCES` is optional; an empty value means no DXTrade instances are
  configured and all `signals.dxtrade.>` messages will be nak'd then terminated.

## Phase 6 -- Portal API

Status: PASS.

Completed scope:

- Replaced broken placeholder `apps/portal-api/app.py` with a FastAPI REST API.
- Fixed identically broken placeholders in `apps/tasks`, `apps/analytics`,
  and `apps/reports` (each had a stale import from `apps.persist.app`).
- Auth: `POST /auth/register`, `POST /auth/login` → JWT Bearer tokens (PyJWT,
  bcrypt, HS256, 7-day TTL).
- Licenses: `GET /licenses`, `POST /licenses` (generates license_key + hmac_secret),
  `PATCH /licenses/{id}` (toggle active).
- Instances: `GET /licenses/{id}/instances`, `POST /licenses/{id}/instances`
  (platform: mt4/mt5/dxtrade), `PATCH /licenses/{id}/instances/{inst_id}`.
- Config export: `GET /licenses/{id}/config` returns `EXECRELAY_LICENSES` string
  ready to paste into the ingress service env var.
- History: `GET /licenses/{id}/signals?limit=50`,
  `GET /licenses/{id}/instances/{inst_id}/fills?limit=50`.
- `GET /health` returns `{"service":"portal-api","status":"ok"}`.
- Added `apps/portal-api/requirements.txt` (fastapi, uvicorn, asyncpg, bcrypt,
  PyJWT, pydantic[email]).
- Updated `apps/portal-api/Dockerfile` to install deps into `/app/deps`.
- Updated `docker-compose.yml` portal-api service with `DATABASE_URL`,
  `JWT_SECRET`, and postgres healthcheck dependency.

Validation:

- `make test`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- Python syntax check via `py_compile` in Dockerfile build stage.

Notes:

- `hmac_secret_hash` field stores the SHA-256 hash of the HMAC secret for
  lookup purposes. The plaintext secret is returned once at license creation
  and is used verbatim by the ingress for HMAC verification. The ingress
  currently loads secrets from `EXECRELAY_LICENSES` env var; Phase 7+ will
  allow the ingress to load them from Postgres directly.
- `JWT_SECRET` defaults to `changeme-in-production` — must be overridden
  in any non-local deployment.
- All endpoints except `/health` require a valid JWT Bearer token.

## Phase 5 -- Persist Service

Status: PASS.

Completed scope:

- Rewrote `apps/persist/app.py` as an async NATS-to-Postgres persistence worker.
- Hand-written protobuf wire-format parser decodes `Signal` messages from
  `signals.>` without requiring `protoc` or generated `_pb2.py` files.
- Subscribes to JetStream streams `SIGNALS` and `FILLS` with durable push
  consumers (`persist-signals`, `persist-fills`). All messages are acked; DB
  errors are logged and do not cause redelivery storms.
- `persist_signal`: looks up `license_key → licenses.id` and
  `instance_key → instances.id`, then inserts into `accepted_signals`.
  Skips gracefully if the license is not yet in the database.
- `persist_fill`: joins `instances → licenses`, inserts into `fills`.
  Skips gracefully if the instance is not yet in the database.
- Both writes use `ON CONFLICT DO NOTHING` for idempotent at-least-once delivery.
- HTTP health endpoint at `/health` runs in a daemon thread alongside the
  async event loop.
- DB pool (`asyncpg`) is optional: if Postgres is unreachable at startup the
  worker logs a warning and acks all messages without writing.
- Updated bridge `handler.go` to publish fill JSON to NATS on subject
  `fills.<instanceID>.<traceID>` when a fill report arrives from an EA.
- Added `bridge.EnsureFillsStream` to create the `FILLS` JetStream stream on
  bridge startup.
- Added `apps/persist/requirements.txt` (`nats-py==2.9.0`, `asyncpg==0.30.0`).
- Updated `apps/persist/Dockerfile` to install deps into `/app/deps` and
  set `PYTHONPATH` at runtime.
- Updated `docker-compose.yml` persist service with `NATS_URL`, `DATABASE_URL`,
  and healthcheck dependencies on nats and postgres.

Validation:

- `make test`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `go build ./apps/bridge/...`: PASS (fills publisher wired).

Notes:

- The persist service does not create JetStream streams; bridge creates them
  on startup. Persist fails to subscribe if bridge has never started.
- `fills` table has `signal_id UUID` which is nullable; the persist service
  does not populate it in Phase 5 (signal→fill linkage requires a join that
  is deferred to the portal API phase).

## Phase 4 -- MT5 EA

Status: PASS.

Completed scope:

- Implemented `ea/mt5/ExecRelay.mq5` — a single-file MT5 Expert Advisor.
- WebSocket client over raw TCP socket: manual HTTP upgrade handshake,
  RFC 6455 frame encode (masked client→server) and decode (unmasked server→client).
  Handles close frame (opcode 0x8) and ping frame (opcode 0x9) with pong reply.
- `OnTimer` runs every 200ms: reconnects if disconnected, drains incoming frames
  without blocking the MT5 terminal.
- Registration flow: `register` → wait for `registered` ack → then accept signals.
- Executes: `buy`/`sell` (market), `buystop`/`sellstop`/`buylimit`/`selllimit`
  (pending), `closebuy`/`closesell`/`closeall` (by direction), `cancel`
  (delete pending orders).
- SL/TP: accepts explicit price (`sl`, `tp`) or pip-based (`sl_pips`, `tp_pips`).
  Pip size accounts for 3/5-digit vs 4/2-digit broker conventions.
- All positions and orders are filtered by `InpMagicNumber` to avoid interfering
  with manually placed trades.
- Reports every outcome via `fill` message: `filled`, `rejected`, or `error`
  with broker retcode and description.

Validation:

- `make test`: PASS (Go services unaffected).
- `make bench`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- MQL5 compilation requires MT5 build 2715+ (SocketCreate, SocketConnect,
  SocketSend, SocketRead, SocketIsConnected, SocketClose).

Notes:

- EA connects to bridge at `InpBridgeHost:InpBridgePort/ea/ws`. In production,
  bridge is deployed to a VPS reachable from the broker's server.
- Reconnect interval is 3 seconds (hardcoded); backoff is not implemented
  in Phase 4.
- `InpInstanceID` must match the instance_id configured in `EXECRELAY_LICENSES`
  on the ingress service.

## Phase 3 -- Bridge

Status: PASS.

Completed scope:

- Implemented `apps/bridge/internal/bridge` as the persistent EA WebSocket hub.
- `Hub` maps instanceID -> active `Conn`; evicts stale connections on re-register;
  `Unregister` is identity-safe (only removes if the stored pointer matches).
- `Handler` upgrades HTTP to WebSocket at `/ea/ws`, expects a `register` message
  as the first frame, acknowledges with `registered`, then reads fill/pong messages
  in a loop. Deregisters the connection on disconnect.
- `Subscriber` creates a durable JetStream push consumer on the `SIGNALS` stream
  (`signals.>`) and dispatches incoming `Signal` protobufs to the EA via the hub.
  Naks unroutable signals (no live EA connection) so JetStream redelivers them.
- `EnsureStream` creates the `SIGNALS` JetStream stream on startup if absent.
- Bridge<->EA JSON protocol: `register`, `registered`, `signal`, `fill`,
  `ping`, `pong`.
- Updated `apps/bridge/Dockerfile` to copy `go.sum` and `packages/proto`.
- Updated `docker-compose.yml` bridge service with NATS env vars and nats
  healthcheck dependency.
- Added `github.com/gorilla/websocket v1.5.3` to the module.

Validation:

- `make test`: PASS (hub unit tests + WS handler integration tests).
- `make bench`: PASS.
- `docker compose config >/dev/null`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `go build ./apps/bridge/...`: PASS.

Notes:

- Signals are nak'd (not dropped) when no live EA connection is found; JetStream
  redelivers them when the EA reconnects. Redelivery backoff is default NATS policy.
- Fill reports are logged only in Phase 3. Phase 5 (persist) will consume them
  via NATS subject `fills.<instanceID>.<traceID>`.
- Bridge does not authenticate EAs in Phase 3; network-level isolation is assumed.
  Token auth will be added with the portal/license API in a later phase.

## Phase 2 -- Go Ingress

Status: PASS.

Completed scope:

- Wired `apps/ingress/cmd/ingress/main.go` to use the real ingress handler
  instead of the bootstrap placeholder.
- `ConfigFromEnv` loads `HTTP_ADDR`, `NATS_URL`, `INGRESS_REGION`,
  `MAX_BODY_BYTES`, and `EXECRELAY_LICENSES` from the environment.
- NATS publisher drains gracefully on shutdown before the HTTP server closes.
- Updated `apps/ingress/Dockerfile` to copy `go.sum`, `packages/parser-go`,
  and `packages/proto` so the image builds correctly with real dependencies.
- Updated `docker-compose.yml` ingress service to supply `NATS_URL` and
  `INGRESS_REGION`, and to require the `nats` service to be healthy before
  starting.

Validation:

- `make test`: PASS.
- `make bench`: PASS.
- `docker compose config >/dev/null`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `go build ./apps/ingress/...`: PASS.
- Webhook handler benchmarks on darwin/arm64, Apple M1 Pro:
  - `BenchmarkWebhook-10`: ~5.9 µs/op, 9282 B/op, 63 allocs/op.

Notes:

- The hot path (parse → auth → publish → respond) is entirely in the handler.
  NATS publish is synchronous on the hot path; the 1.5s connect timeout and
  unlimited reconnects are configured in `NewNatsPublisher`.
- `EXECRELAY_LICENSES` is optional in dev (empty string means no licenses are
  configured and all requests return 401).

## Phase 1 -- Go Parser

Status: PASS.

Completed scope:

- Implemented `packages/parser-go` as the hot-path PineConnector-compatible
  parser package.
- Added command parsing for market, pending, close, modify, cancel, EA
  management, and close-and-open macro commands.
- Added aliases including `long`, `bull`, `bullish`, `short`, `bear`,
  `bearish`, `CL+OL`, `CL+OS`, `CS+OL`, `CS+OS`, `CLS+OL`, and `CLS+OS`.
- Added explicit and legacy parameter parsing for volume, SL, TP, pending entry,
  trailing, ATR trailing, breakeven, secret, comment, spread, and account filter
  fields.
- Enforced parser invariants:
  - one volume type only,
  - one SL type only,
  - one TP type only,
  - one pending entry type only,
  - pending orders require entry,
  - entry commands require volume,
  - risk-by-loss volume requires SL,
  - `closeall`/`closealleaoff` require chart symbol,
  - `eaon`/`eaoff` require matching special symbol,
  - comments are limited to 20 characters,
  - ATR trailing requires `atrtimeframe` and `atrperiod`,
  - SL/TP modify commands require an SL or TP field.

Validation:

- `make test`: PASS.
- `make bench`: PASS.
- `docker compose config >/dev/null`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `make docker-build`: PASS.
- Parser benchmarks on darwin/arm64, Apple M1 Pro:
  - `BenchmarkParseMarketOrder-10`: 401.6 ns/op, 0 B/op, 0 allocs/op.
  - `BenchmarkParsePendingOrder-10`: 261.1 ns/op, 0 B/op, 0 allocs/op.

Notes:

- Parser target was `<=20us/op` and `<=2 allocations/op`; current benchmarked
  paths are below 1us/op with zero allocations.
- Parsed string values intentionally reference the original alert body so the
  hot path avoids copying. Ingress should keep the raw body alive until it has
  emitted the protobuf signal.

## Phase 0 -- Foundations + Observability + Docker

Status: PASS.

Completed scope:

- Full repository layout scaffold.
- Digest-pinned Dockerfiles for Go, Python, and portal service placeholders.
- Root Docker Compose foundation stack for TimescaleDB/Postgres, NATS JetStream,
  Redis, Minio, Tempo, Prometheus, Grafana, and MLflow.
- Initial observability configuration.
- Initial database schema for core contract tables.
- GitHub Actions CI for tests, Docker builds, Trivy, Grype, and Syft SBOMs.
- Renovate configuration for digest maintenance.

Validation:

- `docker compose config >/dev/null`: PASS.
- `docker compose --profile apps config >/dev/null`: PASS.
- `make test`: PASS.
- `make bench`: PASS.
- `make docker-build`: PASS.
- Image sizes:
  - Go services: 7.46MB each, within 30MB.
  - Python services: 212MB each, within 400MB.
  - Reports service: 212MB, within 600MB.
  - Analytics service: 212MB, within 500MB.
  - Portal web placeholder: 12.9MB, within 200MB.

Notes:

- App services are Phase 0 health-checkable placeholders only. Later phases
  replace them with the required parser, ingress, bridge, EA, portal, analytics,
  and reports implementations.
- Local `trivy`, `grype`, and `syft` binaries were not installed; the CI workflow
  runs Trivy, Grype, and Syft SBOM generation during image builds.

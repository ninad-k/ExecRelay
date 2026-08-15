# ExecRelay

## Overview
Low-latency execution infrastructure that relays TradingView (and
TradingView-compatible) webhook alerts through regional ingress, NATS
JetStream, and the ExecRelay Bridge to broker-side Expert Advisors over a
persistent WebSocket. Multi-tenant (license + instance model), multi-broker
(MT4, MT5, DXTrade). Not a signal provider or broker — it is the wire between
a signal source and a broker account. See `docs/ARCHITECTURE.md` for the full
system map before making non-trivial changes.

## Stack
- **Go** (≥1.25) — `ingress`, `bridge`, `dxtrade`, `packages/parser-go`,
  `internal/`, `loadtest/`. The hot path (webhook → NATS publish) is Go.
- **Python** (≥3.12, FastAPI/asyncio) — `persist`, `portal-api`, `tasks`,
  `analytics`, `reports`, `risk`, `backtester`, `ml-feature-extractor`,
  `ml-predictor`. `ruff format` enforced.
- **TypeScript/Next.js** — `portal-web` (the only browser-facing app; talks to
  `portal-api` over REST/JWT).
- **Postgres/TimescaleDB**, **NATS JetStream**, **Prometheus/Grafana/Tempo**,
  **Kubernetes (Helm)** / **Docker Compose** / bare-metal installers
  (`scripts/install.sh`, `scripts/install.ps1`).
- MQL4/MQL5 Expert Advisors under `ea/` (Windows-only, MetaTrader terminal).

## Service map (`apps/`)
| Service | Language | Role |
|---|---|---|
| `ingress` | Go | Public webhook endpoint, auth, publish to NATS (95ms p99 budget) |
| `bridge` | Go | Holds the persistent WS to MT4/MT5 EAs, dispatches orders |
| `dxtrade` | Go | REST adapter for DXTrade accounts |
| `persist` | Python | Consumes NATS, writes fills/signals to Postgres |
| `portal-api` | Python (FastAPI) | JWT-authed REST API behind `portal-web` — no HTML templates, JSON only |
| `portal-web` | TypeScript (Next.js) | Trader/admin web UI |
| `risk` | Python | Position sizing / kill-switch logic |
| `tasks` | Python | Scheduled/background jobs |
| `analytics`, `reports` | Python | Aggregation + reporting over persisted data |
| `backtester` | Python | `/backtest` DB replay endpoint *and* the standalone `backtest_ml.py` CLI harness (see below) |
| `ml-feature-extractor`, `ml-predictor` | Python | Pine-formula feature computation + XGBoost "Option 1" filter |

## How to run
- **Windows demo stack** (native, against a running MT5 terminal):
  `.\run.ps1` to start everything (tails logs; Ctrl+C stops), `.\stop.ps1`
  from another window. See `docs/development/windows-local-stack.md`.
- **Docker Compose** (any host): `cp .env.example .env` then
  `docker compose --profile apps up -d --build`.
- **Kubernetes**: `infra/helm/`.

## Conventions
- **Config is env vars, not files.** Services read `.env` / process env
  (`.env.example` is the source of truth for every variable). `config.example.json`
  at the repo root is the exception — it's local machine settings for the
  Windows-only dev tools (`scripts/trade_dashboard.py`, `scripts/ea_shim.py`,
  `scripts/generate-dev-certs.py`), not service config. Copy it to
  `config.json` (gitignored) and edit locally; never commit real MT5
  credentials or terminal paths.
- **`ENV=production`** flips services into prod-safe behavior (refuses
  default DB/NATS/JWT secrets, refuses wildcard CORS). See `.env.example`.
- **The ML feature contract is load-bearing.** `apps/ml-predictor/model/feature_order.txt`
  is the exact, order-sensitive column list the XGBoost model expects
  (validated against `EXPECTED_FEATURE_COUNT` and `SHA256SUMS` at load time).
  `apps/backtester/feature_builder.py` and `apps/backtester/xgb_predictor.py`
  consume the *same* file — don't fork a second copy; if the feature set
  changes, retrain and update `feature_order.txt` + `xgb_production.json`
  together (see `apps/ml-predictor/model/TRAINING.md`).
- **Branch naming**: `feat/`, `fix/`, `chore/`, `docs/`, `test/`, `perf/`,
  `security/` + kebab-case (see `CONTRIBUTING.md`).
- **Commits**: `<scope>: <imperative summary>` where scope is the directory
  (`ingress`, `portal-api`, `scripts`, `docs`, ...).
- **Dual scripts**: cross-platform helper scripts under `scripts/` ship as
  both `.sh` and `.ps1` (e.g. `install.sh`/`install.ps1`,
  `configure-prod.sh`/`configure-prod.ps1`) — keep both in sync when adding one.

## Testing
- Go: `go test ./...` (or `go test -race ./apps/<service>/...` while iterating).
  Benchmark anything touching the ingress hot path: `go test -bench=. -benchmem ./apps/ingress/...`.
- Python: `pytest apps/<service>/tests/`. New Python code needs at least a
  smoke-test extension or a real pytest case.
- TypeScript: `npm run type-check` in `apps/portal-web`.
- Full sweep before pushing: `pre-commit run --all-files` (or `make lint`),
  plus `make check` for compose-config/test/bench/docker-build.
- `apps/backtester/backtest_ml.py` is a standalone CLI (candles.csv +
  signals.csv → filtered-vs-unfiltered PnL comparison); it is not wired into
  the live `/backtest` DB endpoint (see the module docstring for why).

## Important notes
- **Never commit secrets.** `.env`, `config.json`, `*.pem`/`*.key` dev certs,
  and MT5 account credentials are all gitignored — use the `.example`
  counterparts as templates. `gitleaks` runs in pre-commit and CI.
- **MT5 components are Windows-only** (`scripts/trade_dashboard.py`,
  `scripts/ea_shim.py`, `ea/mt4`, `ea/mt5`, `packaging/dashboard/`) — the
  MetaTrader5 Python package and MQL EAs only run on Windows.
- **Ingress hot path**: no DB writes, no external HTTP calls, no `defer` for
  anything that should run immediately. See `CONTRIBUTING.md` § Performance.
- **Large/generated files**: `apps/ml-predictor/model/*` (trained artifact +
  feature order + checksums) are intentionally tracked despite the
  large-file/end-of-file-fixer pre-commit exclusions — don't "clean up"
  trailing whitespace or normalize line endings there.

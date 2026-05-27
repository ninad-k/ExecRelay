# IDE setup (GoLand / IntelliJ IDEA / VS Code)

This is the **single consolidated guide** for running ExecRelay from
your IDE without Docker. It replaces the four older root-level
`IDE_*.md` / `GOLAND_IDE_SETUP.md` files.

If you just want to run everything in containers, you don't need this —
`docker compose --profile apps up -d --build` is the supported path.
This guide is for developers who want to attach a debugger or iterate
on a single service.

---

## Prerequisites

- **GoLand or IntelliJ IDEA Ultimate** (the Go plugin, free, is fine
  too) — the `.run/*.xml` configurations are JetBrains-format. VS Code
  users can ignore the configurations and use the manual `go run` /
  `python -m` commands at the bottom of this doc.
- **Go 1.25+** on PATH
- **Python 3.12+** on PATH
- **Node 22+** for `apps/portal-web`
- **Postgres + NATS running locally** (easiest path: just run the
  foundation tier from compose):
  ```sh
  docker compose up -d postgres nats redis
  docker compose run --rm migrate
  ```

---

## 1. Open the project

`File → Open` → select the repo root. GoLand auto-discovers the
`go.mod` and indexes everything. First-time indexing takes a couple
minutes.

The repo ships **run configurations** under `.run/` that the IDE picks
up automatically. You should see them in the run dropdown
(top-right of the IDE window).

---

## 2. Run configurations bundled

| Configuration | What it does |
|---|---|
| `00. All Services (Compound)` | Launches all 13 service configs in one click |
| `01. Ingress` | Runs `go run ./apps/ingress/cmd/ingress` with the right env |
| `02. Bridge` | `go run ./apps/bridge/cmd/bridge` |
| `03. DXTrade` | `go run ./apps/dxtrade/cmd/dxtrade` |
| `04. Persist` | `python apps/persist/app.py` |
| `05. Portal API` | `python apps/portal-api/app.py` |
| `06. Tasks` | `python apps/tasks/app.py` |
| `07. Analytics` | `python apps/analytics/app.py` |
| `08. Reports` | `python apps/reports/app.py` |
| `09. Risk` | `python apps/risk/app.py` |
| `10. ML Predictor` | `python apps/ml-predictor/app.py` |
| `11. ML Feature Extractor` | `python apps/ml-feature-extractor/app.py` |
| `12. Backtester` | `python apps/backtester/app.py` |

Each Go config has the race detector on (`-race`) and points at
`http://localhost:8081/...` style URLs that match the compose-defaults.

---

## 3. First run

1. Make sure the foundation tier is up:
   `docker compose ps` — postgres + nats + redis should be `healthy`.
2. Pick `00. All Services` from the run dropdown, click Run.
3. Watch the run console — each service prints its bound port on
   startup. Sanity check:
   ```sh
   curl -sf http://localhost:8081/health   # ingress
   curl -sf http://localhost:8085/health   # portal-api
   ```

---

## 4. Debugging a single service

1. Set breakpoints (Go: click in the gutter; Python: same).
2. Run that service's config with **Debug** instead of Run
   (`Shift+F9` by default in GoLand).
3. Trigger the code path — for ingress, fire a curl webhook; for
   portal-api, hit an endpoint with `curl` or the IDE's HTTP client.

For **`ingress` specifically**, the smoke-test from the customer guide
makes a good single-shot debug input:

```sh
curl -X POST http://localhost:8081/webhook \
  -H 'Content-Type: text/plain' \
  -d '60123456789,BUY,EURUSD,vol_lots=0.1,secret=alert-secret'
```

---

## 5. Manual commands (no IDE / VS Code)

If you're not using JetBrains, these are the same commands the run
configurations execute. Run each in its own terminal:

```sh
# Make sure foundation tier is up
docker compose up -d postgres nats redis
docker compose run --rm migrate

# Source your local env (use .env.example as a starter)
set -a; . .env; set +a

# Go services
go run ./apps/ingress/cmd/ingress
go run ./apps/bridge/cmd/bridge
go run ./apps/dxtrade/cmd/dxtrade

# Python services
python apps/persist/app.py
python apps/portal-api/app.py
python apps/tasks/app.py
python apps/analytics/app.py
python apps/reports/app.py
python apps/risk/app.py
python apps/ml-feature-extractor/app.py
python apps/ml-predictor/app.py
python apps/backtester/app.py

# Portal web (Next.js)
cd apps/portal-web && npm install && npm run dev
```

Python services need their requirements installed:
```sh
cd apps/portal-api && pip install -r requirements.txt
# (repeat for each python service)
```

---

## 6. Troubleshooting

### Run configurations don't show in the dropdown

The `.run/` files are present in the repo (check `ls -la .run/`).
If the IDE doesn't pick them up:

1. Close GoLand / IntelliJ completely.
2. Delete IDE caches: `File → Invalidate Caches… → Invalidate and Restart`.
3. After restart, the configurations should appear. If still missing,
   check `.run/*.xml` aren't malformed (`git status` to confirm
   they're not unstaged corrupted edits).

### `address already in use`

Another service is on that port — either compose is running an app
container, or you have a previous IDE run still alive.

```sh
# Stop the compose app tier if it's up:
docker compose --profile apps down
# Or find what's holding the port:
lsof -iTCP:8081 -sTCP:LISTEN
```

### "Cannot connect to NATS"

Foundation tier not running, or NATS is on a different password than
your `.env` says.

```sh
docker compose ps nats
docker compose logs nats --tail=20
```

### Python service can't find dependencies

You haven't `pip install`-ed that service's requirements. Each Python
service has its own `requirements.txt` in `apps/<service>/`.

### Hot-reload doesn't work for Python services

The bundled configurations don't enable autoreload by default. For
iterative dev, add `--reload` to the uvicorn invocation in your local
edits (don't commit). Or use `make` or `air` for Go services.

---

## See also

- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — branch / commit / PR
  conventions
- [`STANDALONE_DEPLOYMENT.md`](../../STANDALONE_DEPLOYMENT.md) — full
  installer flow (for ops, not dev)
- [`docs/operations/debug-logging.md`](../operations/debug-logging.md) —
  how to use `DEBUG=true` and trace IDs to diagnose flow

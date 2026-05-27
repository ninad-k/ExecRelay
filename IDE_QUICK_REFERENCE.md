# IDE Quick Reference Card

## 1. Start Everything (Fastest)

**Keyboard:** `Ctrl+Shift+F10` (then select "00. All Services" → Enter)

Or: **GUI** → Run dropdown (top-right) → select "00. All Services (Compound)" → click green Run button

## 2. Service List & Ports

| # | Service | Port | Language | Purpose |
|---|---------|------|----------|---------|
| 00 | All Services | - | Compound | Launch all 12 in one click |
| 01 | Ingress | 8080 | Go | Webhook entry point |
| 02 | Bridge | 8081 | Go | NATS → EA dispatcher |
| 03 | DXTrade | 8082 | Go | DXTrade adapter |
| 04 | Persist | 8083 | Python | Signal storage |
| 05 | Portal API | 8084 | Python | Backend API |
| 06 | Risk | 8085 | Python | Risk tracking |
| 07 | Tasks | 8086 | Python | Async jobs |
| 08 | Analytics | 8087 | Python | Data analysis |
| 09 | Reports | 8088 | Python | Report generation |
| 10 | Backtester | 8089 | Python | Backtesting |
| 11 | ML Feature Extr. | 8090 | Python | Feature extraction |
| 12 | ML Predictor | 8091 | Python | ML inference |

## 3. Quick Tests

All services running? Try:
```bash
curl http://localhost:8080/health  # Should return {"service":"ingress","status":"ok"}
curl http://localhost:8084/health  # Should return {"service":"portal-api","status":"ok"}
```

Send a test signal:
```bash
curl -X POST -H "Content-Type: text/plain" \
  -H "X-ExecRelay-Timestamp: $(date +%s)" \
  -d "550e8400-e29b-41d4-a716-446655440000:buy:test:symbol=EURUSD" \
  http://localhost:8080/webhook
```

## 4. View Logs

- **Services tab:** List of running processes
- **Console tabs:** One per service, shows live debug output
- **Search:** `Ctrl+F` in console → filter by keyword (e.g., "error", "trace_id")

## 5. Prerequisites (One-time Setup)

```bash
# PostgreSQL
docker run -d -e POSTGRES_PASSWORD=execrelay_dev_password -e POSTGRES_DB=execrelay -p 5432:5432 postgres:14

# NATS with JetStream
docker run -d -p 4222:4222 -p 8222:8222 nats:latest -js

# Initialize DB (run once)
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/001_schema.sql
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/002_advanced_features.sql
```

## 6. Stop Services

- **GUI:** Click red Stop button in Run window
- **Keyboard:** `Ctrl+F2`

All services stop gracefully.

## 7. Debug Logging

All services start with `DEBUG=true` (verbose logging enabled). To disable:

1. **Run** → **Edit Configurations...**
2. Select service
3. Find "Environment variables" field
4. Change `DEBUG=true` to `DEBUG=false`
5. Click **Apply** → **OK**
6. Re-run service

## 8. Issues?

| Problem | Solution |
|---------|----------|
| "Address already in use" | `lsof -i :<port> \| grep LISTEN \| awk '{print $2}' \| xargs kill -9` |
| Database error | Check: `docker ps \| grep postgres` (must be running) |
| NATS error | Check: `docker ps \| grep nats` (must be running) |
| Python import error | `pip install asyncpg fastapi uvicorn prometheus-client nats-py` |
| Go won't compile | Make sure IDE GOROOT is set: **Preferences** → **Go** → check GOROOT |

## 9. Edit a Service & Test

1. Modify code (e.g., `apps/ingress/internal/ingress/handler.go`)
2. Save file (`Ctrl+S`)
3. IDE auto-compiles (for Go) or auto-reloads (for Python)
4. Click **Run** button or restart service

## 10. File Locations

- **Configurations:** `.run/*.xml` (auto-loaded by IDE)
- **Full guide:** `GOLAND_IDE_SETUP.md`
- **Logging reference:** `DEBUG_LOGGING.md`
- **Standalone deployment:** `STANDALONE_DEPLOYMENT.md`

---

**Tip:** Bookmark `http://localhost:8084` (Portal API) to monitor system in real-time!

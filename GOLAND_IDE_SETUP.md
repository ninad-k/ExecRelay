# GoLand/IntelliJ IDE Setup Guide

This guide explains how to run ExecRelay services directly from GoLand/IntelliJ IDE without Docker or Kubernetes.

## Quick Start (5 minutes)

### 1. Prerequisites

Ensure you have running:
- **PostgreSQL 14+** (local docker: `docker run -d -e POSTGRES_PASSWORD=execrelay_dev_password -e POSTGRES_DB=execrelay -p 5432:5432 postgres:14`)
- **NATS with JetStream** (local docker: `docker run -d -p 4222:4222 -p 8222:8222 nats:latest -js`)
- **Go 1.21+** (for running Go services)
- **Python 3.10+** (for running Python services)

Initialize the database:
```bash
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/001_schema.sql
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/002_advanced_features.sql
```

### 2. Open Run Configurations

In GoLand/IntelliJ:
1. Click **Run** menu → **Edit Configurations...**
2. You should see 13 configurations listed (numbered 00–12)
3. Configuration **"00. All Services (Compound)"** launches everything at once

### 3. Launch All Services

**Option A: Via UI**
1. Select **"00. All Services (Compound)"** from the run dropdown (top right)
2. Click the green **Run** button (or press `Ctrl+Shift+F10` on Linux/Windows, `Ctrl+Shift+R` on Mac)

**Option B: Via keyboard**
1. Press `Alt+Shift+F9` (or Cmd+Shift+F9 on Mac)
2. Select **"00. All Services (Compound)"** → press Enter

All 12 services will launch in separate tabs in the Run window.

### 4. View Logs

- **Services tab:** Shows all running services
- **Console tabs:** Each service has a tab showing live output with `DEBUG=true` logs
- **Filter:** Use the filter icon (binoculars) to search logs by keyword
- **Scroll:** Logs auto-scroll; click the "pin" icon to pause and scroll manually

### 5. Stop All Services

**Option A:** Click the red Stop button in Run window

**Option B:** Press `Ctrl+F2` (Run → Stop)

All services will terminate gracefully.

---

## Service Details

### Go Services

**Port Assignments:**
- **Ingress** (01): `:8080` — webhook endpoint, request validation, signal publishing
- **Bridge** (02): `:8081` — NATS consumer, EA connections, signal dispatch
- **DXTrade** (03): `:8082` — DXTrade adapter, order execution

**Environment Variables (pre-configured):**
- `DEBUG=true` — enables debug logging
- `NATS_URL=nats://localhost:4222` — NATS connection
- `DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay` — PostgreSQL
- Service-specific timeouts and limits

**How to run individually:**
1. Select service from dropdown (e.g., **"01. Ingress"**)
2. Click Run or press `Ctrl+Shift+F10`
3. View logs in Console tab

### Python Services

**Port Assignments:**
```
04. Persist           → :8083  (signal storage)
05. Portal API        → :8084  (portal backend API)
06. Risk              → :8085  (risk tracking)
07. Tasks             → :8086  (async tasks)
08. Analytics         → :8087  (data analytics)
09. Reports           → :8088  (report generation)
10. Backtester        → :8089  (backtesting engine)
11. ML Feature Extr.  → :8090  (feature extraction)
12. ML Predictor      → :8091  (ML inference)
```

**How to run individually:**
Same as Go services — select from dropdown and click Run.

---

## Testing the Setup

Once all services are running, test them:

### Health Checks
```bash
# Go services
curl http://localhost:8080/health  # Ingress
curl http://localhost:8081/health  # Bridge
curl http://localhost:8082/health  # DXTrade

# Python services
curl http://localhost:8084/health  # Portal API
curl http://localhost:8088/health  # Reports
```

### Send Test Signal
```bash
curl -X POST \
  -H "Content-Type: text/plain" \
  -H "X-ExecRelay-Timestamp: $(date +%s)" \
  -d "550e8400-e29b-41d4-a716-446655440000:buy:test-instance:symbol=EURUSD" \
  http://localhost:8080/webhook
```

### Check Logs for Debug Output
In any service's Console tab, you should see:
```
"msg":"webhook request received","client":"127.0.0.1","method":"POST"
"msg":"signal parsed","license":"550e8400-...","symbol":"EURUSD"
"msg":"signal published successfully","license":"550e8400-..."
```

---

## Debugging Tips

### View Debug Logs for Specific Component

**In Ingress console:**
```
Ctrl+F (or Cmd+F on Mac) → search for:
- "signature" — signature validation logs
- "exposure" — exposure limit checks
- "ML scoring" — machine learning scoring
- "trace_id" — end-to-end signal flow
```

### Monitor NATS Consumer Lag
```bash
curl http://localhost:8222/jsz | jq '.consumer_info[] | select(.name=="SIGNALS")'
```

### Check Database Connection
```bash
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay -c "SELECT COUNT(*) FROM accepted_signals;"
```

### Find Errors in Logs
Click the binoculars icon in Run window → filter for: `ERROR`, `WARN`, `exception`

---

## Environment Variables

All services use these pre-configured environment variables:

```bash
# Global
DEBUG=true                   # Enable debug logging (default: true)

# Database
DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay

# NATS (messaging)
NATS_URL=nats://localhost:4222

# Service Ports (Go)
HTTP_ADDR=:<port>          # :8080 (Ingress), :8081 (Bridge), :8082 (DXTrade)

# Service Ports (Python)
HTTP_ADDR=0.0.0.0:<port>   # Or HTTP_PORT=<port>
HTTP_PORT=<port>           # 8083 (Persist), 8084 (Portal), etc.

# Go Service Timeouts
WEBHOOK_RATE_LIMIT=1000            # Ingress
WEBHOOK_TIMESTAMP_WINDOW_SECS=30   # Ingress
EA_CONNECTION_TIMEOUT_SECS=10      # Bridge
DXTRADE_REQUEST_TIMEOUT_MS=5000    # DXTrade
CIRCUIT_BREAKER_FAILURE_THRESHOLD=5
CIRCUIT_BREAKER_SUCCESS_THRESHOLD=2

# Python Service Configuration
TASK_POLL_INTERVAL=10      # Tasks service
FILL_TIMEOUT_SECS=30       # Tasks service
RETENTION_DAYS=90          # Tasks service
```

### Modify Environment Variables

To change environment variables for a service:

1. **In IDE:**
   - Go to **Run** → **Edit Configurations...**
   - Select a service (e.g., **"01. Ingress"**)
   - Click **Environment variables** field
   - Add/modify variables (format: `KEY=value`)
   - Click **Apply** → **OK**

2. **Or edit `.run/*.xml` files directly:**
   - Open `.run/01_Ingress.xml`
   - Find `<env name="DEBUG" value="true" />`
   - Modify as needed
   - Save; IDE auto-reloads configuration

---

## Common Issues & Solutions

### "Address already in use" Error
A service port is already bound. Either:
- Stop the existing service: `lsof -i :8080 | grep LISTEN | awk '{print $2}' | xargs kill -9`
- Or change the port in configuration and restart

### "Connection refused" (Database)
PostgreSQL isn't running:
```bash
docker run -d \
  -e POSTGRES_PASSWORD=execrelay_dev_password \
  -e POSTGRES_DB=execrelay \
  -p 5432:5432 \
  postgres:14
```

### "Connection refused" (NATS)
NATS isn't running:
```bash
docker run -d \
  -p 4222:4222 \
  -p 8222:8222 \
  nats:latest -js
```

### Services won't start (ModuleNotFoundError in Python)
Install dependencies:
```bash
cd apps/persist && pip install -r requirements.txt
cd ../.. && pip install asyncpg fastapi uvicorn prometheus-client nats-py
```

### Go service won't compile
Make sure you're in the project root:
```bash
cd /Users/ninadk/GolandProjects/ExecRelay
```

IDE should auto-detect GOPATH. If not, go to **Preferences** → **Go** → **GOROOT** and set it to your Go installation.

---

## Advanced: Custom Run Configurations

To add a custom configuration:

1. **Run** → **Edit Configurations...**
2. Click **+** → **Go Application** (or **Python**)
3. Set:
   - **Name:** e.g., "Custom Ingress (Debug)"
   - **File:** `apps/ingress/cmd/ingress/main.go`
   - **Working directory:** `$PROJECT_DIR$`
   - **Environment variables:** `DEBUG=true;NATS_URL=nats://localhost:4222`
4. Click **Apply** → **OK**

---

## Performance Monitoring

While services run:

### CPU & Memory
Click **Services** tab → right-click service → **Show statistics** (if available in your IDE version)

### Request Latency
In Ingress console, look for:
```
"latency_ms": 42.5
```

### Database Query Performance
In Persist/Portal API console, look for:
```
"query_duration_ms": 15.3
```

### NATS Throughput
Check bridge/analytics logs for message processing rate

---

## Next Steps

1. **Test webhook flow:** Send a signal via curl, trace it through all services in logs
2. **Modify and debug:** Edit handler code, rebuild (GoLand auto-compiles on save), re-run
3. **Add breakpoints:** In Go/Python editor, click line number to add breakpoint, debug mode available
4. **Production deployment:** When ready, use `STANDALONE_DEPLOYMENT.md` for command-line steps or `infra/helm/` for Kubernetes

---

## File Structure

```
.run/
├── 00_All_Services.xml          # Compound config to launch all
├── 01_Ingress.xml               # Go service: webhook endpoint
├── 02_Bridge.xml                # Go service: EA dispatcher
├── 03_DXTrade.xml               # Go service: DXTrade adapter
├── 04_Persist.xml               # Python: signal storage
├── 05_Portal_API.xml            # Python: API backend
├── 06_Risk.xml                  # Python: risk tracking
├── 07_Tasks.xml                 # Python: async tasks
├── 08_Analytics.xml             # Python: analytics
├── 09_Reports.xml               # Python: reporting
├── 10_Backtester.xml            # Python: backtesting
├── 11_ML_Feature_Extractor.xml  # Python: feature extraction
└── 12_ML_Predictor.xml          # Python: ML inference
```

All configurations are pre-created. Simply open **Run** → **Edit Configurations** to see them.

---

## Support

- Check logs in **Run** window for errors
- Verify database/NATS running: `docker ps | grep postgres\|nats`
- Review `DEBUG_LOGGING.md` for comprehensive logging documentation
- Check `STANDALONE_DEPLOYMENT.md` for environment setup details

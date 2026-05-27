# ExecRelay Standalone Deployment Guide

This guide shows how to deploy ExecRelay without Docker or Kubernetes, perfect for development, testing, or single-node production deployments.

## Prerequisites

- Go 1.21+
- Python 3.10+
- PostgreSQL 14+
- NATS server
- curl, bash, jq (optional but useful)

## Quick Start (5 minutes)

### 1. Setup Environment

```bash
cp .env.example .env
# Edit .env if needed - defaults work for local development
```

### 2. Start Infrastructure Services

```bash
# Terminal 1: PostgreSQL
docker run -d \
  -e POSTGRES_PASSWORD=execrelay_dev_password \
  -e POSTGRES_DB=execrelay \
  -p 5432:5432 \
  --name execrelay-postgres \
  postgres:14

# Terminal 2: NATS with JetStream
docker run -d \
  -p 4222:4222 \
  -p 8222:8222 \
  --name execrelay-nats \
  nats:latest -js

# Wait for services to be ready
sleep 5
```

### 3. Initialize Database

```bash
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/001_schema.sql
psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/002_advanced_features.sql
```

### 4. Build Go Services

```bash
cd apps/ingress
go build -o ../../ingress cmd/ingress/main.go
cd ../..

cd apps/bridge
go build -o ../../bridge cmd/bridge/main.go
cd ../..

cd apps/dxtrade
go build -o ../../dxtrade cmd/dxtrade/main.go
cd ../..
```

### 5. Start Go Services (in separate terminals)

```bash
# Terminal 3: Ingress
DEBUG=true HTTP_ADDR=:8080 NATS_URL=nats://localhost:4222 ./ingress

# Terminal 4: Bridge
DEBUG=true NATS_URL=nats://localhost:4222 HTTP_ADDR=:8081 ./bridge

# Terminal 5: DXTrade
DEBUG=true HTTP_ADDR=:8082 ./dxtrade
```

### 6. Start Python Services (in separate terminals)

```bash
# Terminal 6: Persist
DEBUG=true HTTP_PORT=8083 DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay NATS_URL=nats://localhost:4222 python3 apps/persist/app.py

# Terminal 7: Portal API
DEBUG=true HTTP_ADDR=0.0.0.0:8084 DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay python3 apps/portal-api/app.py

# Terminal 8: Risk Service
DEBUG=true HTTP_PORT=8085 DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay NATS_URL=nats://localhost:4222 python3 apps/risk/app.py

# Additional services as needed
python3 apps/ml-feature-extractor/app.py
python3 apps/ml-predictor/app.py
python3 apps/backtester/app.py
python3 apps/tasks/app.py
python3 apps/analytics/app.py
python3 apps/reports/app.py
```

## Testing the Deployment

### Health Check

```bash
# Ingress service
curl -s http://localhost:8080/health | jq .

# Portal API
curl -s http://localhost:8084/health | jq .
```

### Send Test Signal

```bash
curl -X POST \
  -H "Content-Type: text/plain" \
  -H "X-ExecRelay-Timestamp: $(date +%s)" \
  -d "550e8400-e29b-41d4-a716-446655440000:buy:test-instance:symbol=EURUSD" \
  http://localhost:8080/webhook

# Should return:
# {"status":"accepted","trace_id":"...", "ml_confidence":"..."}
```

### View Metrics

```bash
# Ingress metrics
curl -s http://localhost:8080/metrics | grep "ingress_"

# Portal API metrics
curl -s http://localhost:8084/metrics | grep "portal_"
```

### Check Logs for Debug Messages

Each service logs to stdout with debug messages when `DEBUG=true`:

```bash
# In ingress terminal, you should see:
# ... "msg":"webhook request received","client":"127.0.0.1","method":"POST"
# ... "msg":"signal parsed","client":"127.0.0.1","license":"550e8400-...","symbol":"EURUSD"
# ... "msg":"ML scoring completed","symbol":"EURUSD","confidence":0.75
# ... "msg":"signal published successfully","license":"550e8400-...","symbol":"EURUSD"
```

## Debug Logging

### Enable/Disable Debug Logging

```bash
# Enable debug (default)
DEBUG=true ./ingress

# Disable debug
DEBUG=false ./ingress

# Also accepts: DEBUG=1, DEBUG=yes, DEBUG=on (enable)
#              DEBUG=0, DEBUG=no, DEBUG=off (disable)
```

### Debug Output Includes

- **Ingress Service**: All webhook processing, license validation, signature verification, ML scoring, exposure checks
- **Bridge Service**: Signal subscription, EA connections, order dispatch, error handling
- **DXTrade Service**: Broker connections, command execution, circuit breaker state
- **Persist Service**: Signal storage, fill processing, database operations
- **Python Services**: Request handling, database queries, NATS messages, error cases

### Common Debug Patterns

```bash
# Filter logs by service and trace
./ingress 2>&1 | grep "trace_id"

# Watch for errors
./ingress 2>&1 | grep "ERR\|WARN\|error\|failed"

# Monitor signal flow
./ingress 2>&1 | grep "signal parsed\|signal published"

# Check exposure limit enforcement
./ingress 2>&1 | grep "exposure"

# Monitor ML scoring
./ingress 2>&1 | grep "ML scoring\|confidence"
```

## Configuration for Different Environments

### Local Development (Current)
```bash
DEBUG=true
HTTP_ADDR=:8080
NATS_URL=nats://localhost:4222
DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay
```

### Staging (Docker Network)
```bash
DEBUG=false  # Reduce log volume
HTTP_ADDR=0.0.0.0:8080
NATS_URL=nats://nats:4222
DATABASE_URL=postgresql://execrelay:password@postgres:5432/execrelay
```

### Production (AWS)
```bash
DEBUG=false
HTTP_ADDR=0.0.0.0:8080
NATS_URL=nats://aws-mq-broker.amazonaws.com:4222
DATABASE_URL=postgresql://user:password@db.rds.amazonaws.com:5432/execrelay
WEBHOOK_TIMESTAMP_WINDOW_SECS=30
WEBHOOK_RATE_LIMIT=5000
WEBHOOK_ALLOWED_CIDRS=1.2.3.4/32,5.6.7.8/32
```

## Stopping Services

```bash
# Kill all running services
pkill -f "ingress\|bridge\|dxtrade\|python3 apps"

# Stop Docker containers
docker stop execrelay-postgres execrelay-nats
docker rm execrelay-postgres execrelay-nats
```

## Troubleshooting

### Service won't start
- Check logs: `tail -f service.log`
- Verify ports are available: `lsof -i :8080`
- Check database connection: `psql $DATABASE_URL -c "SELECT 1"`

### NATS connection errors
- Verify NATS is running: `ps aux | grep nats`
- Check NATS URL in .env matches running instance
- Test NATS connection: `telnet localhost 4222`

### Database connection errors
- Verify PostgreSQL is running: `psql $DATABASE_URL -c "SELECT 1"`
- Check database exists: `psql -l`
- Initialize schema if needed: `psql $DATABASE_URL < infra/postgres/init/001_schema.sql`

### High log volume
- Set `DEBUG=false` to switch to INFO level only
- Redirect logs to file: `./ingress > logs/ingress.log 2>&1 &`

### Memory usage growing
- Check for NATS consumer lag: `curl http://localhost:8222/jsz`
- Review task cleanup in `apps/tasks/app.py`
- Check database connection pooling settings

## Performance Monitoring

### Key Metrics to Watch

```bash
# Ingress request rate
curl -s http://localhost:8080/metrics | grep "ingress_signals_accepted_total"

# Bridge dispatch latency
curl -s http://localhost:8081/metrics | grep "bridge_.*latency"

# Database lag
curl -s http://localhost:8083/metrics | grep "persist_lag"

# ML prediction time
curl -s http://localhost:8080/metrics | grep "ml_prediction_duration"
```

### Load Testing

```bash
# Simple load test: 10 requests/sec for 60 seconds
for i in {1..600}; do
  curl -s -X POST \
    -H "Content-Type: text/plain" \
    -d "550e8400-e29b-41d4-a716-446655440000:buy:test:symbol=EURUSD" \
    http://localhost:8080/webhook > /dev/null &
  [ $((i % 10)) -eq 0 ] && sleep 1
done
```

## Next Steps

1. **Integration Testing**: Run `bash infra/test/integration_test.sh`
2. **Chaos Testing**: Run `bash infra/test/chaos_test.sh`
3. **Production Deployment**: Use Docker Compose or Kubernetes (see other guides)
4. **Monitoring**: Setup Prometheus and Grafana (see Phase 2 guide)
5. **Load Testing**: Run `make loadtest-suite` for baseline performance data

## Support

For issues or questions:
- Check debug logs with `DEBUG=true`
- Review logs in service terminal windows
- Examine database directly: `psql $DATABASE_URL`
- Check NATS status: `curl http://localhost:8222/jsz`

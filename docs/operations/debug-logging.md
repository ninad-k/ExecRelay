# Debug Logging Guide

ExecRelay includes comprehensive debug logging throughout all services. Debug logging is **enabled by default in production** for maximum visibility, with an environment variable switch to disable when needed.

## Quick Reference

### Enable/Disable Debug Logging

```bash
# Enable debug logging (default)
DEBUG=true ./ingress
DEBUG=1 python3 apps/persist/app.py

# Disable debug logging
DEBUG=false ./ingress
DEBUG=0 python3 apps/persist/app.py

# Also accepted: yes/no, on/off, true/false, 1/0
```

### Valid Values

| Value | Effect | Use Case |
|-------|--------|----------|
| `true`, `1`, `yes`, `on` | Enable debug logging | Default, development, staging |
| `false`, `0`, `no`, `off` | Disable debug logging | Reduce noise, production (if needed) |
| Not set | Default to `true` | Production - verbose by default |

## Service Debug Logging Details

### Ingress Service (`apps/ingress`)

**Debug logs include:**
- Incoming request details (IP, method, size)
- License lookup and validation
- Signature/HMAC verification results
- Timestamp validation checks
- Rate limiting decisions
- Daily signal count tracking
- Exposure limit checks with current/limit values
- ML confidence scores
- Protobuf encoding details
- NATS publish status
- Trace ID assignment

**Example debug output:**
```
"msg":"webhook request received","client":"127.0.0.1","method":"POST"
"msg":"body received","client":"127.0.0.1","size":58
"msg":"signal parsed","client":"127.0.0.1","license":"550e8400-...","symbol":"EURUSD","command":"buy"
"msg":"license found","client":"127.0.0.1","license":"550e8400-...","instance":"test-mt5"
"msg":"signature validated","client":"127.0.0.1","license":"550e8400-...","primary":true
"msg":"daily signal count","license":"550e8400-...","count":42,"limit":1000
"msg":"exposure check","license":"550e8400-...","account":"test-mt5","current":150000.00,"limit":200000.00
"msg":"exposure within limits","license":"550e8400-...","account":"test-mt5","current":150000.00,"limit":200000.00
"msg":"ML scoring completed","symbol":"EURUSD","hour":14,"day_of_week":2
"msg":"ML prediction received","symbol":"EURUSD","confidence":0.85
"msg":"trace ID assigned","trace_id":"a1b2c3d4...","license":"550e8400-..."
"msg":"signal encoded","trace_id":"a1b2c3d4...","payload_size":156
"msg":"publishing signal","trace_id":"a1b2c3d4...","subject":"signals.mt5.550e8400-....test-mt5"
"msg":"signal published successfully","trace_id":"a1b2c3d4...","license":"550e8400-...","symbol":"EURUSD"
```

### Bridge Service (`apps/bridge`)

**Debug logs include:**
- NATS consumer subscription status
- Signal reception from queue
- EA connection attempts and status
- Order dispatch details and latency
- Circuit breaker state changes
- Fill reception and processing
- Reconnection attempts and backoff
- Consumer lag monitoring

### DXTrade Service (`apps/dxtrade`)

**Debug logs include:**
- Broker connection attempts
- Command execution with parameters
- Execution status and latency
- Circuit breaker trips and recovery
- Error details and retry logic
- Connection pool status

### Persist Service (`apps/persist`)

**Debug logs include:**
- Signal storage operations
- Fill record processing
- Database write latency
- Connection errors and recovery
- Event publishing to NATS
- Data cleanup operations

### Python Services (Portal API, Risk, Analytics, etc.)

**Debug logs include:**
- HTTP request details
- Database query execution
- API endpoint processing
- NATS message consumption
- Error handling and recovery
- Service initialization and configuration

**Example:**
```python
logger.debug("processing POST /api/backtest", extra={
    "license_id": license_id,
    "date_range": f"{date_start} to {date_end}"
})
logger.debug("backtest query completed", extra={
    "job_id": job_id,
    "duration_ms": elapsed_ms
})
```

## Analyzing Debug Logs

### Filter by Service

```bash
# Show only ingress debug logs
./ingress 2>&1 | jq 'select(.logger=="ingress")'

# Show only persist debug logs
python3 apps/persist/app.py 2>&1 | grep "persist"
```

### Filter by Component

```bash
# Show signature validation logs
./ingress 2>&1 | grep "signature"

# Show exposure limit checks
./ingress 2>&1 | grep "exposure"

# Show ML scoring
./ingress 2>&1 | grep "ML\|confidence"

# Show NATS operations
./ingress 2>&1 | grep "publish\|subscribe"
```

### Filter by Severity

```bash
# Show errors and warnings only
./ingress 2>&1 | grep '"level":"ERROR\|"level":"WARN"'

# Show info and above (production default)
DEBUG=false ./ingress 2>&1 | grep '"level":"INFO\|WARN\|ERROR"'

# Show all debug messages
DEBUG=true ./ingress 2>&1 | grep '"level":"DEBUG"'
```

### Trace a Specific Signal

```bash
# Generate a test signal and capture trace_id
RESPONSE=$(curl -s -X POST \
  -H "Content-Type: text/plain" \
  -d "550e8400-e29b-41d4-a716-446655440000:buy:test:symbol=EURUSD" \
  http://localhost:8080/webhook)

TRACE_ID=$(echo $RESPONSE | jq -r '.trace_id')
echo "Trace ID: $TRACE_ID"

# View all logs for this trace
./ingress 2>&1 | grep $TRACE_ID
```

## Performance Impact

### Debug Logging Overhead

- **Enabled (default)**: ~2-5% CPU overhead, ~1-3% memory increase
- **Disabled**: Minimal overhead (structured logging framework still active)
- **Recommendation**: Enable by default in production for visibility. Only disable if CPU/memory is severely constrained.

### Log Volume

With debug enabled at 100 requests/sec:
- **Ingress**: ~200 KB/sec of logs (easily handled by containers)
- **Python services**: ~50-100 KB/sec each
- **All services**: ~1 MB/sec total (rotate logs regularly)

## Structured Logging Format

All debug logs use structured JSON format for easy parsing:

```json
{
  "time": "2026-05-27T12:34:56.789Z",
  "level": "DEBUG",
  "logger": "ingress",
  "msg": "signal parsed",
  "client": "127.0.0.1",
  "license": "550e8400-e29b-41d4-a716-446655440000",
  "symbol": "EURUSD",
  "command": "buy"
}
```

### Key Fields

- `time`: ISO 8601 timestamp with millisecond precision
- `level`: DEBUG, INFO, WARN, ERROR
- `logger`: Service name (ingress, bridge, persist, etc.)
- `msg`: Human-readable message
- Additional context fields: trace_id, license, account, symbol, etc.

## Monitoring & Alerting

### Parse logs for monitoring

```bash
# Count errors per service
cat *.log | jq 'select(.level=="ERROR") | .logger' | sort | uniq -c

# Track average ML confidence
cat ingress.log | jq 'select(.msg=="ML prediction received") | .confidence' | jq -s 'add/length'

# Monitor exposure limit violations
cat ingress.log | jq 'select(.msg=="exposure limit exceeded")'

# Track signal latency
cat ingress.log | jq 'select(.msg=="signal published successfully") | .'
```

### Real-time Monitoring

```bash
# Watch for rejections
tail -f ingress.log | jq 'select(.level=="WARN" or .msg|contains("rejected"))'

# Monitor error rate
tail -f ingress.log | jq 'select(.level=="ERROR")' | wc -l

# Track performance
tail -f ingress.log | jq 'select(.msg|contains("latency"))' | jq '.duration_ms'
```

## Troubleshooting with Debug Logs

### Signal not accepted

Enable debug and look for:
```bash
# 1. Was request received?
grep "webhook request received" ingress.log

# 2. Did parsing succeed?
grep "signal parsed" ingress.log

# 3. What was the rejection reason?
grep "rejecting\|rejected\|error" ingress.log
```

### ML scoring not working

```bash
# Check ML predictor availability
grep "ML scoring\|ML prediction\|unavailable" ingress.log

# See confidence scores returned
grep "ML prediction received" ingress.log
```

### Exposure limit issues

```bash
# Verify exposure is being checked
grep "exposure check\|exposure within\|exposure limit exceeded" ingress.log

# Check current vs limit values
grep "exposure" ingress.log | jq '{license, account, current, limit}'
```

### Performance analysis

```bash
# Find slowest operations
grep "latency\|duration" *.log | jq '.duration_ms' | sort -n | tail -10

# Count operations by type
grep "msg" *.log | jq '.msg' | sort | uniq -c | sort -rn
```

## Best Practices

1. **Default to enabled**: Leave `DEBUG=true` by default in all environments
2. **Log rotation**: Rotate logs daily or by size (100 MB+)
3. **Centralized logging**: Send logs to ELK, Splunk, or CloudWatch in production
4. **Alert on errors**: Setup alerts for ERROR level messages
5. **Monitor latency**: Track operation durations in debug output
6. **Clean sensitive data**: Debug logs don't include secrets (only license IDs, which are non-sensitive)

## Configuration Examples

### Development

```bash
# Maximum visibility
DEBUG=true HTTP_ADDR=:8080 ./ingress 2>&1 | tee ingress.log
```

### Staging

```bash
# Debug enabled but output to file to avoid console spam
DEBUG=true HTTP_ADDR=:8080 ./ingress >> ingress.log 2>&1 &
```

### Production (recommended)

```bash
# Debug enabled for troubleshooting, logs to file
DEBUG=true HTTP_ADDR=0.0.0.0:8080 ./ingress >> /var/log/execrelay/ingress.log 2>&1 &

# If CPU/memory is critical, can disable
DEBUG=false HTTP_ADDR=0.0.0.0:8080 ./ingress >> /var/log/execrelay/ingress.log 2>&1 &
```

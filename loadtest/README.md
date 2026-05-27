# ExecRelay Load Testing

This directory contains load testing tools for validating ExecRelay throughput, latency, and resilience.

## Quick Start

### Single Rate Test
Test ingress at a specific rate:

```bash
make up                    # Start docker-compose stack
make loadtest RATE=100     # Test at 100 req/s
```

### Multi-Rate Test Suite
Run automated tests across multiple rates (10, 50, 100, 500 req/s):

```bash
make up                    # Start docker-compose stack (wait for all services healthy)
make loadtest-suite        # Run full suite, writes results to loadtest-results.txt
```

## Scenarios

### Webhook Throughput (Scenario 1)
Measures end-to-end latency from ingress webhook receipt through NATS publish and database persistence.

**Metrics:**
- Request rate (req/s)
- Success rate (%)
- Latency percentiles (p50, p95, p99)
- Error distribution by HTTP status code

**Target:** p99 ≤ 95ms at 500 req/s with ≥99% success rate

**Run:**
```bash
make loadtest RATE=500 DURATION=60s WORKERS=20
```

### Bridge Fan-Out (Scenario 2 - Future)
Simulates multiple EAs connecting via WebSocket and receiving dispatched signals.

**Metrics:**
- Active EA connections
- Signal dispatch latency
- NACK rate (rejected signals)
- Connection stability

**Run:**
```bash
# Requires bridge WebSocket endpoint instrumentation
# Planned for Phase 3B
```

### Database Write Bottleneck (Scenario 3 - Future)
Direct measurement of PostgreSQL write latency independent of NATS.

**Metrics:**
- Write latency at various throughput levels
- Hypertable chunk compression overhead
- Maximum sustainable write rate

**Run:**
```bash
# Requires persist service direct HTTP endpoint
# Planned for Phase 3B
```

### Circuit Breaker Resilience (Scenario 4 - Future)
Validates DXTrade circuit breaker behavior under failure conditions.

**Metrics:**
- Trip/recovery timing
- Error handling correctness
- Graceful degradation curve

**Run:**
```bash
# Requires DXTrade mock failure injection
# Planned for Phase 3B
```

## Output

Results are written to `loadtest-results.txt` with format:

```
--- Webhook Throughput Test: 100 req/s ---
Total: 6000 | Success: 5970 (99.5%) | Failed: 30
Latency (ms): p50=12.34 p95=67.89 p99=92.10 min=5.67 max=152.34
PASS: 99.5% success rate, p99=92.10ms
```

## Interpreting Results

| Metric | Threshold | Severity |
|--------|-----------|----------|
| Success Rate | < 99% | WARNING |
| p99 Latency | > 95ms @ 500req/s | WARNING |
| p99 Latency | > 500ms | CRITICAL |
| Error 5xx Rate | > 1% | CRITICAL |
| Connection Errors | > 0.5% | WARNING |

## Baseline Targets

After Phase 3 load testing (docker-compose):

- **Throughput:** 500+ req/s sustained
- **Latency p99:** ≤95ms at 500 req/s
- **Success Rate:** ≥99.5%
- **Bridge dispatch latency:** ≤150ms p99
- **Database write latency:** ≤250ms p99
- **Circuit breaker recovery:** <5s

## Under Load Expectations

When load is applied and all services are running:

1. **Ingress** processes webhook → validates signature → publishes to NATS (≤50ms typically)
2. **Bridge** consumes signal → broadcasts to connected EAs (≤20ms typically)
3. **Persist** consumes signal → inserts to PostgreSQL → returns (≤30ms typically)
4. **DXTrade** adapter processes command → connects to broker → executes (≤100ms typically)

Total end-to-end path: ~200ms p99

## Notes for Production

- Load tests assume healthy docker-compose stack with all services running
- Use `docker compose logs -f` to monitor service health during testing
- Network latency: docker-compose adds ~1-5ms per hop (negligible)
- PostgreSQL: Single instance, autovacuum enabled, checkpoint tuning recommended for load
- NATS: JetStream persistence enabled, may hit fsync limits at very high rates (>5000/s)
- Resource monitoring: Use `docker stats` to watch CPU/memory utilization during tests

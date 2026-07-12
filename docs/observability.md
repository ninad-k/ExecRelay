# Observability

ExecRelay exposes Prometheus metrics on every service, distributed tracing via
OpenTelemetry to Tempo, and JSON-structured logs to stdout. Grafana + Alertmanager
are pre-wired in `docker-compose.yml`.

This document is the **metrics catalog** — what every metric means, healthy
ranges, and recommended alert thresholds. If you add a new metric, add it
here in the same PR.

---

## Where metrics are scraped

Every service exposes `/metrics` on its HTTP port. Prometheus is configured
via [`infra/docker/prometheus/prometheus.yml`](../infra/docker/prometheus/prometheus.yml)
to scrape every app service plus:

| Exporter | Port | What it covers |
|---|---|---|
| `postgres-exporter` | 9187 | Postgres stats: connections, locks, tuples, replication lag |
| `redis-exporter` | 9121 | Redis memory, ops/s, clients |
| Prometheus itself | 9090 | Scrape success/failure |
| Alertmanager | 9093 | Notification delivery |

---

## Metric catalog — Go services

### `ingress`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `ingress_webhook_requests_total` | counter | `status` (HTTP code) | Every request that hit `/webhook`. The single most useful series for traffic sanity. |
| `ingress_webhook_duration_seconds` | histogram | — | End-to-end webhook handling latency. **The hot-path SLI.** |
| `ingress_rejections_total` | counter | `reason` | Why webhooks were rejected. Values: `perimeter_rejected`, `rate_limit_exceeded`, `ip_not_allowed`, `timestamp_rejected`, `license_rejected`, `secret_rejected`, `signature_rejected`, `plan_limit_exceeded`, `exposure_limit_exceeded`, `trading_halted`. |
| `ingress_license_config_warnings` | gauge | `license_id`, `issue` | Set by `AuditLicenses()` at startup + SIGHUP. `issue` is `no_auth`, `no_hmac`, `no_secret`, or `rotation_active`. **`no_auth` is the high-priority one — license accepts unauthenticated webhooks.** |
| `ingress_trading_halted` | gauge | — | Kill-switch state. `1` = halted, `0` = normal. |
| `ingress_ml_webhook_requests_total` | counter | `outcome` (`accepted`, `skipped`, `fail_open`, `rejected`) | `POST /webhook/ml` (ADR 0008) requests by outcome. `fail_open` means the predictor call errored/timed out and the caller's original action was published unfiltered — never a rejection. `skipped` is an enforced `NOTHING` decision (nothing published). |
| `ingress_ml_webhook_duration_seconds` | histogram | — | `/webhook/ml` request latency, including the synchronous `ml-predictor` call. Deliberately looser than the flat path's 95 ms SLO — callers opt into this. |
| `ingress_ml_predictor_errors_total` | counter | — | Errors calling `ml-predictor`'s `/predict` (timeout, connection refused, non-2xx, malformed response). Every one of these fails open (see `outcome="fail_open"` above). |

`/webhook/ml` is an opt-in JSON path that scores requests through `ml-predictor`
before publishing a command (see [ADR 0008](adr/0008-opt-in-json-ml-webhook-path.md)
and [`apps/ingress/internal/ingress/ml.go`](../apps/ingress/internal/ingress/ml.go)).
It defaults to **shadow mode** (`ML_ENFORCE=false`): the model is scored and
audited on every request, but the caller's original `buy`/`sell` action is
always published until an operator flips `ML_ENFORCE=true`. It shares the
same auth/gating chain as the flat `/webhook` path.

### `bridge`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `bridge_ea_connections_active` | gauge | — | How many EAs are currently connected via WebSocket. Drops to 0 = nobody can execute. |
| `bridge_signals_dispatched_total` | counter | — | Signals successfully forwarded to an EA. |
| `bridge_signals_nacked_total` | counter | — | Signals rejected by the bridge (no matching EA, invalid payload, etc.). |
| `bridge_fills_received_total` | counter | — | Fill reports received back from EAs. |
| `bridge_consumer_lag_pending` | gauge | `subject` | NATS JetStream pending message count by subject. **Rising lag = bridge is falling behind.** |

### `dxtrade`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `dxtrade_commands_processed_total` | counter | `outcome` | Commands sent to DXTrade. Outcomes: `success`, `error`, `retried`. |
| `dxtrade_execution_latency_seconds` | histogram | — | Time from NATS subscribe to DXTrade REST response. |
| `dxtrade_circuit_breaker_trips_total` | counter | — | Sony/gobreaker fired open. Hot when DXTrade is misbehaving. |
| `dxtrade_broker_failures_total` | counter | `error_type` | DXTrade-side errors (auth, rate limit, server error). |

---

## Metric catalog — Python services

### `persist`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `persist_fills_processed_total` | counter | — | Fills written to TimescaleDB. |

<!-- TODO: persist should expose lag-vs-NATS and write-error counters too. Open issue. -->

### `risk`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `risk_positions_updated_total` | counter | — | Position updates applied from broker reconciliation. |
| `risk_drawdowns_recorded_total` | counter | — | Drawdown snapshots written. |

### `backtester`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `backtester_errors_total` | counter | `kind` | Errors during backtest runs. |

### `ml-predictor`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `ml_predictions_total` | counter | `action` | Predictions made, by `action_summary` (`OPEN_LONG`, `OPEN_SHORT`, `FLIP_LONG`, `FLIP_SHORT`, `CLOSE_ONLY`, `NOTHING`). |
| `ml_prediction_errors_total` | counter | — | `/predict` requests that failed (bad JSON, oversized/invalid body, missing/unloaded model, or a prediction error). |
| `ml_prediction_latency_seconds` | histogram | — | Time spent scoring a single `/predict` request (off the event loop; see `apps/ml-predictor/app.py`). |
| `ml_prob_win` | histogram | — | Model win-probability output per prediction. |
| `ml_model_loaded` | gauge | — | `1` if the XGBoost model loaded successfully at startup, else `0`. Drives `/readyz`. |
| `ml_model_info` | gauge | `version` | Info-style gauge, always `1`, one series per currently-loaded model version (`XGBPredictor.model_version`). Join on `version` in dashboards/alerts to see which artifact is live, or to catch an unexpected version after a deploy. |

### `ml-feature-extractor`

| Metric | Type | Labels | What it measures |
|---|---|---|---|
| `ml_signals_processed_total` | counter | — | Signal messages consumed off the `signals.>` NATS subject. |
| `ml_features_extracted_total` | counter | — | Feature rows successfully written to `signal_features`. |
| `ml_extraction_errors_total` | counter | — | Errors during feature extraction or DB write. |

---

## Recommended alerts

Wire these in `infra/prometheus/alert_rules.yml` (and route them in
`infra/docker/alertmanager/alertmanager.yml`).

### Critical — page on-call

| Alert | Expression | Why |
|---|---|---|
| **IngressDown** | `up{job="ingress"} == 0` for 1m | No webhooks can land at all. |
| **IngressHighErrorRate** | `sum(rate(ingress_webhook_requests_total{status=~"5.."}[5m])) > 1` | Persistent 5xx means publishing to NATS is broken. |
| **IngressHighLatency** | `histogram_quantile(0.99, rate(ingress_webhook_duration_seconds_bucket[5m])) > 0.095` for 5m | p99 above the 95 ms SLO. |
| **LicenseHasNoAuth** | `ingress_license_config_warnings{issue="no_auth"} == 1` | License accepts unauthenticated webhooks; anyone with the ID can trade. |
| **TradingHalted** | `ingress_trading_halted == 1` for 5m | Confirm this is intentional, not an unintended halt. |
| **BridgeNoEAs** | `bridge_ea_connections_active == 0` for 2m | Nobody is connected — no signals can be executed. |
| **BridgeLagGrowing** | `delta(bridge_consumer_lag_pending[10m]) > 1000` | Bridge is falling behind ingress; queue is growing. |
| **DXTradeCircuitOpen** | `increase(dxtrade_circuit_breaker_trips_total[5m]) > 0` | Broker is upstream-broken; trades will fail. |
| **PostgresDown** | `pg_up == 0` | All cold-path services break shortly after. |
| **MLModelNotLoaded** | `ml_model_loaded == 0` for 5m | The filter service is up (scrapeable) but scoring nothing — every `/predict` call 503s and every `/webhook/ml` call fails open. |

### Warning — Slack / email

| Alert | Expression | Why |
|---|---|---|
| **HighRejectionRate** | `sum(rate(ingress_rejections_total[15m])) by (reason) > 5` | Either an attack or a misconfigured customer. Investigate by `reason`. |
| **PendingHmacRotation** | `ingress_license_config_warnings{issue="rotation_active"} == 1` for 24h | Customer started HMAC rotation but didn't finish it. |
| **PostgresHighConnections** | `pg_stat_database_numbackends > 80` | Connection pool exhaustion incoming. |
| **PersistFallingBehind** | (NATS pending grows over 10m) | persist hasn't kept up; fills aren't being recorded. |
| **MLPredictorErrorRate** | error share of `ml_prediction_errors_total` vs. total `/predict` attempts `> 5%` for 10m | The model is up but a rising share of requests are failing (bad payload, feature-contract drift, inference errors). |
| **MLIngressFailOpen** | `sum(rate(ingress_ml_webhook_requests_total{outcome="fail_open"}[5m])) > 0` for 10m | Trades are bypassing the filter because `ml-predictor` is unreachable/erroring — the ML gate is silently a no-op. |
| **MLModelVersionChanged** | new `version` label appears on `ml_model_info` vs. 15m ago | A model swap should be a known, deliberate event, not a surprise found by dashboard. Informational — confirm it was an intended deploy. |
| **MLProbWinDistributionShift** | median (`histogram_quantile(0.5, ...)`) of `ml_prob_win` over 1h `< 0.1` or `> 0.9` | Feature-contract drift symptom — the model's output has collapsed to one extreme, most likely a mismatched or missing feature vector rather than a genuine regime shift. |

### Informational — no page, dashboard only

- `ingress_webhook_requests_total` rate — traffic
- `bridge_fills_received_total` rate — execution throughput
- `dxtrade_execution_latency_seconds` p99 — broker responsiveness
- `pg_stat_database_xact_commit` rate — DB health
- `ml_predictions_total` rate — feature use

---

## Logs

All services emit JSON to stdout. Aggregate with whatever you use
(Loki, CloudWatch, Datadog, or just `docker compose logs -f`).

Per-service log format:

- **Go** (`ingress`, `bridge`, `dxtrade`): `log/slog` with JSON handler.
  Common fields: `time`, `level`, `msg`, `client`, `trace_id`, `license`.
- **Python** (everything else): `logging.basicConfig(format="%(asctime)s
  %(name)s %(levelname)s %(message)s", stream=sys.stdout)`.
  <!-- TODO: standardise Python services on JSON output too (python-json-logger) so log aggregation is uniform. -->

**`trace_id`** is propagated end-to-end: assigned at ingress, embedded in
the protobuf `Signal`, attached to fill reports, and stored in the `fills`
table. Use it to follow a single trade across services:

```sh
docker compose --profile apps logs | grep '"trace_id":"abc123"'
```

---

## Distributed tracing (Tempo)

Tempo is deployed but not yet instrumented in all services.
<!-- TODO: enable OTel SDK in ingress, bridge, dxtrade and emit spans
     to the OTLP endpoint (env var OTEL_EXPORTER_OTLP_ENDPOINT). Once
     enabled, Grafana's "Service Graph" panel becomes usable. -->

When wired, each `trace_id` from ingress will surface in Tempo as a
multi-span trace covering ingress → NATS → bridge → EA.

---

## Dashboards

<!-- TODO: ship pre-built Grafana dashboards as JSON in
infra/grafana/dashboards/ so users see something useful on first login
instead of an empty grid.

Recommended starter dashboards:
  - Hot path overview (req rate, latency p50/p95/p99, rejection rate
    by reason)
  - License health (config warnings, rejection rate per license,
    daily quota usage)
  - Broker connectivity (EA count over time, dxtrade circuit state,
    execution latency by broker)
  - System health (CPU/mem per service, Postgres connections, NATS
    consumer lag)
-->

---

## See also

- [`apps/ingress/internal/ingress/metrics.go`](../apps/ingress/internal/ingress/metrics.go)
  — ingress metric definitions
- [`apps/bridge/internal/bridge/metrics.go`](../apps/bridge/internal/bridge/metrics.go)
  — bridge metric definitions
- [`apps/dxtrade/internal/dxtrade/metrics.go`](../apps/dxtrade/internal/dxtrade/metrics.go)
  — dxtrade metric definitions
- [`infra/prometheus/alert_rules.yml`](../infra/prometheus/alert_rules.yml)
  — current alert rules
- [`docs/runbooks/`](runbooks/) — when an alert fires, follow the matching
  runbook

# Data model

ExecRelay's relational store is **TimescaleDB** (PostgreSQL 16 + TimescaleDB
extension). Schema lives in [`infra/migrations/`](../infra/migrations/) and is
managed by [`golang-migrate`](https://github.com/golang-migrate/migrate).

This document explains **what each table is for, why it exists, and how the
tables relate**. It is hand-maintained — if you add a new table, update this
doc in the same PR.

If you just need DDL, read the SQL. This doc is the prose version.

---

## Tables at a glance

33 tables organised into 9 logical groups:

| Group | Tables |
|---|---|
| **Identity & access** | `users`, `roles`, `user_roles`, `plan_tiers`, `user_limit_overrides`, `admin_audit_log` |
| **Tenancy** | `licenses`, `instances`, `regions`, `instance_region_pref` |
| **Signals (hot-path produce)** | `accepted_signals`, `audit_rejections`, `signal_fingerprints`, `daily_signal_counts` |
| **Fills (hot-path consume)** | `fills`, `ea_connection_sessions` |
| **Background work** | `tasks`, `notifications_log` |
| **Reports** | `report_runs`, `report_findings`, `report_subscriptions` |
| **System events** | `system_events` |
| **Risk & exposure (Phase 6+)** | `account_positions`, `account_drawdowns`, `portfolio_exposure_limits`, `risk_breach_log`, `signal_groups`, `signal_group_members`, `symbol_correlations` |
| **ML / backtesting** | `model_registry`, `ml_models`, `signal_features`, `backtesting_results` |

---

## ER diagram (text)

```
       users ─┬─── user_roles ──── roles
              │
              ├─── user_limit_overrides
              │
              └─── licenses ──┬── instances ─── instance_region_pref ─── regions
                              │      │
                              │      └── ea_connection_sessions
                              │
                              ├── accepted_signals ─── fills
                              ├── audit_rejections
                              ├── signal_fingerprints
                              ├── daily_signal_counts
                              ├── notifications_log
                              ├── risk_breach_log
                              ├── account_positions ─── account_drawdowns
                              ├── portfolio_exposure_limits
                              ├── signal_groups ─── signal_group_members
                              ├── symbol_correlations
                              └── backtesting_results

       admin_audit_log ─── (actor_user_id, target_user_id)
       system_events    ─── (no FK, freeform JSONB)
       tasks            ─── (no FK; background work queue)
       model_registry / ml_models / signal_features ── ML lifecycle
       report_runs ─── report_findings  /  report_subscriptions
       plan_tiers (lookup table; no FK back from users today)
```

The single most-referenced parent is **`licenses`** — every operational table
has a `license_id UUID NOT NULL REFERENCES licenses(id)` to give tenancy
isolation by default.

---

## Identity & access

### `users`
The person who logs into the portal.
- `id UUID PK`, `email TEXT UNIQUE`, `password_hash TEXT` (bcrypt), `created_at`, `updated_at`.

### `roles`
Lookup table with exactly three rows: `'user'`, `'support'`, `'super_admin'`.
Seeded by the migration via `INSERT … ON CONFLICT DO NOTHING`.

### `user_roles`
Join table: `(user_id, role_id)` composite PK. Multiple roles per user are
allowed; the portal-api checks roles via `require_role()`.

### `plan_tiers`
Defines the limits that come with each pricing plan: `max_instances`,
`max_concurrent_connections`, `max_signals_per_day`. Today the table is a
lookup; the mapping from `users` to `plan_tiers` is determined dynamically in application code based on roles and overrides, keeping the schema clean.

### `user_limit_overrides`
Per-user overrides on top of the plan tier. Used when sales has cut a custom
deal. `reason TEXT NOT NULL` is required so it's clear *why* the override
exists.

### `admin_audit_log`
Records every privileged action: `(actor_user_id, target_user_id,
action TEXT, details JSONB, created_at)`. Read this when investigating
"who promoted X to super_admin?".

---

## Tenancy

### `licenses`
The credential by which a customer authenticates with the ingress webhook.

| Column | Notes |
|---|---|
| `id` | UUID PK, sent as the first comma-separated field in TradingView alerts |
| `user_id` | Owning user |
| `license_key` | Human-readable display key (different from `id`) |
| `secret` | Body-embedded `secret=` parameter expected on every webhook |
| `hmac_secret` | Key for the `X-ExecRelay-Signature` HMAC header |
| `pending_hmac_secret` | For HMAC rotation; primary + pending both accepted |
| `active` | Soft-delete flag; lookups return `ErrLicenseInactive` when false |
| `max_signals_per_day` | Hard daily quota; `0` = unlimited |
| `created_at` | |

The ingress runs `AuditLicenses()` at startup and on SIGHUP, warning if a
license has neither `secret` nor `hmac_secret` (`issue="no_auth"`). See
[`apps/ingress/internal/ingress/license.go`](../apps/ingress/internal/ingress/license.go).

### `instances`
A specific broker terminal session — typically one MT4/MT5/DXTrade account.
A license can own many instances. Identified by `(license_id, instance_key)`.
The `Platform` column drives NATS subject routing
(`signals.<platform>.<license>.<instance>`).

### `regions`
Available ExecRelay regions (e.g., `iad`, `sfo`, `fra`). Lookup table.

### `instance_region_pref`
Pinning an instance to its preferred region; ingress stamps every signal
with `INGRESS_REGION` but a multi-region deployment can use this to route
home.

---

## Signals (hot path produce side)

### `accepted_signals`
Every signal that passed every auth/validation check in ingress and was
published to NATS. **Written by `persist`**, not by ingress (ingress never
touches the DB on the hot path). Has `trace_id` so you can join through to
fills.

### `audit_rejections`
Every signal ingress *rejected*: license unknown, signature bad, quota
exceeded, etc. Body SHA256 + reason code. Useful for spotting attack
patterns or misconfigured customers.

### `signal_fingerprints`
Dedup table — `(license_id, body_sha256)` composite primary key with
`ON CONFLICT DO NOTHING` so a TradingView retry doesn't double-execute.

### `daily_signal_counts`
Counter table keyed `(license_id, day_utc)` for fast quota enforcement.
Incremented in-memory by ingress's `dailyCounter` and flushed by `persist`.

---

## Fills (hot path consume side)

### `fills`
The durable record of a broker execution.

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `signal_id` | Nullable — fills can arrive without a known signal (manual close, etc.) |
| `license_id` | Always set; the tenancy anchor |
| `instance_id` | Which broker terminal executed it |
| `trace_id` | Propagated from the originating signal — the join key for end-to-end latency analysis |
| `broker_order_id` | The broker-side ID; useful when reconciling positions |
| `status` | `filled`, `partially_filled`, `rejected`, `error` |
| `error_code` / `error_message` | Set when `status` is `rejected` or `error` |
| `payload` | `JSONB` — full broker response for forensics |
| `created_at` | TIMESTAMPTZ; this column is what TimescaleDB partitions on |

The `journal_export` endpoint in portal-api streams from this table.

### `ea_connection_sessions`
Heartbeat / session record for each MT4/MT5 EA WebSocket connection. Used
by the bridge to detect drops and emit `bridge_ea_disconnected` events.

---

## Background work

### `tasks`
Generic background-work queue consumed by the `tasks` service. Examples:
fill timeout checks, retention cleanup, scheduled reports. `status` /
`scheduled_for` / `attempts` columns mean it can also stand in as a
crude job scheduler.

### `notifications_log`
Sent-notification history (email/Slack/webhook). De-dup + audit.

---

## Reports

### `report_runs`
One row per generated report (daily / weekly). `status` tracks
`pending → running → succeeded|failed`.

### `report_findings`
Individual line items inside a report (e.g., "license X had 12% rejection
rate this week"). FK to `report_runs`.

### `report_subscriptions`
Which users want which reports, on what cadence, delivered how.

---

## System events

### `system_events`
Catch-all freeform event log with `event_type TEXT` and `payload JSONB`.
Used when a structured table would be overkill (e.g., `kill_switch_toggled`,
`license_reloaded`). Not normalised on purpose.

---

## Risk & exposure (Phase 6+)

### `portfolio_exposure_limits`
Per-license/account caps. Checked by ingress's `checkExposureLimits()`
before publishing.

### `account_positions`
Snapshot of broker-side open positions per account, refreshed by the
DxTrade adapter / reconciliation job (Phase 6+).

### `account_drawdowns`
Drawdown tracking: peak equity, current equity, % drawdown.

### `risk_breach_log`
Audit trail of every breached limit: which license, which limit, what
the value was vs the threshold.

### `signal_groups` + `signal_group_members`
Logical grouping of signals (e.g., all legs of a multi-leg trade) so risk
can reason about them together.

### `symbol_correlations`
Cached pairwise symbol correlation coefficients used by the exposure
calculator to flag concentration risk.

---

## ML / backtesting

### `model_registry`
Generic registry of models (broker-specific scoring, signal-quality
classifier, etc.). `model_uri` typically points at MLflow.

### `ml_models`
Newer, richer version of `model_registry` introduced in Phase 6 with
`version`, `status`, `metrics JSONB`.

### `signal_features`
Per-signal feature vectors used as ML training input.

### `backtesting_results`
One row per backtest run: parameters, P&L, max drawdown, Sharpe, etc.

---

## TimescaleDB hypertables

Currently, the `accepted_signals` table is configured as a TimescaleDB hypertable partitioned by `received_at` (defined in the `000001_foundation` migration). 
Under high-scale production deployments, candidate tables for migration to hypertables include:
- `fills` (partition by `created_at`)
- `audit_rejections` (partition by `rejected_at`)
- `system_events` (partition by `created_at`)
- `signal_features` (partition by `created_at`)

---

## Conventions

- **Primary keys**: `UUID` with `DEFAULT gen_random_uuid()` (via `pgcrypto`).
  Exception: `BIGSERIAL` on `risk_breach_log`, `daily_signal_counts` where
  the row is purely a counter and an `id` PK isn't useful externally.
- **Timestamps**: `TIMESTAMPTZ NOT NULL DEFAULT now()` — always UTC, always
  populated server-side.
- **Tenancy**: every operational row has a `license_id UUID NOT NULL` with
  `REFERENCES licenses(id) ON DELETE CASCADE`. Multi-tenant queries
  **must** JOIN through `licenses.user_id` and filter on the current user —
  see [`apps/portal-api/app.py`](../apps/portal-api/app.py) for the pattern.
- **Soft delete via `active BOOLEAN`** on `licenses`. Hard-deleting a
  license cascades to fills/signals — usually not what you want.
- **`payload JSONB`** is used liberally for forensic detail. Don't query
  inside it on the hot path; use it for "show me everything we knew about
  this rejection."

---

## Adding a new table

1. `make migrate-new NAME=add_<table>`
2. Write the `up` SQL (idempotent `CREATE TABLE IF NOT EXISTS`, FK to
   `licenses(id)` if tenant-scoped, timestamp column).
3. Write the `down` SQL (`DROP TABLE IF EXISTS ...`) OR explicitly leave
   it as `SELECT 1` with a comment if reversal would lose production data.
4. Apply locally: `make migrate-up`.
5. Update **this document** with the new table's purpose and column notes.
6. If the table needs to be a TimescaleDB hypertable, the migration should
   also call `SELECT create_hypertable(...)`.
7. Open the PR. The `migrations.yml` CI workflow will apply the migration
   against a fresh Postgres on every push to verify the SQL is valid.

---

## See also

- [`infra/migrations/README.md`](../infra/migrations/README.md) — migration
  tool usage
- [`docs/ARCHITECTURE.md#7-storage`](ARCHITECTURE.md#7-storage) — storage
  tier overview
- [`docs/observability.md`](observability.md) — which DB metrics matter
  for alerting

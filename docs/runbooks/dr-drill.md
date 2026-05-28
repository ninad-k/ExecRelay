# DR drill runbook

> Practice the restore so the first time you do it isn't 03:00 during an
> incident. Run this **quarterly minimum**, and append the resulting log
> entry to `dr-drill-log.md` so we have a trend of RTO numbers over time.

## What this drill proves

1. The nightly `scripts/backup.sh` dump is actually restorable (not just
   "the file exists and is non-zero bytes").
2. Restoring takes a predictable amount of time → that's our RTO ceiling.
3. The schema in the dump matches the migrations on disk (catches a missed
   `migrate up` on the live DB before it bites us in an incident).
4. Critical-table row counts match end-to-end (no data loss in pipe).

## Pre-reqs

- `psql` + `pg_dump` installed locally (PostgreSQL client tools)
- A **scratch Postgres** the drill can wipe. Easiest:
  ```bash
  docker run -d --name pg-scratch -p 5433:5432 \
      -e POSTGRES_PASSWORD=execrelay_dev_password \
      -e POSTGRES_USER=execrelay \
      -e POSTGRES_DB=execrelay_restore \
      postgres:14
  ```
- Read-only access to the live DB. **Never** restore back over live.

## Running the drill

```bash
make dr-drill \
    MIGRATE_DB="postgres://user:pw@live-host:5432/execrelay" \
    DR_SCRATCH_DSN="postgres://execrelay:execrelay_dev_password@localhost:5433/execrelay_restore"
```

Or run the script directly:

```bash
scripts/dr-drill.sh \
    "postgres://user:pw@live-host:5432/execrelay" \
    "postgres://execrelay:execrelay_dev_password@localhost:5433/execrelay_restore"
```

The script:
- `pg_dump`s the live DB to a temp gzipped file
- Restores into the scratch DB inside a single transaction
- Compares row counts on `licenses`, `instances`, `accepted_signals`,
  `fills`, `request_log`, `dead_letter_messages`
- Appends a timed entry to `dr-drill-log.md` and exits non-zero on any
  mismatch

## Reading the result

A passing entry looks like:

```
## 20260528T034400Z → 20260528T035130Z
- dump: 220s, 184392011 bytes
- restore: 230s
- **RTO (dump + restore)**: 450s
| table | live | scratch | match |
| accepted_signals | 1283441 | 1283441 | OK |
```

The "RTO" number is the worst-case time-to-restore from the most recent
backup; it should be in line with the `RTO_TARGET_SECS` documented in
[disaster-recovery.md](../disaster-recovery.md). If it drifts above the
target, escalate to capacity planning before the next drill.

A failing entry has `**DIFF**` in the match column for at least one row.
Investigate before the next backup runs — the dump may be silently
truncating large tables under load.

## When the drill fails

| Symptom | Likely cause | Action |
|---|---|---|
| `pg_dump: connection failed` | DSN wrong, or no network to live DB | Fix DSN, retry |
| `ON_ERROR_STOP` aborts mid-restore | Schema drift (live migrated newer than scratch base) | Run `make migrate-up MIGRATE_DB=$SCRATCH` against scratch first |
| Row count diff on one table | Possible mid-dump truncation, or trigger/FK rejecting rows | Inspect with `psql`, compare `\d+ <table>` |
| RTO above target | Dump or restore is the bottleneck — measure each phase | Consider `pg_dump --jobs` (directory format) for parallel dump |

## Cleanup

```bash
docker rm -f pg-scratch  # if you used the docker option above
```

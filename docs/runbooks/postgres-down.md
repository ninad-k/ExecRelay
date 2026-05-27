# Runbook: Postgres is down

## Symptom

- `PostgresDown` alert: `pg_up == 0`
- `postgres-exporter` no longer scraping
- Cold-path services (`portal-api`, `analytics`, `reports`, `persist`)
  returning 5xx or stuck "starting"

## What's affected

| Service | Effect |
|---|---|
| **`ingress`** (hot path) | **Unaffected** — no DB writes on the hot path; trades still execute. (The exposure-limit check degrades gracefully — `if h.db != nil`.) |
| **`bridge`** | Unaffected — routes from NATS to EAs without DB |
| **`persist`** | **Buffers in NATS durable consumer**, no data loss yet. Lag grows. |
| **`portal-api`** / **`portal-web`** | 5xx on every request — users can't log in or manage licenses |
| **`analytics`** / **`reports`** | 5xx |
| **`tasks`** | Background jobs fail; retried by tasks worker |

The hot path keeps working. **Do not panic-failover** if trades are still
landing — that's the system behaving as designed.

## Triage (first 60 seconds)

```sh
# 1. Is the container running?
docker compose ps postgres

# 2. If running, why is it failing?
docker compose logs postgres --tail=100

# 3. Disk full?
df -h /var/lib/docker
# Or look at the volume specifically:
docker system df -v | grep postgres

# 4. Memory?
docker stats postgres --no-stream

# 5. Recent backups OK? (so we know our recovery options)
ls -la /var/backups/execrelay/daily/
```

## Common causes

### Disk full

```sh
df -h /var/lib/docker
```

If > 95%, postgres won't write WAL. **Free space first**, then restart.

```sh
# Quick wins — clean up Docker
docker system prune -af
docker volume prune -f

# Old backups (be conservative; verify retention policy first)
ls -t /var/backups/execrelay/daily/ | tail -n +30 | \
  xargs -I{} rm /var/backups/execrelay/daily/{}
```

If the postgres data volume itself is full, you need a bigger disk —
this is a real incident, not a quick fix.

### OOM-killed

```sh
docker compose logs postgres --tail=200 | grep -i 'killed\|oom'
dmesg | tail -30 | grep -i 'killed process'
```

If postgres was OOM-killed, raise the container memory limit or the
host's swap. Restart:

```sh
docker compose up -d --force-recreate postgres
```

### Corrupted WAL after sudden host kill

```sh
docker compose logs postgres --tail=100 | grep -i 'corrupt\|invalid\|panic'
```

If postgres won't even start due to corruption, this is a **disaster
recovery** scenario — go to [`docs/disaster-recovery.md`](../disaster-recovery.md).

### Migration left it in a weird state

If you ran `migrate down` and something half-applied:

```sh
docker compose exec postgres psql -U execrelay -d execrelay -c \
  "SELECT version, dirty FROM schema_migrations;"
```

If `dirty=t`, you need `migrate force <version>` to mark it clean once
you've manually fixed the schema. See
[`infra/migrations/README.md`](../../infra/migrations/README.md).

## Mitigation

### Restart

```sh
docker compose up -d --force-recreate postgres
# Wait for healthy
until docker compose ps postgres | grep -q 'healthy'; do sleep 2; done
echo "postgres back"

# Cold-path services will reconnect automatically. Force them if needed:
docker compose --profile apps restart portal-api persist analytics reports tasks
```

### Hot path: protect it

If postgres is going to be down for a long time and you want to be
extra-conservative, halt trading so unfilled exposure doesn't accumulate
without monitoring:

```sh
curl -X POST "https://hook.example.com/admin/kill-switch?token=$TOKEN&state=on"
```

Resume when ready:

```sh
curl -X POST "https://hook.example.com/admin/kill-switch?token=$TOKEN&state=off"
```

### Restore from backup

Only if you have actual data loss / corruption. **You will lose any
data written since the last backup.**

See [`docs/disaster-recovery.md#partial-failures`](../disaster-recovery.md#partial-failures).

## Drain the NATS backlog

After postgres comes back, `persist` will catch up automatically by
draining its durable consumer. Watch the lag fall:

```sh
watch 'curl -s http://localhost:8081/metrics | grep bridge_consumer_lag_pending'
# Or for persist if it exposes one:
docker compose logs persist --tail=50 | grep 'lag\|pending'
```

If the consumer lag is *not* falling after postgres is healthy, persist
is stuck. Restart it:

```sh
docker compose restart persist
```

## Root cause checklist

- [ ] What was the host disk %? Set up a `node_exporter` alert at 80% if not present.
- [ ] What was host memory? Was postgres OOM-killed?
- [ ] Any recent migration? Check `schema_migrations` for `dirty=t`.
- [ ] Was a long-running query holding locks? Check
      `pg_stat_activity` for queries with `state='active'` and old
      `query_start`.
- [ ] Any deploys to postgres-using services in the same window?
- [ ] Did backups complete successfully? Check
      `journalctl -u execrelay-backup`.

## Postmortem prompts

- How long was portal-api / reports unavailable?
- Did any fills get lost (vs. just delayed in NATS)?
- Was the kill switch used? Would the symptoms have been worse without it?
- Do we need to raise the disk alert threshold (or get a bigger disk)?
- Should we add a synthetic check that exercises a `SELECT 1` from
  portal-api and pages on failure?

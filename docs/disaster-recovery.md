# Disaster Recovery (DR)

This plan covers data loss, total server loss, regional outage, and
operational mishaps (accidental DELETE, ransomware, etc.).

The goal is to make recovery a **checklist exercise**, not improvisation.

---

## RPO / RTO targets

> **Important — these are recommended starting values.** Confirm with
> business stakeholders and update before publishing externally.

The values below are the standard operational default targets for the ExecRelay single-host deployment model.

| Scenario | RPO (max data loss) | RTO (max downtime) | Strategy |
|---|---|---|---|
| **Single-host single-server** (default install) | 24 h (last nightly backup) | 1 h (rebuild from backup) | Nightly `pg_dump`; off-host backup copy |
| **k8s multi-AZ** (Helm chart with HA Postgres) | Minutes (synchronous replica) | Minutes (automatic failover) | Patroni / RDS Multi-AZ |
| **Multi-region** (Phase 6+) | < 1 minute (NATS super-cluster + Postgres logical replication) | < 5 minutes (DNS failover) | Phase 6 roadmap |

---

## Backups

### What's backed up

`scripts/backup.sh` (Linux) and `scripts/install-backups.ps1` (Windows)
run `pg_dump` against the Postgres container nightly, compress with gzip,
and rotate:

- **7 daily** retained in `/var/backups/execrelay/daily/`
- **4 weekly** snapshots (taken Sundays) in `/var/backups/execrelay/weekly/`
- **Optional S3 mirror** — set `BACKUP_S3_BUCKET` in the systemd drop-in
  to upload each dump to S3.

Backup file naming: `execrelay-YYYYMMDDTHHMMSSZ.sql.gz` (UTC).

### What's NOT backed up (today)

| Data | Status | Mitigation |
|---|---|---|
| NATS JetStream durable consumer state | Not backed up | Consumers re-subscribe from the latest position on restart. **In-flight messages older than the dispatch backlog could be lost** if NATS itself dies. |
| MinIO blob storage (backtest artifacts, MLflow models) | Mirrored nightly when configured | `scripts/minio-mirror.sh` syncs the MinIO buckets to AWS S3 via the `aws` CLI. `scripts/install-backups.sh` installs an `execrelay-minio-mirror.{service,timer}` (04:15 UTC) and enables it automatically when `MIRROR_S3_BUCKET` is set in `.env`; otherwise the units are installed but the timer stays disabled until you set `MIRROR_S3_BUCKET` + MinIO creds. |
| Redis | Not backed up; ephemeral | OK — Redis holds rate-limiter / cache state only. |
| `.env` and Caddyfile | Manual | Copy `/path/to/ExecRelay/.env` and `/etc/caddy/Caddyfile` to a secure store on every config change. |
| Grafana dashboards (when present) | Provisioned from Git | Dashboard definitions are provisioned from JSON in `infra/grafana/provisioning/dashboards/`. |
| The git repo itself | Hosted in GitHub | Make sure CI / Renovate / branch-protection state is documented separately. |

### Where backups physically live

| Tier | Location | Rotation |
|---|---|---|
| Hot | The local host, `/var/backups/execrelay/daily/` | 7 days |
| Weekly | The local host, `/var/backups/execrelay/weekly/` | 4 weeks |
| Off-host (optional) | S3 bucket via `BACKUP_S3_BUCKET` | Per S3 lifecycle policy |
| Cold-archive (recommended) | S3 Glacier Flexible Retrieval | Transition objects from Standard to Glacier after ~90 days. Runnable lifecycle config in [`infra/aws/AWS_SETUP.md`](../infra/aws/AWS_SETUP.md) §7b — cloud configuration applied per environment (committed Terraform deferred to Phase 6). |

**Rule of three**: a backup that exists in only one place doesn't exist.
At minimum, configure the S3 mirror.

---

## Backup verification (the part everyone skips)

A backup you haven't tested is a hope, not a backup. **Verify quarterly.**

### Manual restore drill

```sh
# On a throwaway machine (NOT the production host):
git clone https://github.com/ninad-k/ExecRelay.git scratch
cd scratch
sudo bash scripts/install.sh                 # spin up a fresh stack

# Copy a recent backup over (download from S3 or scp from prod-host)
scp prod-host:/var/backups/execrelay/daily/execrelay-LATEST.sql.gz ./

# Drop the current empty DB content and restore
gunzip -c execrelay-LATEST.sql.gz \
  | docker compose exec -T postgres psql -U execrelay execrelay

# Spot-check: do a few rows show up?
docker compose exec postgres psql -U execrelay -d execrelay \
  -c "SELECT count(*) FROM users; SELECT count(*) FROM fills; SELECT max(created_at) FROM fills;"
```

**What "verified" means:**

1. The restore command completes without errors.
2. Row counts on key tables (`users`, `licenses`, `fills`,
   `accepted_signals`) are non-zero and in the right ballpark vs. prod.
3. `MAX(created_at) FROM fills` is within the expected age of the backup.
4. The portal-web container can start and log in works for a known test
   user (proves the schema is intact).

Log the verification in a shared spreadsheet or Confluence page: date,
backup file tested, restore time elapsed, who ran it.

### Automated verification

Automated restore verification is executed via the `dr-drill` target in the `Makefile`, which triggers `scripts/dr-drill.sh`. The script spins up a scratch database container, restores the latest dump, and asserts database integrity, logging metrics and elapsed execution times.

---

## Total host loss (the most common DR scenario)

Steps when the production server is dead, gone, on fire, or otherwise
unrecoverable:

### 1. Provision a fresh host

Ubuntu 22.04/24.04 VM (or Windows Server 2022, if that was your prod
choice). Same specs as prod.

### 2. Run the installer

```sh
git clone https://github.com/ninad-k/ExecRelay.git
cd ExecRelay
sudo bash scripts/install.sh
# DO NOT run configure-prod.sh yet — we restore data first.
```

### 3. Stop the app tier so it doesn't write while we restore

```sh
docker compose --profile apps stop
```

### 4. Pull the latest backup

```sh
# If you have S3 mirror:
aws s3 cp s3://YOUR-BUCKET/execrelay-LATEST.sql.gz /tmp/

# Otherwise — your off-site backup location.
```

### 5. Restore

```sh
gunzip -c /tmp/execrelay-LATEST.sql.gz \
  | docker compose exec -T postgres psql -U execrelay execrelay
```

### 6. Verify integrity

```sh
docker compose exec postgres psql -U execrelay -d execrelay -c "
  SELECT 'users' AS t, COUNT(*) FROM users
  UNION ALL SELECT 'licenses', COUNT(*) FROM licenses
  UNION ALL SELECT 'fills', COUNT(*) FROM fills
  UNION ALL SELECT 'recent_fills', COUNT(*) FROM fills WHERE created_at > now() - INTERVAL '7 days';
"
```

Sanity-check the numbers against your last-known prod metrics
(Grafana history, or your weekly business report).

### 7. Bring the stack back up

```sh
docker compose --profile apps up -d
```

### 8. Restore the perimeter

```sh
sudo DOMAIN=execrelay.example.com EMAIL=ops@example.com \
  bash scripts/configure-prod.sh
sudo bash scripts/install-backups.sh
```

### 9. Re-issue DNS

Point your A records at the new server's public IP. Caddy requests fresh
Let's Encrypt certs on first request to each domain.

### 10. Comm

Send a customer-facing status update. Note that **any signals received
during the outage were rejected by the old server's offline state** —
TradingView usually retries a few times, but anything older than that
window is gone. Customers should be advised to manually verify positions
with their broker.

---

## Partial failures

### Postgres data corruption / accidental DELETE

1. Identify the affected table(s) and the timestamp of the bad data.
2. Restore a backup to a **temporary database** (not the live one):
   ```sh
   docker compose exec postgres createdb -U execrelay execrelay_restore
   gunzip -c BACKUP.sql.gz \
     | docker compose exec -T postgres psql -U execrelay execrelay_restore
   ```
3. `COPY` or `INSERT … SELECT` the affected rows from the restore DB
   into live.
4. Verify and drop the restore DB.

### Kill switch tripped, customer impact

See [`docs/runbooks/kill-switch-tripped.md`](runbooks/kill-switch-tripped.md).

### NATS data loss

NATS JetStream stores its data in the `nats-data` Docker volume. If lost,
durable consumer subscriptions reset — consumers start from the latest
message. **Effect**: any unprocessed message in flight at the moment of
loss is gone. Since persist is the only thing whose state is the
authoritative record of fills, the practical impact is missing rows in
`fills` for trades that were *executed but not yet recorded*.

Recovery: re-subscribe consumers (handled automatically on bridge / persist
restart). Reconcile against broker positions manually if any fills are
missing.

---

## Runbooks for common incidents

See [`docs/runbooks/`](runbooks/):

- `ingress-5xx.md`
- `postgres-down.md`
- `kill-switch-tripped.md`
- `fills-not-arriving.md`
- `license-misconfigured.md`

---

## DR drill checklist

Run this list quarterly. Tick the box; record date in your DR log.

- [ ] Pull the most recent S3 backup and restore on a scratch machine.
- [ ] Verify row counts vs prod.
- [ ] Time the restore — record it. Are we within RTO?
- [ ] Try a portal-web login on the restored stack (proves schema works).
- [ ] Confirm `scripts/install-backups.sh` is still wiring the cron /
      systemd timer correctly (check `systemctl list-timers
      execrelay-backup.timer`).
- [ ] Confirm at least one off-host backup copy exists for the last 7 days.
- [ ] Confirm `.env` and `/etc/caddy/Caddyfile` are stored in a
      secure-but-recoverable location (password manager? infra-as-code repo?).
- [ ] Confirm DNS records' TTLs are short enough to re-point quickly
      (recommend ≤ 300 s on the A records).
- [ ] Walk through the "Total host loss" steps mentally and confirm
      everyone on the on-call rotation can execute them.

---

## Roles & escalation

| Role | Responsibility | When to escalate |
|---|---|---|
| **Primary On-Call** | DevOps / SRE On-Call Engineer | First responder for any DR scenario. |
| **Secondary On-Call** | Systems Administrator | Escalated if primary is unresponsive within 15 minutes. |
| **Engineering Lead** | Infrastructure Engineering Lead | Escalated if recovery progress exceeds the RTO target. |
| **Customer Communications** | Product Operations Lead | Triggered once downtime impact is confirmed and exceeds 15 minutes. |
| **Legal & Compliance** | Legal & Compliance Officer | Contacted immediately if data exposure is suspected or regulated data is unrecoverable. |

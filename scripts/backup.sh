#!/usr/bin/env bash
#
# scripts/backup.sh — nightly Postgres backup for ExecRelay.
#
# Runs `pg_dump` inside the postgres container, compresses with gzip, and
# rotates: keeps the last 7 daily backups and the last 4 weekly backups
# (taken every Sunday).
#
# Optional: set BACKUP_S3_BUCKET to also upload to S3. Requires `aws` CLI
# installed and credentials in /root/.aws or via env.
#
# Designed to be run from cron OR systemd timer (execrelay-backup.timer).
# Exits non-zero on any failure so the timer / cron can alert.

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/execrelay}"
RETAIN_DAILY="${RETAIN_DAILY:-7}"
RETAIN_WEEKLY="${RETAIN_WEEKLY:-4}"
S3_BUCKET="${BACKUP_S3_BUCKET:-}"

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

# Load POSTGRES_USER / POSTGRES_DB from .env so we don't hard-code dev defaults.
if [ -f "$REPO_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$REPO_ROOT/.env"; set +a
fi
PG_USER="${POSTGRES_USER:-execrelay}"
PG_DB="${POSTGRES_DB:-execrelay}"

ts=$(date -u +%Y%m%dT%H%M%SZ)
dst="$BACKUP_DIR/daily/${PG_DB}-${ts}.sql.gz"

log "dumping ${PG_DB} → ${dst}"
( cd "$REPO_ROOT" && \
  docker compose exec -T postgres pg_dump --no-owner --clean --if-exists -U "$PG_USER" "$PG_DB" ) \
  | gzip -9 > "$dst.tmp"
mv "$dst.tmp" "$dst"
chmod 600 "$dst"
ok "wrote $(du -h "$dst" | cut -f1) → $dst"

# Sunday → also copy to weekly
if [ "$(date -u +%u)" -eq 7 ]; then
  cp "$dst" "$BACKUP_DIR/weekly/${PG_DB}-${ts}.sql.gz"
  ok "weekly snapshot saved"
fi

# Rotate: keep the N newest in each tier.
log "rotating: keeping $RETAIN_DAILY daily, $RETAIN_WEEKLY weekly"
find "$BACKUP_DIR/daily"  -maxdepth 1 -type f -name "${PG_DB}-*.sql.gz" -printf '%T@ %p\n' \
  | sort -nr | tail -n "+$((RETAIN_DAILY + 1))" | cut -d' ' -f2- | xargs -r rm -v
find "$BACKUP_DIR/weekly" -maxdepth 1 -type f -name "${PG_DB}-*.sql.gz" -printf '%T@ %p\n' \
  | sort -nr | tail -n "+$((RETAIN_WEEKLY + 1))" | cut -d' ' -f2- | xargs -r rm -v

# Optional S3 upload
if [ -n "$S3_BUCKET" ]; then
  if ! command -v aws >/dev/null 2>&1; then
    warn "BACKUP_S3_BUCKET set but aws CLI not installed; skipping upload"
  else
    log "uploading to s3://$S3_BUCKET/"
    aws s3 cp "$dst" "s3://$S3_BUCKET/$(basename "$dst")" --no-progress
    ok "uploaded"
  fi
fi

ok "backup complete"

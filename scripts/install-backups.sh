#!/usr/bin/env bash
#
# scripts/install-backups.sh — install + enable nightly Postgres backups.
#
# Installs the execrelay-backup.service + .timer systemd units. After this,
# pg_dump runs every night at 03:15 UTC (with up to 10 min jitter) and
# rotates: 7 daily + 4 weekly. Output goes to /var/backups/execrelay/.
#
# Optional: set BACKUP_S3_BUCKET in the environment that systemd sees
# (drop a file in /etc/systemd/system/execrelay-backup.service.d/) to
# additionally upload each dump to S3.

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

require_root
require_ubuntu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
log "installing execrelay-backup.{service,timer} from $REPO_ROOT/infra/systemd"

sed "s|{{REPO_ROOT}}|$REPO_ROOT|g" "$REPO_ROOT/infra/systemd/execrelay-backup.service" \
  > /etc/systemd/system/execrelay-backup.service
install -m 0644 "$REPO_ROOT/infra/systemd/execrelay-backup.timer" \
  /etc/systemd/system/execrelay-backup.timer

mkdir -p /var/backups/execrelay/daily /var/backups/execrelay/weekly
chmod 700 /var/backups/execrelay

systemctl daemon-reload
systemctl enable --now execrelay-backup.timer

ok "backup timer enabled. Next run:"
systemctl list-timers execrelay-backup.timer --no-pager | head -3

cat <<NEXT

  To test the backup right now (won't wait for 03:15):
      sudo systemctl start execrelay-backup.service
      journalctl -u execrelay-backup -f

  To enable S3 upload, write a drop-in:
      sudo mkdir -p /etc/systemd/system/execrelay-backup.service.d
      sudo tee /etc/systemd/system/execrelay-backup.service.d/s3.conf <<EOF
  [Service]
  Environment=BACKUP_S3_BUCKET=your-bucket-name
  Environment=AWS_REGION=us-east-1
  EOF
      sudo systemctl daemon-reload
NEXT

#!/usr/bin/env bash
#
# scripts/dr-drill.sh — disaster-recovery rehearsal.
#
# Dumps the live ExecRelay Postgres, restores it into a scratch database,
# verifies row counts on the critical tables, and records the timings so we
# have an honest RTO/RPO number to put in the runbook.
#
# Usage:
#   scripts/dr-drill.sh \
#       postgres://user:pw@live-host:5432/execrelay \
#       postgres://user:pw@scratch-host:5433/execrelay_restore
#
# Both DSNs must already be reachable. The scratch DB must be empty (the
# script will drop+recreate the schemas inside it).
#
# Output is appended to docs/runbooks/dr-drill-log.md so successive drills
# build a track record. Exits non-zero if any check fails so the runbook
# entry is unmistakably a failure.

set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

LIVE_DSN="${1:?usage: dr-drill.sh <live-dsn> <scratch-dsn>}"
SCRATCH_DSN="${2:?usage: dr-drill.sh <live-dsn> <scratch-dsn>}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PATH="$REPO_ROOT/docs/runbooks/dr-drill-log.md"

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not installed; install postgresql-client" >&2
  exit 2
fi
if ! command -v pg_dump >/dev/null 2>&1; then
  echo "pg_dump not installed; install postgresql-client" >&2
  exit 2
fi

started_ts=$(date -u +%Y%m%dT%H%M%SZ)
tmp_dump="$(mktemp -t execrelay-dr-XXXXXX.sql.gz)"
trap 'rm -f "$tmp_dump"' EXIT

log "dr-drill: dumping live DB → $tmp_dump"
dump_start=$(date -u +%s)
pg_dump --no-owner --clean --if-exists --format=plain "$LIVE_DSN" | gzip -9 > "$tmp_dump"
dump_end=$(date -u +%s)
dump_secs=$((dump_end - dump_start))
dump_bytes=$(stat -c%s "$tmp_dump" 2>/dev/null || stat -f%z "$tmp_dump")
ok "dump complete: ${dump_bytes} bytes in ${dump_secs}s"

log "dr-drill: restoring into scratch DB"
restore_start=$(date -u +%s)
gunzip -c "$tmp_dump" | psql --quiet --single-transaction --set ON_ERROR_STOP=on "$SCRATCH_DSN" >/dev/null
restore_end=$(date -u +%s)
restore_secs=$((restore_end - restore_start))
ok "restore complete in ${restore_secs}s"

log "dr-drill: verifying row counts on critical tables"
verify_failed=0
# Tables we expect to find populated on a healthy live system. Empty live
# tables aren't necessarily failure (a fresh install has zero signals) but
# we record the numbers either way.
critical_tables=(licenses instances accepted_signals fills request_log dead_letter_messages)
declare -A live_counts scratch_counts
for table in "${critical_tables[@]}"; do
  live=$(psql --quiet --tuples-only --no-align "$LIVE_DSN" -c "SELECT COUNT(*) FROM $table" 2>/dev/null || echo "MISSING")
  scratch=$(psql --quiet --tuples-only --no-align "$SCRATCH_DSN" -c "SELECT COUNT(*) FROM $table" 2>/dev/null || echo "MISSING")
  live_counts[$table]="$live"
  scratch_counts[$table]="$scratch"
  if [ "$live" != "$scratch" ]; then
    warn "  $table: live=$live scratch=$scratch (DIFF)"
    verify_failed=1
  else
    log "  $table: $live rows OK"
  fi
done

total_secs=$((restore_end - dump_end + dump_secs))
finished_ts=$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$(dirname "$LOG_PATH")"
{
  echo
  echo "## $started_ts → $finished_ts"
  echo "- live: \`$(echo "$LIVE_DSN" | sed -E 's|://[^@]*@|://***@|')\`"
  echo "- scratch: \`$(echo "$SCRATCH_DSN" | sed -E 's|://[^@]*@|://***@|')\`"
  echo "- dump: ${dump_secs}s, ${dump_bytes} bytes"
  echo "- restore: ${restore_secs}s"
  echo "- **RTO (dump + restore)**: ${total_secs}s"
  echo
  echo "| table | live | scratch | match |"
  echo "|---|---|---|---|"
  for table in "${critical_tables[@]}"; do
    match="OK"
    if [ "${live_counts[$table]}" != "${scratch_counts[$table]}" ]; then
      match="**DIFF**"
    fi
    echo "| $table | ${live_counts[$table]} | ${scratch_counts[$table]} | $match |"
  done
} >> "$LOG_PATH"

if [ "$verify_failed" -ne 0 ]; then
  echo "::error::dr-drill row count mismatch; see $LOG_PATH"
  exit 1
fi
ok "dr-drill PASS — log appended to $LOG_PATH"

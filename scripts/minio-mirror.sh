#!/usr/bin/env bash
#
# scripts/minio-mirror.sh — mirror MinIO blob storage to AWS S3 for DR.
#
# MinIO holds the artifacts that the nightly Postgres dump does NOT cover:
# backtest result artifacts and MLflow model files. This script syncs the
# local MinIO buckets to an off-host S3 bucket so a host loss does not take
# those blobs with it.
#
# It uses the `aws` CLI with a MinIO endpoint override (MinIO speaks the S3
# API), so no extra tooling is required beyond what backup.sh already needs.
#
# Required env (typically sourced from .env):
#   MINIO_ENDPOINT        e.g. http://127.0.0.1:9000   (local MinIO S3 API)
#   MINIO_ACCESS_KEY      MinIO access key
#   MINIO_SECRET_KEY      MinIO secret key
#   MIRROR_S3_BUCKET      destination AWS S3 bucket (without s3:// prefix)
# Optional:
#   MINIO_BUCKETS         space-separated source buckets
#                         (default: "backtests mlflow")
#
# Designed to be run daily from cron OR a systemd timer. Exits non-zero on any
# failure so the timer / cron can alert.

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load MinIO / mirror config from .env if present.
if [ -f "$REPO_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$REPO_ROOT/.env"; set +a
fi

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://127.0.0.1:9000}"
DEST_BUCKET="${MIRROR_S3_BUCKET:?MIRROR_S3_BUCKET must be set (destination AWS S3 bucket)}"
BUCKETS="${MINIO_BUCKETS:-backtests mlflow}"

if ! command -v aws >/dev/null 2>&1; then
  die "aws CLI not installed; cannot mirror MinIO to S3"
fi
: "${MINIO_ACCESS_KEY:?MINIO_ACCESS_KEY must be set}"
: "${MINIO_SECRET_KEY:?MINIO_SECRET_KEY must be set}"

# A single `aws s3 sync` can't bridge two endpoints — `--endpoint-url` is
# global, so it would point the destination at MinIO too. We stage to a local
# temp dir (pull from MinIO with MinIO creds + endpoint override) and then push
# that staging dir to real AWS S3 with the ambient AWS credentials.
src_creds() { AWS_ACCESS_KEY_ID="$MINIO_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$MINIO_SECRET_KEY" "$@"; }

STAGING="$(mktemp -d -t execrelay-minio-mirror-XXXXXX)"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT

for bucket in $BUCKETS; do
  log "pulling minio://${bucket} → staging"
  mkdir -p "$STAGING/$bucket"
  src_creds aws --endpoint-url "$MINIO_ENDPOINT" \
    s3 sync "s3://${bucket}" "$STAGING/$bucket" --delete --no-progress \
    || die "pull from MinIO failed for bucket ${bucket}"

  log "pushing staging → s3://${DEST_BUCKET}/${bucket}/"
  # --delete keeps the mirror a faithful copy (objects removed in MinIO are
  # removed in S3). Drop --delete if you want the S3 side to be append-only.
  aws s3 sync "$STAGING/$bucket" "s3://${DEST_BUCKET}/${bucket}/" \
    --delete --no-progress \
    || die "push to S3 failed for bucket ${bucket}"
  ok "mirrored ${bucket}"
done

ok "minio mirror complete"

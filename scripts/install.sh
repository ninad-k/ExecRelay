#!/usr/bin/env bash
#
# scripts/install.sh — single-server installer for ExecRelay on Ubuntu 22.04/24.04.
#
# What this does:
#   1. Validates the host is Ubuntu 22.04 or 24.04.
#   2. Installs Docker Engine + the Compose plugin from Docker's official apt repo.
#   3. Generates .env from .env.example with strong random secrets where the
#      template has dev defaults (idempotent — won't overwrite an existing .env).
#   4. Pulls/builds all images and brings up the full stack
#      (`docker compose --profile apps up -d --build`).
#   5. Waits for postgres to be healthy, runs the migrate service to apply
#      every pending DB migration, then prints the running URLs.
#
# What this DOES NOT do (run scripts/configure-prod.sh for those):
#   - Reverse proxy + TLS in front of public endpoints (Caddy + Let's Encrypt).
#   - Firewall rules (UFW).
#   - Systemd unit to restart the stack on boot.
#   - Backups (run scripts/install-backups.sh).
#
# Usage (as root or with sudo, from the repo root):
#   sudo bash scripts/install.sh

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

require_root
require_ubuntu

# ---- 1. Docker -----------------------------------------------------------------
install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker + compose plugin already installed ($(docker --version))"
    return
  fi
  log "installing Docker Engine + compose plugin from docker.com"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  ok "Docker installed: $(docker --version)"
}

# ---- 2. .env with strong secrets -----------------------------------------------
write_env() {
  if [ -f .env ]; then
    ok ".env already exists; leaving it alone (delete it and re-run if you want fresh secrets)"
    return
  fi
  if [ ! -f .env.example ]; then die ".env.example not found in $(pwd); run this from the repo root"; fi
  log "generating .env from .env.example with random secrets"
  cp .env.example .env
  chmod 600 .env

  # Generate strong secrets for everything sensitive. These vars are read by
  # docker-compose.yml via ${VAR:-default} substitutions — see docker-compose.yml
  # for the full list.
  set_env_var .env POSTGRES_PASSWORD     "$(gen_secret 40)"
  set_env_var .env NATS_PASS             "$(gen_secret 40)"
  set_env_var .env MINIO_ROOT_PASSWORD   "$(gen_secret 40)"
  set_env_var .env INGRESS_PERIMETER_TOKEN "$(gen_secret 48)"
  set_env_var .env DEBUG                 "false"

  # The default DATABASE_URL in .env.example points at the dev password; rewrite
  # it so it uses the freshly-generated POSTGRES_PASSWORD.
  pg_pass=$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)
  set_env_var .env DATABASE_URL "postgresql://execrelay:${pg_pass}@postgres:5432/execrelay"
  # Inside the docker network NATS resolves as "nats", not localhost.
  set_env_var .env NATS_URL "nats://execrelay:$(grep -E '^NATS_PASS=' .env | cut -d= -f2-)@nats:4222"

  ok "wrote .env with random secrets (mode 0600)"
  warn "the EXECRELAY_LICENSES line in .env still contains TEST DATA. Replace it with your real licenses before going live."
}

# ---- 3. Build + run ------------------------------------------------------------
bring_up_stack() {
  log "building images (this can take a few minutes on first run)"
  docker compose --profile apps build --quiet

  log "starting infrastructure tier (postgres, nats, redis, minio, observability)"
  docker compose up -d
  log "waiting for postgres to become healthy"
  for i in $(seq 1 30); do
    status=$(docker compose ps postgres --format json | grep -oE '"Health":"[^"]*"' | head -1 || true)
    case "$status" in
      *healthy*) ok "postgres healthy"; break ;;
    esac
    sleep 2
    [ "$i" -eq 30 ] && die "postgres did not become healthy within 60s; check 'docker compose logs postgres'"
  done

  log "running migrations"
  if ! docker compose run --rm migrate; then
    die "migrations failed; see output above"
  fi

  log "starting application tier"
  docker compose --profile apps up -d
  ok "all services started"
}

# ---- 4. Print URLs -------------------------------------------------------------
print_urls() {
  cat <<URL_EOF

  ✓ ExecRelay is running.

    User-facing endpoints (bind on all interfaces — gate them with
    scripts/configure-prod.sh before exposing the box to the internet):

      Portal web   →  http://$(hostname -I | awk '{print $1}'):3001
      Portal API   →  http://$(hostname -I | awk '{print $1}'):8085
      Ingress      →  http://$(hostname -I | awk '{print $1}'):8081/webhook
      Grafana      →  http://$(hostname -I | awk '{print $1}'):3000   (admin / admin)
      Prometheus   →  http://$(hostname -I | awk '{print $1}'):9090
      Alertmanager →  http://$(hostname -I | awk '{print $1}'):9093

    Next steps:
      1. Replace EXECRELAY_LICENSES in .env with your real licenses
         (docker compose restart ingress to pick them up).
      2. For internet-facing deploys, run:
            sudo DOMAIN=your.domain.com EMAIL=you@example.com \\
              bash scripts/configure-prod.sh
      3. For nightly DB backups, run:
            sudo bash scripts/install-backups.sh

    Service status:  docker compose --profile apps ps
    Tail logs:       docker compose --profile apps logs -f
URL_EOF
}

# ---- main ----------------------------------------------------------------------
log "ExecRelay single-server installer"
install_docker
write_env
bring_up_stack
print_urls

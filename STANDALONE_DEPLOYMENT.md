# Single-server deployment

Run the entire ExecRelay stack — 9 application services plus Postgres, NATS,
Redis, MinIO, Prometheus, Grafana, Alertmanager, Tempo, MLflow — on **one
Ubuntu 22.04/24.04 box** via the installer scripts.

For dev (anywhere with Docker): see the [Local development](#local-development)
section at the bottom.

---

## Production install (fresh VM)

```bash
# 1. As root, clone the repo and run the bootstrap installer.
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/ninad-k/ExecRelay.git
cd ExecRelay

# 2. Bootstrap: installs Docker, generates .env with random secrets,
#    builds images, runs migrations, brings up the stack.
sudo bash scripts/install.sh

# 3. Harden for the internet: Caddy + Let's Encrypt TLS, UFW, systemd unit.
#    Requires DNS A records for your DOMAIN, api.DOMAIN, hook.DOMAIN,
#    admin.DOMAIN pointing at this server's public IP.
sudo DOMAIN=execrelay.example.com EMAIL=you@example.com \
  bash scripts/configure-prod.sh

# 4. Nightly Postgres backups (7 daily + 4 weekly rotation, 03:15 UTC).
sudo bash scripts/install-backups.sh
```

After step 4, the box is running ExecRelay in production. Caddy terminates
TLS, UFW blocks everything except 22/80/443, and the systemd unit restarts
the stack on reboot.

### What's where after install

| URL | Service | Auth |
|---|---|---|
| `https://DOMAIN` | Portal web (Next.js) | App-level (registration/login) |
| `https://api.DOMAIN` | Portal API | Bearer token from `/auth/login` |
| `https://hook.DOMAIN/webhook` | Trade webhook ingress | Per-license HMAC + optional perimeter token |
| `https://admin.DOMAIN` | Grafana | Basic-auth (password printed by `configure-prod.sh` and saved in `/etc/caddy/admin_password.txt`) |

Internal services (Postgres, NATS, Redis, MinIO, Prometheus, Alertmanager,
Tempo, MLflow, and the other app services) are bound to `127.0.0.1` by the
override file `docker-compose.override.yml` written by `configure-prod.sh`.
They are reachable from Caddy on the host but not from the public internet.

### Post-install checklist

1. **Replace test licenses.** `.env`'s `EXECRELAY_LICENSES` line contains a
   test UUID. Replace it with your real licenses, then:
   `docker compose restart ingress`
2. **Alert destinations.** Set `PAGERDUTY_INTEGRATION_KEY` and/or
   `SLACK_WEBHOOK_URL` in `.env`, then
   `docker compose restart alertmanager`.
3. **Save the Grafana password** that `configure-prod.sh` printed once, then
   `sudo shred -u /etc/caddy/admin_password.txt`.
4. **Test the backup.** `sudo systemctl start execrelay-backup.service` then
   `ls /var/backups/execrelay/daily/`.

---

## Operations cookbook

| Goal | Command |
|---|---|
| Tail all logs | `docker compose --profile apps logs -f` |
| Tail one service | `docker compose logs -f ingress` |
| Restart one service | `docker compose restart portal-api` |
| Apply a new migration | `docker compose run --rm migrate` (or `make migrate-up`) |
| See current status | `docker compose --profile apps ps` |
| Halt all trading NOW | `curl -X POST "https://hook.DOMAIN/admin/kill-switch?token=$TOKEN&state=on"` |
| Resume trading | `curl -X POST "https://hook.DOMAIN/admin/kill-switch?token=$TOKEN&state=off"` |
| Manual backup | `sudo systemctl start execrelay-backup.service` |
| List recent backups | `ls -la /var/backups/execrelay/daily/` |
| Restore from backup | `gunzip -c FILE.sql.gz \| docker compose exec -T postgres psql -U execrelay execrelay` |
| Pull a new release | `git pull && docker compose --profile apps up -d --build` |
| Upgrade Docker images | `docker compose --profile apps pull && docker compose --profile apps up -d` |

---

## Upgrading

```bash
cd /path/to/ExecRelay
git pull
docker compose --profile apps build
docker compose run --rm migrate         # apply any new schema changes
docker compose --profile apps up -d     # zero-downtime per service via compose
```

If a release notes a breaking config change, the upgrade notes will be in
`CHANGES.md`.

---

## Disaster recovery

1. Provision a fresh Ubuntu 22.04/24.04 box.
2. `git clone` the repo and run `scripts/install.sh` (don't run
   `configure-prod.sh` yet).
3. Drop your latest backup into `/var/backups/execrelay/daily/`.
4. Restore:
   ```bash
   gunzip -c /var/backups/execrelay/daily/execrelay-LATEST.sql.gz \
     | docker compose exec -T postgres psql -U execrelay execrelay
   ```
5. Now run `configure-prod.sh` with the same `DOMAIN` and re-issue DNS if
   the IP changed.

---

## Local development

For local dev (anywhere with Docker) you can skip the installer entirely:

```bash
cp .env.example .env                    # the defaults work for local
docker compose --profile apps up -d --build
```

The `apps` profile starts the 9 application services; without it you get
just the infrastructure tier. Compose binds every port to `0.0.0.0` in dev
so you can hit each service directly:

```
Ingress      → http://localhost:8081/webhook
Portal web   → http://localhost:3001
Portal API   → http://localhost:8085
Grafana      → http://localhost:3000   (admin / admin)
```

Stop everything: `docker compose --profile apps down`.

---

## Manual installation (without scripts)

If you need to deploy on a non-Ubuntu host or want full control, the
installer does roughly:

1. Install Docker Engine + the compose plugin.
2. Copy `.env.example` → `.env`, generate secrets, set
   `DATABASE_URL` and `NATS_URL` to use the new credentials.
3. `docker compose --profile apps up -d --build`.
4. `docker compose run --rm migrate`.

Reverse proxy, firewall, and systemd are entirely optional but recommended
for any internet-facing deployment. See `infra/caddy/Caddyfile.template`
and `infra/systemd/execrelay.service` for the templates the installer uses.

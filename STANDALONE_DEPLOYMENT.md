# Single-server deployment

Run the entire ExecRelay stack — 9 application services plus Postgres, NATS,
Redis, MinIO, Prometheus, Grafana, Alertmanager, Tempo, MLflow — on **one
host** via the installer scripts. Supported targets:

- **Ubuntu 22.04 / 24.04** — bash scripts under `scripts/*.sh`
- **Windows Server 2022** — PowerShell scripts under `scripts/*.ps1` that
  set up WSL2 + Ubuntu and call the same Linux installers underneath

For dev (anywhere with Docker): see the [Local development](#local-development)
section at the bottom.

---

## Production install on Ubuntu 22.04/24.04

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

## Production install on Windows Server 2022

All application containers are Linux images, so the Windows path runs them
inside **WSL2 + Ubuntu 22.04** and uses a Windows-native **Caddy** for TLS.
You're paying for Windows licenses to host Linux containers — if you have
no AD/group-policy/on-prem reason for Windows, deploy on Ubuntu instead.

Prereqs: Windows Server 2022 with **hardware virtualization enabled in
BIOS/UEFI** (required for WSL2). The bootstrap will fail-fast with a
clear message if it's not.

From an **elevated PowerShell prompt** (Run as Administrator):

```powershell
# 1. Clone the repo (just to get the scripts; the bootstrap will re-clone
#    inside WSL where it can run fast).
git clone https://github.com/ninad-k/ExecRelay.git
cd ExecRelay

# 2. Bootstrap WSL2 + Ubuntu 22.04 + Docker + .env + stack.
#    On a fresh host this REBOOTS after enabling Windows features;
#    re-run the same command after reboot — it's idempotent.
.\scripts\install.ps1

# 3. Harden: Caddy as a Windows Service, Windows Firewall rules, and a
#    boot-time Scheduled Task that brings the WSL stack up.
.\scripts\configure-prod.ps1 `
  -Domain execrelay.example.com `
  -Email  ops@example.com

# 4. Nightly Postgres backups (Scheduled Task wrapping the same backup.sh).
.\scripts\install-backups.ps1
```

After step 4 the box is internet-ready: Windows Firewall allows only
22/80/443 inbound, Caddy holds Let's Encrypt certs for your domains, and
the boot task brings the WSL stack back up after every restart.

### Windows-specific notes

| | |
|---|---|
| Stack lives in | `~/ExecRelay` **inside WSL** (`\\wsl$\Ubuntu-22.04\root\ExecRelay` from Windows Explorer) — not under `C:\` |
| WSL networking | Mirrored mode (`%USERPROFILE%\.wslconfig`) so services bound in WSL show up as `localhost` on Windows |
| Caddy install path | `C:\caddy\caddy.exe` (managed by the `ExecRelay-Caddy` Windows Service) |
| Grafana admin password | Printed once + saved to `C:\caddy\admin_password.txt` (delete after copying) |
| Auto-start at boot | Scheduled Task `ExecRelay-Stack-Startup` running as SYSTEM |
| Backups visible from Windows | `C:\backups\execrelay\` (symlink into the WSL filesystem) |
| Restart everything | `Restart-Service ExecRelay-Caddy; wsl --shutdown; wsl -d Ubuntu-22.04 -- echo ok` |
| Tail logs | `wsl -d Ubuntu-22.04 -- bash -lc 'docker compose --profile apps logs -f'` |

### Differences from the Ubuntu install

- **Two layers, two timers.** Both the Windows Scheduled Task and the
  Linux systemd timer fire the backup. The script writes uniquely
  timestamped files so duplicates don't conflict — it's defense in depth.
- **WSL2 has lower IO throughput** than bare-metal Linux (~10–20% in
  benchmarks). For most workloads this is invisible; if you're saturating
  Postgres, bare-metal Ubuntu is faster.
- **`docker compose --profile apps` runs inside WSL**, not on the
  Windows host. There is no Docker Desktop dependency.

### Uninstall (Windows)

```powershell
# Stop and remove the Windows Service
Stop-Service ExecRelay-Caddy; sc.exe delete ExecRelay-Caddy

# Remove scheduled tasks
Unregister-ScheduledTask -TaskName ExecRelay-Stack-Startup, ExecRelay-Postgres-Backup -Confirm:$false

# Remove firewall rules
Get-NetFirewallRule -DisplayName 'ExecRelay-*' | Remove-NetFirewallRule

# Optionally tear down the WSL distro (DESTROYS DATA — including DB):
wsl --unregister Ubuntu-22.04
```

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

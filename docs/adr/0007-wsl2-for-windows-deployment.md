# 7. Deploy on Windows Server via WSL2 instead of native Windows containers

Date: 2026-05-27
Status: Accepted

## Context

A customer requirement landed for **on-premises Windows Server**
deployment. Every service image in this codebase is a Linux image
(`golang:1.25-alpine`, `python:3.12-slim-bookworm`, `node:22-alpine`,
`scratch`). Three paths exist for running them on Windows:

1. **Native Windows containers**. Re-author every Dockerfile to use a
   Windows base image (Nano Server / Server Core). Run via Docker
   Engine for Windows in Windows-container mode.
2. **Docker Desktop for Windows**. Ships with WSL2 backend; Linux
   containers run unchanged. Commercial license required for orgs
   above the Docker Desktop free-tier threshold (>250 employees or
   >$10M revenue at time of writing).
3. **WSL2 + Docker Engine inside WSL**. Free for any use; Linux
   containers run natively.

### Cost of native Windows containers (option 1)

- Rewrite ~13 Dockerfiles.
- Cross-compile the Go services for Windows; rebuild C++ deps (NATS
  client) for Windows.
- Re-author the Python images on a Windows Python base (e.g.,
  `python:3.12-windowsservercore-ltsc2022`) which is significantly
  larger than `slim-bookworm` and slower to pull.
- Lose CI parity — the `app-<name>` workflows build Linux images;
  we'd need a parallel Windows build pipeline.
- Some libraries (Alpine-based base images, alpine-specific package
  managers) don't have a clean Windows analog.

Conservative estimate: **multiple weeks of work and ongoing
multi-platform maintenance for every PR that touches a Dockerfile.**

### Cost of Docker Desktop (option 2)

- Licensing fee for the operator's org.
- Doesn't fundamentally change the architecture — Linux containers still
  run via WSL2 underneath. We're paying for a GUI we don't use on a
  headless Windows Server.

### Cost of WSL2 + Docker inside (option 3)

- Add a WSL2 setup step to the installer (`Enable-WindowsOptionalFeature`,
  install Ubuntu distro, configure `/etc/wsl.conf` and `.wslconfig`).
- Run the existing Linux installer (`scripts/install.sh`) inside WSL.
- Wrap with Windows-side glue: Caddy as a native Windows Service for
  TLS, Scheduled Tasks for auto-start + backups.

We pay ~3 hours of PowerShell scripting (already done) and ~10–20%
disk-IO overhead vs bare-metal Linux. No other costs.

## Decision

Adopt option 3: **WSL2 + Ubuntu 22.04 + Docker Engine inside WSL** as
the Windows deployment story. Caddy runs as a Windows-native binary
(via winget) for TLS termination on 80/443; everything else runs
inside the WSL distro.

Implementation:

- `scripts/install.ps1` — Windows bootstrap (WSL features, Ubuntu
  distro, mirrored networking, run the Linux installer inside).
- `scripts/configure-prod.ps1` — Caddy install + Windows Firewall +
  Scheduled Task autostart.
- `scripts/install-backups.ps1` — Scheduled Task wrapping the existing
  Linux `scripts/backup.sh` via WSL.

## Consequences

**Positive**

- Single source of truth for Dockerfiles — the same images run on
  Linux, Windows-via-WSL, and Kubernetes.
- Same CI matrix (per-app workflows run only Linux builds).
- Same operational model — operators learn one set of `docker compose`
  + `kubectl` commands.
- Adds Windows as a target in ~3 hours of work, not weeks.
- No Docker Desktop license dependency.

**Negative**

- Operators pay for a Windows Server license to run Linux containers.
  Honest framing: if there's no AD / group policy / on-prem-mandate
  reason for Windows, **Ubuntu is the better choice**. We document
  this prominently in the Windows install path.
- WSL2 I/O is 10–20% slower than bare-metal Linux. For most workloads
  invisible; for high-throughput Postgres, bare-metal Ubuntu is
  faster.
- The deployment crosses an extra trust boundary (WSL VM ↔ Windows
  host). The PowerShell installer sets WSL2 mirrored networking
  (`%USERPROFILE%\.wslconfig`) so services bind to `localhost` on
  the host and the Windows Firewall can govern access.
- WSL2 mirrored mode requires recent Windows Server 2022 updates;
  older builds need port-proxy fallbacks (we don't ship that today —
  documented requirement).
- We can't easily test the Windows installer from a Linux CI runner.
  `windows-latest` runs PSScriptAnalyzer + parse checks in CI but
  doesn't end-to-end execute the installer.

## Notes for future ADRs

If Microsoft ships Linux containers as a first-class Windows Server
feature (without the WSL hop), revisit. As of decision date,
WSL2 + Docker-inside is the pragmatic answer.

If we get a Windows-only customer requirement that *can't* be satisfied
via WSL (rare — usually it's about regulatory checkboxes, not
technical needs), revisit option 1 (native containers) as a separate
SKU rather than the default.

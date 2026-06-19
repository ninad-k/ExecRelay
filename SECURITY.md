# Security policy

ExecRelay places real trades on real brokerage accounts. We take security
seriously and welcome reports from researchers, customers, and the public.

---

## Reporting a vulnerability

**Do not file public GitHub issues for security problems.** Report privately:

- **Email**: `security@reycapitalsfo.com`
- **PGP**: Not maintained at this time; report via email using standard TLS encryption.
- **Expected response time**: we will acknowledge within **2 business days** and
  provide a triage assessment within **5 business days**.

When reporting, please include:
1. A clear description of the issue and its impact.
2. Steps to reproduce (curl commands, request payloads, etc.).
3. The affected ExecRelay version (`git rev-parse HEAD` of your deploy if
   self-hosted, or describe the environment you observed it on).
4. Your name and how you'd like to be credited (or whether you prefer
   anonymity).

**Coordinated disclosure**: we ask that you give us a reasonable window
(typically 90 days) to ship a fix before public disclosure. We will keep you
updated on remediation progress and credit you in the changelog (unless you
opt out).

---

## Scope

| In scope | Out of scope |
|---|---|
| `apps/*` services (ingress, bridge, dxtrade, persist, portal-api, etc.) | Issues in third-party libraries already tracked by Renovate / Dependabot — please report upstream, but tell us if we're using a known-vulnerable version |
| `packages/*` shared libraries | Issues that require physical access to the host |
| `scripts/*` installers | Self-hosted misconfiguration (e.g., admin set HMAC to `password` — that's the operator's responsibility) |
| `infra/migrations/` SQL | DoS via volumetric traffic outside the application layer |
| Documented public endpoints (`/webhook`, `/health`, `/metrics`, `/admin/kill-switch`, portal-api routes) | Anything explicitly marked TODO or alpha in the code |

---

## Supported versions

| Version | Security fixes |
|---|---|
| `main` | Yes — fixes ship as new commits |
| Tagged releases ≥ v1.0 | Latest minor + one previous minor release (e.g., if current is v1.2, we support v1.2 and v1.1) |

We do not currently maintain LTS branches. If you're running an old release,
the recommended remediation for a published vulnerability is to upgrade.

---

## Security model

### Authentication layers (defense in depth)

The ingress endpoint applies the following checks in order before publishing
any signal. Each layer can be enabled independently.

| Layer | Control | Failure mode |
|---|---|---|
| **Network** | UFW (Linux) / Windows Firewall blocks all inbound except 22/80/443 | Connection refused at the OS |
| **Edge** | Cloudflare WAF (optional), Caddy TLS termination | TLS handshake failure |
| **Perimeter** | `INGRESS_PERIMETER_TOKEN` required as `?token=<value>` query param | 401 `perimeter_rejected` |
| **Replay** | `X-ExecRelay-Timestamp` must be within `WEBHOOK_TIMESTAMP_WINDOW_SECS` | 401 `timestamp_rejected` |
| **IP allowlist** | `WEBHOOK_ALLOWED_CIDRS` CIDR matching | 403 `ip_not_allowed` |
| **Rate limit** | Per-IP token bucket; `WEBHOOK_RATE_LIMIT` per minute | 429 `rate_limit_exceeded` |
| **License** | License ID must exist and be Active | 401/403 `license_rejected` |
| **Body secret** | `secret=<value>` parameter in the parsed signal | 401 `secret_rejected` |
| **HMAC signature** | `X-ExecRelay-Signature` (or `X-Signature`/`X-Hub-Signature-256`) verified against per-license `HMACSecret` | 401 `signature_rejected` |
| **Daily quota** | Per-license `MaxSignalsPerDay` | 429 `plan_limit_exceeded` |
| **Risk** | Exposure-limit check (Phase 7, requires DB) | 429 `exposure_limit_exceeded` |
| **Kill switch** | `INGRESS_TRADING_HALTED` env or `/admin/kill-switch?state=on` | 503 `trading_halted` |

`AuditLicenses()` runs at startup and on `SIGHUP` license reload, emitting
warnings + the `ingress_license_config_warnings{license_id, issue}` Prometheus
gauge for any license missing both HMAC and secret. **Treat that gauge as a
high-priority alert** — a license with `issue="no_auth"` accepts unauthenticated
webhooks from anyone who guesses the license ID.

### Trust boundaries

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#4-trust-boundaries) for the
diagram. Summary:

1. **Public internet** → only reachable services are Caddy (80/443).
2. **Edge / app tier** → ingress, portal-api, portal-web. Per-request auth
   required.
3. **Internal tier** → bridge, dxtrade, persist, etc. Not reachable from the
   public internet; trust their network but validate all payloads.

### Secrets handling

- All secrets are stored in `.env` with `chmod 600` (set by `scripts/install.sh`).
- Secrets are **never** baked into Docker images.
- The installer generates `POSTGRES_PASSWORD`, `NATS_PASS`, `MINIO_ROOT_PASSWORD`,
  and `INGRESS_PERIMETER_TOKEN` with `openssl rand` (Linux) /
  `RandomNumberGenerator.GetBytes` (Windows) — CSPRNG-backed, not `Math.random`.
- HMAC comparisons use `hmac.Equal` (Go) / `hmac.compare_digest` (Python) —
  constant-time, no timing side channel.
- Pre-commit runs `gitleaks` on every commit to catch accidentally-committed
  credentials. CI re-runs it on every PR.

### Audit logging

- `admin_audit_log` table records support / super-admin actions in portal-api.
- `audit_rejections` table records ingress signal rejections.
- `risk_breach_log` table records risk-limit breaches.
- All slog output (Go services) and Python `logging` output goes to stdout,
  collected by the container runtime, scrapable by Loki / CloudWatch.
- Kill switch toggles are `slog.Warn`-ed with the client IP. The supplied
  token is **never** logged.

### Threat model — high level (STRIDE)

| Threat | Vector | Mitigation |
|---|---|---|
| **Spoofing** | Attacker forges a TradingView alert | Per-license HMAC + perimeter token + IP allowlist |
| **Tampering** | MITM between TradingView and ingress | TLS required (Caddy), HMAC over body |
| **Repudiation** | Customer denies sending a signal | HMAC proves origin; `trace_id` propagated end-to-end into fills |
| **Information disclosure** | Cross-tenant data leak via portal-api | Every query JOINs on `licenses.user_id = $current_user`; tested in `apps/portal-api/` |
| **DoS** | Volumetric attack on `/webhook` | Per-IP rate limit, perimeter token gate, Caddy / Cloudflare in front |
| **Elevation of privilege** | Regular user → super_admin | RBAC checks in portal-api (`require_role()`); `admin_audit_log` records all privileged actions |

A fuller threat model lives at [docs/threat-model.md](docs/threat-model.md).

---

## Known limitations

- **No mTLS between services.** Internal-tier services trust their network. If
  your deployment exposes the Docker network to other untrusted workloads,
  enable mTLS via the NATS config + a service mesh.
- **NATS auth is username/password today**, not token-based. Suitable for a
  single-tenant deployment behind a firewall.
- **Per-IP rate limit is per-pod**, not cluster-wide. For high-traffic
  deployments behind a load balancer, Redis-backed rate limiting is recommended
  (groundwork exists in `apps/ingress/internal/ingress/counter.go`; cluster-wide
  Redis rate limiting is planned for Phase 7).
- **The kill switch is per-ingress instance**, not cluster-wide. If you run
  multiple ingress pods you must toggle each one (cluster-wide kill switch via
  Redis state is planned for Phase 7).
- **WSL2 deployment on Windows Server** crosses an extra trust boundary
  (WSL VM ↔ host). The PowerShell installer configures mirrored networking
  + firewall rules to keep service ports loopback-only, but you should
  treat the Windows host as part of the security perimeter.

---

## Cryptographic primitives in use

| Where | Algorithm | Why |
|---|---|---|
| Webhook HMAC | HMAC-SHA256 | Industry standard; supported natively by TradingView Pro+ |
| JWT signing (portal-api) | HMAC-SHA256 with `JWT_SECRET` | Simple, no key distribution needed |
| Password hashing (portal-api) | bcrypt | Adaptive; battle-tested |
| Random secret generation | `openssl rand` / `RandomNumberGenerator` | CSPRNG |
| TLS | Whatever Caddy chooses by default (TLS 1.3 + safe ciphers) | Caddy team is the expert here, not us |
| Inter-service auth | Network-layer (Docker bridge / k8s service network) + NATS user/pass | See "Known limitations" above for mTLS |

---

## Compliance & data handling

See [`docs/compliance.md`](docs/compliance.md) for data retention policy,
restricted jurisdictions, and regulatory considerations.

---

## Acknowledgements

We credit reporters in the [`CHANGELOG.md`](CHANGELOG.md) once a fix ships,
unless you ask us not to.

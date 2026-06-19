# Compliance

> **Important — this is a template, not legal advice.** ExecRelay's compliance
> posture depends on which jurisdictions you operate in, the products you offer,
> and your customer base. The decisions captured below **must** be reviewed
> with your legal counsel before publishing this document externally or
> relying on it for a regulated deployment.

This document captures the operational side of compliance: data retention,
audit logging, restricted-jurisdiction handling, and the processes you'd need
to demonstrate to an auditor.

---

## Regulatory framing

ExecRelay is configured to support compliance under the following regimes depending on deployment jurisdiction:
- **CFTC / NFA (US):** Under US regulations, specifically regarding trade recordkeeping requirements.
- **GDPR / CCPA / LGPD:** Personal data handling and user data rights management.
- **AML / KYC:** Handled at the brokerage level. ExecRelay does not hold customer funds or act as an execution counterparty, meaning it operates as trade routing infrastructure.

---

## Personal data handling (GDPR-style)

### What personal data we store

| Field | Where | Purpose | Retention |
|---|---|---|---|
| Email address | `users.email` | Login, password reset, support | Account lifetime. On account deletion the row is removed immediately (CASCADE); it persists only in backups until they age out of rotation. **Note:** post-deletion retention for audit purposes (soft-delete + scheduled purge) is *not yet implemented* — see "Data subject rights" below. |
| Password hash | `users.password_hash` | Authentication | Bcrypt; never logged; deleted on account deletion |
| IP address | `audit_rejections`, log files | Abuse detection, debugging | Per `RETENTION_DAYS` env (default 90 days) |
| License key | `licenses.license_key`, signal payloads | Tenancy & routing | License lifetime |
| Trade history | `fills`, `accepted_signals` | Customer-facing journal, reports | Per `RETENTION_DAYS` env (default 90 days) |

### Data subject rights

If your jurisdiction grants data-subject rights (access, rectification,
erasure, portability):

- **Access**: `GET /user/export` returns user details, active roles, licenses, instances, audit logs, and risk limits in machine-readable JSON format.
- **Erasure**: deleting a user CASCADE-deletes their licenses, instances,
  fills, and signals (the FK relationships in `infra/migrations/`
  enforce this). **Backups are NOT scrubbed retroactively** — document
  this in the privacy policy as "your data persists in backups for up to
  N days/weeks until they age out of rotation."
- **Portability**: same `/journal/export` endpoint; JSON format is
  machine-readable.

### What we don't store (intentionally)

- Real names, addresses, phone numbers — unless you add them. The schema
  doesn't have columns for them.
- Government ID, KYC documents — these belong with the broker, not with
  ExecRelay.
- Payment card data — ExecRelay does not process payments.

---

## Audit logging

| What | Where | Retention |
|---|---|---|
| Privileged admin actions (promote user, change limits, modify license) | `admin_audit_log` table | Retained indefinitely — append-only and **not** touched by the `RETENTION_DAYS` sweep (which only covers signals/fills/fingerprints). This satisfies the 5–7 year financial-audit norm; there is no automated purge today. |
| Webhook rejections (license, signature, quota, IP) | `audit_rejections` table | Per `RETENTION_DAYS` |
| Risk-limit breaches | `risk_breach_log` table | Per `RETENTION_DAYS` |
| Kill-switch toggles | `system_events` table (`event_type = 'kill_switch_toggled'`, with client IP, halted state, previous state) **and** slog `Warn` lines in container logs | `system_events` row: indefinite; log lines: per log aggregator retention |
| Signal lifecycle (accepted → routed → filled) | `accepted_signals` + `fills` joined by `trace_id` | Per `RETENTION_DAYS` |

Audit log retention is decoupled from operational data retention:
1. `admin_audit_log` is stored in an append-only Postgres table with database triggers preventing modification/deletion, and is never swept by the retention job. The trigger permits exactly one exception — the FK `ON DELETE SET NULL` that clears `actor_user_id`/`target_user_id` when a referenced user is erased (see migration `000005_audit_append_only_fk`), so the audit row survives a user deletion while its content stays immutable.
2. Kill-switch toggles are written to the `system_events` table (`event_type = 'kill_switch_toggled'`) by the ingress handler, so they are queryable rather than log-only.
3. **WORM archive (runbook provided, applied per environment):** the audit archive can be replicated to AWS S3 with Object Lock in COMPLIANCE mode (7-year retention) per [`infra/aws/AWS_SETUP.md`](../infra/aws/AWS_SETUP.md) §7a. This is cloud configuration applied per environment; committed Terraform for it is deferred to Phase 6 (see `infra/terraform/README.md`).

---

## Restricted jurisdictions

ExecRelay aims not to serve users located in OFAC comprehensively-sanctioned
jurisdictions (Iran, North Korea, Cuba, Syria, and the Crimea/Donetsk/Luhansk
regions of Ukraine).

- **Application Layer (implemented):** `/auth/register` screens the declared
  country code against `BLOCKED_REGISTRATION_COUNTRIES`
  (`{CU, IR, KP, SY}`, see `apps/portal-api/app.py`) and rejects with HTTP 451.
  Region-level blocks (Crimea/Donetsk/Luhansk) have no standalone ISO country
  code and are out of scope for this declared-country check.
- **Network Layer (runbook provided, applied per environment):** a Cloudflare
  WAF country rule blocks inbound requests from sanctioned regions at the edge,
  including the region-level cases (Crimea/Donetsk/Luhansk) the application
  check cannot see, and catches registrants who omit/misstate their country.
  The exact rule (country + Ukraine subdivision codes) is in
  [`infra/aws/AWS_SETUP.md`](../infra/aws/AWS_SETUP.md) §7c. It is cloud
  configuration applied per environment, not committed IaC.

---

## Marketing & onboarding claims

To maintain strict compliance, marketing copy and tagline wording must avoid terms like "guaranteed execution" or "best execution". Taglines are limited to accurate descriptions such as: "low-latency execution routing infrastructure for automated traders".

---

## Vendor & dependency tracking

ExecRelay depends on third-party services and libraries. For an audit
trail of "what's in the build":

- **SBOM**: each Docker image was previously scanned with Trivy + an
  anchore SBOM step in CI (now temporarily removed because
  `aquasecurity/trivy-action@0.28.0` no longer resolves; re-enable with
  a pinned newer version before any compliance review).
- **Dependency updates**: Renovate is configured in `renovate.json` to
  raise PRs for Go modules, npm packages, Docker base images, and GitHub
  Action versions.
- **Critical third-party services**:
  - Let's Encrypt (TLS certificates)
  - TradingView (upstream alert producer; not strictly a vendor — the
    customer chooses the producer)
  - Broker APIs (DXTrade, customer's MT4/MT5 broker) — out of our
    control; reliability is broker-by-broker

---

## Incident & breach notification

| Severity | Definition | Notification timeline | Audience |
|---|---|---|---|
| **Critical** | Data exposure, unauthorised trades placed, kill switch failed | Within 24 h | Affected customers, regulator (if required by your jurisdiction), engineering lead |
| **High** | Service outage > 1 h, persistent rejection of legitimate signals | Within 24 h | Affected customers, engineering lead |
| **Medium** | Latency degradation > 2× SLO, partial feature outage | Status page update | Public status page |
| **Low** | Single-user issue, transient blip | Support ticket only | Affected user |

Breach notification timelines comply with the following standards:
- **GDPR:** Within 72 hours from awareness for personal data breaches.
- **US State Laws (e.g., California CCPA):** Without unreasonable delay.
- **UK FCA:** Within 24 hours for material operational incidents.

---

## Vendor audit support

If a customer's compliance team requests an audit:

- This document.
- [`SECURITY.md`](../SECURITY.md) — auth, threat model, known limits.
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — system design.
- [`docs/observability.md`](observability.md) — monitoring & alerting
  coverage.
- [`docs/disaster-recovery.md`](disaster-recovery.md) — backup & DR.
- [`CHANGELOG.md`](../CHANGELOG.md) — release history.
- ADRs under [`docs/adr/`](adr/) — decision audit trail.
- Migration history under [`infra/migrations/`](../infra/migrations/) —
  schema evolution audit trail.
- CI logs in GitHub Actions — build & test evidence.
- Renovate dashboard issue — open vulnerability tracking.

For SOC 2 / ISO 27001 / similar formal certifications, this document is a starting point. Formal compliance audits must be conducted by external certified auditors.

---

## See also

- [`SECURITY.md`](../SECURITY.md) — security policy and threat model
- [`docs/data-model.md`](data-model.md) — what data we store and where
- [`docs/disaster-recovery.md`](disaster-recovery.md) — backups, retention
  in practice

# Compliance

> **Important — this is a template, not legal advice.** ExecRelay's compliance
> posture depends on which jurisdictions you operate in, the products you offer,
> and your customer base. Every section below has a `<!-- TODO -->` marker
> identifying decisions that **must** be made with your legal counsel before
> publishing this document externally.

This document captures the operational side of compliance: data retention,
audit logging, restricted-jurisdiction handling, and the processes you'd need
to demonstrate to an auditor.

---

## Regulatory framing

<!-- TODO: identify and document which of the following regimes apply to your
deployment. Common ones for a trading-execution platform:

  - **CFTC / NFA** (US) — if you operate or serve US-resident traders.
  - **FCA** (UK) — if you serve UK clients.
  - **MiFID II / ESMA** (EU) — if you serve EU clients.
  - **ASIC** (Australia) — if you serve Australian clients.
  - **MAS** (Singapore) — if you serve Singaporean clients.
  - **GDPR / CCPA / LGPD** — for personal data handling regardless of
    financial regulation.
  - **AML / KYC** — typically required by the broker, but if you do customer
    onboarding directly you may inherit obligations.

ExecRelay does not directly hold customer funds or act as a counterparty;
it is execution-routing infrastructure. That likely places it outside
most "broker" regulatory definitions, but legal counsel must confirm
for each jurisdiction you operate in.
-->

---

## Personal data handling (GDPR-style)

### What personal data we store

| Field | Where | Purpose | Retention |
|---|---|---|---|
| Email address | `users.email` | Login, password reset, support | <!-- TODO: confirm — typically: account lifetime + N years after account deletion --> |
| Password hash | `users.password_hash` | Authentication | Bcrypt; never logged; deleted on account deletion |
| IP address | `audit_rejections`, log files | Abuse detection, debugging | Per `RETENTION_DAYS` env (default 90 days) |
| License key | `licenses.license_key`, signal payloads | Tenancy & routing | License lifetime |
| Trade history | `fills`, `accepted_signals` | Customer-facing journal, reports | Per `RETENTION_DAYS` env (default 90 days) |

### Data subject rights

If your jurisdiction grants data-subject rights (access, rectification,
erasure, portability):

- **Access**: `GET /journal/export` already exposes a user's fills as
  CSV/JSON. Add an analogous endpoint for account profile + audit log.
  <!-- TODO: implement /user/export -->
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
| Privileged admin actions (promote user, change limits, modify license) | `admin_audit_log` table | <!-- TODO: 7 years is typical for financial; default `RETENTION_DAYS` of 90 is almost certainly too short for audit logs --> |
| Webhook rejections (license, signature, quota, IP) | `audit_rejections` table | Per `RETENTION_DAYS` |
| Risk-limit breaches | `risk_breach_log` table | Per `RETENTION_DAYS` |
| Kill-switch toggles | slog `Warn` lines in container logs (client IP, halted state, previous state). **NOT in a structured table today.** | Per log aggregator retention |
| Signal lifecycle (accepted → routed → filled) | `accepted_signals` + `fills` joined by `trace_id` | Per `RETENTION_DAYS` |

<!-- TODO: separate audit-log retention from operational-data retention.
Audit logs typically need to be kept much longer (5-7 years for most
financial regs) and stored in an append-only manner. Recommendations:

  1. Move admin_audit_log into a dedicated "audit-archive" bucket /
     immutable table outside the normal RETENTION_DAYS sweep.
  2. Add a `system_events` insert for kill-switch toggles so they're
     queryable, not just log-aggregator-only.
  3. Consider WORM (write-once-read-many) storage in S3 with Object
     Lock for the audit-archive bucket.
-->

---

## Restricted jurisdictions

<!-- TODO: list of countries you do NOT serve.

The platform doesn't enforce jurisdictional restrictions at the code
level today — there's no geo-IP block on registration or signal
ingestion. You probably want either:

  (a) Block at the registration step (portal-api `/auth/register`) based
      on the user's declared country, OR
  (b) Block at the network layer via Cloudflare WAF country rules, OR
  (c) Both.

Common restricted jurisdictions for execution platforms:
  - Countries on OFAC sanctions lists (US sanctions enforcement)
  - Iran, North Korea, Cuba, Syria, Crimea
  - Locally restricted: e.g., if you're not FCA-authorised, you can't
    offer execution services to UK retail clients

Document the rationale and the enforcement mechanism for each restricted
jurisdiction. -->

---

## Marketing & onboarding claims

<!-- TODO: confirm with legal/marketing. Common statements that need
care:

  - "Low latency" — fine; you have benchmarks to back it up.
  - "Guaranteed execution" — DO NOT say this; brokers reject orders.
  - "Algorithmic trading platform" — depending on jurisdiction this
    may trigger licensing obligations.
  - "Suitable for retail traders" — may trigger MiFID II
    appropriateness assessments in the EU.
  - "Best execution" — a specific term of art in MiFID II / FINRA; do
    not use casually.

The README's tagline "low-latency execution infrastructure for automated
traders" is intentionally accurate and conservative. Keep marketing
copy at that level of precision. -->

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

<!-- TODO: confirm regulatory breach notification timelines for each
jurisdiction you operate in.

  - GDPR: 72 hours from awareness for personal-data breaches.
  - Some US states (e.g., California): "without unreasonable delay".
  - UK FCA: within 24 hours for "material" operational incidents.
-->

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

For SOC 2 / ISO 27001 / similar formal certifications, this is a
starting point, not a complete control set. <!-- TODO: engage an
external auditor before claiming any formal certification. -->

---

## See also

- [`SECURITY.md`](../SECURITY.md) — security policy and threat model
- [`docs/data-model.md`](data-model.md) — what data we store and where
- [`docs/disaster-recovery.md`](disaster-recovery.md) — backups, retention
  in practice

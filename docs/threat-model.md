# Threat Model

This document outlines the detailed threat model for ExecRelay, utilizing the STRIDE methodology to identify threats and state corresponding mitigations across all system components.

---

## Threat Analysis (STRIDE)

### 1. Spoofing Identity
* **Threat:** An attacker sends forged webhook alerts pretending to be TradingView or another trusted alert source to trigger unauthorized trades.
* **Mitigation:** 
  * Ingress routes validate an optional perimeter query token (`INGRESS_PERIMETER_TOKEN`).
  * Ingress enforces a per-license HMAC signature validation (`X-ExecRelay-Signature`) computed with the license's shared secret.
  * Ingress can restrict webhook processing to defined CIDR blocks (`WEBHOOK_ALLOWED_CIDRS`).

### 2. Tampering with Data
* **Threat:** An attacker intercepts and modifies the alert payload in transit between TradingView and ExecRelay.
* **Mitigation:**
  * Strict HTTPS enforcement at Caddy (SSL/TLS termination).
  * Payload integrity verified via HMAC hashes over the request body.

### 3. Repudiation
* **Threat:** A user claims ExecRelay executed unauthorized trades or that they did not send a specific signal.
* **Mitigation:**
  * Every incoming webhook is stamped with a unique `trace_id` and recorded in the database.
  * `accepted_signals` and `fills` are correlated by `trace_id`, providing a complete trace audit trail from ingest to broker execution.
  * SHA256 hashes of all webhook bodies are logged in the `request_log` and `accepted_signals` tables.

### 4. Information Disclosure
* **Threat:** A tenant views or deletes another tenant's licenses, instances, configurations, or fills.
* **Mitigation:**
  * Multi-tenant database design: every client query in `portal-api` dynamically joins the target tables on `licenses.user_id = $current_user` to ensure database-level partition isolation.
  * Strict JSON web tokens (JWT) authentication for all user routes.

### 5. Denial of Service (DoS)
* **Threat:** An attacker floods the webhook endpoint, exhausting execution workers or connection pools.
* **Mitigation:**
  * Perimeter token validation checks happen before database lookups to prevent database connection exhaustion.
  * Token bucket rate limiting is applied at the application level (in-memory per-ingress pod; extensible to Redis in high-scale environments).
  * Connection limits and query timeouts (10 seconds) enforced in database pools.

### 6. Elevation of Privilege
* **Threat:** A regular user accesses administrative endpoints (e.g. promoting users, changing system-wide limit overrides).
* **Mitigation:**
  * Role-Based Access Control (RBAC) checked in `portal-api` (`require_role()` dependency).
  * Append-only `admin_audit_log` records all support or administrator actions. Mutation or deletion of audit logs is blocked by Postgres database triggers.

---

## Secrets Management

* **No Plaintext Secrets in Code:** Configuration and sensitive credentials (DB passwords, JWT secrets, NATS passwords) are exclusively managed via environment variables.
* **Development vs. Production:** portal-api fails fast and refuses to start in production (`ENV=production`) if default development secrets are detected.

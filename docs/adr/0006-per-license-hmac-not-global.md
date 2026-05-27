# 6. Per-license HMAC + body secret; perimeter token is *additional*, not primary

Date: 2026-05-27
Status: Accepted

## Context

The ingress endpoint needs to authenticate every webhook. Two
architectural styles are common:

1. **Global perimeter token** — a single shared secret the gateway
   checks; all callers send the same token.
2. **Per-caller credentials** — each license has its own secret(s);
   the request carries credentials specific to that license.

For a multi-tenant trading platform:

- A global token means **one leak compromises every customer** — anyone
  with the token can place trades for any license.
- A global token can't be rotated without coordinating with every
  customer simultaneously.
- A global token offers no per-customer audit, no per-customer rate
  limit, no per-customer kill switch.

Per-caller credentials trade more complexity (each customer must set
their own secret) for actual tenancy isolation.

## Decision

**Per-license credentials are the primary auth.** Each `licenses` row
has:

- `secret TEXT` — body-embedded `secret=<value>` parameter; works with
  TradingView's plain-text alert format (TradingView's only built-in
  auth mechanism).
- `hmac_secret TEXT` — HMAC-SHA256 key for the `X-ExecRelay-Signature`
  header; defends against body inspection and replay.
- `pending_hmac_secret TEXT` — supports zero-downtime HMAC rotation
  (both keys accepted until rotation is confirmed).

The ingress check order is:

1. Validate license exists and is `active`.
2. If `secret` is set, validate the body's `secret=` matches
   (constant-time compare).
3. If `hmac_secret` is set, validate the HMAC header (try primary,
   then pending).

**The perimeter token** (`INGRESS_PERIMETER_TOKEN`) is an *optional
additional layer* in front of per-license auth — not a replacement.
It exists for defense in depth:

- Reject obviously-malicious traffic before parsing the body.
- Block traffic from outside the customer's expected source if
  per-license HMAC isn't configured on every license.
- Protect against the case where one license has weak auth (the
  `AuditLicenses()` warning we ship as `ingress_license_config_warnings`).

The `/admin/kill-switch` endpoint requires the perimeter token *only*
— it's not per-license, so it has nothing per-license to authenticate
against. If `INGRESS_PERIMETER_TOKEN` is unset, the kill-switch
endpoint refuses to act (`kill_switch_disabled`), so a wide-open
ingress can't have its kill switch toggled by anyone on the network.

## Consequences

**Positive**

- A leaked credential affects exactly one license, not the platform.
- Each customer can rotate independently (`POST
  /licenses/{id}/rotate-hmac` + `POST /licenses/{id}/confirm-rotation`).
- Per-license rate limits and daily quotas are meaningful — the auth
  identifies the tenant.
- Audit logs in `audit_rejections` carry a real `license_id` for every
  rejected request.

**Negative**

- Customers have to configure their alert producer with the secret.
  TradingView's body-embedded `secret=` is workable; HMAC requires
  a webhook-signing proxy (we document this in
  [`docs/customer/webhook-integration.md`](../customer/webhook-integration.md)).
- A misconfigured license can accept unauthenticated webhooks
  (`no_auth` warning). We mitigate by:
  1. Surfacing as a Prometheus alert (`LicenseHasNoAuth`).
  2. Logging at startup and on SIGHUP reload.
  3. Recommending the perimeter token as belt-and-braces.
- Two auth mechanisms (secret + HMAC) instead of one. We keep both
  because they defend against different threats: secret is simple but
  visible in TradingView logs; HMAC is invisible but requires a
  signing proxy.

## Notes

The `LicenseAudit` infrastructure (function + Prometheus gauge) was
added specifically to make the per-license model auditable at scale —
"do all 200 of our customers have HMAC set?" must be answerable in one
PromQL query.

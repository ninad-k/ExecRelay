# Telegram notifications — feature brief

Owner: product · Date: 2026-07-24 · Status: **Shipped (backend v1 + portal
Settings panel)**

One-line: PineConnector-parity Telegram bot that messages traders the moment
a trade fills, fails, or times out.

---

## Problem & motivation

ExecRelay's target users are largely PineConnector migrants. PineConnector
ships a Telegram bot (<https://docs.pineconnector.com/telegram>) and users
treat it as table stakes: an unattended webhook pipeline needs a push
channel, because the failure mode of silence is discovering a rejected order
hours later on the broker statement. Before this feature the only feedback
surface was the portal, which nobody keeps open.

## What shipped (v1 scope)

- Self-serve account linking: deep-link/QR → bot → linked in ~10 seconds;
  unlink via bot (`/stop`) or portal API.
- One Telegram message per fill event — success ✅, broker rejection ❌,
  fill timeout ⏱ — identified by the user's own strategy tag, with ML
  confidence shown for ML-path signals and a trace id for support.
- Per-user preferences: trade results and timeout alerts independently
  toggleable.
- Self-hosted: each deployment runs its own bot (two env vars); the feature
  is dormant when unconfigured, so nothing changes for existing installs.

**Guarantee inherited from the architecture:** notifications are strictly
cold-path — a Telegram outage can never delay or drop a trade
([ADR 0009](../adr/0009-telegram-notifications-cold-path.md)).

## Explicitly out of scope (v1)

| Deferred | Why / trigger to revisit |
|---|---|
| Per-license or per-strategy routing | PineConnector parity is account-level; revisit if multi-account prop-firm users ask. |
| Two-way commands (close trade, kill switch from chat) | Security posture: the bot is read-only by design; a control channel needs its own threat-model review first. |
| Daily/weekly digest messages | `reports` service is the natural producer; needs the shared-channel refactor noted in the dev guide. |
| Additional channels (email, Slack, Discord) | Same pattern, `notifications_log` already channel-keyed; prioritise by demand. |
| Per-alert mute flag in the Pine payload | Requires a hot-path contract change → ADR first. |

## Success metrics

All measurable from existing tables — no new instrumentation needed:

| Metric | Source | Healthy signal |
|---|---|---|
| Link adoption | `telegram_links` with `chat_id IS NOT NULL` ÷ active users | ≥ 40 % of weekly-active traders within a quarter (PineConnector anecdata: most users link) |
| Delivery health | `notifications_log` `status='failed'` ratio, channel `telegram` | < 2 % (failures ≈ users who blocked the bot) |
| Notification latency | `notifications_log.created_at − fills.created_at` | p95 ≤ 30 s |
| Churn guard | unlink rate (`/stop` + DELETE) | spikes = message fatigue → prioritise digests/preferences |

## Rollout

1. **Now:** enabled per-deployment by ops (BotFather token + 2 env vars +
   auto-migration); linking is self-serve via the portal Settings → Telegram
   panel (QR / deep link) → announce in changelog.
2. **Later:** digest + additional triggers (risk breaches, EA disconnects)
   based on the metrics above.

No pricing/entitlement gating in v1 (PineConnector ties Telegram to premium
tiers; we can gate at the `POST /me/telegram/link` endpoint later — one
check against `plan_tiers` — without touching the delivery pipeline).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Telegram Bot API limits (~30 msg/s/bot) at scale | Per-cycle LIMIT 50 + 10 s cadence caps throughput far below the limit; revisit with >10k linked users |
| Users treat notifications as execution confirmation of record | Docs state the portal/broker statement is authoritative; timeout messages exist precisely to flag uncertainty |
| Bot token leak (operator-side) | Token grants messaging only, no account data; rotate at BotFather + env swap; linking tokens separately expire in 15 min |
| Single-replica constraint on `tasks` | Documented in ADR 0009; needs a leader lock before tasks is ever scaled out |
| Support load: "no messages arriving" | Troubleshooting section in the [user guide](../customer/telegram-notifications.md); every message carries a trace id, `notifications_log` is the audit trail |

## Team pointers

- Engineering: [`docs/development/telegram-notifications.md`](../development/telegram-notifications.md)
- Architecture decision: [ADR 0009](../adr/0009-telegram-notifications-cold-path.md)
- Design/UX: [`docs/design/telegram-notifications-ux.md`](../design/telegram-notifications-ux.md)
- API reference: [`docs/api/portal-api.md`](../api/portal-api.md#telegram-notifications)
- User guide: [`docs/customer/telegram-notifications.md`](../customer/telegram-notifications.md)

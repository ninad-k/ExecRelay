# 9. Telegram notifications as a cold-path consumer (bot in `tasks`, long-polling)

Date: 2026-07-24
Status: Proposed

## Context

Traders coming from PineConnector expect a Telegram bot that pings them when
a trade executes (<https://docs.pineconnector.com/telegram>). ExecRelay had
no push notification channel: the only way to see a fill was to open the
portal.

Constraints that shaped the design:

1. **The hot path is untouchable.** Per [`ARCHITECTURE.md`](../ARCHITECTURE.md)
   §3, TradingView → broker must never block on anything cold. Telegram's
   Bot API is a third-party dependency with multi-second worst-case latency
   and rate limits (~30 msg/s per bot) — it can never sit anywhere near
   `ingress`, `bridge`, or the EA.
2. **The schema already anticipated notifications.** `notifications_log`
   (migration 000001) exists precisely for sent-notification history with
   de-dup + audit, and the `tasks` service already polls the DB on the cold
   path ("tasks may notify the user" — ARCHITECTURE.md §5 step 10).
3. **Account linking must be self-serve and safe.** PineConnector's UX is a
   QR/deep link that opens the bot with a pre-filled `/start <token>`;
   users never type chat IDs by hand.
4. **Self-hosted deployments.** Each ExecRelay deployment runs its own bot
   (its own @BotFather token); nothing may assume a single central bot.

### Options considered

**Where the bot lives**

- **New `notifier` microservice** — cleanest isolation, but a 14th container
  for two async loops is operational overhead the compose/Helm stacks don't
  need; the tasks service is the designated home for periodic cold-path work.
- **`portal-api`** — already public-facing and FastAPI, but it's a
  request/response service; putting a perpetual long-poll loop inside a
  uvicorn worker couples bot liveness to API deploys and worker counts.
- **`tasks` (chosen)** — already runs `run_periodically` DB-polling loops,
  already has the DB pool, restarts cheaply, and a bot outage degrades
  nothing but notifications.

**How updates reach the bot**

- **Webhook mode** (`setWebhook`) — lower latency, but requires a new public
  HTTPS endpoint (Caddy route + auth + threat surface) on a service that is
  currently internal-only. Rejected: notifications are seconds-tolerant.
- **Long-polling `getUpdates` (chosen)** — zero public surface, works behind
  NAT, one outbound HTTPS connection. The 25 s long-poll keeps latency for
  `/start` handling near-instant in practice.

**What triggers a notification**

- **NATS consumer on `fills.*`** — reacts faster, but requires a durable
  consumer, reconnect handling, and a NATS client dependency in tasks; and
  it still needs the DB for the license → user → chat join.
- **DB polling of `fills` (chosen)** — the fill row written by `persist` is
  the durable source of truth (including synthetic `timeout` fills that only
  exist in the DB, never on NATS). A 10 s poll matches the feature's latency
  budget and reuses the service's existing pattern.

**De-dup / delivery state**

- In-memory watermark — lost on restart; double-sends or gaps.
- **`notifications_log` row per fill keyed on `payload->>'fill_id'`
  (chosen)** — restart-safe exactly-once-ish delivery, doubles as the audit
  trail the table was built for, and needs only a partial expression index
  (migration 000007). Fills older than `TELEGRAM_NOTIFY_LOOKBACK_SECS`
  (default 1 h) are never notified, which bounds the catch-up burst after
  downtime.

**Link identity**

- Per-license linking — finer-grained, but PineConnector parity is
  account-level, and per-license adds a second linking UX for no requested
  benefit. **Per-user chosen** (`telegram_links.user_id` PK); a partial
  unique index on `chat_id` enforces one account per chat. Preferences
  (`notify_fills`, `notify_timeouts`) live on the same row.

## Decision

Telegram notifications are a **cold-path DB consumer inside the `tasks`
service**, using **long-polling** against the Bot API, with **per-user
account linking** minted by portal-api:

- portal-api issues a 15-minute single-use `link_token`
  (`POST /me/telegram/link`) and builds the `https://t.me/<bot>?start=<token>`
  deep link (`TELEGRAM_BOT_USERNAME`).
- tasks runs two loops when `TELEGRAM_BOT_TOKEN` is set: an update poller
  that resolves `/start <token>` → `chat_id` (plus `/status`, `/stop`) and a
  fill notifier that joins
  `fills → licenses → telegram_links (LEFT JOIN accepted_signals, ml_decisions)`
  and records every send in `notifications_log`.
- Message content is derived entirely from data already persisted by the
  pipeline (command, symbol, `comment` strategy tag, broker order id,
  `prob_win`). **No new fields cross the hot path**, and the Pine script
  contract (ADR 0008) is unchanged.

## Consequences

- A Telegram/bot outage cannot affect trade execution; worst case is late or
  missing notifications, retried while fills remain inside the lookback
  window (transport errors leave no log row, so the next cycle retries;
  definitive Bot API rejections are logged as `failed` and not retried).
- Notification latency is bounded by `persist` lag + `TELEGRAM_NOTIFY_INTERVAL`
  (default 10 s) — fine for a human channel, not for machine consumption.
- The `getUpdates` offset is in-memory; a tasks restart replays un-acked
  updates. All command handlers are idempotent, so replays are harmless.
- One bot token per deployment implies one `tasks` replica doing Telegram
  work; if tasks is ever scaled horizontally, the Telegram loops need a
  leader lock (getUpdates from two consumers conflicts). Acceptable today —
  tasks is single-replica everywhere.
- Notification fan-out is per user, not per license; users with many
  licenses get all fills in one chat, disambiguated by the strategy tag.
- Adding channels later (email, Slack) can follow the identical pattern:
  another `channel` value in `notifications_log`, another poller loop.

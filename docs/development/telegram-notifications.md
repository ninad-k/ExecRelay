# Telegram notifications — developer guide

How the feature is put together, how to run it locally, and where to extend
it. Design rationale lives in
[ADR 0009](../adr/0009-telegram-notifications-cold-path.md); end-user setup
in [`docs/customer/telegram-notifications.md`](../customer/telegram-notifications.md).

---

## Code map

| Piece | Where | What |
|---|---|---|
| Link table | [`infra/migrations/000007_telegram_links.up.sql`](../../infra/migrations/000007_telegram_links.up.sql) | `telegram_links` (one row per user) + de-dup index on `notifications_log` |
| Link endpoints | [`apps/portal-api/app.py`](../../apps/portal-api/app.py) — search `Telegram notifications` | `POST /me/telegram/link`, `GET`/`PATCH`/`DELETE /me/telegram` |
| Bot + notifier | [`apps/tasks/app.py`](../../apps/tasks/app.py) — search `Telegram notifications` | `telegram_update_poller`, `telegram_fill_notifier`, `format_fill_message` |
| Tests | `apps/tasks/tests/test_telegram.py`, `apps/portal-api/tests/test_telegram.py` | Formatter, command handler, endpoint tests (stubbed pool, no network) |

No Go code changes: the hot path (`ingress`, `bridge`, EA) is not involved.

## Runtime model

```
portal-api                          tasks service
    │  POST /me/telegram/link           │
    │  mint link_token (15 min TTL)     ├── telegram_update_poller ──────────┐
    ▼                                   │   getUpdates long-poll (25 s)      │
telegram_links ◄────────────────────────┤   /start <token> → set chat_id     │ Telegram
    ▲                                   │   /stop → clear   /status → info   │ Bot API
    │ join                              │                                    │ (outbound
fills ── licenses ── users              ├── telegram_fill_notifier ──────────┤  HTTPS
    │  LEFT JOIN accepted_signals       │   10 s poll: new fills w/ linked   │  only)
    │  LEFT JOIN ml_decisions           │   chat & no notifications_log row  │
    ▼                                   │   → sendMessage → log row          │
notifications_log (channel='telegram')  └────────────────────────────────────┘
```

Both loops start only when `TELEGRAM_BOT_TOKEN` is set — without it the
service logs `telegram notifications disabled` and behaves exactly as
before.

### Delivery semantics

- **De-dup:** one `notifications_log` row per fill
  (`channel='telegram'`, `payload->>'fill_id'`), checked with `NOT EXISTS`
  and backed by a partial expression index. Restart-safe.
- **Retry:** a *transport* failure (socket error, timeout) writes no log row
  → retried next cycle, until the fill falls out of
  `TELEGRAM_NOTIFY_LOOKBACK_SECS` (default 3600 s). A *definitive* Bot API
  rejection (`ok:false`, e.g. user blocked the bot) writes `status='failed'`
  → never retried.
- **Ordering:** oldest-first, `LIMIT 50` per cycle. The lookback window
  bounds the worst-case catch-up burst after downtime.
- **Preferences:** `notify_fills` / `notify_timeouts` are applied in SQL, so
  suppressed fills never occupy the LIMIT.

### getUpdates offset

Kept in-memory (`_tg_offset`). On restart Telegram redelivers un-acked
updates; every handler is idempotent (`/start` re-links, `/stop` re-clears),
so replays are harmless. Do **not** run two tasks replicas with the same bot
token — concurrent `getUpdates` consumers conflict (see ADR 0009).

## Local development

1. Create a throwaway bot: message [@BotFather](https://t.me/BotFather),
   `/newbot`, copy the token and username.
2. `.env` (or shell env for `scripts/local-stack.sh`):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_BOT_USERNAME=MyDevExecRelayBot
   ```

3. `docker compose --profile apps up` (migration 000007 applies via the
   `migrate` service).
4. Link yourself:

   ```sh
   TOKEN=$(curl -s -X POST localhost:8085/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"you@example.com","password":"..."}' | jq -r .access_token)
   curl -s -X POST localhost:8085/me/telegram/link -H "Authorization: Bearer $TOKEN"
   # → open .deep_link on a device with Telegram, tap Start
   ```

5. Fire a signal (e.g. the demo E2E harness,
   [`docs/development/demo-e2e-test.md`](demo-e2e-test.md), or Pine TEST
   MODE) and watch the chat. A fill notification arrives within
   ~`TELEGRAM_NOTIFY_INTERVAL` seconds of the fill row landing.

To iterate on the bot **without** Telegram, point `TELEGRAM_API_BASE` at a
stub server that speaks the two methods used (`getUpdates`, `sendMessage`) —
that's also the seam the unit tests use (they monkeypatch `_tg_send` /
stub the pool; nothing in the test suite touches the network).

### Env reference

| Var | Service | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | tasks | *(empty = off)* | BotFather token; feature gate |
| `TELEGRAM_API_BASE` | tasks | `https://api.telegram.org` | Override for tests / self-hosted Bot API |
| `TELEGRAM_NOTIFY_INTERVAL` | tasks | `10` | Seconds between fill scans |
| `TELEGRAM_NOTIFY_LOOKBACK_SECS` | tasks | `3600` | Max fill age ever notified |
| `TELEGRAM_POLL_TIMEOUT` | tasks | `25` | getUpdates long-poll seconds |
| `TELEGRAM_BOT_USERNAME` | portal-api | *(empty = 503 on link)* | Builds the t.me deep link |
| `TELEGRAM_LINK_TTL_MINUTES` | portal-api | `15` | Link token lifetime |

## Extending

- **New notification triggers** (risk breaches, EA disconnects, daily
  summary): add a poller `run_periodically` loop over the relevant table
  (`risk_breach_log`, `system_events`, …), reuse `_tg_send` + a
  `notifications_log` template value for de-dup. Keep each trigger's de-dup
  key in `payload` and give it a partial index if the log grows.
- **New message fields:** extend `format_fill_message` (pure function —
  add a test per branch). Anything already in `accepted_signals.payload`
  params, `fills.payload`, or `ml_decisions` is available via the join.
- **New channels (email/Slack):** same pattern, different `channel` value in
  `notifications_log`; consider factoring the send loop before adding a
  third channel.
- **Per-alert muting from Pine:** would require carrying a new field through
  the flat parser or `/webhook/ml` — a hot-path contract change. Write an
  ADR first (see ADR 0008 for how the last field-carrying decision went).

## Testing

```sh
# Run per-app (same as CI); test basenames collide across apps if combined.
python -m pytest apps/tasks -q
python -m pytest apps/portal-api -q
python -m ruff check apps/tasks apps/portal-api
```

The migration pair is exercised by the standard `migrate` up/down flow
(`infra/migrations/README.md`).

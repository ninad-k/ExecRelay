# Telegram notifications

Get a Telegram message every time ExecRelay executes (or fails to execute)
one of your trades — the same flow you may know from PineConnector's
Telegram bot, but self-hosted with your own bot.

Audience: traders who already have signals flowing (see
[`webhook-integration.md`](webhook-integration.md)). Operators: see
"Deployment setup" at the bottom.

---

## What you'll receive

One message per fill event on any of your licenses:

```
✅ Order filled — BUY BTCUSD
Strategy: AlgoCombo
Volume: 0.1 lots
Order: 12345678
ML: prob_win 0.62 (OPEN_LONG)
Trace: 9f2c1a...
```

- ✅ **Filled** — the broker executed the order
- 📌 **Placed** — a pending order (limit/stop) was accepted by the broker but
  has not executed yet; you'll get a separate ✅ when it activates
- 🚫 **Cancelled** — a pending order was deleted or expired before activating
- ❌ **Rejected / error** — the broker refused it (message includes the error)
- ⏱ **Fill timeout** — no fill report arrived within the timeout window

The `Strategy` line is the `comment` / strategy tag from your alert — the
same tag the Pine script sends (`Comment / strategy tag` input in
[`pine/Combo_Webhook_Pine.pine`](../../pine/README.md)), so each strategy
is identifiable in the feed. Signals that went through the ML path
(`/webhook/ml`) also show the model's probability and decision.

---

## Linking your account

1. In the portal, call **`POST /me/telegram/link`** (portal UI: Settings →
   Telegram → Connect). You get back a one-time deep link like
   `https://t.me/YourExecRelayBot?start=<token>`.
2. Open the link on any device with Telegram and tap **Start**.
   The token is single-account and expires after 15 minutes — don't share
   the link.
3. The bot replies **"✅ Connection successful"**. Done — notifications are
   now on for every license under your account.

Check the link any time with **`GET /me/telegram`**, or by sending
`/status` to the bot.

### Bot commands

| Command | Effect |
|---|---|
| `/start <token>` | Link this chat (normally done via the deep link) |
| `/status` | Show link status and notification preferences |
| `/stop` | Unlink and stop all notifications |

### Notification preferences

`PATCH /me/telegram` with any of:

```json
{ "notify_fills": true, "notify_timeouts": false }
```

`DELETE /me/telegram` removes the link entirely (same as `/stop`).

Re-linking from a new chat simply moves the link — one Telegram chat per
account, one account per chat.

---

## Troubleshooting

**The link says "invalid or has expired".**
Link tokens are single-use and expire after 15 minutes. Generate a fresh
one (`POST /me/telegram/link`) and open the new link. Each new link
replaces the previous one.

**I tapped Start but the portal still shows "not connected".**
Give it a few seconds and refresh (`GET /me/telegram`). If it stays
unlinked, the bot most likely wasn't running when you tapped Start — ask
your operator to check that the tasks service logs show
`telegram notifications enabled`, then generate a new link.

**I'm linked but no messages arrive.**
In order of likelihood:

1. You blocked or deleted the chat with the bot — unblock it, or send
   `/start` again with a fresh link. Blocked-bot deliveries are recorded as
   `failed` and are **not** retried.
2. Your preferences filter them out — check with `/status`, fix with
   `PATCH /me/telegram`.
3. No fills are actually happening — confirm in the portal that signals are
   arriving and being executed. Notifications fire on **fills**, not on
   incoming alerts: an alert rejected by ingress (bad secret, quota, kill
   switch) never reaches Telegram.
4. The fill happened while notifications were down and is now older than
   the lookback window (1 hour by default) — such fills are skipped on
   catch-up by design, to avoid a wall of stale alerts.

**Messages arrive late.**
Normal delivery is within ~10–30 seconds of the broker fill (the notifier
scans on an interval). Consistently longer delays usually mean the tasks
service or database is under pressure — operator territory.

**I got a ⏱ timeout message — did my trade execute?**
Unknown, and that's exactly what the message means: the signal was accepted
but no broker confirmation arrived within the timeout window. Check the
broker terminal directly. The `Trace:` id in the message is what support
needs to investigate.

**How do I move notifications to a different phone/chat?**
Just link again from the new device — the old chat stops receiving
automatically. One chat per account.

## FAQ

**Can the bot place or close trades?**
No, by design. It's strictly one-way notifications; it will never ask you
for credentials or accept trade commands. Anything else claiming to be the
bot is not yours.

**Does this slow down my executions?**
No. Notifications are generated after the fill is already recorded, on a
separate cold path. A Telegram outage has zero effect on execution — see
the [architecture guarantee](../ARCHITECTURE.md#3-hot-path-vs-cold-path).

**One account, several strategies — how do I tell messages apart?**
Set a distinct `comment` / strategy tag per alert (in the Pine script: the
"Comment / strategy tag" input). It appears as the `Strategy:` line in
every message.

**Is the message the authoritative record of my trade?**
No — the broker statement and the portal (which read the same fills table)
are. Treat Telegram as a heads-up, not an audit trail.

**What data leaves my deployment?**
Only the message text you see (symbol, direction, status, your strategy
tag, trace id) — sent to Telegram's Bot API by your own bot. No secrets,
no account balances, no license keys.

---

## Deployment setup (operators)

The feature is off until both services get their env vars:

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), note
   the **bot token** and **bot username**.
2. Set in your `.env` / compose environment:
   - `TELEGRAM_BOT_TOKEN` — consumed by the **tasks** service, which runs
     the bot (long-polls `getUpdates`; no public webhook endpoint needed)
   - `TELEGRAM_BOT_USERNAME` — consumed by **portal-api** to build the
     deep link
3. Apply migration `000007_telegram_links` (runs automatically via the
   `migrate` service).

Tuning knobs (tasks service): `TELEGRAM_NOTIFY_INTERVAL` (default 10 s
between fill scans), `TELEGRAM_NOTIFY_LOOKBACK_SECS` (default 3600 —
fills older than this are never notified, which also bounds the catch-up
burst after downtime), `TELEGRAM_POLL_TIMEOUT` (long-poll seconds).

Notifications are cold-path only: the notifier reads `fills` after
`persist` has written them, and every sent message is recorded in
`notifications_log` (channel `telegram`) for de-dup and audit. A Telegram
outage can never affect trade execution.

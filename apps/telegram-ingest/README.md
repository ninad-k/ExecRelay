# telegram-ingest — Telegram channel → ExecRelay webhook adapter

Turns signal messages posted in a Telegram channel into ExecRelay flat
webhook commands, so channel signals execute like any TradingView alert.
**Ships in dry-run mode**: it logs the exact commands it would send and
trades nothing until you explicitly flip the switch.

```
Telegram channel ──getUpdates──► telegram-ingest ──POST /webhook──► ingress ─► ... ─► broker
                                  strict parser,
                                  fixed lot, dry-run
```

## Supported message format

Strict by design — anything that doesn't match is ignored; anything that
matches but is internally inconsistent (SL on the wrong side, second entry
outside the bracket, mismatched sides) is rejected and logged, and **no
order is placed**:

```
GOLD SELL @ 4099
SECOND SELL LIMIT @ 4109
SL @ 4119
TP @ 4089
```

- First line → by default a pending **limit order at the stated entry
  price** (`TELEGRAM_INGEST_ENTRY_MODE=limit`); set `market` to execute the
  first leg immediately instead. `SL`/`TP` attached as absolute prices.
  Note: brokers reject a limit order on the wrong side of the current
  market price (e.g. a buy limit above market), so in limit mode a signal
  quoted at a worse-than-market entry is rejected by the broker rather
  than chased.
- `SECOND <side> LIMIT|STOP @ <price>` (the `SECOND` prefix is optional) →
  pending order (`selllimit` etc.) with the same SL/TP.
- Lot size is **never** taken from the message — it is fixed by
  `TELEGRAM_INGEST_FIXED_LOT` (default `0.01`).
- Trailing commentary (risk tables, disclaimers, emojis) is ignored.

## Setup

1. The bot must be able to *read* the messages. Two supported layouts:
   - **You control the channel:** add the bot as a channel admin; it
     receives every post via `channel_post` updates.
   - **You follow someone else's channel:** Telegram bots cannot read
     channels they aren't in. Create a private group, add the bot, and
     forward (or auto-forward) the signals there. Driving a *user* session
     with automation (Telethon et al.) is possible but sits in a grey zone
     of Telegram's terms — this service deliberately only speaks the Bot API.
2. Find the chat id (forward a message to `@userinfobot`, or read this
   service's debug logs) and allowlist it.
3. Configure and start (compose profile `apps`):

| Env | Required | Default | Meaning |
|---|---|---|---|
| `TELEGRAM_INGEST_BOT_TOKEN` | yes | — | Bot API token (separate bot from the notifications one is fine) |
| `TELEGRAM_INGEST_ALLOWED_CHAT_IDS` | yes | — | Comma-separated chat ids; everything else is ignored |
| `TELEGRAM_INGEST_LICENSE_ID` | live mode | — | ExecRelay license used for the webhook commands |
| `TELEGRAM_INGEST_SECRET` | if license has one | — | Body-embedded alert secret |
| `TELEGRAM_INGEST_WEBHOOK_URL` | no | `http://ingress:8080/webhook` | Add `?token=...` if a perimeter token is configured |
| `TELEGRAM_INGEST_FIXED_LOT` | no | `0.01` | Lot size for every order this adapter places |
| `TELEGRAM_INGEST_ENTRY_MODE` | no | `limit` | `limit` = first leg rests at the stated entry price; `market` = first leg executes immediately |
| `TELEGRAM_INGEST_SYMBOL_MAP` | no | — | Channel jargon → canonical name, e.g. `GOLD=XAUUSD` (per-broker suffixes belong in the EA's `InpSymbolMap`) |
| `TELEGRAM_INGEST_DRY_RUN` | no | **`true`** | Log commands instead of POSTing them |
| `TELEGRAM_INGEST_COMMENT` | no | `tg-ingest` | Strategy tag on every order |

## Go-live checklist

1. Run in dry-run against the live channel for at least a few days; check
   the logs after every signal: exactly the right commands, every
   non-signal post ignored.
2. Point it at a **demo-account license** first
   (`scripts/` demo E2E runbook) and watch fills arrive in the portal and
   via Telegram notifications (📌 placed → ✅ filled for the limit leg).
3. Only then move to a live license. Ingress-side backstops still apply:
   daily quota, kill switch, exposure limits.

## Guarantees & limits

- One market + at most one pending order per message; edited posts and
  redelivered updates are dropped (bounded in-memory dedup, plus ingress
  duplicate/quota checks as the durable backstop).
- The adapter is an upstream producer like TradingView — it holds no DB
  access and cannot bypass any ingress auth or risk check.
- It never reads lot sizes, leverage, or "risk %" from the channel text.

> Copying someone else's signals is your own trading decision. This adapter
> is plumbing, not endorsement — nothing here is financial advice.

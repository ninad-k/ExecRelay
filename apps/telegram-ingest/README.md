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

## Supported message formats

Strict by design — anything that doesn't match is ignored; anything that
matches but is internally inconsistent (SL on the wrong side, a target on
the losing side, second entry outside the bracket, mismatched sides) is
rejected and logged, and **no order is placed**.

### A. Explicit "@" format

```
GOLD SELL @ 4099
SECOND SELL LIMIT @ 4109
SL @ 4119
TP @ 4089
```

- First line → by default a pending **limit order at the stated entry
  price** (`TELEGRAM_INGEST_ENTRY_MODE=limit`); set `market` to execute the
  first leg immediately instead. `SL`/`TP` attached as absolute prices.
  The entry line may state its own kind — `GOLD SELL LIMIT @ 4380` /
  `GOLD SELL STOP @ 4360` — and then that kind is placed regardless of
  `ENTRY_MODE`.
  Note: brokers reject a limit order on the wrong side of the current
  market price (e.g. a buy limit above market), so in limit mode a signal
  quoted at a worse-than-market entry is rejected by the broker rather
  than chased.
- `SECOND <side> LIMIT|STOP @ <price>` (the `SECOND` prefix is optional) →
  pending order (`selllimit` etc.) with the same SL/TP.

### B. "Trigger" format

```
XAUUSD Sell Trigger only Below 4792 📉
🛑 SL 4808 ⚠️
🎯 Target 4790 4788 4784 4770+ 🎯
```

- The **above/below keyword picks the pending order type**, so
  `TELEGRAM_INGEST_ENTRY_MODE` does not apply here:

  | Message | Order |
  |---|---|
  | Buy … above | `buystop` |
  | Buy … below | `buylimit` |
  | Sell … above | `selllimit` |
  | Sell … below | `sellstop` |

- The symbol must **lead a line** and be one this adapter knows
  (majors, metals, indices, BTC/ETH — see `KNOWN_SYMBOLS` in `app.py`).
  A caption line above the entry ("SIGNAL 4 🔽", "TRADE #12") is therefore
  fine; a symbol mentioned mid-sentence in a disclaimer is not, because it
  does not lead its line. No such line at all means "not a signal", never a
  guess.
- `SL 4808`, `SL: 4808` and `Stop Loss 4808` all work; so do
  `Target a b c+` and `TP1: a TP2: b TP3: c`.
- Common aliases are normalised before the operator map:
  `GOLD→XAUUSD`, `SILVER→XAGUSD`, `WTI→USOIL`, `BRENT→UKOIL`,
  `DJ30→US30`, `SPX500→US500`, `USTEC→NAS100`.
- A flat webhook command carries one `tp`, so a target ladder is reduced by
  `TELEGRAM_INGEST_TP_MODE`: `first` (nearest, default), `last`
  (furthest), or `ladder` — one order per target, **each at the full fixed
  lot, so N targets means N× the exposure**.

### C. "Entry range" format

```
Gold buy limit 4342 - 4339
SL 4336.00
Tp 4347
Tp 4355
Tp 4390
Tp Open
```

- The entry is a **zone, not a price**, so **both ends are traded** — one
  order at each, sharing the stated SL and the same target:

  | Order | Entry | SL | TP |
  |---|---|---|---|
  | `buylimit` | 4342 | 4336 | 4347 |
  | `buylimit` | 4339 | 4336 | 4347 |

  Sells mirror it exactly (`Gold sell limit 4360 - 4363` → `selllimit` at
  4360 and at 4363).
- **Which end leads is derived, not read off the message**: the leg price
  reaches first (higher on a buy limit, lower on a sell limit; the other way
  round for stops). `4339 - 4342` therefore places the same two orders as
  `4342 - 4339`.
- `limit` / `zone` / `area` / `range` in the line → limit orders; `stop` →
  stop orders; neither → `TELEGRAM_INGEST_ENTRY_MODE` decides, as in format A.
- Separators `-`, `–`, `—`, `/` and `to` all work.
- Targets may sit on their **own lines** (`Tp 4347`) as well as inline. They
  are sorted **nearest-first regardless of the order typed**, so the default
  `TELEGRAM_INGEST_TP_MODE=first` takes the closest target (4347 above).
  Lines with no price — `Tp Open` — are ignored.
- The whole message is rejected if the SL sits inside or beyond the zone:
  both legs are bracket-checked, not just the leading one.

### All formats

- Lot size is **never** taken from the message — it is fixed by
  `TELEGRAM_INGEST_FIXED_LOT` (default `0.01`), or split across the signal's
  orders when `TELEGRAM_INGEST_RISK_USD` is set.
- Emoji, risk tables, disclaimers and referral links are stripped/ignored.

## Setup

1. The bot must be able to *read* the messages. Two supported layouts:
   - **You control the channel:** add the bot as a channel admin; it
     receives every post via `channel_post` updates.
   - **You follow someone else's channel:** Telegram bots cannot read
     channels they aren't in. Create a private group, add the bot, and
     forward the signals there — by hand, or automatically with
     [`scripts/telegram_user_forwarder.py`](../../scripts/telegram_user_forwarder.py),
     which relays posts using your own account (MTProto/Telethon). This
     service itself only ever speaks the Bot API; the forwarder is a
     separate, optional script that runs on your machine under your account.
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
| `TELEGRAM_INGEST_ENTRY_MODE` | no | `limit` | `limit` = first leg rests at the stated entry price; `market` = first leg executes immediately. Ignored by trigger-format signals |
| `TELEGRAM_INGEST_TP_MODE` | no | `first` | Which target of a ladder to trade: `first`, `last`, or `ladder` (one order each) |
| `TELEGRAM_INGEST_SYMBOL_MAP` | no | — | Channel jargon → canonical name, e.g. `GOLD=XAUUSD` (per-broker suffixes belong in the EA's `InpSymbolMap`) |
| `TELEGRAM_INGEST_DRY_RUN` | no | **`true`** | Log commands instead of POSTing them. Boot default only — the trade dashboard's pipeline-bar switch overrides it at runtime (stored in the trade store, re-read here every ~10s), and that override wins until cleared |
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

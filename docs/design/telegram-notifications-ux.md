# Telegram notifications — UX specification

Design reference for the portal UI (Settings → Telegram) and for the bot's
conversational surface. The backend contract this designs against is
[`docs/api/portal-api.md`](../api/portal-api.md#telegram-notifications);
the message content rules bind `format_fill_message` in
[`apps/tasks/app.py`](../../apps/tasks/app.py).

Status: **implemented** — the portal panel lives at
[`apps/portal-web/app/dashboard/settings/page.tsx`](../../apps/portal-web/app/dashboard/settings/page.tsx)
(Settings → Telegram), alongside the bot surface and message formats below.

---

## 1. Linking flow (happy path)

```
Portal: Settings → Telegram                     Telegram app
┌──────────────────────────────┐
│ Telegram notifications       │
│ Status: ● Not connected      │
│                              │
│ [ Connect Telegram ]         │──POST /me/telegram/link
└──────────────────────────────┘
              │
              ▼
┌──────────────────────────────┐
│ Scan with your phone, or     │
│ open the link on this device │
│                              │
│   ▓▓ QR of deep_link ▓▓      │        ┌─────────────────────────┐
│   [ Open in Telegram ]       │───────►│ @YourExecRelayBot       │
│                              │        │ [ START ]               │
│ Link expires in 14:59 ⏳     │        └─────────────────────────┘
└──────────────────────────────┘                    │ /start <token>
              │                                     ▼
              │ poll GET /me/telegram   ┌─────────────────────────┐
              ▼ every 3 s while open    │ ✅ Connection successful.│
┌──────────────────────────────┐        │ This chat will now       │
│ Status: ● Connected          │        │ receive ExecRelay trade  │
│ Chat: 5512…89 · since 24 Jul │        │ notifications. Send      │
│ [ Disconnect ]  prefs below  │        │ /stop to unlink.         │
└──────────────────────────────┘        └─────────────────────────┘
```

Design rules:

- **QR + button, always both.** Desktop users scan; mobile users tap. The
  QR encodes exactly `deep_link` — nothing else.
- **Countdown, not surprise expiry.** Token TTL is 15 min; show remaining
  time. On expiry, swap in a "Generate new link" button in place — do not
  error.
- **Never show the raw token** as copyable text; it is a bearer credential
  for the linking step. Caption the QR: *"This link is unique to your
  account — don't share it."* (Same warning PineConnector uses.)
- **Confirmation is pull-based.** The portal polls `GET /me/telegram` while
  the connect panel is open and flips to Connected without a page refresh.
- Re-clicking Connect while already connected is allowed (rotates the token,
  relinks a new chat); the previous chat is silently replaced — state this
  in the confirm dialog: *"Connecting a new device will disconnect the
  current one."*

## 2. Settings panel — states

| State | Trigger | UI |
|---|---|---|
| Not configured | `POST /me/telegram/link` → 503 | Panel collapsed: "Telegram notifications aren't enabled on this deployment." (operator hint only for admin roles) |
| Not connected | `GET /me/telegram` → `linked:false` | Status dot grey + [Connect Telegram] |
| Connecting | link created, not yet linked | QR + countdown (§1) |
| Connected | `linked:true` | Status dot green, chat id (truncated middle), linked-since date, toggles, [Disconnect] |
| Disconnect confirm | click Disconnect | Modal: "Stop all Telegram notifications? You can reconnect any time." → `DELETE /me/telegram` |

Preference toggles (Connected state only), wired to `PATCH /me/telegram`:

- **Trade results** (`notify_fills`) — fills and rejections. Default on.
- **Fill timeouts** (`notify_timeouts`) — "signal accepted but no broker
  confirmation within the timeout window". Default on.

Toggles apply optimistically; on PATCH failure, revert and toast.

## 3. Bot conversational surface

The bot is deliberately **not** a control channel — it never places, closes,
or modifies trades, and never asks the user for credentials. Three commands,
all idempotent:

| Command | Reply (implemented copy) |
|---|---|
| `/start <token>` | ✅ Connection successful… / ❌ Link token is invalid or has expired… |
| `/status` | 🔗 Linked since `<date>` + both preference states |
| `/stop` | 🔕 Unlinked… / "This chat is not linked to any ExecRelay account." |
| anything else | Short help text listing the three commands |

Copy rules: first char is a status emoji, one idea per line, no markdown
formatting (messages are sent as plain text — resilient to symbols in user
data like strategy tags).

## 4. Notification message anatomy

```
✅ Order filled — BUY BTCUSD          ← line 1: outcome + command + symbol
Strategy: AlgoCombo                   ← alert's comment/strategy tag
Volume: 0.1 lots                      ← only if present on the signal
Order: 12345678                       ← broker order id, only if present
ML: prob_win 0.62 (OPEN_LONG)         ← only for /webhook/ml signals
Trace: 9f2c1a…                        ← always last; support handle
```

Outcome vocabulary (line 1):

| Emoji | When | Wording |
|---|---|---|
| ✅ | `status: filled` | `Order filled — <CMD> <SYMBOL>` |
| 📌 | `status: placed` — pending order accepted, not yet executed | `Order placed — <CMD> <SYMBOL>` (a separate ✅ follows on activation) |
| 🚫 | `status: cancelled` — pending order removed before activation | `Order cancelled — <CMD> <SYMBOL>` |
| ❌ | broker rejection / error | `Order <status> — <CMD> <SYMBOL>` + `Error: <message>` |
| ⏱ | synthetic timeout fill | `Fill timeout — <CMD> <SYMBOL>` (no Error line — the status is the error) |

Principles:

- **Scannable on a lock screen.** The first line alone must carry outcome,
  direction, and instrument; everything below is progressive detail.
- **Omit, don't placeholder.** Absent fields (no SL/TP, no ML, no order id)
  drop their line entirely — never "N/A".
- **Strategy tag is the user's own label** — surfaced verbatim so users
  running several charts can tell streams apart (this is the documented way
  to disambiguate; see [`pine/README.md`](../../pine/README.md)).
- **Trace id closes every message** so a screenshot is a complete support
  ticket.
- One message per fill event; no batching, no digests (out of scope v1 —
  see the [product brief](../product/telegram-notifications-brief.md)).

## 5. Edge cases & guardrails

- **Blocked bot:** delivery fails definitively; we do not surface this in
  Telegram (we can't) — the portal Connected panel shows an amber warning
  badge when `GET /me/telegram` reports `failed_last_24h > 0` or a `failed`
  most-recent delivery (implemented).
- **Relink from a second chat:** old chat silently stops receiving; new chat
  gets the confirmation. One chat per account, one account per chat — the
  DB enforces both.
- **Token pasted by hand** (`/start abc123` typed manually) works
  identically to the deep link — don't design against it, but don't break it.
- **i18n:** v1 is English-only; all user-facing strings live in two places
  (`_TG_HELP` + handler replies, and `format_fill_message`) so a locale pass
  touches only `apps/tasks/app.py`.
- **Accessibility:** emoji are prefixes, never the sole carrier of meaning —
  every status is also stated in words.

# Windows demo stack — `run.ps1`

The Windows-native way to run the full ExecRelay signal path against a live,
demo-logged-in MT5 terminal: Telegram channel → forwarder → ingest bot →
ingress → NATS → bridge → EA shim → broker, plus the public TradingView
webhook and the Rey Capital trade dashboard.

```powershell
.\run.ps1              # build + start everything, print links (public webhook on)
.\run.ps1 -LocalOnly   # same, but skip public-exposure verification
.\stop.ps1             # stop every service the stack started
scripts\local-stack.ps1 status   # health-check each component
```

`run.ps1` / `stop.ps1` are thin forwarders; the implementation is
`scripts\local-stack.ps1`.

## Services

| Service | Port | What it does |
|---|---|---|
| nats | 4222 | Message bus (JetStream) |
| ml-predictor | 8080 | Signal scoring |
| ingress | 8081 | Webhook receiver (TradingView + telegram-ingest) |
| bridge | 8082 | EA WebSocket hub |
| ea-shim | — | Executes signals in the running MT5 terminal |
| telegram-ingest | 8089 | Parses signal messages from the bot's chat |
| telegram-forwarder | — | Relays the source channel into the bot's chat using your personal account |
| trade-dashboard | 8090 | Rey Capital trade dashboard (localhost only) |

Logs are date-stamped under `.local-stack\logs\`; the JSONL audit trail of
every signal and order is under `.local-stack\logs\transactions\`.

## Public TradingView webhook

TradingView only delivers to ports 80/443, so ingress (8081) is exposed via a
one-time Windows portproxy (run as Administrator, persists across reboots):

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=80 connectaddress=127.0.0.1 connectport=8081
New-NetFirewallRule -DisplayName "ExecRelay ingress 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

plus an inbound TCP 80 rule in the cloud firewall (AWS security group).
`run.ps1` resolves the machine's public IP, verifies the portproxy, and
prints the webhook URL. Alert URL shape:

```
http://<public-ip>/webhook?token=<INGRESS_PERIMETER_TOKEN>
```

Alert body: `<license>,BUY,XAUUSD,risk=100,sl=...,tp=...,secret=<secret>` —
see `docs/customer/webhook-integration.md` for the full grammar.

## Risk sizing — max loss per trade

Position size is derived from the SL distance so a stopped-out trade loses a
fixed dollar amount, never more:

- `EA_SHIM_RISK_USD` (.env) — per-ORDER cap applied by the EA shim to any
  order that opens exposure (TradingView alerts included). If even the
  broker's minimum lot would risk more, the order is **rejected**.
- `TELEGRAM_INGEST_RISK_USD` (.env) — per-SIGNAL budget for Telegram
  signals. telegram-ingest splits it across every order the signal expands
  into (`risk=<budget/N>` per order), so a multi-leg signal still risks the
  budget in total, not per leg.
- Signals without a stop loss fall back to `TELEGRAM_INGEST_FIXED_LOT`.

## Telegram

- **Ingest bot**: reads the chat(s) in `TELEGRAM_INGEST_ALLOWED_CHAT_IDS`,
  parses the strict signal grammar, posts to ingress. Notifies the chat when
  an order is accepted, and (via the shim's position monitor) when a
  position opens and when it closes with realized P/L.
- **Forwarder** (`scripts/telegram_user_forwarder.py`): relays a channel you
  follow into the bot's chat using your own account. One-time login:
  `python scripts\telegram_user_forwarder.py login` (SMS code) or `qrlogin`
  (scan from the phone app — also works when code delivery is
  flood-limited). Configure `TG_FORWARDER_SOURCE_CHAT` (numeric id is
  rename-proof) and `TG_FORWARDER_TARGET_CHAT` in `.env`; the stack then
  starts it automatically.

## Trade dashboard

`http://127.0.0.1:8090` — Rey Capital-branded summary of the whole account:
signals received/routed, orders by source (Telegram / TradingView / other
EAs on the same account), open positions, closed trades with win/loss and
net P/L, and a per-trade **trading journal** (setup, emotion, mistakes,
rating, notes, reviewed) stored in `.local-stack\journal.json`. The journal
fields mirror the ReyLens `trades` schema so entries can be migrated there.

The dashboard binds to localhost only — it shows account balances and has no
auth. View it in an RDP session; do not expose the port.

## Broker symbol names

The CFI demo server suffixes instruments with an underscore (`XAUUSD_`, not
`XAUUSD`). `TELEGRAM_INGEST_SYMBOL_MAP` maps channel jargon to broker names,
and the EA shim additionally tries `_`, `.`, and `m` suffix variants when a
symbol is unknown — check `.local-stack\logs\ea-shim-*.log` if an order is
rejected with a symbol error.

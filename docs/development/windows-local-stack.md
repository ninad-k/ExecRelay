# Windows demo stack — `run.ps1`

The Windows-native way to run the full ExecRelay signal path against a live,
demo-logged-in MT5 terminal: TradingView webhook → ingress → NATS → bridge →
EA shim → broker, plus the public TradingView webhook endpoint and the Rey
Capital trade dashboard.

```powershell
.\run.ps1                     # start + follow logs; Ctrl+C stops the stack
.\run.ps1 -LocalOnly          # ...without public-exposure verification
.\run.ps1 -NoFollow           # start, print links, and return
.\run.ps1 -StopOnExit:$false  # Ctrl+C detaches; services keep running
.\stop.ps1                    # stop everything (e.g. from another window)
scripts\local-stack.ps1 status   # health-check each component
```

`run.ps1` stays attached after startup, streaming every service's log to the
console (color-coded per service, `.err.log` lines in red) until Ctrl+C —
which shuts the stack down with it. It refuses to double-start: a second
`run.ps1` (or a `run.ps1` while the stack is already up) attaches to the
logs instead, because duplicate services don't fail cleanly on Windows and a
duplicate ea-shim would execute every signal twice. Startup/stop behavior
lives in `scripts\local-stack.ps1`.

## Services

| Service | Port | What it does |
|---|---|---|
| nats | 4222 | Message bus (JetStream) |
| ml-predictor | 8080 | Signal scoring |
| ingress | 8081 | Webhook receiver (TradingView) |
| bridge | 8082 | EA WebSocket hub |
| ea-shim | — | Executes signals in the running MT5 terminal |
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

## Trade dashboard

`http://127.0.0.1:8090` — Rey Capital-branded summary of the whole account:
orders by source (TradingView / other EAs on the same account), open
positions, closed trades with win/loss and net P/L, and a per-trade
**trading journal** (setup, emotion, mistakes, rating, notes, reviewed)
stored in `.local-stack\journal.json`. The journal fields mirror the
ReyLens `trades` schema so entries can be migrated there.

The dashboard binds to localhost only — it shows account balances and has no
auth. View it in an RDP session; do not expose the port.

## Management reporting

The dashboard is backed by a SQLite store, `.local-stack\execrelay.db` (WAL
mode; module `scripts/_tradestore.py`) — every order the EA shim
places/reports, every closed position, and a periodic account-equity
snapshot lands there, correlated by ExecRelay's `trace_id`. Not the
production persistence path (that's `apps/persist/app.py` via NATS) —
dev-harness tooling only, same tier as `_txnlog.py`.

```powershell
python scripts\_tradestore.py backfill   # idempotent import from JSONL txn logs + 90d MT5 closed-deal history
python scripts\_tradestore.py stats      # row counts per table
```

### Channel scorecard

`GET /api/scorecard?days=N` and a "Channel scorecard" table on the
dashboard: two synthetic reconciliation rows, **tradingview** (orders with
`source=tradingview`) and **other EAs** (`closed_trades.source=other` — the
rest of the account, e.g. other EAs or manual trades), each with orders
executed and win/loss/net P/L/avg R of the closed outcomes, so the section
totals reconcile against the whole account. (The underlying query also joins
against a legacy `signals` table from a since-removed signal-ingest path;
that table is no longer populated, so it never contributes rows today.)

The order → closed-trade join is a heuristic: `orders.broker_order_id ==
closed_trades.position_id`, which only resolves once MT5 reports a market
fill's close. Pending limit orders, or fills the closed-trade recorder
hasn't caught up to yet, show as **open/pending** rather than a win or loss.

### Risk & exposure panel

Embedded in `/api/summary` under `"risk"`, and separately at `GET
/api/risk?days=N`:

- **Equity curve** — from `equity_snapshots` over the selected window. With
  fewer than 2 snapshots in the window (e.g. a fresh stack), it falls back
  to a synthetic curve — current balance minus each day's closed P/L
  reverse-cumulated backwards — clearly flagged `"estimated": true` in the
  JSON and labeled "estimated" under the chart. The estimate ignores
  floating P/L on open positions and any deposits/withdrawals.
- **Max / current drawdown %** computed off that curve.
- **Live margin usage** (`margin`, `margin_free`, `margin_level` from MT5
  `account_info`) and total open lots.
- **Compliance strip**: the `EA_SHIM_RISK_USD` cap value, the count and list
  of risk-sized orders (`orders.requested_risk IS NOT NULL`), the count of
  risk-cap rejections (`orders.status='rejected' AND error LIKE 'risk
  sizing%'`), and the max single-order `requested_risk` in the window vs the
  cap.

### Monthly P/L calendar

`GET /api/calendar?month=YYYY-MM&stack_only=1` and a Mon–Sun grid on the
dashboard: each day colored by net closed P/L (profit/loss-dim background),
day number + net amount, month total in the header, prev/next navigation.
Data is `closed_trades` grouped by UTC close date; the "stack only" /
"all sources" toggle filters out `source=other` (other EAs / manual trades
on the same account) or includes everything.

### Console digest

`python scripts\trade_dashboard.py --digest-now` prints a plain-text summary
(period, trades closed W/L, net P/L, per-source split, floating P/L now,
equity now vs period start, margin level, risk-cap rejection count) to the
console — useful for a quick check without opening the dashboard UI:

```powershell
python scripts\trade_dashboard.py --digest-now                    # prints today's/weekly digest text
python scripts\trade_dashboard.py --digest-now --period-days 7    # prints a 7-day digest
```

### Weekly XLSX export

`GET /api/export/weekly.xlsx` (also linked next to the CSV exports) builds
a workbook with four sheets — **Summary** (the digest numbers), **Closed
Trades** (the window), **Scorecard**, and **Equity** (snapshots) — using
`openpyxl`. This is the one dashboard dependency beyond the stdlib:

```powershell
python -m pip install openpyxl --quiet --disable-pip-version-check
```

If `openpyxl` isn't installed, the route responds `501` with a message
telling you to install it rather than erroring the whole dashboard.

## Broker symbol names

The CFI demo server suffixes instruments with an underscore (`XAUUSD_`, not
`XAUUSD`). The EA shim tries `_`, `.`, and `m` suffix variants when a symbol
is unknown — check `.local-stack\logs\ea-shim-*.log` if an order is
rejected with a symbol error.

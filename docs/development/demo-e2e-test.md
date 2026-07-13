# Demo-account end-to-end test (MT5, local stack)

How to verify the full signal path — TradingView-shaped alert → `ingress`
auth → XGBoost scoring → NATS JetStream → `bridge` → broker execution →
fill reporting — against a **live MT5 demo account**, entirely on one
machine with no Docker and no cloud dependencies.

First executed 2026-07-13 against ICMarkets demo (hedge) on BTCUSD; every
case below passed. Results table at the bottom.

## Prerequisites

- MetaTrader 5 terminal **running and logged into a demo account**. The
  shim hard-refuses non-demo accounts (`ACCOUNT_TRADE_MODE_DEMO` check),
  but don't rely on that alone — use a demo terminal.
- Python 3.11+ with `pip install MetaTrader5 websockets` (plus the
  `apps/ml-predictor` requirements for the predictor).
- Go toolchain (a portable unzip of go.dev's archive works fine).
- No Docker needed — everything runs natively via `scripts/local-stack.sh`.

## 1. Start the stack

```bash
# License with NO hmac (TradingView-style body-secret auth): the third
# field is empty. The default local-stack license requires HMAC.
EXECRELAY_LICENSES="60000000001:test-secret::test-instance:mt5" \
  scripts/local-stack.sh start
scripts/local-stack.sh status   # all four components healthy
```

Defaults: NATS :4222, ml-predictor :8080, ingress :8081 (shadow mode),
bridge :8082.

## 2. Connect the execution side

Two options:

**Real EA** (production path): compile `ea/mt5/ExecRelay.mq5` and attach it
per [`ea/mt5/README.md`](../../ea/mt5/README.md) — inputs `127.0.0.1`,
`8082`, `test-instance`, `test-bridge-token`, and allow `127.0.0.1` in
Tools → Options → Expert Advisors → WebRequest list.

**Python shim** (no GUI needed): plays the EA's role over the same
WebSocket protocol, executing via the official MetaTrader5 python API:

```bash
python scripts/ea_shim.py
# 08:48:38 attached to DEMO account 52634101 (Raw Trading Ltd), ...
# 08:48:38 REGISTERED with bridge
```

Either way, the bridge log (`.local-stack/logs/bridge.log`) shows
`EA registered instance_id=test-instance` on success.

## 3. Run the decision matrix

`scripts/e2e_demo_matrix.py` fires one case per invocation and asserts the
resulting MT5 position state. Cases depend on the ingress/predictor mode —
run them in this order, restarting services between mode switches (kill the
process on the port, relaunch with the new env):

| # | Case | Required mode | Expected |
|---|---|---|---|
| 1 | Shadow-mode buy passthrough | `ML_ENFORCE=false` (default) | scored, published anyway → BUY opens |
| 2 | Same-direction ignore | `ML_ENFORCE=true` | `NOTHING` → `status:skipped`, no trade |
| 3 | Flip on opposite signal | enforce + `ML_THRESHOLD=0.05` | `FLIP_SHORT` → long closes, short opens |
| 4 | Close-only | enforce + `ML_THRESHOLD=0.99` | `CLOSE_ONLY` → short closes, no re-entry |
| 5 | Filtered entry while flat | enforce + `ML_THRESHOLD=0.99` | `NOTHING` → skipped, still flat |
| 6 | Predictor down | enforce, predictor **stopped** | **fail-open**: trade executes, `ml.error` set |
| 7 | Legacy flat path | any | `/webhook` closelong executes |

The sequence is position-neutral: it opens, flips, and closes 0.01-lot
positions and ends flat. All orders carry magic `20240101` so they never
touch positions owned by anything else.

```bash
python scripts/e2e_demo_matrix.py case1   # repeat per case
```

## 4. TradingView → local delivery

TradingView webhooks fire from TradingView's **cloud**, so `127.0.0.1` is
unreachable — choose one:

- **Tunnel** (real delivery): expose the local ingress with a tunnel
  (e.g. `cloudflared tunnel --url http://127.0.0.1:8081`) and use
  `https://<tunnel>/webhook/ml` as the alert's webhook URL. The license
  secret is the only auth TradingView can supply — treat tunnel URLs as
  temporary and tear them down after testing.
- **Alert-log replay** (no exposure): create the alert with no webhook URL,
  copy the fired alert's JSON from TradingView's alert log, and POST it to
  the local ingress yourself.

The Pine script ([`pine/Combo_Webhook_Pine.pine`](../../pine/Combo_Webhook_Pine.pine))
emits the ExecRelay-native `/webhook/ml` body directly (license/secret
inputs) and has a TEST MODE input that fires alternating buy/sell every
closed bar — see [`pine/README.md`](../../pine/README.md).

## Results — 2026-07-13, ICMarkets demo 52634101, BTCUSD

All 7 cases passed. Broker deal history for the run (magic 20240101):

```
11:49:09  BUY  in   0.01 @ 62710.70   e2e-demo-test      (case 1)
11:52:08  SELL out  0.01 @ 62711.17   execrelay-close    (case 3 flip: close long)
11:52:43  SELL in   0.01 @ 62704.87   matrix-sell        (case 3 flip: open short)
11:55:34  BUY  out  0.01 @ 62722.90   execrelay-close    (case 4 close-only)
11:55:49  BUY  in   0.01 @ 62715.58   matrix-buy         (case 6 fail-open)
11:55:50  SELL out  0.01 @ 62714.38   execrelay-close    (case 7 flat path)
```

Model `6bd309cb2fe0` scored the fixture payload at `prob_win=0.2986`
throughout; threshold pinning (0.05 / 0.99) forced the pass/fail branches.

## Gotchas learned the hard way

- **WebSocket keepalive**: the `websockets` client's protocol-level ping
  can time out and kill the connection (the bridge relies on app-level
  ping/pong instead). The shim disables it (`ping_interval=None`), runs
  MT5 calls in an executor, and auto-reconnects. JetStream redelivery
  covered the gap when this bit us mid-run.
- **NATS startup race**: ingress/bridge exit immediately if NATS isn't
  accepting yet; `local-stack.sh` now waits for the NATS port before
  starting them.
- **HMAC vs TradingView**: TradingView cannot compute HMAC signatures.
  Any license used for TV alerts must have an empty `hmac_secret` (body
  `secret` remains). Keep HMAC for everything that can sign.

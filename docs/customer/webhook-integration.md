# Webhook integration guide

End-to-end walkthrough: from "I just signed up" to "my TradingView alert
just placed an order on my MT5 account."

Audience: traders setting up their first ExecRelay account. No code
knowledge required for the basic flow; HMAC is optional but recommended.

---

## What you'll need

- An **ExecRelay portal account** ([`docs/api/portal-api.md`](../api/portal-api.md))
- A **broker terminal** (MT4, MT5, or DXTrade) with an Expert Advisor slot
- A **TradingView alert** with the "Webhook URL" feature (available on
  TradingView Pro+ plans)
- 15 minutes

---

## Step 1 — Create a license

Log into the portal, navigate to **Licenses → New License**. Set:

- **Name** — any human-readable label ("My MT5 prop account")
- **Daily signal cap** — leave unlimited unless you want a quota; can be
  changed later
- **HMAC** — leave the "Generate HMAC secret" box checked (recommended);
  you can also use just the body-embedded secret if you can't compute
  HMAC in your alert producer

On creation, the portal shows you **three values you must save now** —
they are only displayed once:

| Value | What it's for |
|---|---|
| **License ID** | UUID; first field of every alert body |
| **Body secret** | `secret=<value>` in alert body |
| **HMAC secret** | Used to sign the alert body |

If you lose them, you can rotate them — but every active integration
breaks until you re-sign with the new value.

---

## Step 2 — Create an instance

An instance represents one broker terminal. From the License page,
**Instances → New Instance**:

- **Instance key** — short identifier ("mt5-prop"). Becomes part of the
  NATS routing key.
- **Platform** — `mt5`, `mt4`, or `dxtrade`
- **Region** — your region (e.g., `iad` for US East)

For `dxtrade`, you also enter the broker credentials. For MT4/MT5, the
EA will connect with the license/instance pair instead.

---

## Step 3 — Install the EA (MT4 / MT5 only)

Skip this step if you're using DXTrade.

### MT5

1. Copy `ea/mt5/ExecRelay.mq5` to your `MQL5/Experts/` folder.
2. Open MetaEditor, compile (`F7`).
3. Drag `ExecRelay` onto a chart.
4. In the EA properties dialog, paste:
   - **License ID**
   - **Instance ID**
   - **Bridge URL** (e.g., `wss://bridge.yourdomain.com/ws`)
5. Allow WebRequest URLs in MT5's Tools → Options → Expert Advisors.
6. Confirm the EA shows ✓ ("Connected to bridge") in the chart corner.

### MT4

1. Build `ea/mt4-ws-dll/ExecRelayWS.dll` (or use the pre-built signed
   binary from your account dashboard). Copy to `MQL4/Libraries/`.
2. Copy `ea/mt4/ExecRelay.mq4` to `MQL4/Experts/`, compile.
3. Allow DLL imports in MT4 (Tools → Options → Expert Advisors → "Allow
   DLL imports").
4. Drag the EA onto a chart, paste the License ID + Instance ID + bridge
   URL into the inputs.
5. Confirm ✓.

If the EA doesn't connect, see
[`docs/runbooks/fills-not-arriving.md`](../runbooks/fills-not-arriving.md).

---

## Step 4 — Configure your TradingView alert

In TradingView's alert dialog:

1. **Webhook URL**: `https://hook.<your-domain>/webhook?token=<perimeter>`
   - The `?token=` part is only required if your ExecRelay operator
     configured `INGRESS_PERIMETER_TOKEN`. Ask them; if unset, omit the
     query string.
2. **Message** (body):
   ```
   <LICENSE_ID>,<COMMAND>,<SYMBOL>,vol_lots=<SIZE>,sl_pips=<SL>,tp_pips=<TP>,secret=<BODY_SECRET>
   ```

### Worked example

```
60123456789,BUY,EURUSD,vol_lots=0.10,sl_pips=20,tp_pips=40,secret=abc123
```

This places a 0.10-lot BUY on EURUSD with a 20-pip stop loss and 40-pip
take profit, authenticated by the body secret `abc123` for license
`60123456789`.

### Supported commands

| Command | What it does |
|---|---|
| `BUY` | Market buy of `vol_lots` size |
| `SELL` | Market sell |
| `CLOSE` | Close any open position on `<symbol>` |
| `CLOSEALL` | Close all positions for this instance |
| `BUYLIMIT` / `SELLLIMIT` | Pending limit order at `entry=<price>` |
| `BUYSTOP` / `SELLSTOP` | Pending stop order at `entry=<price>` |
| `MODIFY` | Adjust SL/TP on existing position |

### Supported parameters

| Key | Type | Notes |
|---|---|---|
| `vol_lots` | float | Lot size (required for opens) |
| `sl_pips` | float | Stop loss in pips |
| `tp_pips` | float | Take profit in pips |
| `entry` | float | Entry price (limit/stop orders) |
| `comment` | string | EA passes through to broker order comment |
| `secret` | string | Body-embedded auth (if license has one) |
| `instance` | string | Override the default instance for this license |

---

## Step 5 — Optional but strongly recommended: HMAC signing

The body-embedded `secret=` is visible in TradingView alert logs and in
any HTTP proxy logs along the path. HMAC adds a signature over the body
that an attacker can't forge even with the body in hand.

### TradingView doesn't natively sign

TradingView itself can't compute HMAC. To use HMAC, either:

- **Use a webhook-signing proxy** (e.g., a cheap AWS Lambda function the
  alert posts to first, which adds the `X-ExecRelay-Signature` header
  and forwards). Sample Lambda code:
  ```python
  import hmac, hashlib, json, urllib.request
  HMAC_SECRET = "your-hmac-secret"
  INGRESS_URL = "https://hook.example.com/webhook?token=..."

  def handler(event, _):
      body = event["body"].encode()
      sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
      req = urllib.request.Request(
          INGRESS_URL, data=body, method="POST",
          headers={
              "Content-Type": "text/plain",
              "X-ExecRelay-Signature": f"sha256={sig}",
          },
      )
      with urllib.request.urlopen(req) as r:
          return {"statusCode": r.status, "body": r.read().decode()}
  ```
- **Skip HMAC and rely on body secret + IP allowlist** (`WEBHOOK_ALLOWED_CIDRS`
  on the operator side, set to TradingView's egress IPs).

For programmatic alert producers (your own bot, a custom script), HMAC
is trivial — see the example in
[`docs/api/ingress.md#hmac-computation`](../api/ingress.md#hmac-computation).

---

## Step 6 — Test it

The portal has a **Test Signal** button on each license. It fires a
synthetic alert end-to-end and shows you the resulting trace:

- ✓ Ingress accepted
- ✓ Bridge routed to instance
- ✓ EA received and called OrderSend
- ✓ Fill returned

If any step fails, the trace shows you where. See also
[`docs/runbooks/fills-not-arriving.md`](../runbooks/fills-not-arriving.md).

You can also fire a test from the command line:

```sh
curl -X POST 'https://hook.example.com/webhook?token=PERIMETER' \
  -H 'Content-Type: text/plain' \
  -d '60123456789,BUY,EURUSD,vol_lots=0.01,secret=YOUR_BODY_SECRET'
# {"status":"accepted","trace_id":"3a..."}

# Then check what happened:
curl -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/traces/3a...
```

---

## Step 7 — Save the journal

For accounting, tax, or audit purposes, export your fill history:

```sh
curl 'https://api.example.com/journal/export?from=2026-01-01&to=2026-04-01&format=csv' \
  -H "Authorization: Bearer $TOKEN" \
  -o my-trades-Q1-2026.csv
```

Or use the **Journal → Download** button in the portal UI.

---

## Common gotchas

| Symptom | Likely cause | Fix |
|---|---|---|
| `400 parse_error` | Body has extra whitespace, wrong field order | Compare to the working example; license ID must be first, comma-separated |
| `401 secret_rejected` | The `secret=` value doesn't match the license | Re-copy from the portal; check no trailing whitespace |
| `401 signature_rejected` | HMAC computed over the wrong bytes | Body must be exactly what's POSTed; no JSON wrapping, no trailing newline |
| `403 ip_not_allowed` | Your alert producer's IP isn't in the operator's CIDR allowlist | Coordinate with operator to add it (TradingView publishes its egress IPs) |
| `429 rate_limit_exceeded` | Your alert producer is firing too fast | Either rate-limit your producer or ask operator to raise `WEBHOOK_RATE_LIMIT` |
| `429 plan_limit_exceeded` | License hit its daily cap | Raise `max_signals_per_day` via the portal or wait for the day boundary (UTC) |
| `503 trading_halted` | Operator tripped the kill switch | Check with operations; do not retry |
| `503 publish_failed` | NATS is down on the operator side | This is an operator incident; TradingView will retry — be patient |
| `200 accepted` but no fill | EA not connected, or signal didn't match the EA's instance ID | Check EA chart for the connection ✓; verify instance ID matches |

For deeper debugging, every accepted webhook returns a `trace_id`. Look
it up via `GET /traces/{trace_id}` to see exactly where in the pipeline
the signal stopped.

---

## Webhook security checklist

Before going live with real money:

- [ ] License has an HMAC secret configured (`no_hmac` warning resolved)
- [ ] License has a body secret configured (`no_secret` warning resolved)
- [ ] If publicly routable, perimeter token (`INGRESS_PERIMETER_TOKEN`)
      is set and you're sending it
- [ ] Replay window (`WEBHOOK_TIMESTAMP_WINDOW_SECS`) is enabled on the
      operator side and your alerts include `X-ExecRelay-Timestamp`
- [ ] You have a documented runbook for "what do I do if my position
      gets out of sync with the broker?"
- [ ] You know how to reach the operator's kill switch in an emergency

---

## See also

- [`docs/api/ingress.md`](../api/ingress.md) — webhook endpoint reference
- [`docs/api/portal-api.md`](../api/portal-api.md) — managing licenses
  programmatically
- [`docs/glossary.md`](../glossary.md) — domain terms
- [`docs/runbooks/`](../runbooks/) — what to do when something goes wrong

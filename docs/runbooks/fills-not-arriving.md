# Runbook: Signals accepted, fills not arriving

## Symptom

- Customer: "ingress returned 200 but my account shows no trade"
- `bridge_fills_received_total` flatlined while
  `bridge_signals_dispatched_total` keeps climbing
- `bridge_consumer_lag_pending` rising for some specific NATS subject

## The trace_id is your friend

Every accepted webhook returns a `trace_id`. **Always start the
investigation here**:

```sh
# From the customer's HTTP response (or their TradingView "alert log")
TRACE_ID=abc123def456...

# What does the system know about this trace?
docker compose --profile apps logs | grep -F "\"trace_id\":\"$TRACE_ID\"" | head -30
```

You'll see one of three patterns:

### Pattern 1: ingress accepted, nothing else

```
ingress  ... "msg":"signal published successfully" trace_id=abc...
```

…and nothing from bridge / dxtrade / persist. The signal was published
to NATS but no consumer picked it up.

**Check**:
- Which NATS subject was used? The log line above includes `subject`.
- Is there a consumer for it? `curl -s http://localhost:8222/jsz?streams=1`
- If `subject=signals.mt5.LICENSE.INSTANCE` and bridge has no
  subscription, bridge isn't running or isn't subscribed.

### Pattern 2: ingress + bridge, no EA fill

```
ingress  ... "trace_id":"abc..."
bridge   ... "msg":"dispatching to EA" instance=mt5-a
bridge   ... "msg":"no connected EA for instance" instance=mt5-a
```

The EA is **disconnected**. Either:

- The customer's MT4/MT5 terminal is closed.
- The EA was removed from the chart.
- The EA can't connect to bridge (WebSocket URL wrong, certificate
  expired, firewall on the customer side).

**Check**:
```sh
curl -s http://localhost:8082/metrics | grep bridge_ea_connections_active
# Should be > 0 if anyone is connected
```

Customer remediation: have them open MT4/MT5, ensure the EA is on a
chart with a green ✓, and that the bridge URL in EA properties is right.

### Pattern 3: ingress + bridge + EA, broker rejected

```
ingress  ... "trace_id":"abc..."
bridge   ... "dispatching to EA"
bridge   ... "fill received" status=rejected error_code=10018
```

The EA called OrderSend but the broker rejected. Common
`error_code` values are MT5's enum
([docs](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes)):

| Code | Name | Likely cause |
|---|---|---|
| 10006 | TRADE_RETCODE_REJECT | Generic reject — broker logs needed |
| 10014 | TRADE_RETCODE_INVALID_VOLUME | Lot size violates broker rules |
| 10015 | TRADE_RETCODE_INVALID_PRICE | Price moved past acceptable slippage |
| 10018 | TRADE_RETCODE_MARKET_CLOSED | Market closed (weekend, broker holiday) |
| 10019 | TRADE_RETCODE_NO_MONEY | Insufficient margin |

Customer needs to verify with their broker. This isn't an ExecRelay
problem.

## If multiple customers are affected at once

Then it's not a single-customer EA disconnect — bridge or NATS is the
suspect.

```sh
# Bridge healthy?
docker compose ps bridge
docker compose logs bridge --tail=200 | grep -iE 'error|panic|disconnect'

# How many EAs connected?
curl -s http://localhost:8082/metrics | grep bridge_ea_connections_active

# NATS healthy + has subscriptions?
curl -s http://localhost:8222/jsz | grep -A 1 streams
curl -s http://localhost:8222/connz?subs=1 | grep -A 5 bridge
```

If bridge dropped many EAs at once, check whether bridge itself
restarted (`docker compose ps bridge` shows uptime; logs show start time).

## DXTrade specifically

For DXTrade fills (no EA — direct REST):

```sh
docker compose logs dxtrade --tail=200 | grep -F "$TRACE_ID"
```

Look for `circuit breaker open` — if `dxtrade_circuit_breaker_trips_total`
spiked, DXTrade's API is unhappy and the breaker is shedding load.
Customer needs to wait for the breaker to close (or contact DXTrade
support if the outage is theirs).

## Persist hasn't recorded the fill yet

If bridge reports the fill but it's not in `fills`:

```sh
# Has persist consumed it?
docker compose logs persist --tail=200 | grep -F "$TRACE_ID"

# What's its NATS lag?
docker compose exec nats nats stream info FILLS 2>/dev/null | head -20
```

If persist is behind, it'll catch up. If persist is crashing in a
loop, see [`postgres-down.md`](postgres-down.md) — Postgres is the
usual reason persist can't write.

## Mitigation

- **Single customer's EA disconnected** → customer-side fix; no operator
  action needed.
- **Bridge degraded** → `docker compose restart bridge`. NATS holds
  messages in the durable consumer; bridge drains them on restart.
- **DXTrade circuit open** → wait for it to close (default a few
  seconds), or restart `dxtrade` if it's stuck open.
- **NATS jet lag** → patience; the consumer drains automatically.

## Replay a lost fill (advanced)

If a fill genuinely got lost (broker confirms the trade happened, but
nothing in our `fills` table), you can re-replay the signal — bridge
will re-dispatch to the EA, which will see it's a duplicate and either
return the cached fill or reject. Use carefully:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/signals/$SIGNAL_ID/replay
```

This is **not** a "place another trade" button — see
[`apps/portal-api/app.py`](../../apps/portal-api/app.py) for the exact
semantics.

## Root cause checklist

- [ ] Was it one customer or many?
- [ ] If many: did bridge or NATS restart?
- [ ] If one: is the customer's EA actually connected (greppable in
      bridge logs)?
- [ ] Was the trade rejected by the broker (not us)?
- [ ] Is the bridge consumer lag elevated?
- [ ] Was the broker market open at the time?

## Postmortem prompts

- Did we have a metric for the failure mode that fired this incident?
  If not, what should we add to `bridge_*` or `dxtrade_*`?
- Should the EA emit a heartbeat that we can alert on disconnect?
- Was the customer surprised? Should the portal show "EA disconnected"
  prominently?

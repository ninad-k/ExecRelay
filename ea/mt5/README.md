# MT5 Expert Advisor

The MT5 EA holds a persistent WebSocket connection to ExecRelay's
`bridge` and translates incoming signals to broker `OrderSend()` calls.
**The EA is the execution authority and broker-position source of
truth** — bridge routes and records, but never owns broker state.

For the customer-facing setup flow (TradingView alert → broker fill)
see [`docs/customer/webhook-integration.md`](../../docs/customer/webhook-integration.md).

## Prerequisites

- **MetaTrader 5 build 2715 or newer** (uses the native socket API
  added in 2715).
- The customer's broker permits Expert Advisors and WebRequest URLs.

## Installation

1. **Copy the EA** to your MT5 data folder:
   - In MT5: `File → Open Data Folder`
   - Copy `ExecRelay.mq5` to `MQL5/Experts/`
2. **Open MetaEditor** (`F4` in MT5).
3. **Compile** (`F7`). Should compile clean with zero warnings on
   build 2715+.
4. **Allow WebRequest URLs**:
   - In MT5: `Tools → Options → Expert Advisors`
   - Tick "Allow WebRequest for listed URL"
   - Add your bridge URL (e.g., `https://bridge.execrelay.example.com`)
5. **Drag the EA onto a chart**. Any chart works; the EA isn't tied
   to a specific symbol — it executes whatever symbol the signal says.
6. **Fill the EA inputs**:
   - `LicenseID` — your ExecRelay license UUID (from the portal)
   - `InstanceID` — instance key for this terminal
   - `BridgeURL` — `wss://bridge.<your-domain>/ws`
7. **Confirm**: the EA's chart corner should show ✓ "Connected to
   bridge" once it handshakes successfully.

## Verifying the connection

The EA logs to MT5's `Journal` tab. Look for:

```
ExecRelay: connected to wss://bridge.example.com/ws
ExecRelay: registered as instance mt5-prop
ExecRelay: ready to receive signals
```

If you see connection errors, see
[`docs/runbooks/fills-not-arriving.md`](../../docs/runbooks/fills-not-arriving.md).

## Updating the EA

When a new release ships:

1. Stop the running EA (right-click chart → Expert Advisors → Remove).
2. Replace `MQL5/Experts/ExecRelay.mq5` with the new version.
3. Recompile in MetaEditor.
4. Re-attach to a chart with the same inputs.

There's no in-flight position migration — open positions stay open on
the broker; the new EA picks them up on next signal.

## Supported commands

See [`docs/customer/webhook-integration.md#supported-commands`](../../docs/customer/webhook-integration.md#supported-commands).

## See also

- [`docs/customer/webhook-integration.md`](../../docs/customer/webhook-integration.md) — full end-to-end customer guide
- [`docs/runbooks/fills-not-arriving.md`](../../docs/runbooks/fills-not-arriving.md) — when signals are accepted but fills don't land
- [`ea/mt4/README.md`](../mt4/README.md) — MT4 equivalent (requires DLL)


# MT4 Expert Advisor

The MT4 EA supports the same trade flow as MT5 but relies on the
**`ExecRelayWS.dll`** WebSocket DLL (built from [`ea/mt4-ws-dll/`](../mt4-ws-dll/))
because MQL4 has no native socket API.

> **Prefer MT5 if you have the choice.** MT5's native sockets eliminate
> the DLL dependency, simplify installation, and remove the "allow DLL
> imports" security prompt. This MT4 path exists for traders whose
> broker still only supports MT4.

A pure-HTTP fast-polling mode is also present as a fallback for tightly
locked-down environments where DLL loading is prohibited.

## Prerequisites

- **MetaTrader 4** (any reasonably recent build).
- Broker permits Expert Advisors **and** DLL imports.
- **`ExecRelayWS.dll`** — either built locally (see
  [`ea/mt4-ws-dll/README.md`](../mt4-ws-dll/README.md)) or fetched
  from your ExecRelay portal's downloads page (signed binary).

## Installation

1. **Install the DLL**:
   - In MT4: `File → Open Data Folder`
   - Copy `ExecRelayWS.dll` to `MQL4/Libraries/`
2. **Install the EA**:
   - Copy `ExecRelay.mq4` to `MQL4/Experts/`
3. **Compile**:
   - Open MetaEditor (`F4`), open `ExecRelay.mq4`, compile (`F7`).
4. **Allow DLL imports**:
   - In MT4: `Tools → Options → Expert Advisors`
   - Tick "Allow DLL imports"
   - (Also: "Allow live trading", "Allow WebRequest for listed URL" +
     your bridge URL)
5. **Drag the EA onto a chart**.
6. **Fill the EA inputs**:
   - `LicenseID`
   - `InstanceID`
   - `BridgeURL` (e.g., `wss://bridge.example.com/ws`)
7. **Confirm** the green ✓ in the chart corner.

## Why a DLL is needed

MQL4 doesn't expose TCP sockets. The DLL handles:

- The WebSocket handshake (RFC 6455)
- Frame masking
- Auto-ping / auto-pong
- Connection pooling (up to 8 concurrent handles)

The full build story (CMake + MinGW from Linux, MSVC from Windows) is
in [`ea/mt4-ws-dll/README.md`](../mt4-ws-dll/README.md).

## HTTP fast-polling fallback

If your environment forbids DLL imports, the EA can fall back to
polling `https://bridge.<your-domain>/poll` over MT4's built-in
`WebRequest()` API. This has higher latency (poll interval is bounded
below by MT4's internal scheduler) and is **not recommended for
latency-sensitive strategies**. Enable by setting
`UseHttpFallback = true` in EA inputs.

## See also

- [`ea/mt5/README.md`](../mt5/README.md) — preferred MT5 path
- [`ea/mt4-ws-dll/README.md`](../mt4-ws-dll/README.md) — DLL build details
- [`docs/customer/webhook-integration.md`](../../docs/customer/webhook-integration.md) — end-to-end customer setup
- [`docs/runbooks/fills-not-arriving.md`](../../docs/runbooks/fills-not-arriving.md) — when signals don't lead to fills


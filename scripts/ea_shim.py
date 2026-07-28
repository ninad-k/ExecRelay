"""ExecRelay EA shim — plays the MT5 EA's role for local end-to-end testing.

Registers with a locally running `bridge` over the same WebSocket protocol as
ea/mt5/ExecRelay.mq5, executes incoming signals in the RUNNING MetaTrader 5
terminal via the official `MetaTrader5` python package, and reports fills
back to the bridge. Lets you exercise the full signal path (ingress -> NATS
-> bridge -> broker) without compiling/attaching the MQL5 EA.

SAFETY: refuses to start unless the attached MT5 account is a DEMO account.
This is a test harness, not a production executor — the MQL5 EA remains the
execution authority in real deployments.

Usage (see docs/development/demo-e2e-test.md for the full runbook):

    pip install MetaTrader5 websockets
    python scripts/ea_shim.py

Environment overrides:
    EA_SHIM_BRIDGE_URL   default ws://127.0.0.1:8082/ea/ws
    EA_SHIM_INSTANCE_ID  default test-instance (must match EXECRELAY_LICENSES)
    EA_SHIM_TOKEN        default test-bridge-token (must match BRIDGE_AUTH_TOKEN)
    EA_SHIM_MAGIC        default 20240101 (order magic; isolates shim positions)
"""

import asyncio
import json
import os
import sys
import time

import MetaTrader5 as mt5
import websockets

BRIDGE_URL = os.environ.get("EA_SHIM_BRIDGE_URL", "ws://127.0.0.1:8082/ea/ws")
INSTANCE_ID = os.environ.get("EA_SHIM_INSTANCE_ID", "test-instance")
BRIDGE_TOKEN = os.environ.get("EA_SHIM_TOKEN", "test-bridge-token")
MAGIC = int(os.environ.get("EA_SHIM_MAGIC", "20240101"))
DEVIATION = 50
# When a pending order is rejected for being on the wrong side of the market
# (TRADE_RETCODE_INVALID_PRICE), execute at market instead, tagging the order
# comment with "_M". Mirrors the EAs' InpPendingFallbackMarket input.
PENDING_FALLBACK = os.environ.get("EA_SHIM_PENDING_FALLBACK", "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def die(msg):
    log("FATAL:", msg)
    sys.exit(1)


def init_mt5():
    if not mt5.initialize():
        die(f"mt5.initialize failed: {mt5.last_error()}")
    acct = mt5.account_info()
    if acct is None:
        die("no account info — is the terminal logged in?")
    if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        die(f"account {acct.login} is NOT a demo account — refusing to trade")
    log(
        f"attached to DEMO account {acct.login} ({acct.company}), balance {acct.balance}"
    )
    return acct


def fnum(params, *keys, default=0.0):
    for k in keys:
        v = params.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def send_market(action_type, symbol, volume, comment, sl=0.0, tp=0.0):
    tick = mt5.symbol_info_tick(symbol)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": action_type,
        "price": tick.ask if action_type == mt5.ORDER_TYPE_BUY else tick.bid,
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": (comment or "execrelay-shim")[:26],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if sl > 0:
        req["sl"] = sl
    if tp > 0:
        req["tp"] = tp
    res = mt5.order_send(req)
    if res is None:
        return None, f"order_send returned None: {mt5.last_error()}"
    if res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
        req["type_filling"] = mt5.ORDER_FILLING_FOK
        res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return None, f"retcode={res.retcode} {res.comment}"
    return res, None


def send_pending(pending_type, symbol, volume, entry, sl, tp, comment):
    """Place a pending order. Returns (result, err, retcode)."""
    req = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": volume,
        "type": pending_type,
        "price": entry,
        "magic": MAGIC,
        "comment": (comment or "execrelay-shim")[:26],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    if sl > 0:
        req["sl"] = sl
    if tp > 0:
        req["tp"] = tp
    res = mt5.order_send(req)
    if res is None:
        return None, f"order_send returned None: {mt5.last_error()}", -1
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return None, f"retcode={res.retcode} {res.comment}", res.retcode
    return res, None, res.retcode


def close_positions(symbol, pos_type):
    """Close all shim-owned positions of the given type. Returns (orders, err)."""
    closed = []
    for p in mt5.positions_get(symbol=symbol) or []:
        if p.magic != MAGIC or p.type != pos_type:
            continue
        opposite = (
            mt5.ORDER_TYPE_SELL
            if p.type == mt5.POSITION_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )
        tick = mt5.symbol_info_tick(symbol)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": p.volume,
            "type": opposite,
            "position": p.ticket,
            "price": tick.bid if opposite == mt5.ORDER_TYPE_SELL else tick.ask,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": "execrelay-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            return (
                closed,
                f"close {p.ticket} failed: {res.retcode if res else mt5.last_error()}",
            )
        closed.append(str(res.order))
    return closed, None


# pending command -> (pending order type, market fallback order type)
_PENDING_TYPES = {
    "buylimit": ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_BUY"),
    "selllimit": ("ORDER_TYPE_SELL_LIMIT", "ORDER_TYPE_SELL"),
    "buystop": ("ORDER_TYPE_BUY_STOP", "ORDER_TYPE_BUY"),
    "sellstop": ("ORDER_TYPE_SELL_STOP", "ORDER_TYPE_SELL"),
}


def execute(trace_id, command, symbol, params):
    """Execute one signal. Returns (status, order_id, err) where status is
    "filled" for executed orders, "placed" for resting pendings."""
    mt5.symbol_select(symbol, True)
    volume = fnum(params, "volume", "vol_lots", default=0.01)
    comment = params.get("comment", "execrelay-shim")
    sl = fnum(params, "sl")
    tp = fnum(params, "tp")
    cmd = command.lower()
    log(f"signal trace={trace_id} cmd={cmd} {symbol} vol={volume}")

    if cmd == "buy":
        res, err = send_market(mt5.ORDER_TYPE_BUY, symbol, volume, comment, sl, tp)
        return "filled", (str(res.order) if res else ""), err
    if cmd == "sell":
        res, err = send_market(mt5.ORDER_TYPE_SELL, symbol, volume, comment, sl, tp)
        return "filled", (str(res.order) if res else ""), err
    if cmd in _PENDING_TYPES:
        entry = fnum(params, "entry", "entry_price")
        if entry <= 0:
            return "rejected", "", "entry required for pending order"
        ptype_name, mtype_name = _PENDING_TYPES[cmd]
        res, err, retcode = send_pending(
            getattr(mt5, ptype_name), symbol, volume, entry, sl, tp, comment
        )
        if res is not None:
            return "placed", str(res.order), None
        if PENDING_FALLBACK and retcode == mt5.TRADE_RETCODE_INVALID_PRICE:
            # Wrong side of the market: execute at market instead, tag _M.
            log(f"pending {cmd}@{entry} invalid price -> market fallback (_M)")
            res, merr = send_market(
                getattr(mt5, mtype_name), symbol, volume, f"{comment}_M", sl, tp
            )
            if res is not None:
                return "filled", str(res.order), None
            return "filled", "", f"pending invalid price; market fallback failed: {merr}"
        return "filled", "", err
    if cmd == "closelong":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_BUY)
        return "filled", ",".join(orders), err
    if cmd == "closeshort":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_SELL)
        return "filled", ",".join(orders), err
    if cmd == "closelongopenshort":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_BUY)
        if err:
            return "filled", ",".join(orders), err
        res, err = send_market(mt5.ORDER_TYPE_SELL, symbol, volume, comment, sl, tp)
        return "filled", ",".join(orders + ([str(res.order)] if res else [])), err
    if cmd == "closeshortopenlong":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_SELL)
        if err:
            return "filled", ",".join(orders), err
        res, err = send_market(mt5.ORDER_TYPE_BUY, symbol, volume, comment, sl, tp)
        return "filled", ",".join(orders + ([str(res.order)] if res else [])), err
    return "rejected", "", f"unknown command {command}"


async def run_session():
    # ping_interval=None: MT5 calls run in an executor, but registration and
    # fills share the loop; the bridge's app-level ping/pong covers liveness,
    # and the protocol-level keepalive caused spurious 1011 disconnects.
    async with websockets.connect(BRIDGE_URL, ping_interval=None) as ws:
        acct = mt5.account_info()
        await ws.send(
            json.dumps(
                {
                    "type": "register",
                    "instance_id": INSTANCE_ID,
                    "token": BRIDGE_TOKEN,
                    "account_number": str(acct.login),
                    "broker": acct.company,
                    "platform": "mt5",
                    "ea_version": "py-shim-1.1",
                }
            )
        )
        log("register sent, waiting for ack...")

        async def heartbeat():
            while True:
                await asyncio.sleep(10)
                a = mt5.account_info()
                if a is None:
                    continue
                await ws.send(
                    json.dumps(
                        {
                            "type": "heartbeat",
                            "free_margin": round(a.margin_free, 2),
                            "equity": round(a.equity, 2),
                            "uptime_secs": int(time.monotonic()),
                        }
                    )
                )

        hb = None
        loop = asyncio.get_running_loop()
        try:
            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "registered":
                    log("REGISTERED with bridge")
                    hb = asyncio.create_task(heartbeat())
                elif mtype == "signal":
                    ok_status, order_id, err = await loop.run_in_executor(
                        None,
                        execute,
                        msg.get("trace_id", ""),
                        msg.get("command", ""),
                        msg.get("symbol", ""),
                        msg.get("params") or {},
                    )
                    fill = {
                        "type": "fill",
                        "trace_id": msg.get("trace_id", ""),
                        "status": "rejected" if err else ok_status,
                        "broker_order_id": order_id,
                        "error_code": "EXEC_FAIL" if err else "",
                        "error_message": err or "",
                    }
                    await ws.send(json.dumps(fill))
                    log("fill reported:", fill["status"], order_id, err or "")
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        finally:
            if hb:
                hb.cancel()


async def main():
    init_mt5()
    while True:
        try:
            await run_session()
            log("connection closed cleanly; reconnecting in 3s")
        except Exception as e:
            log(f"session error: {e!r}; reconnecting in 3s")
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())

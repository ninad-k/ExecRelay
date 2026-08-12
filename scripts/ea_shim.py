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
    EA_SHIM_RISK_USD     max $ loss per order; sizes the lot from the SL
                         distance, overriding the signal's vol_lots. 0 = off.
    EA_SHIM_NOTIFY_CHAT_ID  Telegram chat for open/close notifications
                         (default: first TELEGRAM_INGEST_ALLOWED_CHAT_IDS entry;
                         sent via TELEGRAM_INGEST_BOT_TOKEN, empty = disabled)
"""

import asyncio
import json
import math
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import websockets

from _txnlog import get_txn_logger, log_txn
from _tradestore import (
    channel_flags,
    meta_set,
    record_closed_trade,
    record_equity,
    record_order,
    sibling_broker_order_ids,
    sibling_order_rows,
)

TXN_LOG = get_txn_logger("mt5-fills")

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
RISK_USD = float(os.environ.get("EA_SHIM_RISK_USD", "0") or 0)

# One-cancels-other. A multi-entry signal (an entry range, or an explicit
# "SECOND SELL LIMIT") rests several orders on the same symbol, sharing one
# SL and one TP. Once either bracket side has taken a leg out, the signal has
# RESOLVED -- target reached, or invalidated at the stop -- and a sibling
# still resting is no longer the second half of a live idea. On TP it would
# re-enter a move that already finished; on SL it would re-enter a setup the
# market just proved wrong, behind the very stop that failed. Both ways the
# siblings from that same signal are withdrawn.
#
# The trigger is the broker's own deal reason (DEAL_REASON_TP / _SL), so a
# manual close or a "close longs" command still leaves the other leg exactly
# where it is: the operator closing one trade by hand has said nothing about
# the rest of the signal.
#
# EA_SHIM_CANCEL_SIBLINGS_ON_TP is honoured as a fallback: the switch briefly
# shipped under that name when only the TP side triggered it.
CANCEL_SIBLINGS_ON_CLOSE = (
    os.environ.get("EA_SHIM_CANCEL_SIBLINGS_ON_CLOSE")
    or os.environ.get("EA_SHIM_CANCEL_SIBLINGS_ON_TP")
    or "true"
).lower() in ("true", "1", "yes", "on")

NOTIFY_TOKEN = os.environ.get("TELEGRAM_INGEST_BOT_TOKEN", "")
_notify_chat_raw = (
    os.environ.get("EA_SHIM_NOTIFY_CHAT_ID")
    or os.environ.get("TELEGRAM_INGEST_ALLOWED_CHAT_IDS", "").split(",")[0]
).strip()
NOTIFY_CHAT = int(_notify_chat_raw) if _notify_chat_raw.lstrip("-").isdigit() else 0
POSITION_POLL_SECS = 5


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def short_exc(exc, limit=200):
    """One-line, bounded exception text. repr() on library errors can carry
    huge payloads (websocket frames, MTProto buffers) that flood the log and
    the console tailing it."""
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[:limit] + f"… (+{len(text) - limit} chars)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


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


def sized_volume(symbol, ref_price, sl, requested, risk_usd):
    """$-risk position sizing: the largest lot whose loss from ref_price to
    the SL stays within risk_usd, floored to the broker's volume step.

    Returns the requested volume unchanged when sizing can't run (no risk
    budget, no SL, missing symbol specs), and 0.0 when even the minimum lot
    would lose more than risk_usd — the caller must reject, not oversize.
    """
    if risk_usd <= 0 or sl <= 0 or ref_price <= 0:
        return requested
    info = mt5.symbol_info(symbol)
    if info is None or info.trade_tick_value <= 0 or info.trade_tick_size <= 0:
        log(f"risk sizing: no specs for {symbol}; keeping vol {requested}")
        return requested
    distance = abs(ref_price - sl)
    if distance <= 0:
        return requested
    loss_per_lot = distance / info.trade_tick_size * info.trade_tick_value
    step = info.volume_step or 0.01
    vol = math.floor(risk_usd / loss_per_lot / step) * step
    vol = round(vol, 8)  # kill float dust like 0.19999999
    if vol < info.volume_min:
        return 0.0
    return min(vol, info.volume_max)


def tg_notify(text):
    """Best-effort Telegram message to the signal chat. Never raises."""
    if not (NOTIFY_TOKEN and NOTIFY_CHAT):
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{NOTIFY_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": NOTIFY_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"telegram notify failed: {short_exc(e)}")


def _fmt_position(p):
    side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
    return f"{side} {p.symbol} {p.volume:g} lot @ {p.price_open:g}"


def _closed_result(ticket):
    """Realized P/L (incl. commission/swap), close price, and WHY it closed,
    for a position that just left the open-positions list.

    The reason comes from the broker's own closing deal (mt5.DEAL_REASON_*),
    not inferred from the price or the sign of the P/L: a position can finish
    profitable without having reached its take-profit, and one-cancels-other
    must fire on "hit TP" specifically, never on "ended up green". None when
    no closing deal is readable, which callers treat as "not a TP"."""
    deals = (
        mt5.history_deals_get(
            datetime.now() - timedelta(days=7),
            datetime.now() + timedelta(days=1),
            position=ticket,
        )
        or []
    )
    out = [d for d in deals if d.entry != mt5.DEAL_ENTRY_IN]
    profit = sum(d.profit + d.commission + d.swap for d in out)
    price = out[-1].price if out else 0.0
    reason = getattr(out[-1], "reason", None) if out else None
    return profit, price, reason


def cancel_pending_orders(tickets):
    """Withdraw the named pending orders. Returns (removed, failures).

    Only orders that are still PENDING and shim-owned are touched:
    mt5.orders_get() lists resting orders only, so a sibling that has already
    triggered into a position is simply absent here and is left alone -- this
    cancels orders, it never closes trades. A ticket that is gone (filled,
    expired, already cancelled) is silently skipped for the same reason."""
    wanted = {str(t) for t in tickets}
    if not wanted:
        return [], []
    removed, failures = [], []
    for o in mt5.orders_get() or []:
        if str(o.ticket) not in wanted or o.magic != MAGIC:
            continue
        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            failures.append(
                f"{o.ticket}: {res.retcode if res else mt5.last_error()}"
            )
        else:
            removed.append(str(o.ticket))
    return removed, failures


# Equity snapshot cadence: position_monitor already polls every
# POSITION_POLL_SECS (5s); snapshot on every 60th iteration (~300s = 5min)
# instead of running a second timing loop.
EQUITY_SNAPSHOT_EVERY = 60

# Pipeline-status heartbeat cadence: same trick, every 6th iteration (~30s)
# -- frequent enough that the dashboard's ~90s staleness check has margin,
# infrequent enough not to spam the store with a write every 5s poll.
HEARTBEAT_EVERY = 6


def _cancel_siblings_after_close(ticket, position, close_reason):
    """A leg of a multi-entry signal just left the book at its TP or its SL --
    withdraw the legs of THAT SAME signal that are still resting.

    Siblings are resolved through the trade store, not through the MT5
    comment. The comment names the channel, so comment-matching would also
    cancel a different, still-valid signal the same channel has working on the
    same symbol. sibling_broker_order_ids() walks ticket -> trace_id -> signal
    -> sibling tickets instead, so only legs of this one message are eligible.

    Every failure path here is a no-op by construction: an unresolvable ticket
    returns no siblings, and a sibling that already filled is not a pending
    order and is skipped. Never raises -- this runs inside the monitor loop,
    and a bookkeeping problem must not stop position tracking or notifications.
    """
    hit = "TP" if close_reason == mt5.DEAL_REASON_TP else "SL"
    try:
        siblings = sibling_broker_order_ids(str(ticket))
        if not siblings:
            return
        removed, failures = cancel_pending_orders(siblings)
        if removed:
            log(
                f"{hit} on {position.symbol} #{ticket} -- cancelled sibling "
                f"pending order(s) {', '.join(removed)}"
            )
            log_txn(
                TXN_LOG,
                event="siblings_cancelled_after_close",
                position=ticket,
                symbol=position.symbol,
                close_reason=hit,
                cancelled=removed,
                comment=position.comment,
            )
            tg_notify(
                f"🚫 Pending order cancelled\n"
                f"{position.symbol} hit {hit}, so the other entry from the "
                f"same signal was withdrawn\n"
                f"Ticket(s) {', '.join(removed)}"
            )
        for f in failures:
            log(f"could not cancel sibling pending {f}")
    except Exception as e:  # noqa: BLE001 - monitor loop must survive
        log(f"sibling cancel after {hit} failed: {short_exc(e)}")


def _classify_tp_close(ticket, rows):
    """Half-TP vs full-TP close, decided from the store's view of this
    signal's legs -- never from the MT5 comment, which only names the
    CHANNEL ("tg-VGTA"), not the signal, so it cannot distinguish a
    partial_book split from an ordinary single-order close.

    `rows` is sibling_order_rows(str(ticket)): every order of this ticket's
    signal, INCLUDING this ticket's own row. A partial_book signal's
    half-TP leg always has a tp strictly closer to entry than its full-TP
    sibling, so "some sibling's tp is further in the profit direction than
    this row's own tp" is exactly "this was the half leg". Direction is
    read off the row's own `command`: a buy-side command profits going UP
    (further = higher tp), a sell-side command profits going DOWN (further
    = lower tp).

    Returns ("half" | "full", channel) when decidable, or (None, None) when
    it isn't (this ticket's own row missing from `rows`, no tp recorded, or
    a command that says neither buy nor sell). Callers must treat None the
    same as "full" -- i.e. fall back to the pre-split cancel-siblings
    behaviour, since that was correct before this feature existed and is
    never a false breakeven move."""
    own = next(
        (r for r in rows if str(r.get("broker_order_id")) == str(ticket)), None
    )
    if own is None or own.get("tp") is None:
        return None, None
    try:
        own_tp = float(own["tp"])
    except (TypeError, ValueError):
        return None, None
    command = str(own.get("command") or "").lower()
    if "buy" in command:
        profit_is_up = True
    elif "sell" in command:
        profit_is_up = False
    else:
        return None, None

    for r in rows:
        if str(r.get("broker_order_id")) == str(ticket):
            continue
        tp = r.get("tp")
        if tp is None:
            continue
        try:
            sib_tp = float(tp)
        except (TypeError, ValueError):
            continue
        if (sib_tp > own_tp) if profit_is_up else (sib_tp < own_tp):
            return "half", own.get("channel")
    return "full", own.get("channel")


def _move_siblings_to_breakeven(ticket, position, sibling_ids):
    """Half-TP leg just closed and this channel has breakeven ON -- move the
    SL of this signal's still-OPEN sibling positions to each one's own
    price_open. `sibling_ids` is the full sibling set (pending or filled);
    mt5.positions_get() naturally narrows that to positions that actually
    exist right now, so an already-cancelled or not-yet-triggered sibling is
    silently skipped, same as cancel_pending_orders does for OCO.

    Re-sends each position's existing tp unchanged rather than omitting it:
    modify_positions_sltp's docstring explains why -- MT5 reads a 0 tp as
    "remove the take-profit", so leaving it out of the request would
    silently strip it.

    Never raises -- runs inside the monitor loop; a bookkeeping problem here
    must not stop position tracking or notifications."""
    try:
        wanted = {str(s) for s in sibling_ids if s}
        if not wanted:
            return
        moved, failures = [], []
        for p in mt5.positions_get() or []:
            if p.magic != MAGIC or str(p.ticket) not in wanted:
                continue
            req = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": p.symbol,
                "position": p.ticket,
                "sl": p.price_open,
                "tp": p.tp,
                "magic": MAGIC,
            }
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                failures.append(f"{p.ticket}: {res.retcode if res else mt5.last_error()}")
            else:
                moved.append(str(p.ticket))
        if moved:
            log(
                f"half-TP on {position.symbol} #{ticket} -- moved sibling "
                f"position(s) {', '.join(moved)} to breakeven"
            )
            log_txn(
                TXN_LOG,
                event="breakeven_after_partial_tp",
                position=ticket,
                symbol=position.symbol,
                moved=moved,
                comment=position.comment,
            )
            tg_notify(
                f"🛡️ Breakeven\n"
                f"{position.symbol} booked 50% at TP, so the rest of this "
                f"signal's open position(s) were moved to breakeven\n"
                f"Ticket(s) {', '.join(moved)}"
            )
        for f in failures:
            log(f"could not move sibling to breakeven {f}")
    except Exception as e:  # noqa: BLE001 - monitor loop must survive
        log(f"breakeven move after half-TP failed: {short_exc(e)}")


def _handle_close_oco(ticket, position, close_reason):
    """Dispatch a just-closed position to the right one-cancels-other /
    breakeven action. Replaces a direct _cancel_siblings_after_close() call
    now that a signal can be split into half-TP / full-TP order pairs
    (partial_book) -- see the module-level notes above CANCEL_SIBLINGS_ON_CLOSE
    for the un-split OCO rationale, which still applies unchanged to SL closes
    and to full-TP closes.

    SL close: unchanged -- withdraw pending siblings. Every split order of a
    signal shares one SL, so they close together regardless; this just
    withdraws whatever hasn't triggered yet.

    TP close: classify via the store (_classify_tp_close):
      - half-TP: the signal is still live (the full-TP sibling is still
        resting or still open) -- do NOT cancel pending siblings. If this
        channel has breakeven ON, move this signal's still-open positions'
        SL to their own price_open instead.
      - full-TP, or undecidable (no store rows, ticket unresolvable, row
        missing tp/command): today's pre-split behaviour -- cancel pending
        siblings. Never guesses breakeven when the classification itself is
        unknown.
    """
    if close_reason == mt5.DEAL_REASON_SL:
        _cancel_siblings_after_close(ticket, position, close_reason)
        return
    if close_reason != mt5.DEAL_REASON_TP:
        return
    try:
        rows = sibling_order_rows(str(ticket))
    except Exception as e:  # noqa: BLE001 - monitor loop must survive
        log(f"sibling_order_rows failed for #{ticket}: {short_exc(e)}")
        rows = []
    if not rows:
        _cancel_siblings_after_close(ticket, position, close_reason)
        return
    kind, channel = _classify_tp_close(ticket, rows)
    if kind != "half":
        _cancel_siblings_after_close(ticket, position, close_reason)
        return
    flags = channel_flags(channel)
    if not flags.get("breakeven"):
        return
    sibling_ids = [
        r.get("broker_order_id")
        for r in rows
        if r.get("broker_order_id") and str(r["broker_order_id"]) != str(ticket)
    ]
    _move_siblings_to_breakeven(ticket, position, sibling_ids)


def position_monitor():
    """Notify Telegram when a shim-owned position opens (market fill or a
    pending order triggering) and when one closes (with realized P/L). Also
    persists closed positions to the trade store and, every ~5 minutes,
    an account equity snapshot."""
    known = None
    iteration = 0
    while True:
        try:
            current = {
                p.ticket: p for p in (mt5.positions_get() or []) if p.magic == MAGIC
            }
            if known is not None:
                for ticket, p in current.items():
                    if ticket not in known:
                        tg_notify(
                            f"🟢 Trade opened\n{_fmt_position(p)}\n"
                            f"SL {p.sl:g} | TP {p.tp:g}"
                        )
                for ticket, p in known.items():
                    if ticket not in current:
                        profit, close_price, close_reason = _closed_result(ticket)
                        sign = "+" if profit >= 0 else "-"
                        tg_notify(
                            f"{'✅' if profit >= 0 else '❌'} Trade closed\n"
                            f"{_fmt_position(p)} -> {close_price:g}\n"
                            f"P/L {sign}${abs(profit):.2f}"
                        )
                        side = "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell"
                        log_txn(
                            TXN_LOG,
                            event="position_closed",
                            position=ticket,
                            symbol=p.symbol,
                            side=side,
                            volume=p.volume,
                            open_price=p.price_open,
                            close_price=close_price,
                            profit=round(profit, 2),
                        )
                        record_closed_trade(
                            position_id=str(ticket),
                            close_ts=datetime.now(timezone.utc).isoformat(),
                            symbol=p.symbol,
                            side=side,
                            volume=p.volume,
                            entry_price=p.price_open,
                            close_price=close_price,
                            profit=round(profit, 2),
                            magic=p.magic,
                            comment=p.comment,
                            source="telegram" if str(p.comment).startswith("tg-") else "tradingview",
                        )
                        if CANCEL_SIBLINGS_ON_CLOSE and close_reason in (
                            mt5.DEAL_REASON_TP,
                            mt5.DEAL_REASON_SL,
                        ):
                            _handle_close_oco(ticket, p, close_reason)
            known = current

            iteration += 1
            if iteration % EQUITY_SNAPSHOT_EVERY == 0:
                a = mt5.account_info()
                if a is not None:
                    record_equity(
                        balance=a.balance,
                        equity=a.equity,
                        margin=a.margin,
                        margin_free=a.margin_free,
                        floating=round(a.equity - a.balance, 2),
                    )

            # Pipeline-status heartbeat: proves this process is not merely
            # alive but genuinely polling positions AND still attached to
            # MT5 -- a pid-liveness check alone can't tell a healthy shim
            # from one hung on a login prompt, relaying/executing nothing
            # (see trade_dashboard.py /api/pipeline). Read-only MT5 call +
            # a no-throw store write; never touches order execution/sizing.
            if iteration % HEARTBEAT_EVERY == 0:
                acct_hb = mt5.account_info()
                hb_state = {
                    "mt5": acct_hb is not None,
                    "account": acct_hb.login if acct_hb is not None else None,
                    "demo": (
                        acct_hb.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
                        if acct_hb is not None
                        else None
                    ),
                }
                meta_set("hb_ea_shim", datetime.now(timezone.utc).isoformat())
                meta_set("hb_ea_shim_state", json.dumps(hb_state))
        except Exception as e:
            log(f"position monitor error: {short_exc(e)}")
        time.sleep(POSITION_POLL_SECS)


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


def modify_positions_sltp(symbol, pos_type, tp, sl, comment_scope):
    """Set TP (and optionally SL) on shim-owned positions of the given type.
    Returns (tickets, err).

    `comment_scope` narrows the change to one signal channel: only positions
    whose MT5 comment starts with it are touched, which is how a "TP set @ X"
    from one channel avoids moving another channel's trades. startswith (not
    equality) because the pending->market fallback tags its comment "<c>_M".
    An empty scope means every shim-owned position of that type -- callers
    that mean "one channel" must always pass one.

    A field left at 0 is carried over from the position rather than sent as 0:
    MT5 reads 0 as "remove this level", so a TP-only amendment must re-send
    the existing SL or it would silently strip the stop loss."""
    changed = []
    for p in mt5.positions_get(symbol=symbol) or []:
        if p.magic != MAGIC or p.type != pos_type:
            continue
        if comment_scope and not (p.comment or "").startswith(comment_scope):
            continue
        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": p.ticket,
            "sl": sl if sl > 0 else p.sl,
            "tp": tp if tp > 0 else p.tp,
            "magic": MAGIC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            return (
                changed,
                f"modify {p.ticket} failed: {res.retcode if res else mt5.last_error()}",
            )
        changed.append(str(p.ticket))
    return changed, None


# pending command -> (pending order type, market fallback order type)
_PENDING_TYPES = {
    "buylimit": ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_BUY"),
    "selllimit": ("ORDER_TYPE_SELL_LIMIT", "ORDER_TYPE_SELL"),
    "buystop": ("ORDER_TYPE_BUY_STOP", "ORDER_TYPE_BUY"),
    "sellstop": ("ORDER_TYPE_SELL_STOP", "ORDER_TYPE_SELL"),
}


def execute(trace_id, command, symbol, params):
    """Execute one signal. Returns (status, order_id, err, volume) where
    status is "filled" for executed orders, "placed" for resting pendings,
    and volume is the FINAL sized lot actually sent to the broker (after
    $-risk sizing overrides the requested vol_lots/volume)."""
    # Broker symbol-name fallback: many brokers suffix their instruments
    # (XAUUSD_ here). If the exact name is unknown, try common variants
    # rather than failing the order on a naming mismatch.
    if mt5.symbol_info(symbol) is None:
        for variant in (f"{symbol}_", f"{symbol}.", f"{symbol}m"):
            if mt5.symbol_info(variant) is not None:
                log(f"symbol {symbol} unknown to broker; using {variant}")
                symbol = variant
                break
    mt5.symbol_select(symbol, True)
    volume = fnum(params, "volume", "vol_lots", default=0.01)
    comment = params.get("comment", "execrelay-shim")
    sl = fnum(params, "sl")
    tp = fnum(params, "tp")
    cmd = command.lower()

    # $-risk sizing for every order that opens exposure. A per-order `risk`
    # param (how telegram-ingest splits its per-signal budget across legs)
    # wins over the env-level default. The reference price is the stated
    # entry for pendings, the current market for the rest.
    _opens = {"buy", "sell", "closelongopenshort", "closeshortopenlong"}
    if cmd in _opens or cmd in _PENDING_TYPES:
        risk_usd = fnum(params, "risk", default=RISK_USD)
        if cmd in _PENDING_TYPES:
            ref = fnum(params, "entry", "entry_price")
        else:
            t = mt5.symbol_info_tick(symbol)
            buys = cmd in ("buy", "closeshortopenlong")
            ref = (t.ask if buys else t.bid) if t else 0.0
        sized = sized_volume(symbol, ref, sl, volume, risk_usd)
        if sized <= 0:
            err = (
                f"risk sizing: minimum lot for {symbol} already risks more "
                f"than ${risk_usd:g} at SL {sl:g} (ref {ref:g})"
            )
            log(f"signal trace={trace_id} REJECTED: {err}")
            return "rejected", "", err, sized
        if sized != volume:
            log(
                f"risk sizing: vol {volume:g} -> {sized:g} "
                f"(${risk_usd:g} over {abs(ref - sl):g} SL distance)"
            )
        volume = sized

    log(f"signal trace={trace_id} cmd={cmd} {symbol} vol={volume}")

    if cmd == "buy":
        res, err = send_market(mt5.ORDER_TYPE_BUY, symbol, volume, comment, sl, tp)
        return "filled", (str(res.order) if res else ""), err, volume
    if cmd == "sell":
        res, err = send_market(mt5.ORDER_TYPE_SELL, symbol, volume, comment, sl, tp)
        return "filled", (str(res.order) if res else ""), err, volume
    if cmd in _PENDING_TYPES:
        entry = fnum(params, "entry", "entry_price")
        if entry <= 0:
            return "rejected", "", "entry required for pending order", volume
        ptype_name, mtype_name = _PENDING_TYPES[cmd]
        res, err, retcode = send_pending(
            getattr(mt5, ptype_name), symbol, volume, entry, sl, tp, comment
        )
        if res is not None:
            return "placed", str(res.order), None, volume
        if PENDING_FALLBACK and retcode == mt5.TRADE_RETCODE_INVALID_PRICE:
            # Wrong side of the market: execute at market instead, tag _M.
            log(f"pending {cmd}@{entry} invalid price -> market fallback (_M)")
            res, merr = send_market(
                getattr(mt5, mtype_name), symbol, volume, f"{comment}_M", sl, tp
            )
            if res is not None:
                return "filled", str(res.order), None, volume
            return "filled", "", f"pending invalid price; market fallback failed: {merr}", volume
        return "filled", "", err, volume
    if cmd == "closelong":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_BUY)
        return "filled", ",".join(orders), err, volume
    if cmd == "closeshort":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_SELL)
        return "filled", ",".join(orders), err, volume
    if cmd == "closelongopenshort":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_BUY)
        if err:
            return "filled", ",".join(orders), err, volume
        res, err = send_market(mt5.ORDER_TYPE_SELL, symbol, volume, comment, sl, tp)
        return "filled", ",".join(orders + ([str(res.order)] if res else [])), err, volume
    if cmd == "closeshortopenlong":
        orders, err = close_positions(symbol, mt5.POSITION_TYPE_SELL)
        if err:
            return "filled", ",".join(orders), err, volume
        res, err = send_market(mt5.ORDER_TYPE_BUY, symbol, volume, comment, sl, tp)
        return "filled", ",".join(orders + ([str(res.order)] if res else [])), err, volume
    if cmd in ("newsltplong", "newsltpshort"):
        if tp <= 0 and sl <= 0:
            return "rejected", "", "newsltp requires sl and/or tp", volume
        ptype = mt5.POSITION_TYPE_BUY if cmd == "newsltplong" else mt5.POSITION_TYPE_SELL
        scope = params.get("comment") or ""
        tickets, err = modify_positions_sltp(symbol, ptype, tp, sl, scope)
        if err:
            return "rejected", ",".join(tickets), err, volume
        if not tickets:
            # Nothing of this side/scope is open. Expected: amendments are
            # sent for both sides because the message never says which way
            # the running trades face.
            log(f"{cmd} {symbol}: no open positions matching scope {scope!r} -- no-op")
            return "filled", "", None, volume
        log(f"{cmd} {symbol}: set tp={tp:g} on {len(tickets)} position(s) scope={scope!r}")
        return "filled", ",".join(tickets), None, volume
    return "rejected", "", f"unknown command {command}", volume


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
                    command = msg.get("command", "")
                    symbol = msg.get("symbol", "")
                    params = msg.get("params") or {}
                    ok_status, order_id, err, exec_volume = await loop.run_in_executor(
                        None,
                        execute,
                        msg.get("trace_id", ""),
                        command,
                        symbol,
                        params,
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
                    log_txn(
                        TXN_LOG,
                        account=acct.login,
                        broker=acct.company,
                        instance_id=INSTANCE_ID,
                        trace_id=fill["trace_id"],
                        command=command,
                        symbol=symbol,
                        volume=params.get("volume") or params.get("vol_lots"),
                        risk=params.get("risk"),
                        comment=params.get("comment"),
                        sl=params.get("sl"),
                        tp=params.get("tp"),
                        entry=params.get("entry") or params.get("entry_price"),
                        status=fill["status"],
                        broker_order_id=order_id,
                        error=err or "",
                    )
                    _comment = params.get("comment") or ""
                    record_order(
                        trace_id=fill["trace_id"],
                        source="telegram" if str(_comment).startswith("tg-") else "tradingview",
                        command=command,
                        symbol=symbol,
                        requested_risk=fnum(params, "risk") or None,
                        volume=exec_volume,
                        sl=fnum(params, "sl") or None,
                        tp=fnum(params, "tp") or None,
                        entry=fnum(params, "entry", "entry_price") or None,
                        status=fill["status"],
                        broker_order_id=order_id or None,
                        comment=_comment or None,
                        error=err or None,
                    )
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        finally:
            if hb:
                hb.cancel()


async def main():
    init_mt5()
    if RISK_USD > 0:
        log(f"risk sizing active: max ${RISK_USD:g} loss per order")
    if NOTIFY_TOKEN and NOTIFY_CHAT:
        threading.Thread(target=position_monitor, daemon=True).start()
        log(f"position monitor active: notifying chat {NOTIFY_CHAT}")
    while True:
        try:
            await run_session()
            log("connection closed cleanly; reconnecting in 3s")
        except Exception as e:
            log(f"session error: {short_exc(e)}; reconnecting in 3s")
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())

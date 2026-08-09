"""Rey Capital — combined trade dashboard for the ExecRelay dev stack.

Single-file stdlib HTTP server, branded with the Rey Capital design system
(colors/typography/components mirrored from C:\\AccountManagementSystem
frontend/src/index.css). Combines, on every request:

  * transactions/telegram-signals.log*  -- Telegram-sourced signals
  * transactions/mt5-fills.log*         -- every order the EA shim executed,
    classified Telegram vs TradingView by its comment prefix ("tg-…")
  * the running MT5 terminal (optional) -- open positions + a selectable
    trailing window (7/30/90d) of closed deals for the shim's magic number,
    with realized P/L (partial closes grouped into one row per position)
  * .local-stack/journal.json           -- lightweight trading journal keyed
    by MT5 position ticket; fields mirror the ReyLens journal schema
    (setup / emotion / mistakes / rating / notes / reviewed) so entries can
    be migrated into ReyLens later.

Started by scripts/local-stack.ps1 as service "trade-dashboard". Binds to
localhost only. Two layers of protection since the page shows account
numbers and balances:

  * Host-header check on every request (DNS-rebinding guard) -- only
    127.0.0.1:<port> / localhost:<port> (port derived from DASHBOARD_ADDR).
  * Optional bearer token (env DASHBOARD_TOKEN): when set, every route
    except /health requires it via ?token=<value> or X-Dashboard-Token.

Environment:
    DASHBOARD_ADDR   default 127.0.0.1:8090
    DASHBOARD_TOKEN  optional bearer token; unset/empty = auth disabled
    EA_SHIM_MAGIC    default 20240101 (must match ea_shim.py)
"""

from __future__ import annotations

import csv
import hmac
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import MetaTrader5 as mt5
except ImportError:  # dashboard still works log-only
    mt5 = None

ADDR = os.environ.get("DASHBOARD_ADDR", "127.0.0.1:8090")
MAGIC = int(os.environ.get("EA_SHIM_MAGIC", "20240101"))
DASHBOARD_TOKEN = (os.environ.get("DASHBOARD_TOKEN") or "").strip()
ROOT = Path(__file__).resolve().parent.parent
TXN_DIR = ROOT / ".local-stack" / "logs" / "transactions"
JOURNAL_PATH = ROOT / ".local-stack" / "journal.json"
ASSETS = Path(__file__).resolve().parent / "dashboard-assets"

EMOTIONS = ["calm", "confident", "neutral", "anxious", "fearful", "greedy", "fomo", "revenge", "bored"]


def _allowed_hosts() -> set[str]:
    _, _, port = ADDR.rpartition(":")
    port = port or "8090"
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


ALLOWED_HOSTS = _allowed_hosts()

_journal_lock = threading.Lock()

# MT5 connection cache: avoid reconnecting on every request, but don't cache
# a broken connection forever -- if account_info() ever comes back None
# (terminal closed/relaunched, logged out, etc.) we shut down and retry
# initialize() on a simple time-based backoff so a dead terminal doesn't
# turn every request into a slow retry loop.
_mt5_state = {"ready": False, "last_attempt": 0.0}
_MT5_RETRY_BACKOFF_SEC = 10.0

# Broker-server UTC offset, estimated once from a live tick and cached for
# the life of the process (see _estimate_broker_offset).
_broker_offset_state: dict = {"offset": None}

# Parsed-JSONL cache for the txn log files, keyed by path with (size, mtime)
# validation so unchanged files are not re-read/re-parsed every request.
_txn_cache_lock = threading.Lock()
_txn_cache: dict[str, tuple[int, float, list[dict]]] = {}


def _ensure_mt5() -> bool:
    if mt5 is None:
        return False
    if _mt5_state["ready"]:
        return True
    now = time.time()
    if now - _mt5_state["last_attempt"] < _MT5_RETRY_BACKOFF_SEC:
        return False
    _mt5_state["last_attempt"] = now
    _mt5_state["ready"] = bool(mt5.initialize())
    return _mt5_state["ready"]


def _mt5_reset() -> None:
    """Drop the cached "ready" connection so the next request retries
    initialize() (after the backoff) instead of trusting a stale state."""
    if mt5 is not None:
        try:
            mt5.shutdown()
        except Exception:
            pass
    _mt5_state["ready"] = False


def _estimate_broker_offset(symbol_hint: str | None) -> float | None:
    """MT5 deal/tick times are stamped in the broker server's timezone, not
    UTC. Estimate that offset from a live tick: the raw skew between the
    tick's reported time and our wall clock is (broker offset + tick age).
    Rounding the skew to the nearest 30 minutes gives the offset (brokers
    use whole/half-hour zones); if the leftover residual after rounding is
    more than ~10 minutes, the tick is too stale to trust (market closed)
    and the offset is left unknown rather than caching a bad guess."""
    if _broker_offset_state["offset"] is not None:
        return _broker_offset_state["offset"]
    if mt5 is None:
        return None
    symbol = symbol_hint or "XAUUSD_"
    try:
        tick = mt5.symbol_info_tick(symbol)
    except Exception:
        tick = None
    tick_time = getattr(tick, "time", None) if tick else None
    if not tick_time:
        return None
    delta = tick_time - time.time()
    rounded = round(delta / 1800.0) * 1800.0
    if abs(delta - rounded) > 600:
        return None
    # A stale tick (market closed over a weekend) can be hours old and still
    # round cleanly to a multiple of 30 min, masquerading as an offset. Real
    # broker offsets are a few hours (MT5 brokers cluster around UTC+2/+3),
    # so anything beyond +/-4h is staleness, not timezone -- leave unknown
    # and let times be labeled "broker time" instead of caching a bad guess.
    if abs(rounded) > 4 * 3600:
        return None
    _broker_offset_state["offset"] = rounded
    return rounded


def _fmt_time(raw: float | None, offset: float | None) -> str | None:
    if raw is None:
        return None
    ts = raw - offset if offset is not None else raw
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


def _read_one(f: Path) -> list[dict]:
    try:
        st = f.stat()
    except OSError:
        return []
    key = str(f)
    with _txn_cache_lock:
        cached = _txn_cache.get(key)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]
    records: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        return []
    with _txn_cache_lock:
        _txn_cache[key] = (st.st_size, st.st_mtime, records)
    return records


def _read_txn(name: str) -> list[dict]:
    """All retained JSONL records for one txn logger, oldest first."""
    files = sorted(TXN_DIR.glob(f"{name}.log.*")) + [TXN_DIR / f"{name}.log"]
    records: list[dict] = []
    for f in files:
        records.extend(_read_one(f))
    return records


def _load_journal() -> dict:
    try:
        return json.loads(JOURNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_journal(journal: dict) -> None:
    tmp = JOURNAL_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(journal, indent=1), encoding="utf-8")
    tmp.replace(JOURNAL_PATH)


def _side_of(command: str) -> str:
    c = (command or "").lower()
    if c == "closeshortopenlong" or c.startswith("buy"):
        return "buy"
    if c == "closelongopenshort" or c.startswith("sell"):
        return "sell"
    return ""


def _source_of(comment: str) -> str:
    return "telegram" if (comment or "").startswith("tg-") else "tradingview"


def _deal_source(magic: int, comment: str) -> str:
    """Classify account activity: this stack's trades (by magic) split into
    telegram/tradingview by comment; anything else on the account (other
    EAs, manual trades) is "other"."""
    if magic == MAGIC:
        return _source_of(comment)
    return "other"


def _redact(cmd: str) -> str:
    return re.sub(r"secret=[^,]*", "secret=***", cmd or "")


def _signal_stats() -> dict:
    rows = _read_txn("telegram-signals")
    by_outcome: dict[str, int] = {}
    for r in rows:
        outcome = r.get("outcome", "other")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    recent = [
        {
            "ts": r.get("ts", ""),
            "channel": r.get("channel") or "direct",
            "outcome": r.get("outcome", ""),
            "detail": _redact(r.get("command") or r.get("raw_text") or "")[:130],
        }
        for r in rows[-12:]
    ][::-1]
    return {
        "received": len(rows),
        "posted": by_outcome.get("posted", 0),
        "rejected": by_outcome.get("rejected", 0),
        "dry_run": by_outcome.get("dry_run", 0),
        "errors": by_outcome.get("webhook_error", 0),
        "recent": recent,
    }


def _order_stats() -> dict:
    rows = [r for r in _read_txn("mt5-fills") if r.get("command")]
    out = {"telegram": _empty_bucket(), "tradingview": _empty_bucket()}
    recent = []
    for r in rows:
        b = out[_source_of(r.get("comment", ""))]
        b["total"] += 1
        status = r.get("status")
        if status in ("filled", "placed"):
            b["executed"] += 1
        elif status == "rejected":
            b["rejected"] += 1
        side = _side_of(r.get("command", ""))
        if side == "buy":
            b["buys"] += 1
        elif side == "sell":
            b["sells"] += 1
        if r.get("event") != "position_closed":
            recent.append(
                {
                    "ts": r.get("ts", ""),
                    "source": _source_of(r.get("comment", "")),
                    "command": r.get("command", ""),
                    "symbol": r.get("symbol", ""),
                    "risk": r.get("risk") or "",
                    "volume": r.get("volume") or "",
                    "sl": r.get("sl") or "",
                    "tp": r.get("tp") or "",
                    "status": status or "",
                    "error": (r.get("error") or "")[:80],
                }
            )
    return {"by_source": out, "recent": recent[-12:][::-1]}


def _empty_bucket() -> dict:
    return {"total": 0, "executed": 0, "rejected": 0, "buys": 0, "sells": 0}


def _fetch_deals(days: int) -> list:
    if not _ensure_mt5():
        return []
    now = datetime.now()
    return list(mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or [])


def _group_closed(deals: list) -> list[dict]:
    """Group closing deals by position_id -- a position closed in several
    partial fills otherwise appears as multiple rows and double-counts
    win/loss/net. One row per position: summed profit(+commission+swap),
    summed closed volume, last close price/time; entry price (volume
    weighted) and entry time (first IN deal) pulled from the matching
    DEAL_ENTRY_IN deals in the same window when available."""
    out_entries = (mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", 2))
    ins: dict[int, list] = {}
    outs: dict[int, list] = {}
    for d in deals:
        if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
            continue
        if d.entry == mt5.DEAL_ENTRY_IN:
            ins.setdefault(d.position_id, []).append(d)
        elif d.entry in out_entries:
            outs.setdefault(d.position_id, []).append(d)

    rows: list[dict] = []
    for pos_id, out_deals in outs.items():
        out_deals = sorted(out_deals, key=lambda x: x.time)
        last = out_deals[-1]
        profit = round(sum(x.profit + x.commission + x.swap for x in out_deals), 2)
        commission = round(sum(x.commission for x in out_deals), 2)
        swap = round(sum(x.swap for x in out_deals), 2)
        volume = round(sum(x.volume for x in out_deals), 4)
        side = "sell" if last.type == mt5.DEAL_TYPE_BUY else "buy"

        in_deals = sorted(ins.get(pos_id, []), key=lambda x: x.time)
        entry_price = None
        entry_time_raw = None
        if in_deals:
            tot_vol = sum(x.volume for x in in_deals)
            if tot_vol:
                entry_price = round(sum(x.price * x.volume for x in in_deals) / tot_vol, 5)
            entry_time_raw = in_deals[0].time

        rows.append(
            {
                "ticket": str(pos_id),
                "time_raw": last.time,
                "symbol": last.symbol,
                "side": side,
                "volume": volume,
                "close": last.price,
                "entry": entry_price,
                "entry_time_raw": entry_time_raw,
                "profit": profit,
                "commission": commission,
                "swap": swap,
                "source": _deal_source(last.magic, last.comment),
                "comment": last.comment,
                "magic": last.magic,
            }
        )
    return rows


def _daily_pnl(rows: list[dict], days: int) -> list[dict]:
    buckets: dict[str, float] = {}
    for r in rows:
        d = (r.get("time") or "")[:10]
        if d:
            buckets[d] = round(buckets.get(d, 0.0) + r["profit"], 2)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    out = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        out.append({"date": key, "pnl": buckets.get(key, 0.0)})
        cur += timedelta(days=1)
    return out


def _closed_positions(days: int, journal: dict) -> tuple[list[dict], float | None]:
    """Grouped closed positions for the trailing `days` window, newest
    first, with times converted using the estimated broker offset (see
    _estimate_broker_offset) and journal entries attached."""
    if not _ensure_mt5():
        return [], None
    positions = mt5.positions_get() or []
    offset = _estimate_broker_offset(positions[0].symbol if positions else None)
    deals = _fetch_deals(days)
    grouped = _group_closed(deals)
    for r in grouped:
        r["time"] = _fmt_time(r["time_raw"], offset)
        r["entry_time"] = _fmt_time(r["entry_time_raw"], offset) if r.get("entry_time_raw") else None
        r["journal"] = journal.get(r["ticket"]) or None
    grouped.sort(key=lambda r: r["time_raw"], reverse=True)
    return grouped, offset


def _mt5_stats(journal: dict, days: int) -> dict:
    if not _ensure_mt5():
        return {"available": False}
    acct = mt5.account_info()
    if acct is None:
        # Was ready before but the terminal stopped answering -- reset so
        # the next request retries initialize() instead of caching this
        # broken state forever.
        _mt5_reset()
        return {"available": False}

    open_rows = []
    positions = mt5.positions_get() or []
    for p in positions:
        open_rows.append(
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume": p.volume,
                "entry": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": round(p.profit, 2),
                "source": _deal_source(p.magic, p.comment),
            }
        )
    open_floating = round(sum(p.profit for p in positions), 2) if positions else 0.0

    grouped, offset = _closed_positions(days, journal)
    time_label = "UTC" if offset is not None else "broker time"

    wins = [r for r in grouped if r["profit"] >= 0]
    by_source: dict[str, float] = {"telegram": 0.0, "tradingview": 0.0, "other": 0.0}
    for r in grouped:
        by_source[r["source"]] = round(by_source.get(r["source"], 0.0) + r["profit"], 2)

    review_queue = [r for r in grouped if not r.get("journal")][:8]

    return {
        "available": True,
        "account": acct.login,
        "currency": acct.currency,
        "balance": acct.balance,
        "equity": acct.equity,
        "open": open_rows,
        "open_floating": open_floating,
        "time_label": time_label,
        "closed": {
            "days": days,
            "count": len(grouped),
            "wins": len(wins),
            "losses": len(grouped) - len(wins),
            "net": round(sum(r["profit"] for r in grouped), 2),
            "buys": sum(1 for r in grouped if r["side"] == "buy"),
            "sells": sum(1 for r in grouped if r["side"] == "sell"),
            "rows": grouped,
            "by_source": by_source,
            "daily": _daily_pnl(grouped, days),
            "review_queue": review_queue,
        },
    }


def summary(days: int = 7) -> dict:
    journal = _load_journal()
    journaled = [j for j in journal.values() if j.get("setup") or j.get("notes") or j.get("rating")]
    ratings = [j["rating"] for j in journaled if j.get("rating")]
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "signals": _signal_stats(),
        "orders": _order_stats(),
        "mt5": _mt5_stats(journal, days),
        "journal": {
            "entries": len(journaled),
            "reviewed": sum(1 for j in journaled if j.get("reviewed")),
            "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "emotions": EMOTIONS,
        },
    }


def save_journal_entry(payload: dict) -> dict:
    ticket = str(payload.get("ticket", "")).strip()
    if not ticket:
        raise ValueError("ticket required")
    rating = payload.get("rating")
    entry = {
        "setup": str(payload.get("setup", ""))[:120],
        "emotion": payload.get("emotion") if payload.get("emotion") in EMOTIONS else "",
        "mistakes": str(payload.get("mistakes", ""))[:500],
        "rating": int(rating) if rating and str(rating).isdigit() and 1 <= int(rating) <= 5 else None,
        "notes": str(payload.get("notes", ""))[:4000],
        "reviewed": bool(payload.get("reviewed")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _journal_lock:
        journal = _load_journal()
        journal[ticket] = entry
        _save_journal(journal)
    return entry


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@")


def _csv_cell(v) -> str:
    """CSV-injection guard: a cell opening with =, +, -, or @ can be
    interpreted as a formula by Excel/Sheets when the file is opened;
    prefix it with a single quote to force text interpretation."""
    s = "" if v is None else str(v)
    if s and s[0] in _CSV_DANGEROUS_PREFIXES:
        return "'" + s
    return s


def _write_csv(header: list[str], data_rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in data_rows:
        w.writerow([_csv_cell(x) for x in row])
    return buf.getvalue().encode("utf-8")


def trades_csv(days: int) -> bytes:
    """ReyLens import-template column order for closed (grouped) trades."""
    journal = _load_journal()
    rows: list[dict] = []
    if _ensure_mt5() and mt5.account_info() is not None:
        rows, _ = _closed_positions(days, journal)
    elif _mt5_state["ready"] and mt5 is not None and mt5.account_info() is None:
        _mt5_reset()

    data = [
        [
            "",  # account_id: ReyLens account UUID isn't known here
            r["symbol"],
            r["side"],
            r["volume"],
            r.get("entry") if r.get("entry") is not None else "",
            r["close"],
            r.get("entry_time") or "",
            r.get("time") or "",
            r["profit"],
            "",  # pnl_pct: not reliably derivable without margin/contract size
            r.get("commission", 0),
            r.get("swap", 0),
            r.get("magic", ""),
            r.get("comment", ""),
            "false",
            "closed",
        ]
        for r in rows
    ]
    header = [
        "account_id", "symbol", "side", "lot_size", "entry_price", "exit_price",
        "entry_time", "exit_time", "pnl", "pnl_pct", "commission", "swap",
        "magic_number", "comment", "is_manual", "status",
    ]
    return _write_csv(header, data)


def journal_csv() -> bytes:
    journal = _load_journal()
    lookup: dict[str, dict] = {}
    # Wide window so tickets journaled a while ago still resolve to their
    # symbol/side/close time/profit; on-demand export, not on the hot path.
    if _ensure_mt5() and mt5.account_info() is not None:
        rows, _ = _closed_positions(365, journal)
        lookup = {r["ticket"]: r for r in rows}
    elif _mt5_state["ready"] and mt5 is not None and mt5.account_info() is None:
        _mt5_reset()

    data = []
    for ticket, j in journal.items():
        r = lookup.get(ticket, {})
        data.append(
            [
                ticket,
                r.get("symbol", ""),
                r.get("side", ""),
                r.get("time", ""),
                r.get("profit", ""),
                j.get("setup", ""),
                j.get("emotion", ""),
                j.get("mistakes", ""),
                j.get("rating") if j.get("rating") is not None else "",
                j.get("reviewed", False),
                j.get("notes", ""),
                j.get("updated_at", ""),
            ]
        )
    header = [
        "ticket", "symbol", "side", "close_time", "profit", "setup", "emotion",
        "mistakes", "rating", "reviewed", "notes", "updated_at",
    ]
    return _write_csv(header, data)


def _parse_days(path: str) -> int:
    qs = parse_qs(urlsplit(path).query)
    raw = (qs.get("days") or ["7"])[0]
    try:
        d = int(raw)
    except ValueError:
        d = 7
    return max(1, min(120, d))


# ---------------------------------------------------------------------------
# UI — Rey Capital design system (mirrored from AccountManagementSystem)
# ---------------------------------------------------------------------------

LOGO_SVG = """<svg viewBox="0 0 89 89" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
<g transform="matrix(28.48369473913729,0,0,28.48369473913729,-26.717708028593407,-27.14496304221794)">
<polygon points="2.499,1.705 4.062,2.605 4.062,1.854 2.498,0.953 0.938,1.855 0.938,2.607"/>
<polygon points="3.812,3.51 4.062,3.363 4.062,2.902 2.498,2 0.938,2.902 0.938,3.365 1.188,3.51 2.498,2.752"/>
<polygon points="2.499,3.818 2.896,4.047 3.548,3.672 2.498,3.066 1.452,3.672 2.104,4.047"/>
</g></svg>"""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<script>(function(){try{var k="execrelay-dashboard-theme";var t=localStorage.getItem(k)||((window.matchMedia&&matchMedia("(prefers-color-scheme: light)").matches)?"light":"dark");if(t==="light")document.documentElement.classList.add("light");}catch(e){}})();</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rey Capital | Trade Operations</title>
<link rel="icon" href="/assets/favicon.png">
<style>
:root {
  --color-background: #020c1b;
  --color-surface: #061526;
  --color-surface-2: #0b2040;
  --color-border: #123060;
  --color-border-light: #1d4a8a;
  --color-primary: #00c2e0;
  --color-primary-dim: #00a3be;
  --color-primary-glow: rgb(0 194 224 / 0.18);
  --color-profit: #05e8a4;
  --color-profit-dim: rgb(5 232 164 / 0.14);
  --color-loss: #ff3d5f;
  --color-loss-dim: rgb(255 61 95 / 0.14);
  --color-warning: #ffb52e;
  --color-gold: #f4b942;
  --color-text: #cde4ff;
  --color-text-muted: #4e7aab;
  --color-text-dim: #7aa3cc;
  --radius-sm: 0.375rem;
  --radius: 0.625rem;
  --radius-lg: 0.875rem;
}
html.light {
  --color-background: #eef4ff;
  --color-surface: #ffffff;
  --color-surface-2: #deeaf8;
  --color-border: #b8d0ec;
  --color-border-light: #90b8e4;
  --color-primary: #0077b6;
  --color-primary-dim: #005f99;
  --color-primary-glow: rgb(0 119 182 / 0.12);
  --color-profit: #00916e;
  --color-profit-dim: rgb(0 145 110 / 0.12);
  --color-loss: #d62839;
  --color-loss-dim: rgb(214 40 57 / 0.12);
  --color-warning: #c77c00;
  --color-gold: #b8860b;
  --color-text: #08203f;
  --color-text-muted: #3a6290;
  --color-text-dim: #1e4e7a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, "Segoe UI", Roboto, sans-serif;
  background-color: var(--color-background);
  color: var(--color-text);
}
.number { font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-thumb { background: var(--color-border-light); border-radius: 2px; }

header {
  position: sticky; top: 0; z-index: 10;
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center;
  gap: 1rem; padding: 0.875rem 1.5rem;
  background: color-mix(in srgb, var(--color-surface) 85%, transparent 15%);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--color-border);
  box-shadow: 0 1px 0 color-mix(in srgb, var(--color-primary) 6%, transparent), 0 4px 16px rgb(0 0 0 / 0.25);
}
.brand { display: flex; align-items: center; gap: 0.65rem; }
.logo-tile {
  width: 2.25rem; height: 2.25rem; border-radius: 0.5rem; background: #004AAC;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  box-shadow: 0 4px 14px rgb(0 0 0 / 0.4);
}
.logo-tile svg { width: 58%; height: 58%; color: #fff; }
.brand-name { font-size: 0.95rem; font-weight: 600; line-height: 1.15; }
.brand-sub { font-size: 0.68rem; color: var(--color-text-muted); }
.kpi { text-align: center; }
.kpi-label { font-size: 10px; letter-spacing: 0.15em; text-transform: uppercase; color: var(--color-text-muted); }
.kpi-value { font-size: 1.125rem; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 0.75rem; justify-self: end; }
.icon-btn {
  background: transparent; border: 1px solid var(--color-border-light); color: var(--color-text-dim);
  border-radius: 999px; width: 2rem; height: 2rem; display: flex; align-items: center; justify-content: center;
  cursor: pointer; font-size: 0.95rem; line-height: 1; flex-shrink: 0;
}
.icon-btn:hover { background: var(--color-primary-glow); color: var(--color-primary); }
.acct { text-align: right; font-size: 0.72rem; color: var(--color-text-muted); line-height: 1.5; }
.acct b { color: var(--color-text-dim); font-weight: 500; }

main { max-width: 1600px; margin: 0 auto; padding: 1.5rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; }
.srcpl { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.stat-card {
  background: linear-gradient(180deg, var(--color-surface-2), var(--color-surface));
  border: 1px solid var(--color-border); border-radius: var(--radius);
  padding: 1rem 1.25rem; transition: transform .15s, box-shadow .15s;
}
.stat-card:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgb(0 0 0 / 0.35), 0 0 12px var(--color-primary-glow); }
.stat-card b { display: block; font-size: 1.35rem; font-weight: 600; }
.stat-card span { color: var(--color-text-muted); font-size: 0.72rem; }
.stat-card .sub { font-size: 0.68rem; color: var(--color-text-dim); margin-top: 2px; }

h2 { font-size: 0.82rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--color-text-dim); margin: 2rem 0 0.75rem; display:flex; align-items:center; gap:.5rem; flex-wrap: wrap; }
h2 .chip { font-size: 0.65rem; letter-spacing: normal; text-transform: none; border-radius: 999px; padding: 0.1rem 0.6rem; border: 1px solid var(--color-border-light); color: var(--color-text-muted); }
.grid2 { display: grid; grid-template-columns: 1fr; gap: 1.5rem; }
@media (min-width: 1100px) { .grid2 { grid-template-columns: 1fr 1fr; } }

.seg { display: inline-flex; border: 1px solid var(--color-border-light); border-radius: 999px; overflow: hidden; }
.seg button { background: transparent; border: 0; color: var(--color-text-muted); font-size: 0.68rem; padding: 0.25rem 0.7rem; cursor: pointer; letter-spacing: normal; text-transform: none; }
.seg button.active { background: var(--color-primary-glow); color: var(--color-primary); }
.exports { font-size: 0.72rem; letter-spacing: normal; text-transform: none; }
.exports a { color: var(--color-primary); text-decoration: none; margin-right: 0.75rem; }
.exports a:hover { text-decoration: underline; }

.review-strip { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0 0 1rem; }
.review-chip {
  display: flex; align-items: center; gap: 0.5rem; background: var(--color-surface-2);
  border: 1px solid var(--color-border); border-radius: 999px; padding: 0.3rem 0.4rem 0.3rem 0.75rem;
  font-size: 0.72rem; color: var(--color-text-dim);
}
.review-chip button {
  background: transparent; border: 1px solid var(--color-border-light); color: var(--color-primary);
  border-radius: 999px; padding: 0.1rem 0.6rem; font-size: 0.68rem; cursor: pointer;
}

.chart-card {
  background: linear-gradient(180deg, var(--color-surface-2), var(--color-surface));
  border: 1px solid var(--color-border); border-radius: var(--radius-lg);
  padding: 1rem 1.25rem; margin-bottom: 0.5rem; overflow-x: auto;
}
.chart-card svg { width: 100%; height: 120px; display: block; min-width: 320px; }

.tablewrap { overflow-x: auto; border: 1px solid var(--color-border); border-radius: var(--radius-lg); background: linear-gradient(180deg, color-mix(in srgb, var(--color-surface-2) 60%, var(--color-surface)), var(--color-surface)); }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
thead { background: var(--color-surface-2); }
th { text-align: left; padding: 0.5rem 0.75rem; color: var(--color-text-muted); font-weight: 500; font-size: 0.72rem; white-space: nowrap; }
td { padding: 0.45rem 0.75rem; border-top: 1px solid var(--color-border); white-space: nowrap; }
tfoot td { border-top: 1px solid var(--color-border-light); font-weight: 600; }
.pos { color: var(--color-profit); } .neg { color: var(--color-loss); } .warn { color: var(--color-warning); }
.muted { color: var(--color-text-muted); }
.badge { display: inline-block; padding: 0.05rem 0.5rem; border-radius: 999px; font-size: 0.68rem; }
.badge.buy { background: var(--color-profit-dim); color: var(--color-profit); }
.badge.sell { background: var(--color-loss-dim); color: var(--color-loss); }
.badge.tg { background: var(--color-primary-glow); color: var(--color-primary); }
.badge.tv { background: color-mix(in srgb, var(--color-gold) 15%, transparent); color: var(--color-gold); }
.badge.ok { background: var(--color-profit-dim); color: var(--color-profit); }
.badge.bad { background: var(--color-loss-dim); color: var(--color-loss); }
.badge.neutral { background: var(--color-surface-2); color: var(--color-text-dim); }
.stars { color: var(--color-gold); letter-spacing: 1px; }
button.jbtn {
  background: transparent; border: 1px solid var(--color-border-light); color: var(--color-primary);
  border-radius: var(--radius-sm); padding: 0.15rem 0.6rem; font-size: 0.7rem; cursor: pointer;
}
button.jbtn:hover { background: var(--color-primary-glow); }

#modal-scrim { position: fixed; inset: 0; background: rgb(0 0 0 / 0.55); display: none; align-items: center; justify-content: center; z-index: 50; }
#modal {
  width: min(480px, 92vw); background: linear-gradient(180deg, var(--color-surface-2), var(--color-surface));
  border: 1px solid var(--color-border-light); border-radius: var(--radius-lg); padding: 1.5rem;
  box-shadow: 0 20px 60px rgb(0 0 0 / 0.5);
}
#modal h3 { margin: 0 0 1rem; font-size: 0.95rem; }
#modal label { display: block; font-size: 0.7rem; color: var(--color-text-muted); margin: 0.7rem 0 0.25rem; text-transform: uppercase; letter-spacing: 0.06em; }
#modal input[type=text], #modal textarea, #modal select {
  width: 100%; background: var(--color-background); color: var(--color-text);
  border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 0.45rem 0.6rem; font-size: 0.82rem;
  font-family: inherit;
}
#modal textarea { min-height: 70px; resize: vertical; }
.rating-row { display: flex; gap: 0.3rem; font-size: 1.3rem; cursor: pointer; color: var(--color-border-light); }
.rating-row span.on { color: var(--color-gold); }
.modal-actions { display: flex; justify-content: flex-end; gap: 0.6rem; margin-top: 1.2rem; }
.btn-primary {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dim)); color: #04121f;
  border: 0; border-radius: var(--radius-sm); padding: 0.45rem 1.1rem; font-weight: 600; font-size: 0.8rem; cursor: pointer;
}
.btn-ghost { background: transparent; border: 1px solid var(--color-border); color: var(--color-text-dim); border-radius: var(--radius-sm); padding: 0.45rem 1rem; font-size: 0.8rem; cursor: pointer; }
.checkline { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.8rem; font-size: 0.8rem; color: var(--color-text-dim); }
#meta { color: var(--color-text-muted); font-size: 0.72rem; margin: 2.5rem 0 1rem; }
</style></head><body>
<header>
  <div class="brand">
    <div class="logo-tile">__LOGO__</div>
    <div><div class="brand-name">Rey Capital</div><div class="brand-sub">Trade Operations</div></div>
  </div>
  <div class="kpi"><div class="kpi-label" id="kpi-label">Net P/L · 7 days</div><div class="kpi-value number" id="kpi-net">—</div></div>
  <div class="header-right">
    <button id="theme-toggle" class="icon-btn" type="button" title="Toggle theme" aria-label="Toggle theme">&#9789;</button>
    <div class="acct" id="acct">connecting…</div>
  </div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="grid2">
    <section>
      <h2>Telegram signals <span class="chip" id="chip-tg"></span></h2>
      <div class="tablewrap"><table id="tbl-signals"></table></div>
    </section>
    <section>
      <h2>Orders executed <span class="chip" id="chip-orders"></span></h2>
      <div class="tablewrap"><table id="tbl-orders"></table></div>
    </section>
  </div>

  <h2>Open positions</h2>
  <div class="tablewrap"><table id="tbl-open"></table></div>

  <h2>Performance by source <span class="chip" id="chip-srcpl"></span></h2>
  <div class="srcpl" id="srcpl"></div>

  <h2>Daily P/L</h2>
  <div class="chart-card"><div id="chart-daily"></div></div>

  <h2>Closed trades &amp; journal <span class="chip" id="chip-journal"></span>
    <span class="seg" id="seg-days">
      <button type="button" data-days="7" class="active">7d</button>
      <button type="button" data-days="30">30d</button>
      <button type="button" data-days="90">90d</button>
    </span>
    <span class="exports">
      <a id="export-trades" href="/api/export/trades.csv">Export trades CSV</a><a id="export-journal" href="/api/export/journal.csv">Export journal CSV</a>
    </span>
  </h2>
  <div class="review-strip" id="review-strip" style="display:none"></div>
  <div class="tablewrap"><table id="tbl-closed"></table></div>

  <div id="meta"></div>
</main>

<div id="modal-scrim"><div id="modal">
  <h3 id="modal-title">Journal</h3>
  <input type="hidden" id="j-ticket">
  <label>Setup / pattern</label><input type="text" id="j-setup" placeholder="e.g. breakout retest, supply zone">
  <label>Emotion</label><select id="j-emotion"><option value="">—</option></select>
  <label>Mistakes</label><input type="text" id="j-mistakes" placeholder="comma-separated, e.g. chased entry, moved SL">
  <label>Rating</label><div class="rating-row" id="j-rating"></div>
  <label>Notes / lessons</label><textarea id="j-notes"></textarea>
  <div class="checkline"><input type="checkbox" id="j-reviewed"><label for="j-reviewed" style="margin:0;text-transform:none;letter-spacing:0">Reviewed</label></div>
  <div class="modal-actions">
    <button class="btn-ghost" type="button" id="j-cancel">Cancel</button>
    <button class="btn-primary" type="button" id="j-save">Save entry</button>
  </div>
</div></div>

<script>
const $ = id => document.getElementById(id);
const money = (v, c) => (v >= 0 ? "+" : "\u2212") + "$" + Math.abs(v).toFixed(2);
const cls = v => v >= 0 ? "pos" : "neg";
const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const srcBadge = s => s === "telegram" ? '<span class="badge tg">Telegram</span>'
  : s === "tradingview" ? '<span class="badge tv">TradingView</span>'
  : '<span class="badge neutral">Other EA</span>';
const sideBadge = s => s === "close" ? '<span class="badge neutral">CLOSE</span>'
  : `<span class="badge ${s}">${s.toUpperCase()}</span>`;
let lastSummary = null, ratingVal = 0, currentDays = 7;

const TOKEN = new URLSearchParams(location.search).get("token") || "";
function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (TOKEN) h["X-Dashboard-Token"] = TOKEN;
  return h;
}
function withToken(url) {
  if (!TOKEN) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}
if (TOKEN) {
  const icon = document.querySelector('link[rel="icon"]');
  if (icon) icon.href = withToken("/assets/favicon.png");
}

function card(label, value, extra, sub) {
  return `<div class="stat-card"><b class="number ${extra||""}">${value}</b><span>${label}</span>${sub?`<div class="sub">${sub}</div>`:""}</div>`;
}
function table(id, header, rows, footer) {
  $(id).innerHTML = "<thead><tr>" + header.map(h => `<th>${h}</th>`).join("") + "</tr></thead><tbody>" +
    (rows.length ? rows.join("") : `<tr><td colspan="${header.length}" class="muted">none yet</td></tr>`) + "</tbody>" +
    (footer ? "<tfoot>" + footer + "</tfoot>" : "");
}

async function refresh() {
  const res = await fetch(withToken("/api/summary?days=" + currentDays), { headers: authHeaders() });
  if (!res.ok) { $("meta").textContent = "refresh failed: " + res.status + " " + (await res.text()); return; }
  const s = await res.json();
  lastSummary = s;
  const bs = s.orders.by_source, tg = bs.telegram, tv = bs.tradingview;
  const c = s.mt5.available ? s.mt5.closed : {count:0,wins:0,losses:0,net:0,rows:[],by_source:{},daily:[],review_queue:[]};
  const winRate = c.count ? Math.round(100 * c.wins / c.count) : null;
  const timeLabel = s.mt5.available ? (s.mt5.time_label || "broker time") : "UTC";

  $("kpi-label").textContent = `Net P/L · ${currentDays} days`;
  $("kpi-net").textContent = money(c.net);
  $("kpi-net").className = "kpi-value number " + cls(c.net);
  $("acct").innerHTML = s.mt5.available
    ? `<b>MT5 demo ${s.mt5.account}</b><br>balance $${s.mt5.balance.toFixed(2)} · equity $${s.mt5.equity.toFixed(2)}`
    : '<span class="neg">MT5 offline</span>';

  $("cards").innerHTML =
    card("Telegram signals", s.signals.received, "", `${s.signals.posted} routed · ${s.signals.rejected} rejected`) +
    card("Telegram orders", tg.executed, "", `${tg.buys} buy · ${tg.sells} sell`) +
    card("TradingView orders", tv.executed, "", `${tv.buys} buy · ${tv.sells} sell`) +
    card("Open positions", s.mt5.available ? s.mt5.open.length : "—") +
    card(`Closed · ${currentDays}d`, c.count, "", `${c.wins} win · ${c.losses} loss · ${c.rows.filter(r => r.source !== "other").length} from signals`) +
    card("Win rate", winRate === null ? "—" : winRate + "%", winRate === null ? "" : (winRate >= 50 ? "pos" : "neg")) +
    card(`Net P/L · ${currentDays}d`, money(c.net), cls(c.net)) +
    card("Journal", s.journal.entries, "", s.journal.avg_rating ? `avg rating ${s.journal.avg_rating}\u2605 · ${s.journal.reviewed} reviewed` : `${s.journal.reviewed} reviewed`);

  $("chip-tg").textContent = `${s.signals.received} total`;
  $("chip-orders").textContent = `${tg.total + tv.total} total`;
  $("chip-journal").textContent = `${s.journal.entries} entries`;
  $("chip-srcpl").textContent = `${currentDays}d window`;

  table("tbl-signals", ["time (UTC)","channel","outcome","detail"], s.signals.recent.map(r =>
    `<tr><td class="muted number">${esc((r.ts||"").slice(5,19).replace("T"," "))}</td><td>${esc(r.channel)}</td>
     <td><span class="badge ${r.outcome==="posted"?"ok":(r.outcome==="rejected"||r.outcome==="webhook_error"?"bad":"neutral")}">${esc(r.outcome)}</span></td>
     <td class="muted" style="white-space:normal;max-width:340px">${esc(r.detail)}</td></tr>`));

  table("tbl-orders", ["time (UTC)","source","command","symbol","size","SL","TP","status"], s.orders.recent.map(r =>
    `<tr><td class="muted number">${esc((r.ts||"").slice(5,19).replace("T"," "))}</td><td>${srcBadge(r.source)}</td>
     <td>${sideBadge(_sideOf(r.command))} <span class="muted">${esc(r.command)}</span></td><td>${esc(r.symbol)}</td>
     <td class="number">${r.risk ? "risk $"+esc(r.risk) : esc(r.volume)}</td>
     <td class="number">${esc(r.sl)}</td><td class="number">${esc(r.tp)}</td>
     <td><span class="badge ${r.status==="rejected"?"bad":"ok"}">${esc(r.status)}</span>${r.error?` <span class="neg" title="${esc(r.error)}">!</span>`:""}</td></tr>`));

  const openRows = s.mt5.open || [];
  const floatTotal = s.mt5.available ? (s.mt5.open_floating || 0) : 0;
  table("tbl-open", ["ticket","source","symbol","side","lot","entry","SL","TP","floating P/L"], openRows.map(p =>
    `<tr><td class="muted number">${p.ticket}</td><td>${srcBadge(p.source)}</td><td>${esc(p.symbol)}</td><td>${sideBadge(p.side)}</td>
     <td class="number">${p.volume}</td><td class="number">${p.entry}</td><td class="number">${p.sl}</td><td class="number">${p.tp}</td>
     <td class="number ${cls(p.profit)}">${money(p.profit)}</td></tr>`),
    openRows.length ? `<tr><td colspan="8" class="muted">Total floating</td><td class="number ${cls(floatTotal)}">${money(floatTotal)}</td></tr>` : "");

  const bySrc = c.by_source || {};
  $("srcpl").innerHTML =
    card("Telegram", money(bySrc.telegram || 0), cls(bySrc.telegram || 0), `net over ${currentDays}d`) +
    card("TradingView", money(bySrc.tradingview || 0), cls(bySrc.tradingview || 0), `net over ${currentDays}d`) +
    card("Other EA / manual", money(bySrc.other || 0), cls(bySrc.other || 0), `net over ${currentDays}d`);

  renderChart(c.daily || []);

  const rq = (c.review_queue || []).slice(0, 8);
  if (rq.length) {
    $("review-strip").style.display = "flex";
    $("review-strip").innerHTML = rq.map(r =>
      `<div class="review-chip">Unjournaled: ${r.side.toUpperCase()} ${esc(r.symbol)} ${money(r.profit)}
       <button type="button" data-ticket="${esc(r.ticket)}">Journal</button></div>`).join("");
  } else {
    $("review-strip").style.display = "none";
    $("review-strip").innerHTML = "";
  }

  const closedHeader = [`closed (${timeLabel})`,"source","symbol","side","lot","entry","close","P/L","setup","emotion","rating","",""];
  table("tbl-closed", closedHeader, c.rows.map(r => {
    const j = r.journal || {};
    return `<tr><td class="muted number">${esc((r.time||"").slice(5,19).replace("T"," "))}</td><td>${srcBadge(r.source)}</td>
     <td>${esc(r.symbol)}</td><td>${sideBadge(r.side)}</td><td class="number">${r.volume}</td>
     <td class="number">${r.entry ?? ""}</td><td class="number">${r.close}</td>
     <td class="number ${cls(r.profit)}">${money(r.profit)}</td>
     <td>${esc(j.setup||"")}</td><td class="muted">${esc(j.emotion||"")}</td>
     <td class="stars">${j.rating ? "\u2605".repeat(j.rating) : ""}</td>
     <td>${j.reviewed ? '<span class="badge ok">reviewed</span>' : ""}</td>
     <td><button class="jbtn" type="button" data-ticket="${esc(r.ticket)}">Journal</button></td></tr>`;
  }));

  $("export-trades").href = withToken("/api/export/trades.csv?days=" + currentDays);
  $("export-journal").href = withToken("/api/export/journal.csv");

  $("meta").textContent = "updated " + s.updated + " — auto-refreshes every 10s — journal entries are stored locally and mirror the ReyLens schema";
}

function renderChart(daily) {
  const el = $("chart-daily");
  if (!daily.length) { el.innerHTML = '<span class="muted">no data</span>'; return; }
  const w = 1000, h = 120, padB = 18, padT = 6;
  const max = Math.max(1, ...daily.map(d => Math.abs(d.pnl)));
  const n = daily.length;
  const bw = w / n;
  const zero = padT + (h - padT - padB) / 2;
  const scale = ((h - padT - padB) / 2) / max;
  const tickEvery = Math.max(1, Math.ceil(n / 10));
  let bars = "", labels = "";
  daily.forEach((d, i) => {
    const x = i * bw + bw * 0.15;
    const bwid = bw * 0.7;
    const barH = Math.max(Math.abs(d.pnl) * scale, d.pnl !== 0 ? 1 : 0);
    const y = d.pnl >= 0 ? zero - barH : zero;
    const color = d.pnl >= 0 ? "var(--color-profit)" : "var(--color-loss)";
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bwid.toFixed(1)}" height="${barH.toFixed(1)}" fill="${color}"><title>${esc(d.date)}: ${money(d.pnl)}</title></rect>`;
    if (i % tickEvery === 0 || i === n - 1) {
      labels += `<text x="${(x + bwid/2).toFixed(1)}" y="${h - 4}" font-size="9" text-anchor="middle" fill="var(--color-text-muted)">${esc(d.date.slice(5))}</text>`;
    }
  });
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Daily net P/L">
    <line x1="0" y1="${zero.toFixed(1)}" x2="${w}" y2="${zero.toFixed(1)}" stroke="var(--color-border-light)" stroke-width="1"/>
    ${bars}${labels}
  </svg>`;
}

function _sideOf(cmd) {
  cmd = (cmd||"").toLowerCase();
  if (cmd === "closeshortopenlong" || cmd.startsWith("buy")) return "buy";
  if (cmd === "closelongopenshort" || cmd.startsWith("sell")) return "sell";
  return "close";
}
function openModal(ticket) {
  const row = (lastSummary?.mt5?.closed?.rows || []).find(r => String(r.ticket) === String(ticket));
  const j = row?.journal || {};
  $("modal-title").textContent = `Journal — ${row ? row.side.toUpperCase()+" "+row.symbol+" ("+money(row.profit)+")" : "#"+ticket}`;
  $("j-ticket").value = ticket;
  $("j-setup").value = j.setup || "";
  $("j-emotion").value = j.emotion || "";
  $("j-mistakes").value = j.mistakes || "";
  $("j-notes").value = j.notes || "";
  $("j-reviewed").checked = !!j.reviewed;
  setRating(j.rating || 0);
  $("modal-scrim").style.display = "flex";
}
function closeModal() { $("modal-scrim").style.display = "none"; }
function setRating(n) {
  ratingVal = n;
  $("j-rating").innerHTML = [1,2,3,4,5].map(i =>
    `<span class="${i<=n?"on":""}" data-star="${i}">\u2605</span>`).join("");
}
async function saveJournal() {
  const payload = {
    ticket: $("j-ticket").value, setup: $("j-setup").value, emotion: $("j-emotion").value,
    mistakes: $("j-mistakes").value, rating: ratingVal || null, notes: $("j-notes").value,
    reviewed: $("j-reviewed").checked,
  };
  const res = await fetch(withToken("/api/journal"), { method: "POST", headers: authHeaders({"Content-Type":"application/json"}), body: JSON.stringify(payload) });
  if (res.ok) { closeModal(); refresh(); } else { alert("save failed: " + await res.text()); }
}

document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
$("modal-scrim").addEventListener("click", e => { if (e.target.id === "modal-scrim") closeModal(); });
$("j-cancel").addEventListener("click", closeModal);
$("j-save").addEventListener("click", saveJournal);
document.addEventListener("click", e => {
  const themeBtn = e.target.closest("#theme-toggle");
  if (themeBtn) { toggleTheme(); return; }
  const jbtn = e.target.closest("[data-ticket]");
  if (jbtn) { openModal(jbtn.getAttribute("data-ticket")); return; }
  const star = e.target.closest("[data-star]");
  if (star) { setRating(parseInt(star.getAttribute("data-star"), 10)); return; }
  const segBtn = e.target.closest("#seg-days button");
  if (segBtn) {
    currentDays = parseInt(segBtn.getAttribute("data-days"), 10);
    document.querySelectorAll("#seg-days button").forEach(b => b.classList.toggle("active", b === segBtn));
    refresh();
  }
});

const THEME_KEY = "execrelay-dashboard-theme";
function applyTheme(t) {
  document.documentElement.classList.toggle("light", t === "light");
  $("theme-toggle").innerHTML = t === "light" ? "&#9728;" : "&#9789;";
}
function toggleTheme() {
  const cur = document.documentElement.classList.contains("light") ? "light" : "dark";
  const next = cur === "light" ? "dark" : "light";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
}
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) {}
  const theme = saved || ((window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark");
  applyTheme(theme);
})();

fetch(withToken("/api/summary"), { headers: authHeaders() }).then(r => r.json()).then(s => {
  const sel = $("j-emotion");
  s.journal.emotions.forEach(e => { const o = document.createElement("option"); o.value = e; o.textContent = e; sel.appendChild(o); });
});
setRating(0); refresh(); setInterval(refresh, 10000);
</script></body></html>""".replace("__LOGO__", LOGO_SVG)


class Handler(BaseHTTPRequestHandler):
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ALLOWED_HOSTS

    def _token_ok(self) -> bool:
        if not DASHBOARD_TOKEN:
            return True
        supplied = self.headers.get("X-Dashboard-Token", "")
        if not supplied:
            qs = parse_qs(urlsplit(self.path).query)
            supplied = (qs.get("token") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, DASHBOARD_TOKEN)

    def _reject(self, status: int, msg: str) -> None:
        body = msg.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._host_ok():
            self._reject(403, "forbidden: bad host")
            return
        path = urlsplit(self.path).path
        if path in ("/health", "/healthz"):
            self._send(200, b'{"status":"ok"}', "application/json")
            return
        if not self._token_ok():
            self._reject(401, "unauthorized")
            return
        if path == "/api/summary":
            days = _parse_days(self.path)
            self._send(200, json.dumps(summary(days), default=str).encode(), "application/json")
        elif path == "/api/export/trades.csv":
            days = _parse_days(self.path)
            self._send_csv(trades_csv(days), "trades.csv")
        elif path == "/api/export/journal.csv":
            self._send_csv(journal_csv(), "journal.csv")
        elif path == "/assets/favicon.png":
            try:
                self._send(200, (ASSETS / "favicon.png").read_bytes(), "image/png")
            except OSError:
                self.send_response(404)
                self.end_headers()
        elif path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if not self._host_ok():
            self._reject(403, "forbidden: bad host")
            return
        path = urlsplit(self.path).path
        if path != "/api/journal":
            self.send_response(404)
            self.end_headers()
            return
        if not self._token_ok():
            self._reject(401, "unauthorized")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._reject(403, "forbidden: Content-Type must be application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            entry = save_journal_entry(payload)
            self._send(200, json.dumps(entry).encode(), "application/json")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, str(exc).encode(), "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, body: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


def main() -> None:
    host, port = ADDR.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(time.strftime("%H:%M:%S"), f"trade dashboard listening on http://{ADDR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

"""_tradestore — durable SQLite persistence for the local dev-harness trading
path (telegram-ingest + ea_shim), so management reporting has one queryable
store instead of scraping JSONL logs and MT5 history on every dashboard hit.

DB file: .local-stack/execrelay.db (WAL mode). Every signal telegram-ingest
parses, every order ea_shim places/reports, every closed position, and a
periodic account-equity snapshot lands here, correlated by ExecRelay's
`trace_id` (issued by ingress on webhook accept, carried through the bridge
to the EA's fill report).

Not the production persistence path -- see apps/persist/app.py for that
(ingress -> NATS -> persist, a separate service/DB). This module is dev-
harness tooling only, same tier as _txnlog.py.

CONTRACT: every public helper below (record_*, query, get_conn) swallows all
of its own exceptions, printing a one-line warning to stderr instead. A
SQLite hiccup (locked file, disk full, corrupt DB) must never take down the
telegram-ingest poll loop or an ea_shim fill report -- those are the actual
trading path. Callers should never need a try/except around these calls.

Multiple OS processes (telegram-ingest, ea_shim, and this module's own CLI)
open the database concurrently, so every write is a short-lived
connect -> PRAGMA -> statement -> commit -> close cycle (no long-held
connections, no cross-thread connection sharing) with a 5s busy_timeout to
absorb writer contention under WAL.

CLI:
    python scripts/_tradestore.py backfill   # idempotent import of existing
                                              # JSONL txn logs + MT5 closed-
                                              # deal history (90d)
    python scripts/_tradestore.py stats      # row counts per table
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / ".local-stack" / "execrelay.db"
LOG_DIR = Path(__file__).resolve().parent.parent / ".local-stack" / "logs" / "transactions"

SCHEMA_VERSION = "1"

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT,
        chat_id     INTEGER,
        message_id  INTEGER,
        channel     TEXT,
        outcome     TEXT,
        symbol      TEXT,
        side        TEXT,
        entry       REAL,
        sl          REAL,
        tp          REAL,
        n_commands  INTEGER,
        trace_ids   TEXT,
        raw         TEXT,
        UNIQUE(chat_id, message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               TEXT,
        trace_id         TEXT UNIQUE,
        source           TEXT,
        command          TEXT,
        symbol           TEXT,
        requested_risk   REAL,
        volume           REAL,
        sl               REAL,
        tp               REAL,
        entry            REAL,
        status           TEXT,
        broker_order_id  TEXT,
        comment          TEXT,
        error            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS closed_trades (
        position_id  TEXT PRIMARY KEY,
        close_ts     TEXT,
        symbol       TEXT,
        side         TEXT,
        volume       REAL,
        entry_price  REAL,
        close_price  REAL,
        profit       REAL,
        magic        INTEGER,
        comment      TEXT,
        source       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_snapshots (
        ts           TEXT PRIMARY KEY,
        balance      REAL,
        equity       REAL,
        margin       REAL,
        margin_free  REAL,
        floating     REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_signals_channel ON signals(channel)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source ON orders(source)",
    "CREATE INDEX IF NOT EXISTS idx_closed_trades_close_ts ON closed_trades(close_ts)",
)


def _warn(msg: str) -> None:
    print(f"_tradestore: {msg}", file=sys.stderr)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA_SQL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def get_conn() -> sqlite3.Connection | None:
    """Open a fresh connection to the trade store, WAL + busy_timeout applied,
    schema ensured. Caller owns the connection (close it when done).

    Swallows all exceptions per this module's contract -- returns None on
    any failure (missing dir, locked/corrupt file, etc.) instead of raising.
    Intended for read-only "advanced use" (e.g. a future dashboard); the
    record_* helpers below use this internally for writes.
    """
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)
        return conn
    except Exception as exc:  # noqa: BLE001 - never raise out of this module
        _warn(f"get_conn failed: {exc!r}")
        return None


def query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query, return rows as a list of dicts. Empty list on any
    failure (bad SQL, DB unavailable, ...) -- never raises."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return []
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        _warn(f"query failed: {exc!r}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Write helpers -- every one of these is no-throw (see module docstring).
# ---------------------------------------------------------------------------


def record_signal(
    chat_id: int,
    message_id: int,
    channel: str | None,
    outcome: str,
    symbol: str | None = None,
    side: str | None = None,
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    n_commands: int = 0,
    raw: str | None = None,
) -> None:
    """Insert (or refresh, on a re-processed key) one row per handled signal
    message. trace_ids starts as '[]' and is populated exclusively by
    append_signal_trace -- a re-call here never clobbers it."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO signals
                (ts, chat_id, message_id, channel, outcome, symbol, side,
                 entry, sl, tp, n_commands, trace_ids, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                ts=excluded.ts,
                channel=excluded.channel,
                outcome=excluded.outcome,
                symbol=excluded.symbol,
                side=excluded.side,
                entry=excluded.entry,
                sl=excluded.sl,
                tp=excluded.tp,
                n_commands=excluded.n_commands,
                raw=excluded.raw
            """,
            (
                _utcnow_iso(), chat_id, message_id, channel, outcome, symbol,
                side, entry, sl, tp, n_commands, raw,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"record_signal failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def append_signal_trace(chat_id: int, message_id: int, trace_id: str) -> None:
    """Append one trace_id to a signal row's trace_ids JSON array. No-op
    (with a warning) if the signal row doesn't exist yet -- record_signal
    should always run first in the caller, but this must never raise even
    if it didn't."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT trace_ids FROM signals WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            _warn(
                f"append_signal_trace: no signal row for chat_id={chat_id} "
                f"message_id={message_id}; skipping"
            )
            return
        try:
            ids = json.loads(row["trace_ids"] or "[]")
            if not isinstance(ids, list):
                ids = []
        except json.JSONDecodeError:
            ids = []
        if trace_id not in ids:
            ids.append(trace_id)
        conn.execute(
            "UPDATE signals SET trace_ids=? WHERE chat_id=? AND message_id=?",
            (json.dumps(ids), chat_id, message_id),
        )
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        _warn(f"append_signal_trace failed: {exc!r}")
        try:
            if conn is not None:
                conn.execute("ROLLBACK")
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_order(
    trace_id: str,
    source: str | None = None,
    command: str | None = None,
    symbol: str | None = None,
    requested_risk: float | None = None,
    volume: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    entry: float | None = None,
    status: str | None = None,
    broker_order_id: str | None = None,
    comment: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert one order row keyed by trace_id. telegram-ingest calls this
    first (status="accepted", requested fields) when the webhook accepts a
    command; ea_shim calls it again later with the same trace_id (executed
    volume, status=filled/placed/rejected, broker_order_id, error) when the
    fill comes back. Any field left None on a given call keeps its previous
    value rather than being overwritten (COALESCE), so the two callers can
    each supply a partial picture."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO orders
                (ts, trace_id, source, command, symbol, requested_risk,
                 volume, sl, tp, entry, status, broker_order_id, comment, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id) DO UPDATE SET
                ts=excluded.ts,
                source=COALESCE(excluded.source, orders.source),
                command=COALESCE(excluded.command, orders.command),
                symbol=COALESCE(excluded.symbol, orders.symbol),
                requested_risk=COALESCE(excluded.requested_risk, orders.requested_risk),
                volume=COALESCE(excluded.volume, orders.volume),
                sl=COALESCE(excluded.sl, orders.sl),
                tp=COALESCE(excluded.tp, orders.tp),
                entry=COALESCE(excluded.entry, orders.entry),
                status=COALESCE(excluded.status, orders.status),
                broker_order_id=COALESCE(excluded.broker_order_id, orders.broker_order_id),
                comment=COALESCE(excluded.comment, orders.comment),
                error=COALESCE(excluded.error, orders.error)
            """,
            (
                _utcnow_iso(), trace_id, source, command, symbol,
                requested_risk, volume, sl, tp, entry, status,
                broker_order_id, comment, error,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"record_order failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_closed_trade(
    position_id: str,
    close_ts: str,
    symbol: str,
    side: str,
    volume: float,
    entry_price: float | None,
    close_price: float,
    profit: float,
    magic: int,
    comment: str | None,
    source: str,
) -> None:
    """Upsert one closed-position row keyed by position_id (MT5 ticket).
    Idempotent -- re-recording the same position (e.g. a backfill re-run)
    overwrites with the latest computed values rather than duplicating."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO closed_trades
                (position_id, close_ts, symbol, side, volume, entry_price,
                 close_price, profit, magic, comment, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                close_ts=excluded.close_ts,
                symbol=excluded.symbol,
                side=excluded.side,
                volume=excluded.volume,
                entry_price=excluded.entry_price,
                close_price=excluded.close_price,
                profit=excluded.profit,
                magic=excluded.magic,
                comment=excluded.comment,
                source=excluded.source
            """,
            (
                str(position_id), close_ts, symbol, side, volume, entry_price,
                close_price, profit, magic, comment, source,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"record_closed_trade failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_equity(
    balance: float,
    equity: float,
    margin: float,
    margin_free: float,
    floating: float,
) -> None:
    """Insert one equity snapshot, timestamped now (UTC)."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO equity_snapshots (ts, balance, equity, margin, margin_free, floating)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET
                balance=excluded.balance,
                equity=excluded.equity,
                margin=excluded.margin,
                margin_free=excluded.margin_free,
                floating=excluded.floating
            """,
            (_utcnow_iso(), balance, equity, margin, margin_free, floating),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"record_equity failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Backfill (CLI only) -- imports existing JSONL txn logs + MT5 closed-deal
# history. Safe to re-run: every write above is an upsert.
# ---------------------------------------------------------------------------


def _iter_jsonl(paths: list[Path]):
    for f in paths:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _cmd_field(body: str, key: str) -> str:
    for part in (body or "").split(","):
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return ""


def _as_float(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _backfill_telegram_signals() -> tuple[int, int]:
    files = sorted(LOG_DIR.glob("telegram-signals.log*"))
    grouped: dict[tuple[int, int], list[dict]] = {}
    for rec in _iter_jsonl(files):
        chat_id = rec.get("chat_id")
        message_id = rec.get("message_id")
        if chat_id is None or message_id is None:
            continue
        grouped.setdefault((int(chat_id), int(message_id)), []).append(rec)

    n_signals = 0
    n_orders = 0
    for (chat_id, message_id), recs in grouped.items():
        rejected = [r for r in recs if r.get("outcome") == "rejected"]
        if rejected:
            r = rejected[0]
            record_signal(
                chat_id=chat_id,
                message_id=message_id,
                channel=r.get("channel"),
                outcome="rejected",
                n_commands=0,
                raw=(r.get("raw_text") or "")[:500],
            )
            n_signals += 1
            continue

        first = recs[0]
        sig = first.get("signal") or {}
        outcomes = {r.get("outcome") for r in recs}
        outcome = "posted" if "posted" in outcomes else (
            "dry_run" if "dry_run" in outcomes else (next(iter(outcomes), "other") or "other")
        )
        raw = None
        for r in recs:
            if r.get("command"):
                raw = r["command"][:500]
                break
        record_signal(
            chat_id=chat_id,
            message_id=message_id,
            channel=first.get("channel"),
            outcome=outcome,
            symbol=sig.get("symbol"),
            side=sig.get("side"),
            entry=sig.get("entry"),
            sl=sig.get("sl"),
            tp=sig.get("tp"),
            n_commands=len(recs),
            raw=raw,
        )
        n_signals += 1

        for r in recs:
            if r.get("outcome") != "posted" or r.get("http_status") != 200:
                continue
            try:
                trace_id = json.loads(r.get("response") or "{}").get("trace_id", "")
            except (json.JSONDecodeError, AttributeError):
                trace_id = ""
            if not trace_id:
                continue
            body = r.get("command", "")
            parts = body.split(",", 3)
            command = parts[1] if len(parts) > 1 else None
            symbol = parts[2] if len(parts) > 2 else None
            append_signal_trace(chat_id, message_id, trace_id)
            record_order(
                trace_id=trace_id,
                source="telegram",
                command=command,
                symbol=symbol,
                requested_risk=_as_float(_cmd_field(body, "risk")),
                volume=_as_float(_cmd_field(body, "vol_lots")),
                sl=_as_float(_cmd_field(body, "sl")),
                tp=_as_float(_cmd_field(body, "tp")),
                entry=_as_float(_cmd_field(body, "entry_price")),
                status="accepted",
                comment=_cmd_field(body, "comment") or None,
            )
            n_orders += 1
    return n_signals, n_orders


def _backfill_mt5_fills() -> int:
    files = sorted(LOG_DIR.glob("mt5-fills.log*"))
    n = 0
    for r in _iter_jsonl(files):
        trace_id = r.get("trace_id")
        if not trace_id or r.get("event") == "position_closed":
            continue
        comment = r.get("comment") or ""
        source = "telegram" if str(comment).startswith("tg-") else "tradingview"
        record_order(
            trace_id=trace_id,
            source=source,
            command=r.get("command"),
            symbol=r.get("symbol"),
            requested_risk=_as_float(r.get("risk")),
            volume=_as_float(r.get("volume")),
            sl=_as_float(r.get("sl")),
            tp=_as_float(r.get("tp")),
            entry=_as_float(r.get("entry")),
            status=r.get("status"),
            broker_order_id=r.get("broker_order_id") or None,
            comment=comment or None,
            error=r.get("error") or None,
        )
        n += 1
    return n


def _backfill_mt5_closed(days: int = 90) -> int:
    """Import MT5 closed-deal history, grouped by position_id (same rules
    trade_dashboard.py uses: skip DEAL_ENTRY_IN and non-buy/sell deal types,
    sum profit+commission+swap over OUT deals, source from magic+comment).
    Read-only against MT5. Skips cleanly if the package/terminal aren't
    available -- this is optional enrichment, not required for the rest of
    the backfill to succeed."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        _warn("MetaTrader5 package not importable; skipping closed-trade backfill")
        return 0

    if not mt5.initialize():
        _warn(f"mt5.initialize failed: {mt5.last_error()}; skipping closed-trade backfill")
        return 0

    try:
        magic = int(os.environ.get("EA_SHIM_MAGIC", "20240101"))
        now = datetime.now()
        deals = list(
            mt5.history_deals_get(now - timedelta(days=days), now + timedelta(days=1)) or []
        )
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

        n = 0
        for pos_id, out_deals in outs.items():
            out_deals = sorted(out_deals, key=lambda x: x.time)
            last = out_deals[-1]
            profit = round(sum(x.profit + x.commission + x.swap for x in out_deals), 2)
            volume = round(sum(x.volume for x in out_deals), 4)
            side = "sell" if last.type == mt5.DEAL_TYPE_BUY else "buy"

            in_deals = sorted(ins.get(pos_id, []), key=lambda x: x.time)
            entry_price = None
            if in_deals:
                tot_vol = sum(x.volume for x in in_deals)
                if tot_vol:
                    entry_price = round(sum(x.price * x.volume for x in in_deals) / tot_vol, 5)

            close_ts = datetime.fromtimestamp(last.time, tz=timezone.utc).isoformat()
            source = (
                ("telegram" if str(last.comment).startswith("tg-") else "tradingview")
                if last.magic == magic
                else "other"
            )
            record_closed_trade(
                position_id=str(pos_id),
                close_ts=close_ts,
                symbol=last.symbol,
                side=side,
                volume=volume,
                entry_price=entry_price,
                close_price=last.price,
                profit=profit,
                magic=last.magic,
                comment=last.comment,
                source=source,
            )
            n += 1
        return n
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def backfill() -> dict:
    n_signals, n_orders_tg = _backfill_telegram_signals()
    n_orders_mt5 = _backfill_mt5_fills()
    n_closed = _backfill_mt5_closed()
    return {
        "signals": n_signals,
        "orders_from_telegram_log": n_orders_tg,
        "orders_from_mt5_log": n_orders_mt5,
        "closed_trades_from_mt5": n_closed,
    }


def stats() -> dict:
    conn = get_conn()
    if conn is None:
        return {}
    out = {}
    try:
        for table in ("signals", "orders", "closed_trades", "equity_snapshots"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                out[table] = f"ERR: {exc!r}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["backfill", "stats"])
    args = parser.parse_args()

    if args.cmd == "backfill":
        result = backfill()
        print(f"backfill complete: {json.dumps(result)}")
    elif args.cmd == "stats":
        result = stats()
        for table, count in result.items():
            print(f"{table}: {count}")


if __name__ == "__main__":
    main()

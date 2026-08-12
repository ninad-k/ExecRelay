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

DB_PATH = Path(
    os.environ.get("EXECRELAY_DB_PATH")
    or (Path(__file__).resolve().parent.parent / ".local-stack" / "execrelay.db")
)
LOG_DIR = Path(__file__).resolve().parent.parent / ".local-stack" / "logs" / "transactions"

SCHEMA_VERSION = "4"

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
    # channels: the operator-facing signal-source registry (schema v2). Keyed
    # by the resolved numeric Telegram chat id (as text), or the literal
    # 'direct' pseudo-channel for messages posted straight to the bot with no
    # [SRC:...] tag. A row whose chat_id is neither 'direct' nor purely
    # numeric is "pending resolution" -- it was added by spec text the
    # dashboard couldn't resolve itself (no Telethon session); the forwarder
    # resolves it to a real id/title on its next refresh and renames the row
    # (see resolve_channel / mark_channel_resolution_error below).
    # partial_book / breakeven (schema v4): per-channel switches for the split-
    # TP "book 50%" feature -- see channel_flags() and telegram-ingest's
    # build_commands(split_tp=...) / ea_shim's half-vs-full TP close handling.
    # Declared here so a brand-new DB gets them straight from CREATE TABLE;
    # an already-existing DB gets them from the ALTER TABLE migration below
    # (CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so the
    # column list here is NOT itself sufficient for upgrades).
    """
    CREATE TABLE IF NOT EXISTS channels (
        chat_id       TEXT PRIMARY KEY,
        title         TEXT,
        spec          TEXT,
        enabled       INTEGER NOT NULL DEFAULT 1,
        added_ts      TEXT,
        updated_ts    TEXT,
        note          TEXT,
        partial_book  INTEGER NOT NULL DEFAULT 0,
        breakeven     INTEGER NOT NULL DEFAULT 0
    )
    """,
    # tg_dialogs: forwarder-populated cache of the account's Telegram dialogs
    # (scripts/telegram_user_forwarder.py, refreshed every ~10min), so the
    # dashboard's "add channel" picker can offer real titles/ids instead of
    # making the operator type a numeric chat id from memory.
    """
    CREATE TABLE IF NOT EXISTS tg_dialogs (
        chat_id       TEXT PRIMARY KEY,
        title         TEXT,
        kind          TEXT,
        refreshed_ts  TEXT
    )
    """,
    # resubmits: audit ledger for the dashboard's operator-triggered
    # "resubmit"/"submit manually" action (schema v3) -- see
    # trade_dashboard.py's resubmit_signal(). One row per attempt (never
    # updated in place), so the history of what an operator did/when is
    # never lost even if they resubmit the same signal twice with force.
    # commands/http_statuses/trace_ids are JSON arrays, index-aligned with
    # each other; commands are ALWAYS secret-redacted before being stored
    # here (never the raw webhook body).
    """
    CREATE TABLE IF NOT EXISTS resubmits (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             TEXT,
        signal_id      INTEGER,
        source         TEXT CHECK(source IN ('signal', 'manual')),
        commands       TEXT,
        http_statuses  TEXT,
        trace_ids      TEXT,
        ok             INTEGER,
        note           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_signals_channel ON signals(channel)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source ON orders(source)",
    "CREATE INDEX IF NOT EXISTS idx_closed_trades_close_ts ON closed_trades(close_ts)",
    "CREATE INDEX IF NOT EXISTS idx_channels_enabled ON channels(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_resubmits_signal_id ON resubmits(signal_id)",
)

# Env vars this module reads directly (not via the Telethon-authenticated
# forwarder process) purely to seed the channel registry on first migration
# to schema v2 -- see _migrate_v2_seed_channels.
_ENV_SOURCE_CHAT_VAR = "TG_FORWARDER_SOURCE_CHAT"


def _warn(msg: str) -> None:
    print(f"_tradestore: {msg}", file=sys.stderr)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_dotenv_value(key: str) -> str:
    """Read one key from the repo's .env directly (this module has no
    _load_dotenv of its own, unlike telegram_user_forwarder.py) -- used only
    to seed the channel registry once, on first migration to schema v2.
    os.environ wins if already set (e.g. under local-stack.ps1, which
    exports .env into the process before spawning children)."""
    if os.environ.get(key):
        return os.environ[key]
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return ""


def _migrate_v2_seed_channels(conn: sqlite3.Connection) -> None:
    """One-time seed of the `channels` table from the operator's current
    TG_FORWARDER_SOURCE_CHAT, plus the 'direct' pseudo-channel for messages
    posted straight to the bot. Idempotent by construction: only runs while
    schema_version < 2 (checked by the caller), and only inserts rows that
    don't already exist (INSERT OR IGNORE) so a concurrent/duplicate call
    from another process can never clobber an operator's later edits.
    Behaviour-preserving: every channel currently in TG_FORWARDER_SOURCE_CHAT
    is seeded enabled=1, so nothing stops relaying because of this
    migration."""
    now = _utcnow_iso()
    conn.execute(
        "INSERT OR IGNORE INTO channels (chat_id, title, spec, enabled, added_ts, updated_ts, note) "
        "VALUES ('direct', 'Direct to bot', 'direct', 1, ?, ?, NULL)",
        (now, now),
    )
    raw = _env_dotenv_value(_ENV_SOURCE_CHAT_VAR)
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        # A bare numeric/@username spec becomes the row's key directly (as
        # the forwarder's own _resolve() would treat it); anything else
        # (a title fragment) is also stored as-is under chat_id -- it is
        # "pending resolution" until the forwarder resolves it to a real id
        # on its first refresh, exactly like a spec added via the dashboard.
        key = part.lstrip("@") if not part.lstrip("-").isdigit() else part
        conn.execute(
            "INSERT OR IGNORE INTO channels (chat_id, title, spec, enabled, added_ts, updated_ts, note) "
            "VALUES (?, NULL, ?, 1, ?, ?, NULL)",
            (key, part, now, now),
        )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA_SQL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()

    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    try:
        current = int(row["value"]) if row and row["value"] is not None else 1
    except (TypeError, ValueError):
        current = 1

    # Staged, idempotent migrations -- each block is gated on the version it
    # brings the DB to, so re-running _ensure_schema (every get_conn() call)
    # against an already-migrated DB is always a no-op past this point.
    # Table creation itself is handled unconditionally above via the
    # `CREATE TABLE IF NOT EXISTS` statements in _SCHEMA_SQL; these blocks
    # only carry one-time *data* migrations plus the version bump.
    if current < 2:
        try:
            _migrate_v2_seed_channels(conn)
        except Exception as exc:  # noqa: BLE001 - migration must never crash a caller
            _warn(f"schema v2 channel seed failed: {exc!r}")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
        current = 2

    if current < 3:
        # No data migration needed -- the `resubmits` table is brand new
        # (created empty by the CREATE TABLE IF NOT EXISTS above) and has no
        # prior rows to backfill. Just record the version bump.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '3') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
        current = 3

    if current < 4:
        # channels.partial_book / channels.breakeven -- a DB created before
        # this feature existed has a `channels` table without these columns;
        # CREATE TABLE IF NOT EXISTS above never touches an existing table, so
        # they have to be added explicitly. Guarded by pragma table_info so
        # this is idempotent even if two processes race to migrate at once
        # (ADD COLUMN on a column that already exists raises, which would
        # otherwise wedge every later get_conn() call against this DB).
        try:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(channels)")}
            if "partial_book" not in cols:
                conn.execute(
                    "ALTER TABLE channels ADD COLUMN partial_book INTEGER NOT NULL DEFAULT 0"
                )
            if "breakeven" not in cols:
                conn.execute(
                    "ALTER TABLE channels ADD COLUMN breakeven INTEGER NOT NULL DEFAULT 0"
                )
        except Exception as exc:  # noqa: BLE001 - migration must never crash a caller
            _warn(f"schema v4 channel flag columns migration failed: {exc!r}")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '4') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
        current = 4


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


def record_resubmit(
    signal_id: int | None,
    source: str,
    commands: list,
    http_statuses: list,
    trace_ids: list,
    ok: bool,
    note: str | None = None,
) -> None:
    """Append one row to the resubmit audit ledger -- called exactly once per
    operator-triggered resubmit/manual-submit attempt (trade_dashboard.py's
    resubmit_signal()). Never updates an existing row: every attempt, even a
    forced re-attempt of the same signal, gets its own row so the history is
    never lost. `commands` must already be secret-redacted by the caller --
    this module makes no attempt to redact on the way in."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            """
            INSERT INTO resubmits (ts, signal_id, source, commands, http_statuses, trace_ids, ok, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utcnow_iso(),
                signal_id,
                source,
                json.dumps(commands),
                json.dumps(http_statuses),
                json.dumps(trace_ids),
                1 if ok else 0,
                note,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"record_resubmit failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def latest_resubmit_for_signal(signal_id: int) -> dict | None:
    """Most recent resubmit attempt for one signal, or None if it was never
    resubmitted. Used for the idempotency check in resubmit_signal()."""
    rows = query(
        "SELECT id, ts, source, commands, http_statuses, trace_ids, ok, note "
        "FROM resubmits WHERE signal_id = ? ORDER BY id DESC LIMIT 1",
        (signal_id,),
    )
    return rows[0] if rows else None


def resubmits_for_signals(signal_ids: list) -> dict:
    """Latest resubmit attempt per signal_id, batched into one query for the
    "Signals not placed" table (avoids one query per row). Returns
    {signal_id: row}; signals never resubmitted are simply absent. Empty
    dict on no ids given or any DB failure -- never raises."""
    ids = [i for i in signal_ids if i is not None]
    if not ids:
        return {}
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id, ts, signal_id, source, commands, http_statuses, trace_ids, ok, note "
            f"FROM resubmits WHERE signal_id IN ({placeholders}) ORDER BY id ASC",
            tuple(ids),
        ).fetchall()
        out: dict = {}
        for row in rows:
            # ORDER BY id ASC + plain dict assignment -> later (higher id)
            # rows overwrite earlier ones, so each signal_id ends up mapped
            # to its most recent attempt.
            out[row["signal_id"]] = dict(row)
        return out
    except Exception as exc:  # noqa: BLE001
        _warn(f"resubmits_for_signals failed: {exc!r}")
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def meta_get(key: str) -> str | None:
    """Read one value from the meta table. None if absent or on any DB
    failure -- callers (e.g. a heartbeat freshness check) must treat None
    the same as "missing"."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    except Exception as exc:  # noqa: BLE001
        _warn(f"meta_get failed: {exc!r}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def meta_set(key: str, value: str) -> None:
    """Upsert one meta key/value. No-throw -- used for cross-process
    heartbeats (hb_forwarder, hb_ea_shim, ...) where a DB hiccup must never
    take down the caller's actual job (relaying/trading)."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"meta_set failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Channel registry -- see the `channels` table docstring above _SCHEMA_SQL.
# All no-throw per this module's contract.
# ---------------------------------------------------------------------------


def list_channels() -> list[dict]:
    return query(
        "SELECT chat_id, title, spec, enabled, added_ts, updated_ts, note, "
        "partial_book, breakeven FROM channels "
        "ORDER BY CASE WHEN chat_id='direct' THEN 0 ELSE 1 END, "
        "COALESCE(title, spec, chat_id) COLLATE NOCASE"
    )


def upsert_channel(
    chat_id: str,
    title: str | None = None,
    spec: str | None = None,
    enabled: int | bool = 1,
    note: str | None = None,
) -> None:
    """Insert or fully overwrite one channel row -- the dashboard's
    add-channel action and the schema v2 seed. Overwrites title/spec/note
    unconditionally (last write wins), matching this module's other
    upsert-style writers. For the forwarder's own resolution-in-place
    updates (pending spec -> real id/title) use resolve_channel() /
    mark_channel_resolution_error() instead, which preserve enabled."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        now = _utcnow_iso()
        conn.execute(
            """
            INSERT INTO channels (chat_id, title, spec, enabled, added_ts, updated_ts, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                spec=excluded.spec,
                enabled=excluded.enabled,
                updated_ts=excluded.updated_ts,
                note=excluded.note
            """,
            (str(chat_id), title, spec, 1 if enabled else 0, now, now, note),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"upsert_channel failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Boolean columns on `channels` an operator can flip at runtime, shared by
# set_channel_flag()'s allowlist below. `field` ends up interpolated into SQL
# (sqlite3 can't parameterize identifiers), so this gate is load-bearing, not
# decorative -- keep it in sync with the channels schema.
_CHANNEL_FLAG_COLUMNS = ("enabled", "partial_book", "breakeven")


def set_channel_enabled(chat_id: str, enabled: bool) -> None:
    set_channel_flag(chat_id, "enabled", enabled)


def set_channel_flag(chat_id: str, field: str, value: bool) -> None:
    """Flip one boolean column on a channel row -- shared setter behind
    set_channel_enabled() and the dashboard's partial_book/breakeven toggles.

    `field` MUST be one of _CHANNEL_FLAG_COLUMNS. The dashboard validates the
    same allowlist before calling in (see trade_dashboard.py's
    /api/channels/toggle handler); this check is the second, store-level
    gate -- defense in depth, since an f-string column name is the one place
    in this module SQL injection would actually be possible."""
    if field not in _CHANNEL_FLAG_COLUMNS:
        _warn(f"set_channel_flag: rejected unknown field {field!r}")
        return
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            f"UPDATE channels SET {field}=?, updated_ts=? WHERE chat_id=?",
            (1 if value else 0, _utcnow_iso(), str(chat_id)),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"set_channel_flag failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def delete_channel(chat_id: str) -> None:
    """Removes the registry row only -- signals/orders history referencing
    this channel's title is untouched (the dashboard makes this explicit to
    the operator before calling delete)."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute("DELETE FROM channels WHERE chat_id=?", (str(chat_id),))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"delete_channel failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_channel(pending_chat_id: str, resolved_chat_id: str, resolved_title: str) -> None:
    """Called by the forwarder once it resolves a channel's stored spec to a
    real Telegram id/title. If the row was already keyed by its numeric id
    (only the title was missing), updates title in place. If it was keyed by
    the raw spec text (pending resolution), renames the row's primary key to
    the resolved id, preserving enabled/spec/note. Clears any previous
    "could not resolve" note. No-op if the row was deleted meanwhile."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        now = _utcnow_iso()
        if str(pending_chat_id) == str(resolved_chat_id):
            conn.execute(
                "UPDATE channels SET title=?, updated_ts=?, note=NULL WHERE chat_id=?",
                (resolved_title, now, str(pending_chat_id)),
            )
        else:
            row = conn.execute(
                "SELECT spec, enabled FROM channels WHERE chat_id=?", (str(pending_chat_id),)
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                INSERT INTO channels (chat_id, title, spec, enabled, added_ts, updated_ts, note)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title=excluded.title, spec=excluded.spec, enabled=excluded.enabled,
                    updated_ts=excluded.updated_ts, note=NULL
                """,
                (str(resolved_chat_id), resolved_title, row["spec"], row["enabled"], now, now),
            )
            conn.execute("DELETE FROM channels WHERE chat_id=?", (str(pending_chat_id),))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"resolve_channel failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def mark_channel_resolution_error(chat_id: str, error: str) -> None:
    """Stamps a "could not resolve" note on a still-pending channel row
    (channel not found / account not subscribed) so the dashboard can show a
    red state instead of "resolving..." forever."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.execute(
            "UPDATE channels SET note=?, updated_ts=? WHERE chat_id=?",
            (str(error)[:300], _utcnow_iso(), str(chat_id)),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"mark_channel_resolution_error failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_channel_enabled(channel_name: str | None) -> bool:
    """Hot-path check for telegram-ingest: is this channel allowed to place
    trades? `channel_name` is the [SRC:<title>] tag's title, or None/empty
    for a message posted straight to the bot (matched against the 'direct'
    pseudo-channel row).

    Fail-open by design: a channel with NO registry row at all (a tagged
    channel the operator hasn't registered yet) is treated as enabled --
    a missing config row must never silently stop trading. Only an explicit
    enabled=0 row skips the message. Never raises (query() doesn't)."""
    if channel_name:
        rows = query("SELECT enabled FROM channels WHERE title = ? COLLATE NOCASE", (channel_name,))
    else:
        rows = query("SELECT enabled FROM channels WHERE chat_id = 'direct'")
    if not rows:
        return True
    return bool(rows[0].get("enabled"))


def channel_flags(channel_name: str | None) -> dict:
    """Per-channel partial-booking / breakeven switches -- {"partial_book":
    bool, "breakeven": bool}. `channel_name` matches is_channel_enabled()'s
    lookup exactly (title COLLATE NOCASE, or the 'direct' pseudo-channel for
    None/empty).

    Fail CLOSED, the opposite of is_channel_enabled()'s fail-open: an
    unregistered channel, a missing row, or any store failure returns both
    False. Unlike "is this channel allowed to trade at all" (where a missing
    row must never silently stop trading), a config gap here must never
    silently DOUBLE the order count or start moving stops -- so absence of
    config means "do nothing extra", not "do the new thing"."""
    if channel_name:
        rows = query(
            "SELECT partial_book, breakeven FROM channels WHERE title = ? COLLATE NOCASE",
            (channel_name,),
        )
    else:
        rows = query("SELECT partial_book, breakeven FROM channels WHERE chat_id = 'direct'")
    if not rows:
        return {"partial_book": False, "breakeven": False}
    return {
        "partial_book": bool(rows[0].get("partial_book")),
        "breakeven": bool(rows[0].get("breakeven")),
    }


def recent_symbols_for_channel(channel_name: str | None, days: int = 7) -> list[str]:
    """Distinct symbols this channel has actually traded in the last `days`.

    Follow-up messages ("TP set @ 4346 for both trade") name a new price but no
    instrument, so the instrument has to come from what the channel already has
    working. Ordered most-recently-traded first; empty list on any failure,
    which callers must treat as "address nothing" rather than "address
    everything"."""
    if not channel_name:
        rows = query(
            "SELECT symbol, MAX(ts) AS last_ts FROM signals "
            "WHERE channel IS NULL AND outcome IN ('posted', 'dry_run') "
            "AND symbol IS NOT NULL AND symbol != '' AND ts >= ? "
            "GROUP BY symbol ORDER BY last_ts DESC",
            ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),),
        )
    else:
        rows = query(
            "SELECT symbol, MAX(ts) AS last_ts FROM signals "
            "WHERE channel = ? COLLATE NOCASE AND outcome IN ('posted', 'dry_run') "
            "AND symbol IS NOT NULL AND symbol != '' AND ts >= ? "
            "GROUP BY symbol ORDER BY last_ts DESC",
            (channel_name, (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()),
        )
    return [str(r["symbol"]) for r in rows if r.get("symbol")]


def sibling_order_rows(broker_order_id: str) -> list[dict]:
    """Every order belonging to the same signal as `broker_order_id`,
    INCLUDING the order asked about itself -- unlike sibling_broker_order_ids
    below, which excludes it. Built for ea_shim's half-vs-full TP close
    classification: deciding whether a just-closed order was the "half-TP"
    leg or the "full-TP" leg needs its OWN tp next to its siblings' tps (not
    just their broker order ids), plus the signal's channel (to look up
    channel_flags() for the breakeven switch).

    A range/second-entry signal becomes several orders, one per leg, related
    only through the store: each leg is an `orders` row keyed by its
    trace_id, and the `signals` row that produced them lists every one of
    those trace_ids. So the walk is ticket -> trace_id -> signal -> every
    trace_id on that signal -> every leg's order row.

    That indirection is the point. The MT5 comment identifies the CHANNEL
    ("tg-VGTA"), not the signal, so matching on it would sweep in a
    different, still-live signal from the same channel on the same symbol --
    something these channels do routinely. Only the store knows which legs
    were one message.

    Each row: {broker_order_id, trace_id, command, tp, entry, symbol,
    channel}. `channel` is the signals row's channel value, copied onto every
    row for convenience (it's the same for all of them -- one signal).

    Returns [] on anything unexpected -- no row, no signal, store unreachable.
    Callers must treat that as "cannot resolve this signal's legs" and fall
    back to whatever behaviour is safe when nothing is known (see
    sibling_broker_order_ids and ea_shim's close-classification helper)."""
    if not broker_order_id:
        return []
    rows = query(
        "SELECT trace_id FROM orders WHERE broker_order_id = ?", (str(broker_order_id),)
    )
    trace_id = str(rows[0]["trace_id"]) if rows and rows[0].get("trace_id") else ""
    if not trace_id:
        return []

    # The signal that issued it. LIKE on the JSON array is a containment test,
    # not a parse -- the quotes around the id keep it from matching a prefix of
    # some other trace_id.
    sig_rows = query(
        "SELECT channel, trace_ids FROM signals WHERE trace_ids LIKE ?", (f'%"{trace_id}"%',)
    )
    all_traces: list[str] = []
    channel = None
    for r in sig_rows:
        try:
            ids = json.loads(r.get("trace_ids") or "[]")
        except (TypeError, ValueError):
            continue
        if trace_id in ids:
            all_traces = ids
            channel = r.get("channel")
            break
    if not all_traces:
        return []

    marks = ",".join("?" * len(all_traces))
    out = query(
        f"SELECT broker_order_id, trace_id, command, tp, entry, symbol FROM orders "
        f"WHERE trace_id IN ({marks}) "
        "AND broker_order_id IS NOT NULL AND broker_order_id != ''",
        tuple(all_traces),
    )
    for r in out:
        r["channel"] = channel
    return out


def sibling_broker_order_ids(broker_order_id: str) -> list[str]:
    """The OTHER broker order ids issued by the same signal as this one --
    thin wrapper over sibling_order_rows() (see its docstring for the store
    walk), just excluding the id asked about and flattening to plain ids for
    the OCO cancel path.

    Returns [] on anything unexpected -- no row, no signal, store unreachable.
    Callers must treat that as "cancel nothing", never as "cancel everything".
    """
    this_id = str(broker_order_id)
    return [
        str(r["broker_order_id"])
        for r in sibling_order_rows(broker_order_id)
        if r.get("broker_order_id") and str(r["broker_order_id"]) != this_id
    ]


# ---------------------------------------------------------------------------
# Runtime settings -- operator switches that must take effect without a stack
# restart. Stored in the `meta` kv table, so there is no schema change here:
# `meta` predates schema v1 and a new key is a row, not a migration.
#
# Currently just the dry-run kill switch: the dashboard writes it, and
# telegram-ingest re-reads it on its hot path (cached ~10s) instead of
# trusting the TELEGRAM_INGEST_DRY_RUN value it booted with.
# ---------------------------------------------------------------------------

_DRY_RUN_KEY = "dry_run"
_DRY_RUN_TS_KEY = "dry_run_updated_ts"

# Set by the dashboard, consumed by the forwarder: "re-read the account's
# Telegram dialogs now". The dashboard has no Telethon session of its own --
# only the forwarder is authenticated -- so a newly joined channel can only
# reach the picker by asking the forwarder to look. Named here so the two
# processes cannot drift apart on the spelling.
DIALOG_REFRESH_REQUEST_KEY = "tg_dialogs_refresh_requested"

_TRUEISH = ("1", "true", "yes", "on")
_FALSEISH = ("0", "false", "no", "off")


def get_dry_run(default: bool) -> bool:
    """Effective dry-run state: the operator's stored override if one has ever
    been set, otherwise `default` (the caller's TELEGRAM_INGEST_DRY_RUN value).

    Fail-SAFE, deliberately unlike is_channel_enabled()'s fail-open: any store
    problem (missing row, unreadable DB, junk value) falls back to `default`,
    and that env default is itself `true`. A broken store can therefore only
    ever leave the stack in dry-run -- never silently put it live."""
    rows = query("SELECT value FROM meta WHERE key=?", (_DRY_RUN_KEY,))
    if not rows:
        return default
    val = str(rows[0].get("value") or "").strip().lower()
    if val in _TRUEISH:
        return True
    if val in _FALSEISH:
        return False
    return default


def get_dry_run_meta(default: bool) -> dict:
    """get_dry_run() plus provenance for the dashboard: whether the effective
    value came from a stored override or the env default, and when the
    operator last changed it."""
    rows = query("SELECT value FROM meta WHERE key=?", (_DRY_RUN_KEY,))
    ts_rows = query("SELECT value FROM meta WHERE key=?", (_DRY_RUN_TS_KEY,))
    return {
        "dry_run": get_dry_run(default),
        "source": "override" if rows else "env",
        "updated_ts": (ts_rows[0].get("value") if ts_rows else None),
    }


def set_dry_run(enabled: bool) -> None:
    """Persist the operator's dry-run choice. Takes effect on telegram-ingest's
    next cache expiry (~10s) -- no restart, same as a channel toggle."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                (_DRY_RUN_KEY, "1" if enabled else "0"),
                (_DRY_RUN_TS_KEY, _utcnow_iso()),
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _warn(f"set_dry_run failed: {exc!r}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# tg_dialogs -- forwarder-populated cache backing the dashboard's "add
# channel" picker. Written only by telegram_user_forwarder.py (it's the only
# process with a Telethon session); read by the dashboard.
# ---------------------------------------------------------------------------


def list_tg_dialogs() -> list[dict]:
    return query("SELECT chat_id, title, kind, refreshed_ts FROM tg_dialogs ORDER BY title COLLATE NOCASE")


def replace_tg_dialogs(dialogs: list[tuple[str, str, str]]) -> None:
    """Full refresh of the tg_dialogs picker list in one transaction, so
    readers never see a half-updated set. `dialogs` is a list of
    (chat_id, title, kind) tuples. No-throw; a failure here just means a
    stale picker list, never anything relay/trading-affecting."""
    conn = None
    try:
        conn = get_conn()
        if conn is None:
            return
        now = _utcnow_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM tg_dialogs")
        conn.executemany(
            "INSERT INTO tg_dialogs (chat_id, title, kind, refreshed_ts) VALUES (?, ?, ?, ?)",
            [(str(cid), title, kind, now) for cid, title, kind in dialogs],
        )
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        _warn(f"replace_tg_dialogs failed: {exc!r}")
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

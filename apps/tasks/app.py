from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg

SERVICE = "tasks"
ENV = os.environ.get("ENV", "development").lower()
IS_PROD = ENV in ("prod", "production")
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")

_DEV_DB = "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay"
DATABASE_URL = os.environ.get("DATABASE_URL", _DEV_DB)
DEBUG = os.environ.get("DEBUG", "false" if IS_PROD else "true").lower() in (
    "true",
    "1",
    "yes",
    "on",
)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
FILL_TIMEOUT_SECS = int(os.environ.get("FILL_TIMEOUT_SECS", "30"))
FILL_CHECK_INTERVAL = int(os.environ.get("FILL_CHECK_INTERVAL", "60"))
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", "86400"))
TASK_POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL", "10"))

# Telegram notifications (PineConnector-style). Feature is off unless a bot
# token is configured. TELEGRAM_API_BASE is overridable for tests and
# self-hosted Bot API servers.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
TELEGRAM_NOTIFY_INTERVAL = int(os.environ.get("TELEGRAM_NOTIFY_INTERVAL", "10"))
TELEGRAM_NOTIFY_LOOKBACK_SECS = int(
    os.environ.get("TELEGRAM_NOTIFY_LOOKBACK_SECS", "3600")
)
TELEGRAM_POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "25"))


class _JSONFormatter(logging.Formatter):
    """Single-line JSON per log record so logs from this worker are pivotable
    next to the other services' structured streams."""

    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone

        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


logger = logging.getLogger(SERVICE)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JSONFormatter())
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    handlers=[_handler],
    force=True,
)

if IS_PROD and DATABASE_URL == _DEV_DB:
    logger.error("DATABASE_URL required in prod (refusing dev default)")
    raise SystemExit(2)

# Health state shared between worker loop and HTTP probe thread.
_readiness = {"db_ok": False, "db_err": "not initialized"}


# ---------------------------------------------------------------------------
# Background task implementations
# ---------------------------------------------------------------------------


async def fill_timeout_check(pool: asyncpg.Pool) -> None:
    """Insert synthetic timeout fills for signals that never received a fill report."""
    rows = await pool.fetch(
        """
        SELECT s.id, s.trace_id, s.license_id, s.instance_id, s.received_at
        FROM accepted_signals s
        LEFT JOIN fills f ON f.trace_id = s.trace_id
        WHERE s.received_at < NOW() - ($1 || ' seconds')::interval
          AND f.id IS NULL
        LIMIT 100
        """,
        str(FILL_TIMEOUT_SECS),
    )
    if not rows:
        return
    logger.warning(
        "fill_timeout: %d signals without fill (>%ds old)",
        len(rows),
        FILL_TIMEOUT_SECS,
    )
    async with pool.acquire() as conn:
        for row in rows:
            await conn.execute(
                """
                INSERT INTO fills
                    (signal_id, license_id, instance_id, trace_id,
                     status, error_message, payload)
                VALUES ($1, $2, $3, $4, 'timeout',
                        'Fill not received within timeout window',
                        $5::jsonb)
                ON CONFLICT DO NOTHING
                """,
                row["id"],
                row["license_id"],
                row["instance_id"],
                row["trace_id"],
                json.dumps(
                    {
                        "signal_id": str(row["id"]),
                        "received_at": row["received_at"].isoformat(),
                        "timeout_secs": FILL_TIMEOUT_SECS,
                    }
                ),
            )
            await conn.execute(
                """
                INSERT INTO system_events (trace_id, event_type, severity, payload)
                VALUES ($1, 'fill_timeout', 'warning', $2::jsonb)
                """,
                row["trace_id"],
                json.dumps(
                    {
                        "signal_id": str(row["id"]),
                        "instance_id": str(row["instance_id"])
                        if row["instance_id"]
                        else None,
                        "received_at": row["received_at"].isoformat(),
                        "timeout_secs": FILL_TIMEOUT_SECS,
                    }
                ),
            )


async def data_retention(pool: asyncpg.Pool) -> None:
    """Delete signals, fills, and fingerprints older than RETENTION_DAYS."""
    deleted_fills = await pool.fetchval(
        "WITH d AS (DELETE FROM fills WHERE created_at < NOW() - ($1 || ' days')::interval RETURNING 1)"
        " SELECT count(*) FROM d",
        str(RETENTION_DAYS),
    )
    deleted_fps = await pool.fetchval(
        "WITH d AS (DELETE FROM signal_fingerprints WHERE received_at < NOW() - ($1 || ' days')::interval RETURNING 1)"
        " SELECT count(*) FROM d",
        str(RETENTION_DAYS),
    )
    # Try TimescaleDB drop_chunks; fall back to plain DELETE.
    try:
        await pool.execute(
            "SELECT drop_chunks('accepted_signals', NOW() - ($1 || ' days')::interval)",
            str(RETENTION_DAYS),
        )
        logger.info(
            "retention: dropped old chunks from accepted_signals, deleted %s fills, %s fingerprints",
            deleted_fills,
            deleted_fps,
        )
    except Exception:
        deleted_signals = await pool.fetchval(
            "WITH d AS (DELETE FROM accepted_signals WHERE received_at < NOW() - ($1 || ' days')::interval RETURNING 1)"
            " SELECT count(*) FROM d",
            str(RETENTION_DAYS),
        )
        logger.info(
            "retention: deleted %s signals, %s fills, %s fingerprints",
            deleted_signals,
            deleted_fills,
            deleted_fps,
        )


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------

# getUpdates offset. In-memory only: after a restart Telegram replays un-acked
# updates, and every command handler below is idempotent, so replays are safe.
_tg_offset: int | None = None


def _telegram_call(method: str, params: dict) -> dict:
    """Synchronous Bot API call (run via asyncio.to_thread). Returns the parsed
    JSON envelope; raises on transport errors so callers can decide to retry."""
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Long polls need headroom past the poll timeout itself.
    timeout = TELEGRAM_POLL_TIMEOUT + 10 if method == "getUpdates" else 15
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Bot API errors (400/403/...) still carry a JSON envelope with
        # ok=false + description; surface that instead of raising so callers
        # can distinguish "chat blocked the bot" from a network failure.
        try:
            return json.loads(exc.read().decode())
        except Exception:
            raise exc from None


async def _tg_send(chat_id: int, text: str) -> bool:
    """Send a message; True on success, False on a definitive API rejection.
    Transport errors propagate."""
    resp = await asyncio.to_thread(
        _telegram_call, "sendMessage", {"chat_id": chat_id, "text": text}
    )
    if not resp.get("ok"):
        logger.warning(
            "telegram sendMessage rejected for chat %s: %s",
            chat_id,
            resp.get("description", "unknown"),
        )
    return bool(resp.get("ok"))


_TG_HELP = (
    "ExecRelay notification bot.\n\n"
    "Link your account from the portal (Settings → Telegram) and open the "
    "generated link, or send:\n"
    "/start <token> — link this chat\n"
    "/status — show link status\n"
    "/stop — stop notifications and unlink"
)


async def _handle_telegram_update(pool: asyncpg.Pool, update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    parts = msg["text"].strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start" and arg:
        # One chat per user: claim the token, releasing any previous chat link
        # for this user and any previous user link for this chat.
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT user_id FROM telegram_links
                    WHERE link_token = $1 AND token_expires_at > NOW()
                    FOR UPDATE
                    """,
                    arg,
                )
                if row is None:
                    await _tg_send(
                        chat_id,
                        "❌ Link token is invalid or has expired. Generate a "
                        "fresh link from the ExecRelay portal.",
                    )
                    return
                await conn.execute(
                    "UPDATE telegram_links SET chat_id = NULL, linked_at = NULL, "
                    "updated_at = NOW() WHERE chat_id = $1",
                    chat_id,
                )
                await conn.execute(
                    """
                    UPDATE telegram_links
                    SET chat_id = $1, linked_at = NOW(), updated_at = NOW()
                    WHERE user_id = $2
                    """,
                    chat_id,
                    row["user_id"],
                )
        logger.info("telegram linked chat %s to user %s", chat_id, row["user_id"])
        await _tg_send(
            chat_id,
            "✅ Connection successful. This chat will now receive ExecRelay "
            "trade notifications. Send /stop to unlink.",
        )
    elif cmd == "/stop":
        result = await pool.execute(
            "UPDATE telegram_links SET chat_id = NULL, linked_at = NULL, "
            "updated_at = NOW() WHERE chat_id = $1",
            chat_id,
        )
        unlinked = result.endswith("1")
        await _tg_send(
            chat_id,
            "🔕 Unlinked. You will no longer receive notifications."
            if unlinked
            else "This chat is not linked to any ExecRelay account.",
        )
    elif cmd == "/status":
        row = await pool.fetchrow(
            "SELECT linked_at, notify_fills, notify_timeouts FROM telegram_links "
            "WHERE chat_id = $1",
            chat_id,
        )
        if row is None:
            await _tg_send(chat_id, "This chat is not linked to any ExecRelay account.")
        else:
            await _tg_send(
                chat_id,
                "🔗 Linked since {}.\nFill notifications: {}\nTimeout "
                "notifications: {}".format(
                    row["linked_at"].strftime("%Y-%m-%d %H:%M UTC"),
                    "on" if row["notify_fills"] else "off",
                    "on" if row["notify_timeouts"] else "off",
                ),
            )
    else:
        await _tg_send(chat_id, _TG_HELP)


async def telegram_update_poller(pool: asyncpg.Pool) -> None:
    """Long-poll getUpdates and process bot commands (/start token linking)."""
    global _tg_offset
    params: dict = {
        "timeout": TELEGRAM_POLL_TIMEOUT,
        "allowed_updates": ["message"],
    }
    if _tg_offset is not None:
        params["offset"] = _tg_offset
    resp = await asyncio.to_thread(_telegram_call, "getUpdates", params)
    if not resp.get("ok"):
        logger.warning("telegram getUpdates failed: %s", resp.get("description"))
        return
    for update in resp.get("result", []):
        _tg_offset = update["update_id"] + 1
        try:
            await _handle_telegram_update(pool, update)
        except Exception as exc:
            logger.error("telegram update %s failed: %s", update.get("update_id"), exc)


def _signal_param(signal_payload: dict | None, key: str) -> str:
    for param in (signal_payload or {}).get("params", []):
        if param.get("key") == key:
            return param.get("value", "")
    return ""


def format_fill_message(row: dict) -> str:
    """Render one fill row (joined with its signal + ML decision) as the
    Telegram notification text. Pure function — unit tested directly."""
    status = row.get("status") or "unknown"
    command = (row.get("command") or "").upper()
    symbol = row.get("symbol") or ""
    head = " ".join(x for x in (command, symbol) if x)

    if status == "filled" or status == "ok":
        lines = [f"✅ Order filled — {head}" if head else "✅ Order filled"]
    elif status == "placed":
        # Pending order accepted by the broker but not executed yet; the EA
        # sends a separate "filled" fill when it activates.
        lines = [f"📌 Order placed — {head}" if head else "📌 Order placed"]
    elif status == "cancelled":
        lines = [f"🚫 Order cancelled — {head}" if head else "🚫 Order cancelled"]
    elif status == "timeout":
        lines = [f"⏱ Fill timeout — {head}" if head else "⏱ Fill timeout"]
    else:
        lines = [f"❌ Order {status} — {head}" if head else f"❌ Order {status}"]

    signal_payload = row.get("signal_payload")
    if isinstance(signal_payload, str):
        try:
            signal_payload = json.loads(signal_payload)
        except ValueError:
            signal_payload = None
    comment = _signal_param(signal_payload, "comment")
    if comment:
        lines.append(f"Strategy: {comment}")
    vol = _signal_param(signal_payload, "vol_lots")
    if vol:
        lines.append(f"Volume: {vol} lots")
    if row.get("broker_order_id"):
        lines.append(f"Order: {row['broker_order_id']}")
    if row.get("error_message") and status != "timeout":
        lines.append(f"Error: {row['error_message']}")
    if row.get("prob_win") is not None:
        summary = row.get("action_summary") or ""
        ml = f"ML: prob_win {row['prob_win']:.2f}"
        if summary:
            ml += f" ({summary})"
        lines.append(ml)
    if row.get("trace_id"):
        lines.append(f"Trace: {row['trace_id']}")
    return "\n".join(lines)


async def telegram_fill_notifier(pool: asyncpg.Pool) -> None:
    """Send a Telegram message for each new fill whose owner has a linked chat.
    De-dup is a notifications_log row keyed by fill id, so restarts never
    double-send; unsendable fills age out of the lookback window."""
    rows = await pool.fetch(
        """
        SELECT f.id AS fill_id, f.trace_id, f.status, f.broker_order_id,
               f.error_message, l.user_id, tl.chat_id,
               s.command, s.symbol, s.payload AS signal_payload,
               md.prob_win, md.action_summary
        FROM fills f
        JOIN licenses l ON l.id = f.license_id
        JOIN telegram_links tl ON tl.user_id = l.user_id AND tl.chat_id IS NOT NULL
        LEFT JOIN accepted_signals s ON s.trace_id = f.trace_id
        LEFT JOIN ml_decisions md ON md.trace_id = f.trace_id
        WHERE f.created_at > NOW() - ($1 || ' seconds')::interval
          AND ((f.status = 'timeout' AND tl.notify_timeouts)
               OR (f.status <> 'timeout' AND tl.notify_fills))
          AND NOT EXISTS (
              SELECT 1 FROM notifications_log nl
              WHERE nl.channel = 'telegram'
                AND nl.payload->>'fill_id' = f.id::text)
        ORDER BY f.created_at
        LIMIT 50
        """,
        str(TELEGRAM_NOTIFY_LOOKBACK_SECS),
    )
    for row in rows:
        text = format_fill_message(dict(row))
        try:
            sent = await _tg_send(row["chat_id"], text)
        except Exception as exc:
            # Transport failure: leave no log row so the next cycle retries.
            logger.warning("telegram notify transport error: %s", exc)
            continue
        await pool.execute(
            """
            INSERT INTO notifications_log (user_id, channel, template, payload, status)
            VALUES ($1, 'telegram', 'fill_notification', $2::jsonb, $3)
            """,
            row["user_id"],
            json.dumps(
                {
                    "fill_id": str(row["fill_id"]),
                    "trace_id": row["trace_id"],
                    "chat_id": row["chat_id"],
                    "fill_status": row["status"],
                }
            ),
            "sent" if sent else "failed",
        )


async def task_processor(pool: asyncpg.Pool) -> None:
    """Claim and process pending rows from the tasks table."""
    rows = await pool.fetch(
        """
        UPDATE tasks SET status = 'processing', updated_at = NOW()
        WHERE id IN (
            SELECT id FROM tasks WHERE status = 'pending'
            ORDER BY created_at
            LIMIT 10
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, task_type, payload
        """,
    )
    if not rows:
        return
    for row in rows:
        task_id = row["id"]
        task_type = row["task_type"]
        try:
            logger.info("task %s: processing type=%s", task_id, task_type)
            # Extend here for concrete task_type handlers.
            await pool.execute(
                "UPDATE tasks SET status = 'completed', updated_at = NOW() WHERE id = $1",
                task_id,
            )
        except Exception as exc:
            logger.error("task %s failed: %s", task_id, exc)
            await pool.execute(
                "UPDATE tasks SET status = 'failed', updated_at = NOW() WHERE id = $1",
                task_id,
            )


# ---------------------------------------------------------------------------
# Periodic runner
# ---------------------------------------------------------------------------


async def run_periodically(interval: int, fn, pool: asyncpg.Pool) -> None:
    while True:
        try:
            await fn(pool)
        except Exception as exc:
            logger.error("%s error: %s", fn.__name__, exc)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Health HTTP server (daemon thread)
# ---------------------------------------------------------------------------


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._json(200, {"service": SERVICE, "status": "ok"})
        elif self.path == "/readyz":
            snap = dict(_readiness)
            ok = bool(snap.get("db_ok"))
            self._json(
                200 if ok else 503,
                {
                    "service": SERVICE,
                    "ok": ok,
                    "checks": {
                        "db": {"ok": ok, "err": snap.get("db_err", "")},
                    },
                },
            )
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


def start_health_server(addr: str) -> None:
    host, port_str = addr.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port_str)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _refresh_readiness(pool: asyncpg.Pool | None) -> None:
    while True:
        try:
            if pool is None:
                _readiness["db_ok"] = False
                _readiness["db_err"] = "pool not initialized"
            else:
                await pool.fetchval("SELECT 1")
                _readiness["db_ok"] = True
                _readiness["db_err"] = ""
        except Exception as exc:
            _readiness["db_ok"] = False
            _readiness["db_err"] = repr(exc)[:200]
        await asyncio.sleep(5)


async def async_main() -> None:
    pool: asyncpg.Pool | None = None
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, command_timeout=10
        )
        _readiness["db_ok"] = True
        _readiness["db_err"] = ""
        logger.info("db pool ready")
    except Exception as exc:
        _readiness["db_err"] = repr(exc)[:200]
        logger.warning("db unavailable at startup: %s — tasks will idle", exc)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    bg_tasks = [asyncio.create_task(_refresh_readiness(pool))]
    if pool is not None:
        bg_tasks.extend(
            [
                asyncio.create_task(
                    run_periodically(FILL_CHECK_INTERVAL, fill_timeout_check, pool)
                ),
                asyncio.create_task(
                    run_periodically(RETENTION_INTERVAL, data_retention, pool)
                ),
                asyncio.create_task(
                    run_periodically(TASK_POLL_INTERVAL, task_processor, pool)
                ),
            ]
        )
        if TELEGRAM_BOT_TOKEN:
            bg_tasks.extend(
                [
                    # Interval 1: telegram_update_poller long-polls internally,
                    # so the runner just restarts the poll immediately.
                    asyncio.create_task(
                        run_periodically(1, telegram_update_poller, pool)
                    ),
                    asyncio.create_task(
                        run_periodically(
                            TELEGRAM_NOTIFY_INTERVAL, telegram_fill_notifier, pool
                        )
                    ),
                ]
            )
            logger.info("telegram notifications enabled")
        else:
            logger.info("telegram notifications disabled (no TELEGRAM_BOT_TOKEN)")

    logger.info("tasks service started")
    await stop_event.wait()

    for t in bg_tasks:
        t.cancel()
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    if pool is not None:
        await pool.close()
    logger.info("tasks service stopped")


def healthcheck(addr: str) -> None:
    host = "127.0.0.1" if addr.startswith("0.0.0.0:") else addr.rsplit(":", 1)[0]
    port = addr.rsplit(":", 1)[1]
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as r:
        if r.status != 200:
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        healthcheck(HTTP_ADDR)
        return

    start_health_server(HTTP_ADDR)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg

SERVICE = "tasks"
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay",
)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
FILL_TIMEOUT_SECS = int(os.environ.get("FILL_TIMEOUT_SECS", "30"))
FILL_CHECK_INTERVAL = int(os.environ.get("FILL_CHECK_INTERVAL", "60"))
RETENTION_INTERVAL = int(os.environ.get("RETENTION_INTERVAL", "86400"))
TASK_POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL", "10"))

logger = logging.getLogger(SERVICE)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)


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
                json.dumps({
                    "signal_id": str(row["id"]),
                    "received_at": row["received_at"].isoformat(),
                    "timeout_secs": FILL_TIMEOUT_SECS,
                }),
            )
            await conn.execute(
                """
                INSERT INTO system_events (trace_id, event_type, severity, payload)
                VALUES ($1, 'fill_timeout', 'warning', $2::jsonb)
                """,
                row["trace_id"],
                json.dumps({
                    "signal_id": str(row["id"]),
                    "instance_id": str(row["instance_id"]) if row["instance_id"] else None,
                    "received_at": row["received_at"].isoformat(),
                    "timeout_secs": FILL_TIMEOUT_SECS,
                }),
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
        logger.info("retention: dropped old chunks from accepted_signals, deleted %s fills, %s fingerprints",
                    deleted_fills, deleted_fps)
    except Exception:
        deleted_signals = await pool.fetchval(
            "WITH d AS (DELETE FROM accepted_signals WHERE received_at < NOW() - ($1 || ' days')::interval RETURNING 1)"
            " SELECT count(*) FROM d",
            str(RETENTION_DAYS),
        )
        logger.info("retention: deleted %s signals, %s fills, %s fingerprints",
                    deleted_signals, deleted_fills, deleted_fps)


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
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"service": SERVICE, "status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _fmt: str, *_args: object) -> None:
        pass


def start_health_server(addr: str) -> None:
    host, port_str = addr.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port_str)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def async_main() -> None:
    pool: asyncpg.Pool | None = None
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, command_timeout=10
        )
        logger.info("db pool ready")
    except Exception as exc:
        logger.warning("db unavailable at startup: %s — tasks will idle", exc)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    bg_tasks = []
    if pool is not None:
        bg_tasks = [
            asyncio.create_task(run_periodically(FILL_CHECK_INTERVAL, fill_timeout_check, pool)),
            asyncio.create_task(run_periodically(RETENTION_INTERVAL, data_retention, pool)),
            asyncio.create_task(run_periodically(TASK_POLL_INTERVAL, task_processor, pool)),
        ]

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

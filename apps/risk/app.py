from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from typing import Any

import asyncpg
import nats
from nats.js.api import ConsumerConfig, DeliverPolicy
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

SERVICE = "risk"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay",
)

logger = logging.getLogger(SERVICE)
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes", "on")
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

if DEBUG:
    logger.info("Debug logging enabled")

# Prometheus metrics
fills_processed = Counter(
    "risk_fills_processed_total", "Total fills processed by risk service"
)
positions_updated = Counter("risk_positions_updated_total", "Total position updates")
drawdowns_recorded = Counter("risk_drawdowns_recorded_total", "Total drawdown records")
breaches_detected = Counter(
    "risk_limit_breaches_total", "Total exposure limit breaches", ["account_id"]
)
account_exposure = Gauge(
    "risk_account_exposure_usd",
    "Current account notional exposure",
    ["license_id", "account_id"],
)
account_drawdown = Gauge(
    "risk_account_drawdown_pct",
    "Current account drawdown percentage",
    ["license_id", "account_id"],
)


async def update_position(
    pool: asyncpg.Pool,
    license_id: str,
    account_id: str,
    symbol: str,
    size: float,
    entry_price: float | None,
    current_price: float | None,
) -> None:
    """Update or insert account position."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO account_positions
                (license_id, account_id, symbol, position_size, entry_price, current_price, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (license_id, account_id, symbol)
            DO UPDATE SET
                position_size = $4,
                entry_price = $5,
                current_price = $6,
                updated_at = NOW()
            """,
            license_id,
            account_id,
            symbol,
            size,
            entry_price,
            current_price,
        )
        positions_updated.inc()


async def check_exposure_limits(
    pool: asyncpg.Pool, license_id: str, account_id: str
) -> dict[str, Any]:
    """Check if account exceeds exposure limits."""
    async with pool.acquire() as conn:
        # Get current positions and their notional value
        positions = await conn.fetch(
            """
            SELECT symbol, position_size, current_price
            FROM account_positions
            WHERE license_id = $1 AND account_id = $2 AND position_size != 0
            """,
            license_id,
            account_id,
        )

        total_notional = sum(
            (p["position_size"] or 0) * (p["current_price"] or 0) for p in positions
        )

        # Get exposure limits
        limit = await conn.fetchrow(
            """
            SELECT max_notional_usd, max_position_size_pct, max_loss_pct
            FROM portfolio_exposure_limits
            WHERE license_id = $1 AND account_id = $2
            """,
            license_id,
            account_id,
        )

        breached = False
        breach_reason = None

        if limit:
            if limit["max_notional_usd"] and total_notional > limit["max_notional_usd"]:
                breached = True
                breach_reason = f"Notional exposure ${total_notional:.2f} exceeds limit ${limit['max_notional_usd']:.2f}"

        account_exposure.labels(license_id=license_id, account_id=account_id).set(
            total_notional
        )

        return {
            "account_id": account_id,
            "total_notional": total_notional,
            "limit": limit["max_notional_usd"] if limit else None,
            "breached": breached,
            "reason": breach_reason,
        }


async def record_drawdown(
    pool: asyncpg.Pool,
    license_id: str,
    account_id: str,
    peak_equity: float,
    current_equity: float,
) -> None:
    """Record account drawdown."""
    if peak_equity > 0:
        drawdown_pct = ((peak_equity - current_equity) / peak_equity) * 100
    else:
        drawdown_pct = 0

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO account_drawdowns
                (license_id, account_id, peak_equity, current_equity, drawdown_pct, recorded_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (license_id, account_id)
            DO UPDATE SET
                peak_equity = $3,
                current_equity = $4,
                drawdown_pct = $5,
                recorded_at = NOW()
            """,
            license_id,
            account_id,
            peak_equity,
            current_equity,
            drawdown_pct,
        )
        drawdowns_recorded.inc()
        account_drawdown.labels(license_id=license_id, account_id=account_id).set(
            drawdown_pct
        )


async def on_fill(
    pool: asyncpg.Pool | None, js: nats.aio.JetStreamContext | None, msg: Any
) -> None:
    """Process fill message and update positions."""
    fills_processed.inc()

    # Extract instance_id from subject (fills.{instance_id}.{account_id})
    parts = msg.subject.split(".")
    instance_id = parts[1] if len(parts) > 1 else ""
    account_id = parts[2] if len(parts) > 2 else ""

    try:
        fill = json.loads(msg.data)
    except Exception as exc:
        logger.error("parse fill: %s", exc)
        await msg.ack()
        return

    if not pool or not js:
        await msg.ack()
        return

    try:
        # Get license_id from instance
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT l.id AS license_id
                FROM instances i
                JOIN licenses l ON l.id = i.license_id
                WHERE i.instance_key = $1 AND l.active = TRUE
                LIMIT 1
                """,
                instance_id,
            )

            if row is None:
                await msg.ack()
                return

            license_id = row["license_id"]

        # Extract fill details from payload
        symbol = fill.get("symbol", "")
        entry_price = fill.get("entry_price")
        current_price = fill.get("current_price")
        position_size = fill.get("position_size", 0)

        if symbol and account_id:
            # Update position
            await update_position(
                pool,
                license_id,
                account_id,
                symbol,
                position_size,
                entry_price,
                current_price,
            )

            # Check exposure limits
            exposure = await check_exposure_limits(pool, license_id, account_id)

            if exposure["breached"]:
                breaches_detected.labels(account_id=account_id).inc()

                # Log breach
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO risk_breach_log
                            (license_id, account_id, breach_type, current_value, limit_value, metadata, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, NOW() AT TIME ZONE 'UTC')
                        """,
                        license_id,
                        account_id,
                        "notional_exposure",
                        exposure["total_notional"],
                        exposure["limit"],
                        json.dumps({"reason": exposure["reason"]}),
                    )

                # Publish risk alert
                if js:
                    alert = {
                        "license_id": license_id,
                        "account_id": account_id,
                        "breach_type": "notional_exposure",
                        "current_value": exposure["total_notional"],
                        "limit_value": exposure["limit"],
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    try:
                        await js.publish(
                            f"events.risk.breach.{account_id}",
                            json.dumps(alert).encode(),
                        )
                    except Exception as exc:
                        logger.error("publish risk alert: %s", exc)

    except Exception as exc:
        logger.error("on_fill: %s", exc)

    await msg.ack()


async def setup_fills_stream(js: nats.aio.JetStreamContext) -> None:
    """Ensure FILLS stream exists."""
    try:
        await js.stream_info("FILLS")
    except nats.errors.NotFoundError:
        await js.add_stream(
            name="FILLS",
            subjects=["fills.>"],
            max_age=7 * 24 * 3600 * 1_000_000_000,  # 7 days
        )
        logger.info("created FILLS stream")


async def subscribe_fills(
    js: nats.aio.JetStreamContext, pool: asyncpg.Pool
) -> nats.aio.subscription.Subscription:
    """Subscribe to fills stream."""
    consumer_config = ConsumerConfig(
        deliver_policy=DeliverPolicy.ALL,
        max_ack_pending=100,
    )

    sub = await js.subscribe(
        "fills.>",
        config=consumer_config,
        cb=lambda msg: on_fill(pool, js, msg),
    )
    logger.info("subscribed to fills stream")
    return sub


async def http_handler(reader, writer):
    """Simple HTTP server for metrics and health."""
    request_line = (await reader.readline()).decode().strip()
    if not request_line:
        writer.close()
        return

    path = request_line.split()[1]

    if path == "/health":
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 18\r\nContent-Type: application/json\r\n\r\n{"status":"ok"}'
    elif path == "/metrics":
        metrics_data = generate_latest()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: " + CONTENT_TYPE_LATEST.encode() + b"\r\n"
            b"Content-Length: " + str(len(metrics_data)).encode() + b"\r\n"
            b"\r\n"
        ) + metrics_data
    else:
        response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

    writer.write(response)
    await writer.drain()
    writer.close()


async def main():
    logger.info(f"starting {SERVICE} service")

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    if not pool:
        logger.error("failed to create database pool")
        sys.exit(1)

    try:
        nc = await nats.connect(NATS_URL, name=f"execrelay-{SERVICE}")
        js = nc.jetstream()

        await setup_fills_stream(js)
        sub = await subscribe_fills(js, pool)

        # Start HTTP server
        server = await asyncio.start_server(http_handler, "0.0.0.0", HTTP_PORT)
        logger.info(f"HTTP server listening on :{HTTP_PORT}")

        # Setup signal handlers
        loop = asyncio.get_event_loop()

        def shutdown():
            logger.info(f"shutting down {SERVICE}")
            loop.stop()

        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(sig, shutdown)

        async with server:
            await server.serve_forever()

    except Exception as exc:
        logger.error(f"error in main: {exc}")
    finally:
        await pool.close() if pool else None
        await nc.drain() if nc else None


if __name__ == "__main__":
    asyncio.run(main())

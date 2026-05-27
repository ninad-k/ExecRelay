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
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

SERVICE = "ml-feature-extractor"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
NATS_URL = os.environ.get("NATS_URL", "nats://nats:4222")
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay",
)
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes", "on")

logger = logging.getLogger(SERVICE)
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

if DEBUG:
    logger.info("Debug logging enabled")

# Prometheus metrics
signals_processed = Counter(
    "ml_signals_processed_total", "Total signals processed for feature extraction"
)
features_extracted = Counter(
    "ml_features_extracted_total", "Total signal features extracted"
)
extraction_errors = Counter(
    "ml_extraction_errors_total", "Total feature extraction errors"
)


async def extract_signal_features(
    pool: asyncpg.Pool,
    signal_id: int,
    license_id: str,
    symbol: str,
    signal_data: dict[str, Any],
) -> dict[str, Any]:
    """Extract features from a signal for ML model training."""
    features = {
        "signal_id": signal_id,
        "license_id": license_id,
        "symbol": symbol,
        "time_of_day_hour": datetime.utcnow().hour,
        "day_of_week": datetime.utcnow().weekday(),
        "symbol_volatility": 0.0,
        "signal_frequency": 0,
        "win_rate_pct": 0.0,
        "account_drawdown_pct": 0.0,
        "portfolio_correlation_exposure": 0.0,
    }

    try:
        async with pool.acquire() as conn:
            # Calculate symbol volatility (from recent price history if available)
            volatility_row = await conn.fetchrow(
                """
                SELECT STDDEV(close_price) as volatility
                FROM price_history
                WHERE symbol = $1 AND recorded_at > now() - interval '30 days'
                """,
                symbol,
            )
            if volatility_row and volatility_row["volatility"]:
                features["symbol_volatility"] = float(volatility_row["volatility"])

            # Calculate signal frequency for this symbol
            freq_row = await conn.fetchrow(
                """
                SELECT COUNT(*) as count
                FROM accepted_signals
                WHERE license_id = $1 AND symbol = $2 AND created_at > now() - interval '7 days'
                """,
                license_id,
                symbol,
            )
            if freq_row:
                features["signal_frequency"] = freq_row["count"]

            # Calculate historical win rate for similar signals
            win_rate_row = await conn.fetchrow(
                """
                SELECT
                    COUNT(CASE WHEN f.order_status = 'filled' THEN 1 END)::FLOAT /
                    NULLIF(COUNT(*), 0) * 100 as win_pct
                FROM accepted_signals s
                LEFT JOIN fills f ON s.id = f.signal_id
                WHERE s.license_id = $1 AND s.symbol = $2 AND s.created_at > now() - interval '30 days'
                """,
                license_id,
                symbol,
            )
            if win_rate_row and win_rate_row["win_pct"] is not None:
                features["win_rate_pct"] = float(win_rate_row["win_pct"])

            # Get current account drawdown
            if "account_id" in signal_data:
                drawdown_row = await conn.fetchrow(
                    """
                    SELECT drawdown_pct FROM account_drawdowns
                    WHERE license_id = $1 AND account_id = $2
                    LIMIT 1
                    """,
                    license_id,
                    signal_data["account_id"],
                )
                if drawdown_row:
                    features["account_drawdown_pct"] = float(
                        drawdown_row["drawdown_pct"]
                    )

            # Calculate portfolio correlation exposure
            corr_row = await conn.fetchrow(
                """
                SELECT AVG(ABS(correlation_coefficient)) as avg_corr
                FROM symbol_correlations
                WHERE license_id = $1 AND (symbol_a = $2 OR symbol_b = $2)
                """,
                license_id,
                symbol,
            )
            if corr_row and corr_row["avg_corr"] is not None:
                features["portfolio_correlation_exposure"] = float(corr_row["avg_corr"])

    except Exception as exc:
        logger.error("extract_signal_features: %s", exc)
        extraction_errors.inc()
        return None

    return features


async def on_signal(pool: asyncpg.Pool | None, msg: Any) -> None:
    """Process signal message and extract features."""
    signals_processed.inc()

    if not pool:
        await msg.ack()
        return

    try:
        signal_data = json.loads(msg.data)
        signal_id = signal_data.get("id")
        license_id = signal_data.get("license_id")
        symbol = signal_data.get("symbol")

        if not all([signal_id, license_id, symbol]):
            await msg.ack()
            return

        features = await extract_signal_features(
            pool, signal_id, license_id, symbol, signal_data
        )

        if features:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signal_features
                        (signal_id, license_id, symbol, time_of_day_hour, day_of_week,
                         symbol_volatility, signal_frequency, win_rate_pct,
                         account_drawdown_pct, portfolio_correlation_exposure, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                    ON CONFLICT (signal_id) DO UPDATE SET
                        time_of_day_hour = $4,
                        day_of_week = $5,
                        symbol_volatility = $6,
                        signal_frequency = $7,
                        win_rate_pct = $8,
                        account_drawdown_pct = $9,
                        portfolio_correlation_exposure = $10
                    """,
                    features["signal_id"],
                    features["license_id"],
                    features["symbol"],
                    features["time_of_day_hour"],
                    features["day_of_week"],
                    features["symbol_volatility"],
                    features["signal_frequency"],
                    features["win_rate_pct"],
                    features["account_drawdown_pct"],
                    features["portfolio_correlation_exposure"],
                )
                features_extracted.inc()

    except Exception as exc:
        logger.error("on_signal: %s", exc)
        extraction_errors.inc()

    await msg.ack()


async def setup_signals_stream(js: nats.aio.JetStreamContext) -> None:
    """Ensure SIGNALS stream exists."""
    try:
        await js.stream_info("SIGNALS")
    except nats.errors.NotFoundError:
        await js.add_stream(
            name="SIGNALS",
            subjects=["signals.>"],
            max_age=7 * 24 * 3600 * 1_000_000_000,
        )
        logger.info("created SIGNALS stream")


async def subscribe_signals(
    js: nats.aio.JetStreamContext, pool: asyncpg.Pool
) -> nats.aio.subscription.Subscription:
    """Subscribe to signals stream."""
    consumer_config = ConsumerConfig(
        deliver_policy=DeliverPolicy.ALL,
        max_ack_pending=100,
    )

    sub = await js.subscribe(
        "signals.>",
        config=consumer_config,
        cb=lambda msg: on_signal(pool, msg),
    )
    logger.info("subscribed to signals stream")
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

        await setup_signals_stream(js)
        sub = await subscribe_signals(js, pool)

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

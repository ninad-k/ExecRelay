from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

SERVICE = "backtester"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
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
backtests_started = Counter(
    "backtester_backtests_started_total", "Total backtests started"
)
backtests_completed = Counter(
    "backtester_backtests_completed_total", "Total backtests completed"
)
backtest_errors = Counter("backtester_errors_total", "Total backtest errors")
active_backtests = Gauge("backtester_active_backtests", "Currently active backtests")


async def run_backtest(
    pool: asyncpg.Pool, license_id: str, date_start: str, date_end: str
) -> dict[str, Any]:
    """Run a backtest simulation for a date range."""
    backtests_started.inc()
    backtest_job_id = str(uuid.uuid4())

    try:
        # Parse date strings to date objects
        try:
            start_date = datetime.strptime(date_start, "%Y-%m-%d").date()
            end_date = datetime.strptime(date_end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date format. Use YYYY-MM-DD: {exc}")

        # Convert license_id string to UUID if needed
        try:
            license_uuid = uuid.UUID(license_id)
        except (ValueError, TypeError):
            license_uuid = license_id

        async with pool.acquire() as conn:
            # Get all signals in date range
            signals = await conn.fetch(
                """
                SELECT id, symbol, command, received_at
                FROM accepted_signals
                WHERE license_id = $1 AND DATE(received_at) >= $2 AND DATE(received_at) <= $3
                ORDER BY received_at
                """,
                license_uuid,
                start_date,
                end_date,
            )

            if not signals:
                logger.info(f"backtest {backtest_job_id}: no signals found")
                return {
                    "job_id": backtest_job_id,
                    "total_signals": 0,
                    "total_fills": 0,
                    "status": "completed",
                }

            # Get corresponding fills
            signal_ids = [str(s["id"]) for s in signals]
            fills = await conn.fetch(
                """
                SELECT signal_id, status, payload
                FROM fills
                WHERE signal_id = ANY($1::uuid[])
                """,
                signal_ids,
            )

            # Calculate metrics
            total_signals = len(signals)
            total_fills = len(fills)
            fill_rate = (total_fills / total_signals * 100) if total_signals > 0 else 0

            # Simulate P&L (simplified: assume each fill has an entry/exit price)
            gross_pnl = 0.0
            win_count = 0
            loss_count = 0
            pnl_values = []

            for fill in fills:
                try:
                    payload = (
                        json.loads(fill["payload"])
                        if isinstance(fill["payload"], str)
                        else fill["payload"]
                    )
                    # Extract simulated PnL from fill payload (if available)
                    pnl = payload.get("simulated_pnl", 0.0)
                    gross_pnl += pnl

                    if pnl > 0:
                        win_count += 1
                    elif pnl < 0:
                        loss_count += 1

                    pnl_values.append(pnl)
                except Exception as exc:
                    logger.warning(f"parse fill payload: {exc}")

            win_pct = (win_count / total_fills * 100) if total_fills > 0 else 0
            avg_win = (
                sum(p for p in pnl_values if p > 0) / max(win_count, 1)
                if pnl_values
                else 0
            )
            avg_loss = (
                sum(p for p in pnl_values if p < 0) / max(loss_count, 1)
                if pnl_values
                else 0
            )

            # Calculate Sharpe ratio (simplified: using daily returns)
            daily_returns = {}
            for fill in fills:
                try:
                    payload = (
                        json.loads(fill["payload"])
                        if isinstance(fill["payload"], str)
                        else fill["payload"]
                    )
                    pnl = payload.get("simulated_pnl", 0.0)
                    # Group by day for daily P&L
                    created_at = fill.get("created_at", datetime.utcnow())
                    day = (
                        str(created_at.date())
                        if isinstance(created_at, datetime)
                        else str(created_at)
                    )
                    if day not in daily_returns:
                        daily_returns[day] = 0
                    daily_returns[day] += pnl
                except:
                    pass

            sharpe_ratio = 0.0
            if len(daily_returns) > 1:
                returns = list(daily_returns.values())
                mean_return = sum(returns) / len(returns)
                variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
                std_dev = variance**0.5
                sharpe_ratio = (mean_return / std_dev) if std_dev > 0 else 0

            # Max drawdown (simplified)
            max_drawdown = 0.0
            cumulative_pnl = 0
            peak_pnl = 0
            for pnl in pnl_values:
                cumulative_pnl += pnl
                if cumulative_pnl > peak_pnl:
                    peak_pnl = cumulative_pnl
                drawdown = peak_pnl - cumulative_pnl
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

            # Store result
            await conn.execute(
                """
                INSERT INTO backtesting_results
                    (backtest_job_id, license_id, date_range_start, date_range_end,
                     total_signals, total_fills, fill_rate_pct, gross_pnl, net_pnl,
                     sharpe_ratio, max_drawdown_pct, win_count, loss_count, win_pct,
                     avg_win_pnl, avg_loss_pnl, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, NOW())
                """,
                backtest_job_id,
                license_id,
                date_start,
                date_end,
                total_signals,
                total_fills,
                fill_rate,
                gross_pnl,
                gross_pnl * 0.95,  # net_pnl with 5% fee
                sharpe_ratio,
                max_drawdown,
                win_count,
                loss_count,
                win_pct,
                avg_win,
                avg_loss,
            )

            backtests_completed.inc()
            logger.info(
                f"backtest {backtest_job_id} completed: signals={total_signals}, fills={total_fills}, pnl={gross_pnl:.2f}"
            )

            return {
                "job_id": backtest_job_id,
                "total_signals": total_signals,
                "total_fills": total_fills,
                "fill_rate_pct": fill_rate,
                "gross_pnl": gross_pnl,
                "net_pnl": gross_pnl * 0.95,
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown_pct": max_drawdown,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_pct": win_pct,
                "status": "completed",
            }

    except Exception as exc:
        logger.error(f"backtest error: {exc}")
        backtest_errors.inc()
        return {"job_id": backtest_job_id, "status": "failed", "error": str(exc)}


async def http_handler(reader, writer):
    """HTTP server for backtest requests and metrics."""
    request_line = (await reader.readline()).decode().strip()
    if not request_line:
        writer.close()
        return

    parts = request_line.split()
    if len(parts) < 2:
        writer.close()
        return

    method = parts[0]
    path = parts[1]

    if path == "/health" and method == "GET":
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 18\r\nContent-Type: application/json\r\n\r\n{"status":"ok"}'

    elif path == "/metrics" and method == "GET":
        metrics_data = generate_latest()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: " + CONTENT_TYPE_LATEST.encode() + b"\r\n"
            b"Content-Length: " + str(len(metrics_data)).encode() + b"\r\n"
            b"\r\n"
        ) + metrics_data

    elif path.startswith("/backtest") and method == "POST":
        try:
            # Read POST body
            content_length = 0
            headers = {}
            while True:
                line = (await reader.readline()).decode().strip()
                if not line:
                    break
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.lower()] = val.strip()
                    if key.lower() == "content-length":
                        content_length = int(val.strip())

            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            data = json.loads(body.decode())
            license_id = data.get("license_id")
            date_start = data.get("date_start")
            date_end = data.get("date_end")

            if not all([license_id, date_start, date_end]):
                response_body = json.dumps({"error": "missing parameters"}).encode()
                response = (
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                    b"\r\n"
                ) + response_body
            else:
                # This would normally be async, but for simplicity run in background
                active_backtests.inc()
                result = await run_backtest(pool, license_id, date_start, date_end)
                active_backtests.dec()

                response_body = json.dumps(result).encode()
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(response_body)).encode() + b"\r\n"
                    b"\r\n"
                ) + response_body

        except Exception as exc:
            logger.error(f"backtest endpoint error: {exc}")
            response = (
                b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"
            )

    else:
        response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

    writer.write(response)
    await writer.drain()
    writer.close()


async def main():
    global pool

    logger.info(f"starting {SERVICE} service")

    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    if not pool:
        logger.error("failed to create database pool")
        sys.exit(1)

    try:
        # Start HTTP server
        server = await asyncio.start_server(http_handler, "0.0.0.0", HTTP_PORT)
        logger.info(f"HTTP server listening on :{HTTP_PORT}")

        # Setup signal handlers
        loop = asyncio.get_event_loop()

        def shutdown():
            logger.info(f"shutting down {SERVICE}")
            loop.stop()

        import signal as signal_module

        for sig in [signal_module.SIGINT, signal_module.SIGTERM]:
            loop.add_signal_handler(sig, shutdown)

        async with server:
            await server.serve_forever()

    except Exception as exc:
        logger.error(f"error in main: {exc}")
    finally:
        await pool.close() if pool else None


if __name__ == "__main__":
    pool = None
    asyncio.run(main())

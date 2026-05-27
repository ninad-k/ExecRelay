from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator

import asyncpg
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

SERVICE = "reports"
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay",
)
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes", "on")

REPORT_TYPES = {"daily_signal_summary", "weekly_performance"}

logger = logging.getLogger(SERVICE)
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

if DEBUG:
    logger.info("Debug logging enabled")

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _pool
    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=2, max_size=10, command_timeout=15
        )
        logger.info("db pool ready")
    except Exception as exc:
        logger.warning("db unavailable: %s", exc)
    yield
    if _pool is not None:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "db unavailable")
    return _pool


app = FastAPI(title="ExecRelay Reports", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": SERVICE, "status": "ok"}


@app.get("/reports")
async def list_reports(
    report_type: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    pool = get_pool()
    if report_type:
        rows = await pool.fetch(
            "SELECT id, report_type, data_as_of, status, created_at"
            " FROM report_runs WHERE report_type = $1"
            " ORDER BY created_at DESC LIMIT $2",
            report_type,
            limit,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, report_type, data_as_of, status, created_at"
            " FROM report_runs ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return [
        {
            "id": str(r["id"]),
            "report_type": r["report_type"],
            "data_as_of": r["data_as_of"].isoformat(),
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@app.get("/reports/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT id, report_type, data_as_of, status, payload, created_at"
        " FROM report_runs WHERE id = $1::uuid",
        report_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    return {
        "id": str(row["id"]),
        "report_type": row["report_type"],
        "data_as_of": row["data_as_of"].isoformat(),
        "status": row["status"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"].isoformat(),
    }


@app.post("/reports/generate", status_code=status.HTTP_201_CREATED)
async def generate_report(
    report_type: str = Query(...),
    target_date: date = Query(default_factory=lambda: date.today() - timedelta(days=1)),
    license_id: str | None = Query(None),
) -> dict[str, Any]:
    if report_type not in REPORT_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"report_type must be one of: {', '.join(sorted(REPORT_TYPES))}",
        )
    pool = get_pool()
    data_as_of = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        23,
        59,
        59,
        tzinfo=timezone.utc,
    )

    if report_type == "daily_signal_summary":
        payload = await _build_daily_signal_summary(pool, target_date)
    else:
        payload = await _build_weekly_performance(
            pool, target_date, license_id=license_id
        )

    payload_json = json.dumps(payload)
    content_hash = hashlib.sha256(payload_json.encode()).hexdigest()

    row = await pool.fetchrow(
        """
        INSERT INTO report_runs (report_type, data_as_of, content_hash, status, payload)
        VALUES ($1, $2, $3, 'completed', $4::jsonb)
        ON CONFLICT (report_type, data_as_of, content_hash) DO UPDATE
            SET status = 'completed'
        RETURNING id, created_at
        """,
        report_type,
        data_as_of,
        content_hash,
        payload_json,
    )
    assert row is not None
    logger.info("generated %s for %s", report_type, target_date)
    return {
        "id": str(row["id"]),
        "report_type": report_type,
        "data_as_of": data_as_of.isoformat(),
        "status": "completed",
        "created_at": row["created_at"].isoformat(),
    }


async def _build_daily_signal_summary(
    pool: asyncpg.Pool, target_date: date
) -> dict[str, Any]:
    day_start = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc
    )
    day_end = day_start + timedelta(days=1)

    total_signals = await pool.fetchval(
        "SELECT COUNT(*) FROM accepted_signals WHERE received_at >= $1 AND received_at < $2",
        day_start,
        day_end,
    )
    by_command = await pool.fetch(
        "SELECT command, COUNT(*) AS cnt FROM accepted_signals"
        " WHERE received_at >= $1 AND received_at < $2"
        " GROUP BY command ORDER BY cnt DESC",
        day_start,
        day_end,
    )
    by_symbol = await pool.fetch(
        "SELECT symbol, COUNT(*) AS cnt FROM accepted_signals"
        " WHERE received_at >= $1 AND received_at < $2"
        " GROUP BY symbol ORDER BY cnt DESC LIMIT 20",
        day_start,
        day_end,
    )
    fill_row = await pool.fetchrow(
        "SELECT status, COUNT(*) AS cnt FROM fills"
        " WHERE created_at >= $1 AND created_at < $2"
        " GROUP BY status",
        day_start,
        day_end,
    )

    fills_by_status: dict[str, int] = {}
    if fill_row:
        rows = await pool.fetch(
            "SELECT status, COUNT(*) AS cnt FROM fills"
            " WHERE created_at >= $1 AND created_at < $2 GROUP BY status",
            day_start,
            day_end,
        )
        fills_by_status = {r["status"]: r["cnt"] for r in rows}

    total_fills = sum(fills_by_status.values())
    fill_rate = (
        round(fills_by_status.get("filled", 0) / total_fills * 100, 2)
        if total_fills > 0
        else 0.0
    )

    return {
        "date": target_date.isoformat(),
        "total_signals": total_signals,
        "total_fills": total_fills,
        "fill_rate_pct": fill_rate,
        "fills_by_status": fills_by_status,
        "signals_by_command": [
            {"command": r["command"], "count": r["cnt"]} for r in by_command
        ],
        "signals_by_symbol": [
            {"symbol": r["symbol"], "count": r["cnt"]} for r in by_symbol
        ],
    }


async def _build_weekly_performance(
    pool: asyncpg.Pool,
    target_date: date,
    license_id: str | None = None,
) -> dict[str, Any]:
    week_end = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    week_start = week_end - timedelta(days=7)

    license_filter = "AND s.license_id = $3::uuid" if license_id else ""
    latency_args: list[Any] = [week_start, week_end]
    if license_id:
        latency_args.append(license_id)

    latency_row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*) AS cnt,
            PERCENTILE_CONT(0.50) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p50_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p95_ms,
            PERCENTILE_CONT(0.99) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p99_ms
        FROM accepted_signals s
        JOIN fills f ON f.trace_id = s.trace_id
        WHERE s.received_at >= $1 AND s.received_at < $2
          AND f.status = 'filled'
          {license_filter}
        """,
        *latency_args,
    )

    daily_filter = "AND license_id = $3::uuid" if license_id else ""
    daily_args: list[Any] = [week_start, week_end]
    if license_id:
        daily_args.append(license_id)

    daily_rows = await pool.fetch(
        f"""
        SELECT DATE_TRUNC('day', received_at) AS day, COUNT(*) AS cnt
        FROM accepted_signals
        WHERE received_at >= $1 AND received_at < $2
          {daily_filter}
        GROUP BY day ORDER BY day
        """,
        *daily_args,
    )

    def _ms(v: Any) -> float | None:
        return round(float(v), 1) if v is not None else None

    result: dict[str, Any] = {
        "week_start": week_start.date().isoformat(),
        "week_end": (week_end - timedelta(seconds=1)).date().isoformat(),
        "sample_count": latency_row["cnt"] if latency_row else 0,
        "latency_p50_ms": _ms(latency_row["p50_ms"]) if latency_row else None,
        "latency_p95_ms": _ms(latency_row["p95_ms"]) if latency_row else None,
        "latency_p99_ms": _ms(latency_row["p99_ms"]) if latency_row else None,
        "daily_signal_counts": [
            {"date": r["day"].date().isoformat(), "count": r["cnt"]} for r in daily_rows
        ],
    }
    if license_id:
        result["license_id"] = license_id
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def healthcheck(addr: str) -> None:
    host = "127.0.0.1" if addr.startswith("0.0.0.0:") else addr.rsplit(":", 1)[0]
    port = addr.rsplit(":", 1)[1]
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as r:
        if r.status != 200:
            raise SystemExit(1)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        healthcheck(HTTP_ADDR)
        return

    host, port_str = HTTP_ADDR.rsplit(":", 1)
    uvicorn.run("app:app", host=host, port=int(port_str), log_level="info")


if __name__ == "__main__":
    main()

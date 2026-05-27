from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

SERVICE = "analytics"
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")
DATABASE_URL = os.environ.get(
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


app = FastAPI(title="ExecRelay Analytics", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": SERVICE, "status": "ok"}


@app.get("/analytics/signals/summary")
async def signals_summary(
    license_id: str | None = Query(None),
    window_hours: int = Query(24, ge=1, le=8760),
) -> dict[str, Any]:
    pool = get_pool()
    where = "received_at >= NOW() - ($1 || ' hours')::interval"
    params: list[Any] = [str(window_hours)]
    if license_id:
        params.append(license_id)
        where += f" AND license_id = ${len(params)}::uuid"

    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM accepted_signals WHERE {where}", *params
    )
    by_command = await pool.fetch(
        f"SELECT command, COUNT(*) AS cnt FROM accepted_signals WHERE {where}"
        " GROUP BY command ORDER BY cnt DESC LIMIT 20",
        *params,
    )
    by_symbol = await pool.fetch(
        f"SELECT symbol, COUNT(*) AS cnt FROM accepted_signals WHERE {where}"
        " GROUP BY symbol ORDER BY cnt DESC LIMIT 20",
        *params,
    )
    return {
        "window_hours": window_hours,
        "total": total,
        "by_command": [
            {"command": r["command"], "count": r["cnt"]} for r in by_command
        ],
        "by_symbol": [{"symbol": r["symbol"], "count": r["cnt"]} for r in by_symbol],
    }


@app.get("/analytics/fills/summary")
async def fills_summary(
    license_id: str | None = Query(None),
    window_hours: int = Query(24, ge=1, le=8760),
) -> dict[str, Any]:
    pool = get_pool()
    where = "created_at >= NOW() - ($1 || ' hours')::interval"
    params: list[Any] = [str(window_hours)]
    if license_id:
        params.append(license_id)
        where += f" AND license_id = ${len(params)}::uuid"

    rows = await pool.fetch(
        f"SELECT status, COUNT(*) AS cnt FROM fills WHERE {where}" " GROUP BY status",
        *params,
    )
    counts: dict[str, int] = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    filled = counts.get("filled", 0)
    fill_rate = round(filled / total * 100, 2) if total > 0 else 0.0
    return {
        "window_hours": window_hours,
        "total": total,
        "fill_rate_pct": fill_rate,
        "by_status": counts,
    }


@app.get("/analytics/latency")
async def latency_stats(
    license_id: str | None = Query(None),
    window_hours: int = Query(24, ge=1, le=8760),
) -> dict[str, Any]:
    pool = get_pool()
    where_s = "s.received_at >= NOW() - ($1 || ' hours')::interval"
    params: list[Any] = [str(window_hours)]
    if license_id:
        params.append(license_id)
        where_s += f" AND s.license_id = ${len(params)}::uuid"

    row = await pool.fetchrow(
        f"""
        SELECT
            COUNT(*)                                                      AS cnt,
            ROUND(AVG(EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000))::bigint AS avg_ms,
            PERCENTILE_CONT(0.50) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p50_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p95_ms,
            PERCENTILE_CONT(0.99) WITHIN GROUP
                (ORDER BY EXTRACT(EPOCH FROM (f.created_at - s.received_at)) * 1000) AS p99_ms
        FROM accepted_signals s
        JOIN fills f ON f.trace_id = s.trace_id
        WHERE {where_s}
          AND f.status = 'filled'
        """,
        *params,
    )
    if row is None or row["cnt"] == 0:
        return {"window_hours": window_hours, "sample_count": 0}

    def _ms(v: Any) -> float | None:
        return round(float(v), 1) if v is not None else None

    return {
        "window_hours": window_hours,
        "sample_count": row["cnt"],
        "avg_ms": _ms(row["avg_ms"]),
        "p50_ms": _ms(row["p50_ms"]),
        "p95_ms": _ms(row["p95_ms"]),
        "p99_ms": _ms(row["p99_ms"]),
    }


# ---------------------------------------------------------------------------
# CLI (healthcheck + server start)
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

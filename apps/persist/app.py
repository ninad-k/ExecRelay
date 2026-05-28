from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import os
import signal
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import asyncpg
import nats
from nats.js.api import ConsumerConfig, DeliverPolicy
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE = "persist"
ENV = os.environ.get("ENV", "development").lower()
IS_PROD = ENV in ("prod", "production")
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")

_DEV_NATS = "nats://nats:4222"
_DEV_DB = "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay"
NATS_URL = os.environ.get("NATS_URL", _DEV_NATS)
DB_DSN = os.environ.get("DATABASE_URL", _DEV_DB)
DEBUG = os.environ.get("DEBUG", "false" if IS_PROD else "true").lower() in (
    "true", "1", "yes", "on"
)

# Structured JSON logging with trace_id correlation (the worker pulls trace_id
# from each NATS message into a contextvar so every log line in the handler
# carries it; lets the operator pivot from a failing trade to its logs).
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        tid = _trace_id.get()
        if tid:
            payload["trace_id"] = tid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k in ("args", "msg", "exc_info", "exc_text", "stack_info",
                     "msecs", "relativeCreated", "thread", "threadName",
                     "processName", "process", "levelname", "levelno",
                     "name", "pathname", "filename", "module", "funcName",
                     "lineno", "created", "asctime", "message"):
                continue
            payload[k] = v
        return json.dumps(payload, default=str)


logger = logging.getLogger(SERVICE)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_JSONFormatter())
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    handlers=[_handler],
    force=True,
)

if IS_PROD:
    _config_errors = []
    if DB_DSN == _DEV_DB:
        _config_errors.append("DATABASE_URL required in prod (refusing dev default)")
    if NATS_URL == _DEV_NATS:
        _config_errors.append("NATS_URL required in prod (refusing dev default)")
    if _config_errors:
        for err in _config_errors:
            logger.error(err, extra={"event": "config_error"})
        raise SystemExit(2)

# Prometheus metrics
signals_processed = Counter(
    "persist_signals_processed_total", "Total signals processed"
)
fills_processed = Counter("persist_fills_processed_total", "Total fills processed")
events_processed = Counter(
    "persist_events_processed_total", "Total events processed", ["event_type"]
)
persist_lag = Histogram(
    "persist_processing_duration_seconds", "Duration of persist operations"
)
deadletter_total = Counter(
    "persist_deadletter_total",
    "Messages routed to dead-letter (malformed/invalid)",
    ["subject", "reason"],
)

# ---------------------------------------------------------------------------
# Protobuf wire-format parser for Signal (field layout matches signal.pb.go)
# ---------------------------------------------------------------------------


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


class ProtoParseError(ValueError):
    """Raised when a Signal protobuf is malformed or missing required fields."""


# Fields that must be present on every well-formed Signal published by ingress.
_REQUIRED_SIGNAL_FIELDS = ("trace_id", "license_id", "command", "symbol")


def parse_signal(data: bytes) -> dict[str, Any]:
    """Strict wire-format parser. Raises ProtoParseError on:
    - varint overflow / truncated message
    - unknown wire type
    - missing required fields (trace_id, license_id, command, symbol)
    Malformed messages are routed to the dead-letter table by on_signal."""
    if not data:
        raise ProtoParseError("empty payload")

    sig: dict[str, Any] = {"params": []}
    pos = 0
    n = len(data)
    string_fields = {
        1: "trace_id",
        2: "license_id",
        3: "instance_id",
        4: "command",
        5: "raw_command",
        6: "symbol",
        7: "ingress_region",
        9: "body_sha256",
    }
    while pos < n:
        try:
            tag, pos = _varint(data, pos)
        except IndexError as exc:
            raise ProtoParseError(f"truncated at pos {pos}") from exc
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            try:
                val, pos = _varint(data, pos)
            except IndexError as exc:
                raise ProtoParseError(
                    f"truncated varint for field {field_num}"
                ) from exc
            if field_num == 8:
                sig["received_unix_nano"] = val
        elif wire_type == 2:
            try:
                length, pos = _varint(data, pos)
            except IndexError as exc:
                raise ProtoParseError(
                    f"truncated length for field {field_num}"
                ) from exc
            if length < 0 or pos + length > n:
                raise ProtoParseError(
                    f"field {field_num} length {length} overruns payload"
                )
            raw = data[pos : pos + length]
            pos += length
            if field_num in string_fields:
                try:
                    sig[string_fields[field_num]] = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProtoParseError(
                        f"field {field_num} not valid utf-8"
                    ) from exc
            elif field_num == 10:  # repeated SignalParam nested message
                param: dict[str, str] = {}
                p = 0
                while p < len(raw):
                    try:
                        t, p = _varint(raw, p)
                    except IndexError as exc:
                        raise ProtoParseError(
                            "truncated nested param tag"
                        ) from exc
                    fn, wt = t >> 3, t & 0x7
                    if wt == 2:
                        try:
                            ln, p = _varint(raw, p)
                        except IndexError as exc:
                            raise ProtoParseError(
                                "truncated nested param length"
                            ) from exc
                        if ln < 0 or p + ln > len(raw):
                            raise ProtoParseError("nested param overruns")
                        try:
                            v = raw[p : p + ln].decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise ProtoParseError(
                                "nested param not utf-8"
                            ) from exc
                        p += ln
                        if fn == 1:
                            param["key"] = v
                        elif fn == 2:
                            param["value"] = v
                    else:
                        raise ProtoParseError(
                            f"unknown nested wire type {wt}"
                        )
                sig["params"].append(param)
        else:
            raise ProtoParseError(f"unknown wire type {wire_type}")

    missing = [f for f in _REQUIRED_SIGNAL_FIELDS if not sig.get(f)]
    if missing:
        raise ProtoParseError(f"missing required fields: {','.join(missing)}")

    return sig


async def deadletter(
    pool: asyncpg.Pool | None,
    subject: str,
    raw: bytes,
    reason: str,
    err: str,
) -> None:
    """Park malformed messages in dead_letter_messages so an operator can
    inspect what came in and decide whether to replay, ignore, or alert."""
    deadletter_total.labels(subject=subject, reason=reason).inc()
    logger.error(
        "deadletter",
        extra={
            "event": "deadletter",
            "subject": subject,
            "reason": reason,
            "err": err[:500],
            "bytes": len(raw),
        },
    )
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_messages
                    (subject, reason, error_detail, payload)
                VALUES ($1, $2, $3, $4)
                """,
                subject,
                reason,
                err[:2000],
                raw,
            )
    except Exception as exc:
        logger.warning(
            "deadletter_insert_failed",
            extra={"event": "deadletter_insert_failed", "err": repr(exc)[:200]},
        )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def persist_signal(
    pool: asyncpg.Pool, sig: dict[str, Any], raw_data: bytes
) -> None:
    async with pool.acquire() as conn:
        lic = await conn.fetchrow(
            "SELECT id FROM licenses WHERE license_key = $1 AND active = TRUE",
            sig.get("license_id", ""),
        )
        if lic is None:
            return
        license_uuid = lic["id"]

        body_sha256 = sig.get("body_sha256", "")
        if body_sha256:
            inserted = await conn.fetchval(
                """
                INSERT INTO signal_fingerprints (license_id, body_sha256)
                VALUES ($1, $2)
                ON CONFLICT (license_id, body_sha256) DO NOTHING
                RETURNING TRUE
                """,
                license_uuid,
                body_sha256,
            )
            if not inserted:
                return  # duplicate signal

        inst = await conn.fetchrow(
            "SELECT id FROM instances WHERE license_id = $1 AND instance_key = $2",
            license_uuid,
            sig.get("instance_id", ""),
        )
        instance_uuid = inst["id"] if inst else None

        await conn.execute(
            """
            INSERT INTO accepted_signals
                (received_at, license_id, instance_id, trace_id,
                 ingress_region, command, symbol, payload, raw_payload)
            VALUES
                (to_timestamp($1::bigint / 1e9), $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT DO NOTHING
            """,
            sig.get("received_unix_nano", 0),
            license_uuid,
            instance_uuid,
            sig.get("trace_id", ""),
            sig.get("ingress_region", ""),
            sig.get("command", ""),
            sig.get("symbol", ""),
            json.dumps(
                {"raw_command": sig.get("raw_command"), "params": sig.get("params", [])}
            ),
            raw_data,
        )


async def persist_fill(pool: asyncpg.Pool, fill: dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.id AS license_id, i.id AS instance_id
            FROM instances i
            JOIN licenses  l ON l.id = i.license_id
            WHERE i.instance_key = $1 AND l.active = TRUE
            LIMIT 1
            """,
            fill.get("instance_id", ""),
        )
        if row is None:
            return

        await conn.execute(
            """
            INSERT INTO fills
                (license_id, trace_id, status, broker_order_id,
                 error_code, error_message, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            """,
            row["license_id"],
            fill.get("trace_id", ""),
            fill.get("status", ""),
            fill.get("broker_order_id") or None,
            fill.get("error_code") or None,
            fill.get("error_message") or None,
            json.dumps(fill),
        )


# ---------------------------------------------------------------------------
# NATS message handlers
# ---------------------------------------------------------------------------


async def on_signal(pool: asyncpg.Pool | None, msg: Any) -> None:
    signals_processed.inc()
    try:
        sig = parse_signal(msg.data)
    except ProtoParseError as exc:
        await deadletter(pool, msg.subject, msg.data, "parse_error", str(exc))
        await msg.ack()
        return
    except Exception as exc:
        await deadletter(pool, msg.subject, msg.data, "parse_panic", repr(exc))
        await msg.ack()
        return

    token = _trace_id.set(sig.get("trace_id", ""))
    try:
        if pool:
            try:
                with persist_lag.time():
                    await persist_signal(pool, sig, msg.data)
            except Exception as exc:
                logger.error(
                    "persist_signal_failed",
                    extra={
                        "event": "persist_signal_failed",
                        "license_id": sig.get("license_id"),
                        "err": repr(exc)[:200],
                    },
                )
        await msg.ack()
    finally:
        _trace_id.reset(token)


async def on_fill(pool: asyncpg.Pool | None, msg: Any) -> None:
    fills_processed.inc()
    parts = msg.subject.split(".")
    instance_id = parts[1] if len(parts) > 1 else ""
    try:
        fill = json.loads(msg.data)
        if not isinstance(fill, dict):
            raise ValueError("fill payload must be a JSON object")
        fill["instance_id"] = instance_id
    except Exception as exc:
        await deadletter(pool, msg.subject, msg.data, "invalid_json", repr(exc))
        await msg.ack()
        return

    token = _trace_id.set(fill.get("trace_id", ""))
    try:
        if pool:
            try:
                with persist_lag.time():
                    await persist_fill(pool, fill)
            except Exception as exc:
                logger.error(
                    "persist_fill_failed",
                    extra={
                        "event": "persist_fill_failed",
                        "instance_id": instance_id,
                        "err": repr(exc)[:200],
                    },
                )
        await msg.ack()
    finally:
        _trace_id.reset(token)


async def on_event(pool: asyncpg.Pool | None, msg: Any) -> None:
    try:
        evt = json.loads(msg.data)
        if not isinstance(evt, dict):
            raise ValueError("event payload must be a JSON object")
    except Exception as exc:
        await deadletter(pool, msg.subject, msg.data, "invalid_json", repr(exc))
        await msg.ack()
        return

    subject = msg.subject
    event_type = subject.split(".")[-1] if "." in subject else "unknown"
    events_processed.labels(event_type=event_type).inc()

    token = _trace_id.set(evt.get("trace_id", ""))
    try:
        if pool:
            try:
                with persist_lag.time():
                    if subject == "events.ea.connected":
                        await _persist_ea_connected(pool, evt)
                    elif subject == "events.ea.disconnected":
                        await _persist_ea_disconnected(pool, evt)
                    elif subject == "events.ingress.rejection":
                        await _persist_rejection(pool, evt)
                    elif subject == "events.ingress.request":
                        await _persist_request_log(pool, evt)
            except Exception as exc:
                logger.error(
                    "persist_event_failed",
                    extra={
                        "event": "persist_event_failed",
                        "subject": subject,
                        "err": repr(exc)[:200],
                    },
                )
        await msg.ack()
    finally:
        _trace_id.reset(token)


async def _persist_request_log(pool: asyncpg.Pool, evt: dict[str, Any]) -> None:
    """Insert a row in request_log for every webhook attempt (accept + reject).
    Powers `GET /requests/{request_id}` in portal-api so an operator can answer
    'what happened to this call' for any past request."""
    received_at = evt.get("received_at")
    detail_keys = (
        "request_id", "trace_id", "license_key", "method", "path", "client_ip",
        "status", "outcome", "reason_code", "latency_ms", "body_sha256",
        "user_agent", "received_at", "region", "service",
    )
    detail = {k: v for k, v in evt.items() if k not in detail_keys}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO request_log
                (received_at, request_id, trace_id, service, route, method,
                 client_ip, license_key, status_code, outcome, reason_code,
                 latency_ms, body_sha256, user_agent, detail)
            VALUES (
                COALESCE($1::timestamptz, now()), $2, $3, $4, $5, $6,
                NULLIF($7, '')::inet, $8, $9, $10, $11, $12, $13, $14, $15::jsonb
            )
            """,
            received_at,
            str(evt.get("request_id", "") or ""),
            str(evt.get("trace_id", "") or ""),
            str(evt.get("service", "") or "")[:32],
            str(evt.get("path", "") or "")[:128],
            str(evt.get("method", "") or "")[:8],
            str(evt.get("client_ip", "") or ""),
            str(evt.get("license_key", "") or "")[:64] or None,
            int(evt.get("status") or 0),
            str(evt.get("outcome", "") or "")[:32],
            str(evt.get("reason_code", "") or "")[:64] or None,
            int(evt.get("latency_ms") or 0),
            str(evt.get("body_sha256", "") or "")[:64] or None,
            str(evt.get("user_agent", "") or "")[:256] or None,
            json.dumps(detail) if detail else "{}",
        )


async def _persist_ea_connected(pool: asyncpg.Pool, evt: dict[str, Any]) -> None:
    instance_id = evt.get("instance_id", "")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT i.id AS instance_id, l.id AS license_id
            FROM instances i JOIN licenses l ON l.id = i.license_id
            WHERE i.instance_key = $1 AND l.active = TRUE
            LIMIT 1
            """,
            instance_id,
        )
        if row is None:
            return
        await conn.execute(
            """
            INSERT INTO ea_connection_sessions
                (license_id, instance_id, account_number, broker_name,
                 account_type, platform, ea_version, bridge_region)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            row["license_id"],
            row["instance_id"],
            evt.get("account_number", ""),
            evt.get("broker", ""),
            "live",
            evt.get("platform", ""),
            evt.get("ea_version", ""),
            evt.get("bridge_region", ""),
        )


async def _persist_ea_disconnected(pool: asyncpg.Pool, evt: dict[str, Any]) -> None:
    instance_id = evt.get("instance_id", "")
    async with pool.acquire() as conn:
        inst = await conn.fetchrow(
            "SELECT id FROM instances WHERE instance_key = $1 LIMIT 1", instance_id
        )
        if inst is None:
            return
        await conn.execute(
            """
            UPDATE ea_connection_sessions
            SET disconnected_at = now()
            WHERE id = (
                SELECT id FROM ea_connection_sessions
                WHERE instance_id = $1 AND disconnected_at IS NULL
                ORDER BY connected_at DESC
                LIMIT 1
            )
            """,
            inst["id"],
        )


async def _persist_rejection(pool: asyncpg.Pool, evt: dict[str, Any]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_rejections
                (license_id, reason_code, ingress_region, payload_hash)
            VALUES ($1, $2, $3, $4)
            """,
            evt.get("license_id", ""),
            evt.get("reason_code", ""),
            evt.get("region", ""),
            evt.get("payload_hash", ""),
        )


# ---------------------------------------------------------------------------
# Async worker
# ---------------------------------------------------------------------------


async def _probe_db(pool: asyncpg.Pool | None) -> None:
    if pool is None:
        _set_readiness(db_ok=False, db_err="pool not initialized")
        return
    try:
        await pool.fetchval("SELECT 1")
        _set_readiness(db_ok=True, db_err="")
    except Exception as exc:
        _set_readiness(db_ok=False, db_err=repr(exc)[:200])


async def _readiness_loop(pool: asyncpg.Pool | None, nc: Any, stop_event: asyncio.Event) -> None:
    """Refresh readiness state every 5s. Cheap (one SELECT 1 + attribute read)
    so /readyz returns the most recent ground truth without blocking."""
    while not stop_event.is_set():
        await _probe_db(pool)
        try:
            _set_readiness(
                nats_ok=bool(getattr(nc, "is_connected", False)),
                nats_err="" if getattr(nc, "is_connected", False) else "disconnected",
            )
        except Exception as exc:
            _set_readiness(nats_ok=False, nats_err=repr(exc)[:200])
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            continue


async def run(stop_event: asyncio.Event) -> None:
    try:
        pool: asyncpg.Pool | None = await asyncpg.create_pool(
            DB_DSN,
            min_size=2,
            max_size=10,
            command_timeout=5,
        )
        _set_readiness(db_ok=True, db_err="")
        logger.info("db_connected", extra={"event": "db_connected"})
    except Exception as exc:
        _set_readiness(db_ok=False, db_err=repr(exc)[:200])
        logger.warning(
            "db_unavailable",
            extra={"event": "db_unavailable", "err": repr(exc)[:200]},
        )
        pool = None

    nc = await nats.connect(NATS_URL, name="execrelay-persist")
    _set_readiness(nats_ok=True, nats_err="")
    js = nc.jetstream()

    signal_sub = await js.subscribe(
        "signals.>",
        cb=lambda msg: asyncio.ensure_future(on_signal(pool, msg)),
        durable="persist-signals",
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT
        ),
        stream="SIGNALS",
    )
    fill_sub = await js.subscribe(
        "fills.>",
        cb=lambda msg: asyncio.ensure_future(on_fill(pool, msg)),
        durable="persist-fills",
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT
        ),
        stream="FILLS",
    )
    event_sub = await js.subscribe(
        "events.>",
        cb=lambda msg: asyncio.ensure_future(on_event(pool, msg)),
        durable="persist-events",
        config=ConsumerConfig(
            deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT
        ),
        stream="EVENTS",
    )

    readiness_task = asyncio.create_task(_readiness_loop(pool, nc, stop_event))
    logger.info(
        "worker_started",
        extra={"event": "worker_started", "nats_url": NATS_URL},
    )
    await stop_event.wait()

    readiness_task.cancel()
    await signal_sub.unsubscribe()
    await fill_sub.unsubscribe()
    await event_sub.unsubscribe()
    await nc.drain()
    if pool:
        await pool.close()
    logger.info("worker_stopped", extra={"event": "worker_stopped"})


# ---------------------------------------------------------------------------
# HTTP health server
# ---------------------------------------------------------------------------


# --- Liveness state shared between worker loop and HTTP probe thread --------
# Updated by the worker on connect/disconnect and every successful DB ping.
# Kept simple (single dict + atomic dict assignment) to avoid locking from the
# HTTP thread.
_readiness: dict[str, Any] = {
    "db_ok": False,
    "db_err": "not initialized",
    "nats_ok": False,
    "nats_err": "not initialized",
    "last_check_ts": 0.0,
}


def _set_readiness(**kw: Any) -> None:
    _readiness.update(kw)
    _readiness["last_check_ts"] = datetime.now(timezone.utc).timestamp()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._json(200, {"service": SERVICE, "status": "ok"})
        elif self.path == "/readyz":
            snap = dict(_readiness)
            ok = snap.get("db_ok") and snap.get("nats_ok")
            self._json(
                200 if ok else 503,
                {
                    "service": SERVICE,
                    "ok": bool(ok),
                    "checks": {
                        "db": {
                            "ok": snap.get("db_ok"),
                            "err": snap.get("db_err"),
                        },
                        "nats": {
                            "ok": snap.get("nats_ok"),
                            "err": snap.get("nats_err"),
                        },
                    },
                    "last_check_ts": snap.get("last_check_ts"),
                },
            )
        elif self.path == "/metrics":
            body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def start_http(addr: str) -> ThreadingHTTPServer:
    host, port = addr.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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

    http_server = start_http(HTTP_ADDR)
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def _shutdown(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)
        http_server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(run(stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()

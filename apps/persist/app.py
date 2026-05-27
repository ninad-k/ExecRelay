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
from typing import Any

import asyncpg
import nats
from nats.js.api import ConsumerConfig, DeliverPolicy
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE   = "persist"
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0:8080")
NATS_URL  = os.environ.get("NATS_URL",  "nats://nats:4222")
DB_DSN    = os.environ.get("DATABASE_URL",
                            "postgresql://execrelay:execrelay_dev_password@postgres:5432/execrelay")

logger = logging.getLogger(SERVICE)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    stream=sys.stdout)

# Prometheus metrics
signals_processed = Counter("persist_signals_processed_total", "Total signals processed")
fills_processed = Counter("persist_fills_processed_total", "Total fills processed")
events_processed = Counter("persist_events_processed_total", "Total events processed", ["event_type"])
persist_lag = Histogram("persist_processing_duration_seconds", "Duration of persist operations")

# ---------------------------------------------------------------------------
# Protobuf wire-format parser for Signal (field layout matches signal.pb.go)
# ---------------------------------------------------------------------------

def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def parse_signal(data: bytes) -> dict[str, Any]:
    sig: dict[str, Any] = {"params": []}
    pos = 0
    string_fields = {1: "trace_id", 2: "license_id", 3: "instance_id",
                     4: "command",  5: "raw_command", 6: "symbol",
                     7: "ingress_region", 9: "body_sha256"}
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            val, pos = _varint(data, pos)
            if field_num == 8:
                sig["received_unix_nano"] = val
        elif wire_type == 2:
            length, pos = _varint(data, pos)
            raw = data[pos: pos + length]; pos += length
            if field_num in string_fields:
                sig[string_fields[field_num]] = raw.decode("utf-8", errors="replace")
            elif field_num == 10:  # repeated SignalParam nested message
                param: dict[str, str] = {}
                p = 0
                while p < len(raw):
                    t, p = _varint(raw, p)
                    fn, wt = t >> 3, t & 0x7
                    if wt == 2:
                        ln, p = _varint(raw, p)
                        v = raw[p: p + ln].decode("utf-8", errors="replace"); p += ln
                        if fn == 1:
                            param["key"] = v
                        elif fn == 2:
                            param["value"] = v
                sig["params"].append(param)
        else:
            break
    return sig

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

async def persist_signal(pool: asyncpg.Pool, sig: dict[str, Any], raw_data: bytes) -> None:
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
                license_uuid, body_sha256,
            )
            if not inserted:
                return  # duplicate signal

        inst = await conn.fetchrow(
            "SELECT id FROM instances WHERE license_id = $1 AND instance_key = $2",
            license_uuid, sig.get("instance_id", ""),
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
            license_uuid, instance_uuid,
            sig.get("trace_id", ""),
            sig.get("ingress_region", ""),
            sig.get("command", ""),
            sig.get("symbol", ""),
            json.dumps({"raw_command": sig.get("raw_command"),
                        "params": sig.get("params", [])}),
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
    except Exception as exc:
        logger.error("parse signal: %s", exc)
        await msg.ack(); return
    if pool:
        try:
            with persist_lag.time():
                await persist_signal(pool, sig, msg.data)
        except Exception as exc:
            logger.error("persist signal trace_id=%s: %s", sig.get("trace_id"), exc)
    await msg.ack()


async def on_fill(pool: asyncpg.Pool | None, msg: Any) -> None:
    fills_processed.inc()
    parts = msg.subject.split(".")
    instance_id = parts[1] if len(parts) > 1 else ""
    try:
        fill = json.loads(msg.data)
        fill["instance_id"] = instance_id
    except Exception as exc:
        logger.error("parse fill: %s", exc)
        await msg.ack(); return
    if pool:
        try:
            with persist_lag.time():
                await persist_fill(pool, fill)
        except Exception as exc:
            logger.error("persist fill trace_id=%s: %s", fill.get("trace_id"), exc)
    await msg.ack()


async def on_event(pool: asyncpg.Pool | None, msg: Any) -> None:
    try:
        evt = json.loads(msg.data)
    except Exception as exc:
        logger.error("parse event: %s", exc)
        await msg.ack(); return

    subject = msg.subject
    event_type = subject.split(".")[-1] if "." in subject else "unknown"
    events_processed.labels(event_type=event_type).inc()

    if pool:
        try:
            with persist_lag.time():
                if subject == "events.ea.connected":
                    await _persist_ea_connected(pool, evt)
                elif subject == "events.ea.disconnected":
                    await _persist_ea_disconnected(pool, evt)
                elif subject == "events.ingress.rejection":
                    await _persist_rejection(pool, evt)
        except Exception as exc:
            logger.error("persist event subject=%s: %s", subject, exc)
    await msg.ack()


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

async def run(stop_event: asyncio.Event) -> None:
    try:
        pool: asyncpg.Pool | None = await asyncpg.create_pool(
            DB_DSN, min_size=2, max_size=10, command_timeout=5,
        )
        logger.info("db connected")
    except Exception as exc:
        logger.warning("db unavailable, signals will be acked without persistence: %s", exc)
        pool = None

    nc = await nats.connect(NATS_URL, name="execrelay-persist")
    js = nc.jetstream()

    signal_sub = await js.subscribe(
        "signals.>",
        cb=lambda msg: asyncio.ensure_future(on_signal(pool, msg)),
        durable="persist-signals",
        config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT),
        stream="SIGNALS",
    )
    fill_sub = await js.subscribe(
        "fills.>",
        cb=lambda msg: asyncio.ensure_future(on_fill(pool, msg)),
        durable="persist-fills",
        config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT),
        stream="FILLS",
    )
    event_sub = await js.subscribe(
        "events.>",
        cb=lambda msg: asyncio.ensure_future(on_event(pool, msg)),
        durable="persist-events",
        config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=nats.js.api.AckPolicy.EXPLICIT),
        stream="EVENTS",
    )

    logger.info("persist worker started nats=%s", NATS_URL)
    await stop_event.wait()

    await signal_sub.unsubscribe()
    await fill_sub.unsubscribe()
    await event_sub.unsubscribe()
    await nc.drain()
    if pool:
        await pool.close()
    logger.info("persist worker stopped")

# ---------------------------------------------------------------------------
# HTTP health server
# ---------------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            body = json.dumps({"service": SERVICE, "status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/metrics":
            body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

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
        healthcheck(HTTP_ADDR); return

    http_server = start_http(HTTP_ADDR)
    loop        = asyncio.new_event_loop()
    stop_event  = asyncio.Event()

    def _shutdown(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)
        http_server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    try:
        loop.run_until_complete(run(stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()

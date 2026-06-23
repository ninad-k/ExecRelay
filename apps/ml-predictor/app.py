from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as signal_module
import sys
import time

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from xgb_predictor import XGBPredictor

SERVICE = "ml-predictor"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

# Model artifacts ship with the image (see Dockerfile). Override via env for
# local runs or experimentation.
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")
MODEL_PATH = os.environ.get(
    "ML_MODEL_PATH", os.path.join(_MODEL_DIR, "xgb_production.json")
)
FEATURE_ORDER_PATH = os.environ.get(
    "ML_FEATURE_ORDER_PATH", os.path.join(_MODEL_DIR, "feature_order.txt")
)
THRESHOLD = float(os.environ.get("ML_THRESHOLD", "0.50"))

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
predictions_made = Counter("ml_predictions_total", "Total predictions made", ["action"])
prediction_errors = Counter(
    "ml_prediction_errors_total", "Total prediction errors (bad payload or inference)"
)
prediction_latency = Histogram(
    "ml_prediction_latency_seconds", "Prediction latency in seconds"
)
prob_win = Histogram(
    "ml_prob_win",
    "Model win probability per prediction",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
model_loaded = Gauge("ml_model_loaded", "1 if the model loaded successfully else 0")

# Global model state
predictor: XGBPredictor | None = None


def _json_response(status_line: bytes, obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return (
        status_line
        + b"Content-Type: application/json\r\n"
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


async def handle_predict(reader) -> bytes:
    """Read the POST body and return an HTTP response with the decision."""
    content_length = 0
    while True:
        line = (await reader.readline()).decode().strip()
        if not line:
            break
        if ":" in line:
            key, val = line.split(":", 1)
            if key.lower() == "content-length":
                content_length = int(val.strip())

    body = await reader.readexactly(content_length) if content_length > 0 else b""

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError as exc:
        prediction_errors.inc()
        return _json_response(
            b"HTTP/1.1 400 Bad Request\r\n",
            {"error": f"invalid JSON: {exc}", "action_summary": "NOTHING"},
        )

    if predictor is None:
        prediction_errors.inc()
        return _json_response(
            b"HTTP/1.1 503 Service Unavailable\r\n",
            {"error": "model not loaded", "action_summary": "NOTHING"},
        )

    current_position = payload.get("current_position")

    start = time.perf_counter()
    decision = predictor.predict(payload, current_position=current_position)
    prediction_latency.observe(time.perf_counter() - start)

    if decision.get("error"):
        prediction_errors.inc()
        return _json_response(b"HTTP/1.1 400 Bad Request\r\n", decision)

    predictions_made.labels(action=decision["action_summary"]).inc()
    if decision.get("prob_win") is not None:
        prob_win.observe(decision["prob_win"])

    return _json_response(b"HTTP/1.1 200 OK\r\n", decision)


async def http_handler(reader, writer):
    """HTTP server for health, metrics, and inference."""
    try:
        request_line = (await reader.readline()).decode().strip()
        if not request_line:
            writer.close()
            return

        parts = request_line.split()
        if len(parts) < 2:
            writer.close()
            return

        method, path = parts[0], parts[1]

        if path == "/health" and method == "GET":
            status = (
                b"HTTP/1.1 200 OK\r\n"
                if predictor is not None
                else b"HTTP/1.1 503 Service Unavailable\r\n"
            )
            response = _json_response(
                status,
                {"status": "ok" if predictor is not None else "model not loaded"},
            )
        elif path == "/metrics" and method == "GET":
            metrics_data = generate_latest()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: " + CONTENT_TYPE_LATEST.encode() + b"\r\n"
                b"Content-Length: " + str(len(metrics_data)).encode() + b"\r\n\r\n"
            ) + metrics_data
        elif path == "/predict" and method == "POST":
            response = await handle_predict(reader)
        else:
            response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"

        writer.write(response)
        await writer.drain()
    except Exception as exc:  # noqa: BLE001 - never let one request kill the server
        logger.error("request handling error: %s", exc)
    finally:
        writer.close()


async def main():
    global predictor

    logger.info("starting %s service", SERVICE)

    try:
        predictor = XGBPredictor(MODEL_PATH, FEATURE_ORDER_PATH, threshold=THRESHOLD)
        model_loaded.set(1)
    except Exception as exc:  # noqa: BLE001
        # Stay up and serve /metrics + a 503 /health so the failure is observable
        # rather than crash-looping silently.
        logger.error("failed to load model from %s: %s", MODEL_PATH, exc)
        model_loaded.set(0)

    server = await asyncio.start_server(http_handler, "0.0.0.0", HTTP_PORT)
    logger.info("HTTP server listening on :%d (threshold=%.2f)", HTTP_PORT, THRESHOLD)

    loop = asyncio.get_event_loop()
    stop = loop.create_future()

    def shutdown():
        logger.info("shutting down %s", SERVICE)
        if not stop.done():
            stop.set_result(None)

    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            # Windows Proactor loop has no add_signal_handler; rely on KeyboardInterrupt.
            pass

    async with server:
        await stop


if __name__ == "__main__":
    asyncio.run(main())

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

# /predict request bodies are capped at 1 MiB. A bad/huge Content-Length must
# never make the server block on an unbounded readexactly().
MAX_BODY_BYTES = 1024 * 1024

logger = logging.getLogger(SERVICE)
DEBUG = os.environ.get("DEBUG", "false").lower() in ("true", "1", "yes", "on")
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
# Info-style gauge: one time series per loaded model_version, always set to 1.
# Lets dashboards/alerts join on `version` to see which artifact is live
# (and catch an unexpected version showing up after a deploy).
ml_model_info = Gauge(
    "ml_model_info", "Metadata about the loaded model (always 1)", ["version"]
)

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


def _predict_error_response(status_line: bytes, obj: dict) -> bytes:
    """Build a /predict error response, tagging it with model_version when a
    predictor is loaded. Covers request-parsing failures (bad Content-Length,
    oversized body, invalid JSON) that never reach predictor.predict() --
    that method already stamps model_version on its own result dict for
    every path (success or error) it returns."""
    if predictor is not None:
        obj = {**obj, "model_version": predictor.model_version}
    return _json_response(status_line, obj)


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
                try:
                    content_length = int(val.strip())
                except ValueError:
                    prediction_errors.inc()
                    return _predict_error_response(
                        b"HTTP/1.1 400 Bad Request\r\n",
                        {
                            "error": "invalid Content-Length",
                            "action_summary": "NOTHING",
                        },
                    )
                if content_length < 0:
                    prediction_errors.inc()
                    return _predict_error_response(
                        b"HTTP/1.1 400 Bad Request\r\n",
                        {
                            "error": "invalid Content-Length",
                            "action_summary": "NOTHING",
                        },
                    )

    if content_length > MAX_BODY_BYTES:
        prediction_errors.inc()
        # Do NOT readexactly() an attacker-controlled/oversized length -- the
        # response below signals the client to stop, and the connection is
        # closed by the caller right after; no need to drain first.
        return _predict_error_response(
            b"HTTP/1.1 413 Payload Too Large\r\n",
            {"error": "request body too large", "action_summary": "NOTHING"},
        )

    body = await reader.readexactly(content_length) if content_length > 0 else b""

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError as exc:
        prediction_errors.inc()
        return _predict_error_response(
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
    loop = asyncio.get_event_loop()
    decision = await loop.run_in_executor(
        None, lambda: predictor.predict(payload, current_position=current_position)
    )
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
        elif path == "/healthz" and method == "GET":
            # Liveness: the HTTP server is up and serving. Always 200 -- does
            # not depend on the model having loaded (that's /readyz).
            response = _json_response(b"HTTP/1.1 200 OK\r\n", {"status": "ok"})
        elif path == "/readyz" and method == "GET":
            # Readiness: 200 only once the predictor has loaded successfully.
            status = (
                b"HTTP/1.1 200 OK\r\n"
                if predictor is not None
                else b"HTTP/1.1 503 Service Unavailable\r\n"
            )
            response = _json_response(
                status,
                {"status": "ready" if predictor is not None else "not ready"},
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
        ml_model_info.labels(version=predictor.model_version).set(1)
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

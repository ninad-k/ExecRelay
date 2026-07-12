"""HTTP-layer tests for app.py.

Exercises the real asyncio server end-to-end over TCP sockets on an
ephemeral port: liveness/readiness/health, /metrics, and every /predict
request-parsing edge case (bad Content-Length, oversized body, invalid
JSON, predictor-not-loaded, and the happy path via a monkeypatched
predictor). Unknown paths and the 404 fallback are covered too.

Uses the shared `app_module` fixture (see conftest.py) instead of importing
app.py directly -- app.py registers Prometheus metrics against the global
default CollectorRegistry at import time, so it can only be executed once
per test session across every test file. `app_module.predictor` is swapped
per test rather than reloading the module.
"""

from __future__ import annotations

import asyncio
import json


class _StubPredictor:
    """Stands in for XGBPredictor so /predict tests don't need real model
    inference -- the decision returned by .predict() is pinned per test."""

    def __init__(self, decision: dict):
        self._decision = decision

    def predict(self, payload, current_position=None):
        return dict(self._decision)


def _run(coro):
    return asyncio.run(coro)


async def _request(app_module, raw: bytes) -> bytes:
    """Start a fresh server on an ephemeral port, send one raw HTTP request
    over a real socket, and return the full raw response."""
    server = await asyncio.start_server(app_module.http_handler, "127.0.0.1", 0)
    async with server:
        host, port = server.sockets[0].getsockname()[:2]
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(raw)
        await writer.drain()
        # Half-close the write side so the server's readline()/readexactly()
        # see EOF instead of blocking forever on a test that sends an
        # incomplete or empty request (e.g. the empty-request-line case).
        writer.write_eof()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()
    return data


def _status(resp: bytes) -> str:
    return resp.split(b"\r\n", 1)[0].decode()


def _body(resp: bytes) -> dict:
    return json.loads(resp.split(b"\r\n\r\n", 1)[1].decode())


def _predict_request(body: bytes) -> bytes:
    return (
        b"POST /predict HTTP/1.1\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


# ---- /health ----------------------------------------------------------


def test_health_when_predictor_loaded(app_module):
    app_module.predictor = _StubPredictor({})
    resp = _run(_request(app_module, b"GET /health HTTP/1.1\r\n\r\n"))
    assert "200" in _status(resp)
    assert _body(resp)["status"] == "ok"


def test_health_when_predictor_not_loaded(app_module):
    app_module.predictor = None
    resp = _run(_request(app_module, b"GET /health HTTP/1.1\r\n\r\n"))
    assert "503" in _status(resp)
    assert _body(resp)["status"] == "model not loaded"


# ---- /healthz (liveness) -----------------------------------------------


def test_healthz_is_always_ok_even_when_model_not_loaded(app_module):
    app_module.predictor = None
    resp = _run(_request(app_module, b"GET /healthz HTTP/1.1\r\n\r\n"))
    assert "200" in _status(resp)
    assert _body(resp)["status"] == "ok"


def test_healthz_is_ok_when_model_loaded(app_module):
    app_module.predictor = _StubPredictor({})
    resp = _run(_request(app_module, b"GET /healthz HTTP/1.1\r\n\r\n"))
    assert "200" in _status(resp)
    assert _body(resp)["status"] == "ok"


# ---- /readyz (readiness) ------------------------------------------------


def test_readyz_when_predictor_loaded(app_module):
    app_module.predictor = _StubPredictor({})
    resp = _run(_request(app_module, b"GET /readyz HTTP/1.1\r\n\r\n"))
    assert "200" in _status(resp)
    assert _body(resp)["status"] == "ready"


def test_readyz_when_predictor_not_loaded(app_module):
    app_module.predictor = None
    resp = _run(_request(app_module, b"GET /readyz HTTP/1.1\r\n\r\n"))
    assert "503" in _status(resp)
    assert _body(resp)["status"] == "not ready"


# ---- /metrics ------------------------------------------------------------


def test_metrics_endpoint_exposes_prometheus_text(app_module):
    resp = _run(_request(app_module, b"GET /metrics HTTP/1.1\r\n\r\n"))
    assert "200" in _status(resp)
    assert b"ml_predictions_total" in resp
    assert b"ml_model_loaded" in resp


# ---- /predict --------------------------------------------------------------


def test_predict_happy_path_with_monkeypatched_decision(app_module):
    decision = {
        "signal_direction": "LONG",
        "prob_win": 0.8123,
        "threshold": 0.5,
        "should_close": False,
        "should_open": True,
        "open_direction": "LONG",
        "action_summary": "OPEN_LONG",
        "reason": "ok",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "error": None,
    }
    app_module.predictor = _StubPredictor(decision)
    body = json.dumps({"direction": 1, "features": {}}).encode()
    resp = _run(_request(app_module, _predict_request(body)))
    assert "200" in _status(resp)
    parsed = _body(resp)
    assert parsed["action_summary"] == "OPEN_LONG"
    assert parsed["prob_win"] == 0.8123


def test_predict_decision_error_returns_400(app_module):
    decision = {
        "error": "direction must be 1 or -1, got 0",
        "action_summary": "NOTHING",
    }
    app_module.predictor = _StubPredictor(decision)
    body = json.dumps({"direction": 0, "features": {}}).encode()
    resp = _run(_request(app_module, _predict_request(body)))
    assert "400" in _status(resp)
    assert _body(resp)["error"]


def test_predict_invalid_json_returns_400(app_module):
    app_module.predictor = _StubPredictor({})
    resp = _run(_request(app_module, _predict_request(b"{not valid json")))
    assert "400" in _status(resp)
    assert "invalid JSON" in _body(resp)["error"]


def test_predict_oversized_body_returns_413(app_module):
    app_module.predictor = _StubPredictor({})
    raw = (
        b"POST /predict HTTP/1.1\r\nContent-Length: "
        + str(app_module.MAX_BODY_BYTES + 1).encode()
        + b"\r\n\r\n"
    )
    resp = _run(_request(app_module, raw))
    assert "413" in _status(resp)
    assert "too large" in _body(resp)["error"]


def test_predict_bad_content_length_returns_400(app_module):
    app_module.predictor = _StubPredictor({})
    raw = b"POST /predict HTTP/1.1\r\nContent-Length: not-a-number\r\n\r\n"
    resp = _run(_request(app_module, raw))
    assert "400" in _status(resp)
    assert "Content-Length" in _body(resp)["error"]


def test_predict_negative_content_length_returns_400(app_module):
    app_module.predictor = _StubPredictor({})
    raw = b"POST /predict HTTP/1.1\r\nContent-Length: -5\r\n\r\n"
    resp = _run(_request(app_module, raw))
    assert "400" in _status(resp)
    assert "Content-Length" in _body(resp)["error"]


def test_predict_when_predictor_not_loaded_returns_503(app_module):
    app_module.predictor = None
    body = json.dumps({"direction": 1, "features": {}}).encode()
    resp = _run(_request(app_module, _predict_request(body)))
    assert "503" in _status(resp)
    assert "model not loaded" in _body(resp)["error"]


# ---- misc ------------------------------------------------------------------


def test_unknown_path_returns_404(app_module):
    app_module.predictor = _StubPredictor({})
    resp = _run(_request(app_module, b"GET /nope HTTP/1.1\r\n\r\n"))
    assert "404" in _status(resp)


def test_empty_request_line_closes_without_error(app_module):
    resp = _run(_request(app_module, b""))
    assert resp == b""

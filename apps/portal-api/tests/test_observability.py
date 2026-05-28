"""Tests for the observability middleware: request_id assignment,
trace_id propagation, and structured access logs.

These tests don't need a database — they exercise middleware behavior on
endpoints that short-circuit before hitting the DB (e.g. /health)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_module():
    sys.modules.pop("app", None)
    app_path = Path(__file__).resolve().parent.parent / "app.py"
    spec = importlib.util.spec_from_file_location("app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app)


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"service": "portal-api", "status": "ok"}


def test_healthz_returns_ok(client):
    assert client.get("/healthz").status_code == 200


def test_request_id_assigned_when_missing(client):
    r = client.get("/health")
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) == 32  # uuid4 hex


def test_request_id_honored_when_supplied(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers.get("x-request-id") == "abc-123"


def test_trace_id_propagated_when_supplied(client):
    r = client.get(
        "/health",
        headers={"X-ExecRelay-Trace-ID": "deadbeef" * 4},
    )
    assert r.headers.get("x-execrelay-trace-id") == "deadbeef" * 4


def test_readyz_returns_503_when_dependencies_down(app_module, client):
    # _pool / _nc are None until lifespan runs; TestClient context manager
    # would init them — without it, readyz must report not-ready and return 503.
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["db"]["ok"] is False
    assert body["checks"]["nats"]["ok"] is False


def test_404_routes_still_get_request_id(client):
    r = client.get("/no-such-route")
    assert r.status_code == 404
    assert r.headers.get("x-request-id")  # middleware ran

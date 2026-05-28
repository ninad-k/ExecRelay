"""Tests for _persist_request_log argument handling.

Regression guard: the request_log INSERT passes received_at (an ISO-8601
string from the ingress event) to a timestamptz column. asyncpg refuses to
bind a str to a timestamptz param, so the SQL must double-cast
($1::text::timestamptz) and the function must pass None when the field is
absent. These tests lock in both behaviors with a fake connection."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_module():
    sys.modules.pop("app", None)
    try:
        from prometheus_client import REGISTRY

        for collector in list(REGISTRY._collector_to_names.keys()):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    except Exception:
        pass
    app_path = Path(__file__).resolve().parent.parent / "app.py"
    spec = importlib.util.spec_from_file_location("app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


def _run(coro):
    return asyncio.run(coro)


def test_request_log_sql_double_casts_timestamp(app_module):
    pool = _FakePool()
    evt = {
        "request_id": "rid-1",
        "trace_id": "t-1",
        "service": "ingress",
        "path": "/webhook",
        "method": "POST",
        "client_ip": "10.0.0.1",
        "license_key": "L1",
        "status": 200,
        "outcome": "accepted",
        "reason_code": "accepted",
        "latency_ms": 12,
        "body_sha256": "ab" * 32,
        "user_agent": "tv",
        "received_at": "2026-05-28T02:32:25.870641069Z",
        "region": "local",
    }
    _run(app_module._persist_request_log(pool, evt))
    assert pool.conn.executed, "expected an INSERT to be executed"
    sql, args = pool.conn.executed[0]
    # The cast must go through text so asyncpg accepts the ISO string.
    assert "$1::text::timestamptz" in sql
    # received_at is passed through as the original string (arg position 1).
    assert args[0] == "2026-05-28T02:32:25.870641069Z"
    # request_id is arg position 2.
    assert args[1] == "rid-1"


def test_request_log_missing_received_at_passes_none(app_module):
    pool = _FakePool()
    _run(app_module._persist_request_log(pool, {"request_id": "rid-2", "status": 401}))
    sql, args = pool.conn.executed[0]
    assert (
        args[0] is None
    ), "missing received_at must bind as None (SQL falls back to now())"
    # COALESCE(..., now()) provides the default.
    assert "COALESCE" in sql


def test_request_log_extra_fields_go_to_detail(app_module):
    pool = _FakePool()
    evt = {
        "request_id": "rid-3",
        "status": 200,
        "custom_field": "kept-in-detail",
    }
    _run(app_module._persist_request_log(pool, evt))
    _, args = pool.conn.executed[0]
    # detail is the last arg (jsonb string); the unknown key must be inside it.
    assert "custom_field" in args[-1]
    assert "kept-in-detail" in args[-1]

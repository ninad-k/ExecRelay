"""Tests for the /me/telegram endpoints. Auth is bypassed via a dependency
override; DB via the same stub-pool pattern as test_auth.py."""

from __future__ import annotations

import importlib.util
import sys
import uuid
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
    sys.modules["app"] = module
    spec.loader.exec_module(module)
    return module


class _StubPool:
    def __init__(self):
        self.fetchrow_result = None
        self.executed: list[tuple] = []

    async def fetchrow(self, *_a, **_kw):
        return self.fetchrow_result

    async def fetch(self, *_a, **_kw):
        return []

    async def fetchval(self, *_a, **_kw):
        return None

    async def execute(self, *args, **_kw):
        self.executed.append(args)
        return "DELETE 1"


_USER = {"id": uuid.uuid4(), "email": "trader@example.com"}


@pytest.fixture
def pool():
    return _StubPool()


@pytest.fixture
def client(app_module, pool):
    app_module.app.dependency_overrides[app_module.get_pool] = lambda: pool
    app_module.app.dependency_overrides[app_module.current_user] = lambda: _USER
    try:
        yield TestClient(app_module.app)
    finally:
        app_module.app.dependency_overrides.clear()


def test_link_returns_503_when_bot_not_configured(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_USERNAME", "")
    resp = client.post("/me/telegram/link")
    assert resp.status_code == 503


def test_link_returns_deep_link(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_USERNAME", "ExecRelayTestBot")
    resp = client.post("/me/telegram/link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deep_link"] == (
        f"https://t.me/ExecRelayTestBot?start={body['link_token']}"
    )
    # Telegram start payloads must be [A-Za-z0-9_-] and <= 64 chars.
    assert len(body["link_token"]) <= 64
    assert all(c.isalnum() or c in "-_" for c in body["link_token"])


def test_status_unlinked(client):
    resp = client.get("/me/telegram")
    assert resp.status_code == 200
    assert resp.json()["linked"] is False


def test_status_linked_includes_delivery_health(client, pool):
    from datetime import datetime, timezone

    pool.fetchrow_result = {
        "chat_id": 5512345689,
        "linked_at": datetime(2026, 7, 24, tzinfo=timezone.utc),
        "notify_fills": True,
        "notify_timeouts": False,
        "failed_last_24h": 3,
        "last_delivery_status": "failed",
    }
    resp = client.get("/me/telegram")
    assert resp.status_code == 200
    body = resp.json()
    assert body["linked"] is True
    assert body["chat_id"] == "5512345689"
    assert body["failed_last_24h"] == 3
    assert body["last_delivery_status"] == "failed"


def test_patch_prefs_404_when_no_link(client):
    resp = client.patch("/me/telegram", json={"notify_fills": False})
    assert resp.status_code == 404


def test_delete_link_204(client, pool):
    resp = client.delete("/me/telegram")
    assert resp.status_code == 204
    assert any("DELETE FROM telegram_links" in str(args[0]) for args in pool.executed)

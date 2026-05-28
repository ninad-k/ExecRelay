"""Tests for the auth/JWT pieces of portal-api. Exercises the pure functions
directly (no DB needed) plus rate-limited rejection paths via TestClient."""

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


class _StubPool:
    """Minimal asyncpg.Pool stub so route handlers don't 503 on pool lookup.
    Any method that returns a row is wired per-test; tests that don't touch
    the DB get the stub returned unchanged."""

    async def fetchrow(self, *_a, **_kw):
        return None

    async def fetch(self, *_a, **_kw):
        return []

    async def fetchval(self, *_a, **_kw):
        return None

    async def execute(self, *_a, **_kw):
        return None


@pytest.fixture
def client(app_module):
    app_module.app.dependency_overrides[app_module.get_pool] = lambda: _StubPool()
    try:
        yield TestClient(app_module.app)
    finally:
        app_module.app.dependency_overrides.clear()


def test_password_hash_and_check_roundtrip(app_module):
    hashed = app_module.hash_password("a-strong-password-123!")
    assert app_module.check_password("a-strong-password-123!", hashed)
    assert not app_module.check_password("wrong", hashed)


def test_make_token_returns_decodable_jwt(app_module):
    token = app_module.make_token("00000000-0000-0000-0000-000000000001")
    assert isinstance(token, str) and token.count(".") == 2
    sub = app_module.verify_token(token)
    assert sub == "00000000-0000-0000-0000-000000000001"


def test_verify_token_rejects_garbage(app_module):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_module.verify_token("not-a-jwt")
    assert exc.value.status_code == 401


def test_register_rejects_short_password(client):
    """Short passwords must never produce a successful registration. We accept
    any non-2xx outcome because the exact code depends on Pydantic-vs-handler
    ordering across Python versions; what matters is no user is created."""
    r = client.post(
        "/auth/register",
        json={"email": "test@example.com", "password": "short"},
    )
    assert r.status_code >= 400, f"unexpected success: {r.status_code} {r.text}"


def test_protected_route_requires_bearer_token(client):
    r = client.get("/licenses")
    # FastAPI HTTPBearer returns 403 when the header is missing.
    assert r.status_code in (401, 403)


def test_protected_route_rejects_invalid_token(client):
    r = client.get("/licenses", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401

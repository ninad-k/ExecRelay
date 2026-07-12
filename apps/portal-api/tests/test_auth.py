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
    # Register in sys.modules BEFORE exec so that `from __future__ import
    # annotations` forward refs (e.g. Pydantic model field types) resolve
    # against the module namespace when schemas are built.
    sys.modules["app"] = module
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


def test_register_rejects_sanctioned_country(client):
    """A registrant declaring an OFAC-sanctioned country is blocked with 451
    before any DB access. Case-insensitive and whitespace-tolerant."""
    for country in ("IR", "ir", " KP ", "Cu", "SY"):
        r = client.post(
            "/auth/register",
            json={
                "email": "blocked@example.com",
                "password": "a-sufficiently-long-password",
                "country": country,
            },
        )
        assert (
            r.status_code == 451
        ), f"country {country!r} should be blocked: {r.status_code} {r.text}"


def test_protected_route_requires_bearer_token(client):
    r = client.get("/licenses")
    # FastAPI HTTPBearer returns 403 when the header is missing.
    assert r.status_code in (401, 403)


def test_protected_route_rejects_invalid_token(client):
    r = client.get("/licenses", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_user_export_success(app_module, client):
    # Retrieve user token
    token = app_module.make_token("00000000-0000-0000-0000-000000000001")

    # Mock pool.fetchrow and pool.fetch
    import uuid
    from datetime import datetime, timezone

    class MockPool(_StubPool):
        async def fetchrow(self, query, *args):
            if "users WHERE id =" in query:
                return {
                    "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    "email": "test@example.com",
                    "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            return None

        async def fetch(self, query, *args):
            if "user_roles" in query:
                return [{"name": "user"}]
            elif "portfolio_exposure_limits" in query:
                return []
            elif "licenses WHERE user_id =" in query:
                return [
                    {
                        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
                        "license_key": "LIC123",
                        "active": True,
                        "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
                    }
                ]
            elif "instances" in query:
                return [
                    {
                        "id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
                        "license_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
                        "instance_key": "INST1",
                        "platform": "mt5",
                        "active": True,
                        "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
                    }
                ]
            elif "admin_audit_log" in query:
                return [
                    {
                        "id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
                        "action": "create_license",
                        "reason": "initial",
                        "before_state": None,
                        "after_state": '{"active": true}',
                        "created_at": datetime(2026, 1, 4, tzinfo=timezone.utc),
                    }
                ]
            elif "report_subscriptions" in query:
                return []
            return []

    app_module.app.dependency_overrides[app_module.get_pool] = lambda: MockPool()

    try:
        r = client.get("/user/export", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["profile"]["email"] == "test@example.com"
        assert data["roles"] == ["user"]
        assert len(data["licenses"]) == 1
        assert data["licenses"][0]["license_key"] == "LIC123"
        assert len(data["instances"]) == 1
        assert data["instances"][0]["instance_key"] == "INST1"
        assert len(data["audit_logs"]) == 1
        assert data["audit_logs"][0]["action"] == "create_license"
        assert data["audit_logs"][0]["after_state"] == {"active": True}
    finally:
        app_module.app.dependency_overrides.clear()

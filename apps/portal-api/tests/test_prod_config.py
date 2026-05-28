"""Tests for prod-config fail-fast behavior. Reloading app.py with ENV=production
and dev defaults must raise SystemExit so a misconfigured deploy can't ship."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


APP_PATH = Path(__file__).resolve().parent.parent / "app.py"


def _load_app(env: dict[str, str]):
    saved = dict(os.environ)
    sys.modules.pop("app", None)
    try:
        os.environ.clear()
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location("app", APP_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)
        sys.modules.pop("app", None)


def test_prod_refuses_dev_db_default():
    with pytest.raises(SystemExit) as exc:
        _load_app({"ENV": "production"})
    assert exc.value.code == 2


def test_prod_refuses_wildcard_origins():
    with pytest.raises(SystemExit):
        _load_app({
            "ENV": "production",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "NATS_URL": "nats://x:y@nats:4222",
            "JWT_SECRET": "x" * 64,
            "PORTAL_ALLOWED_ORIGINS": "*",
        })


def test_prod_refuses_short_jwt():
    with pytest.raises(SystemExit):
        _load_app({
            "ENV": "production",
            "DATABASE_URL": "postgresql://u:p@db:5432/x",
            "NATS_URL": "nats://x:y@nats:4222",
            "JWT_SECRET": "tooshort",
            "PORTAL_ALLOWED_ORIGINS": "https://example.com",
        })


def test_prod_accepts_complete_config():
    module = _load_app({
        "ENV": "production",
        "DATABASE_URL": "postgresql://u:p@db:5432/x",
        "NATS_URL": "nats://x:y@nats:4222",
        "JWT_SECRET": "x" * 64,
        "PORTAL_ALLOWED_ORIGINS": "https://app.example.com,https://staging.example.com",
    })
    assert module.IS_PROD is True
    assert module.ALLOWED_ORIGINS == [
        "https://app.example.com",
        "https://staging.example.com",
    ]


def test_dev_keeps_lax_defaults():
    module = _load_app({"ENV": "development"})
    assert module.IS_PROD is False
    assert module.ALLOWED_ORIGINS == ["*"]

"""Tests for the strict protobuf parser. Catches the case where a malformed
payload would have been silently truncated by the old lax parser."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_module():
    """Load app.py with a fresh prometheus registry so multiple test modules
    can each import it without colliding on the global default registry."""
    sys.modules.pop("app", None)
    # Reset prometheus default registry — app.py registers Counter/Histogram
    # at import time and Counter() refuses to re-register on the same names.
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


def _str_field(num: int, value: str) -> bytes:
    """Encode a length-delimited string field (wire type 2)."""
    raw = value.encode("utf-8")
    out = bytearray()
    tag = (num << 3) | 2
    while tag >= 0x80:
        out.append((tag & 0x7F) | 0x80)
        tag >>= 7
    out.append(tag)
    length = len(raw)
    while length >= 0x80:
        out.append((length & 0x7F) | 0x80)
        length >>= 7
    out.append(length)
    out.extend(raw)
    return bytes(out)


def _well_formed_signal() -> bytes:
    return (
        _str_field(1, "trace-1234")
        + _str_field(2, "L-100")
        + _str_field(3, "I-1")
        + _str_field(4, "buy")
        + _str_field(6, "EURUSD")
    )


def test_parses_minimum_required_fields(app_module):
    sig = app_module.parse_signal(_well_formed_signal())
    assert sig["trace_id"] == "trace-1234"
    assert sig["license_id"] == "L-100"
    assert sig["command"] == "buy"
    assert sig["symbol"] == "EURUSD"


def test_rejects_empty_payload(app_module):
    with pytest.raises(app_module.ProtoParseError):
        app_module.parse_signal(b"")


def test_rejects_truncated_length(app_module):
    # tag=field 2 wire-type 2, length=10 but only 2 bytes follow
    bad = bytes([0x12, 0x0A, 0x41, 0x42])
    with pytest.raises(app_module.ProtoParseError):
        app_module.parse_signal(bad)


def test_rejects_missing_required_field(app_module):
    # has trace_id and license_id but missing command + symbol
    payload = _str_field(1, "trace-only") + _str_field(2, "L-1")
    with pytest.raises(app_module.ProtoParseError) as exc:
        app_module.parse_signal(payload)
    assert "command" in str(exc.value)


def test_rejects_unknown_wire_type(app_module):
    # tag = field 1 wire-type 5 (32-bit fixed) — we don't accept that
    bad = bytes([0x0D, 0x01, 0x00, 0x00, 0x00])
    with pytest.raises(app_module.ProtoParseError):
        app_module.parse_signal(bad)


def test_rejects_invalid_utf8_string(app_module):
    # Build a field-1 with raw \xff bytes instead of UTF-8 — should error
    payload = bytearray()
    payload.append(0x0A)  # field 1 wire 2
    payload.append(0x02)  # length 2
    payload.extend(b"\xff\xfe")
    with pytest.raises(app_module.ProtoParseError):
        app_module.parse_signal(bytes(payload))

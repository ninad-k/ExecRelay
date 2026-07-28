"""Unit tests for the Telegram notification pieces of the tasks service:
message formatting (pure) and the bot command handler (stubbed pool + send)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest


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


# ---------------------------------------------------------------------------
# format_fill_message
# ---------------------------------------------------------------------------


def test_format_filled_message_full(app_module):
    text = app_module.format_fill_message(
        {
            "status": "filled",
            "command": "buy",
            "symbol": "BTCUSD",
            "broker_order_id": "12345678",
            "trace_id": "abc123",
            "signal_payload": json.dumps(
                {
                    "raw_command": "buy",
                    "params": [
                        {"key": "comment", "value": "AlgoCombo"},
                        {"key": "vol_lots", "value": "0.1"},
                    ],
                }
            ),
            "prob_win": 0.62,
            "action_summary": "OPEN_LONG",
        }
    )
    assert text.splitlines() == [
        "✅ Order filled — BUY BTCUSD",
        "Strategy: AlgoCombo",
        "Volume: 0.1 lots",
        "Order: 12345678",
        "ML: prob_win 0.62 (OPEN_LONG)",
        "Trace: abc123",
    ]


def test_format_timeout_message_omits_error_line(app_module):
    text = app_module.format_fill_message(
        {
            "status": "timeout",
            "command": "sell",
            "symbol": "EURUSD",
            "error_message": "Fill not received within timeout window",
            "trace_id": "t1",
        }
    )
    assert text.startswith("⏱ Fill timeout — SELL EURUSD")
    assert "Error:" not in text


def test_format_error_message_includes_error(app_module):
    text = app_module.format_fill_message(
        {
            "status": "rejected",
            "command": "buy",
            "symbol": "XAUUSD",
            "error_message": "not enough margin",
        }
    )
    assert "❌ Order rejected — BUY XAUUSD" in text
    assert "Error: not enough margin" in text


def test_format_placed_message_for_pending_order(app_module):
    text = app_module.format_fill_message(
        {
            "status": "placed",
            "command": "selllimit",
            "symbol": "XAUUSD",
            "broker_order_id": "555",
            "trace_id": "t9",
        }
    )
    assert text.startswith("📌 Order placed — SELLLIMIT XAUUSD")
    assert "Order: 555" in text


def test_format_cancelled_message(app_module):
    text = app_module.format_fill_message(
        {
            "status": "cancelled",
            "command": "selllimit",
            "symbol": "XAUUSD",
            "error_message": "pending order deleted or expired before activation",
        }
    )
    assert text.startswith("🚫 Order cancelled — SELLLIMIT XAUUSD")
    assert "Error: pending order deleted" in text


def test_format_message_handles_missing_signal(app_module):
    # Fill with no matching accepted_signal row (LEFT JOIN nulls).
    text = app_module.format_fill_message({"status": "filled"})
    assert text == "✅ Order filled"


def test_format_message_tolerates_bad_signal_payload(app_module):
    text = app_module.format_fill_message(
        {"status": "filled", "signal_payload": "{not json"}
    )
    assert text == "✅ Order filled"


# ---------------------------------------------------------------------------
# _handle_telegram_update (stubbed pool + send)
# ---------------------------------------------------------------------------


class _StubPool:
    def __init__(self, fetchrow_result=None, execute_result="UPDATE 0"):
        self.fetchrow_result = fetchrow_result
        self.execute_result = execute_result

    async def fetchrow(self, *_a, **_kw):
        return self.fetchrow_result

    async def execute(self, *_a, **_kw):
        return self.execute_result


def _update(text: str, chat_id: int = 42) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def _run_handler(app_module, monkeypatch, pool, text):
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id, msg):
        sent.append((chat_id, msg))
        return True

    monkeypatch.setattr(app_module, "_tg_send", fake_send)
    asyncio.run(app_module._handle_telegram_update(pool, _update(text)))
    return sent


def test_status_unlinked_chat(app_module, monkeypatch):
    sent = _run_handler(app_module, monkeypatch, _StubPool(), "/status")
    assert len(sent) == 1
    assert "not linked" in sent[0][1]


def test_stop_unlinked_chat(app_module, monkeypatch):
    sent = _run_handler(
        app_module, monkeypatch, _StubPool(execute_result="UPDATE 0"), "/stop"
    )
    assert len(sent) == 1 and "not linked" in sent[0][1]


def test_stop_linked_chat(app_module, monkeypatch):
    sent = _run_handler(
        app_module, monkeypatch, _StubPool(execute_result="UPDATE 1"), "/stop"
    )
    assert len(sent) == 1 and "Unlinked" in sent[0][1]


def test_unknown_command_gets_help(app_module, monkeypatch):
    sent = _run_handler(app_module, monkeypatch, _StubPool(), "hello there")
    assert len(sent) == 1 and "/start" in sent[0][1]


def test_non_text_update_ignored(app_module, monkeypatch):
    async def fail_send(*_a):  # pragma: no cover - must not be called
        raise AssertionError("should not send")

    monkeypatch.setattr(app_module, "_tg_send", fail_send)
    asyncio.run(
        app_module._handle_telegram_update(
            _StubPool(), {"update_id": 2, "message": {"chat": {"id": 1}}}
        )
    )

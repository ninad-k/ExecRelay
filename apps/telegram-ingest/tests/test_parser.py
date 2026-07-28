"""Parser + command-builder tests for telegram-ingest, anchored on the real
channel message format the adapter was built for."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Fixed config for deterministic command output.
_ENV = {
    "TELEGRAM_INGEST_LICENSE_ID": "60123456789",
    "TELEGRAM_INGEST_SECRET": "s3cret",
    "TELEGRAM_INGEST_FIXED_LOT": "0.01",
    "TELEGRAM_INGEST_SYMBOL_MAP": "GOLD=XAUUSD",
    "TELEGRAM_INGEST_ALLOWED_CHAT_IDS": "-1001234567890",
}


@pytest.fixture(scope="module")
def app_module(request):
    import os

    old = {k: os.environ.get(k) for k in _ENV}
    os.environ.update(_ENV)
    sys.modules.pop("app", None)
    app_path = Path(__file__).resolve().parent.parent / "app.py"
    spec = importlib.util.spec_from_file_location("app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["app"] = module
    spec.loader.exec_module(module)

    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    request.addfinalizer(restore)
    return module


GOLD_MESSAGE = """GOLD SELL @ 4099

SECOND SELL LIMIT @ 4109

SL @ 4119
TP @ 4089

Risk Management Example

\U0001f530ACCOUNT < 2000$
\U0001f530FIRST TRADE LOT 0.01
\U0001f530SECOND TRADE LOT 0.01

⚠️ This is not financial advice. Please trade at your own risk."""


def test_parses_the_real_gold_message(app_module):
    sig = app_module.parse_signal(GOLD_MESSAGE)
    assert sig == {
        "symbol": "GOLD",
        "side": "sell",
        "entry": 4099.0,
        "sl": 4119.0,
        "tp": 4089.0,
        "second": {"kind": "limit", "entry": 4109.0},
    }


def test_builds_two_limit_commands_with_fixed_lot_and_mapped_symbol(app_module):
    # Default entry mode is "limit": BOTH legs rest as pending limit orders.
    sig = app_module.parse_signal(GOLD_MESSAGE)
    cmds = app_module.build_commands(sig)
    assert cmds == [
        "60123456789,selllimit,XAUUSD,entry=4099,vol_lots=0.01,sl=4119,tp=4089,comment=tg-ingest,secret=s3cret",
        "60123456789,selllimit,XAUUSD,entry=4109,vol_lots=0.01,sl=4119,tp=4089,comment=tg-ingest,secret=s3cret",
    ]


def test_market_entry_mode(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ENTRY_MODE", "market")
    sig = app_module.parse_signal(GOLD_MESSAGE)
    cmds = app_module.build_commands(sig)
    assert cmds[0].startswith("60123456789,sell,XAUUSD,vol_lots=0.01,")
    assert cmds[1].startswith("60123456789,selllimit,XAUUSD,entry=4109,")


def test_buy_signal_without_second_order(app_module):
    sig = app_module.parse_signal("EURUSD BUY @ 1.0850\nSL @ 1.0800\nTP @ 1.0950")
    assert sig["side"] == "buy" and sig["second"] is None
    cmds = app_module.build_commands(sig)
    assert len(cmds) == 1
    assert cmds[0].startswith("60123456789,buylimit,EURUSD,entry=1.085,")


def test_non_signal_messages_return_none(app_module):
    for text in (
        "Good morning traders! Big news day today.",
        "TP1 hit! +100 pips \U0001f680",
        "GOLD is looking bearish on H4",
    ):
        assert app_module.parse_signal(text) is None


def test_signal_without_sl_tp_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="missing SL or TP"):
        app_module.parse_signal("GOLD SELL @ 4099")


def test_sell_with_inverted_stops_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="sanity failed"):
        app_module.parse_signal("GOLD SELL @ 4099\nSL @ 4089\nTP @ 4119")


def test_second_order_on_wrong_side_is_rejected(app_module):
    # A SELL LIMIT below the first entry makes no sense — whole message rejected.
    with pytest.raises(app_module.SignalError, match="wrong side"):
        app_module.parse_signal(
            "GOLD SELL @ 4099\nSECOND SELL LIMIT @ 4090\nSL @ 4119\nTP @ 4089"
        )


def test_second_order_side_mismatch_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="side differs"):
        app_module.parse_signal(
            "GOLD SELL @ 4099\nSECOND BUY LIMIT @ 4109\nSL @ 4119\nTP @ 4089"
        )


def test_duplicate_messages_are_dropped(app_module):
    key = (-1001234567890, 424242)
    app_module._seen.discard(key)
    if key in app_module._seen_order:
        app_module._seen_order.remove(key)
    assert app_module._mark_seen(key) is True
    assert app_module._mark_seen(key) is False

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
        "tps": [4089.0],
        # The "@" dialect never says which way price reaches the entry, so the
        # order type is left to ENTRY_MODE.
        "order_type": None,
        "second": {"kind": "limit", "entry": 4109.0},
    }


def test_builds_two_limit_commands_with_fixed_lot_and_mapped_symbol(app_module):
    # Default entry mode is "limit": BOTH legs rest as pending limit orders.
    sig = app_module.parse_signal(GOLD_MESSAGE)
    cmds = app_module.build_commands(sig)
    assert cmds == [
        "60123456789,selllimit,XAUUSD,entry_price=4099,vol_lots=0.01,sl=4119,tp=4089,comment=tg-ingest,secret=s3cret",
        "60123456789,selllimit,XAUUSD,entry_price=4109,vol_lots=0.01,sl=4119,tp=4089,comment=tg-ingest,secret=s3cret",
    ]


def test_market_entry_mode(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ENTRY_MODE", "market")
    sig = app_module.parse_signal(GOLD_MESSAGE)
    cmds = app_module.build_commands(sig)
    assert cmds[0].startswith("60123456789,sell,XAUUSD,vol_lots=0.01,")
    assert cmds[1].startswith("60123456789,selllimit,XAUUSD,entry_price=4109,")


def test_buy_signal_without_second_order(app_module):
    sig = app_module.parse_signal("EURUSD BUY @ 1.0850\nSL @ 1.0800\nTP @ 1.0950")
    assert sig["side"] == "buy" and sig["second"] is None
    cmds = app_module.build_commands(sig)
    assert len(cmds) == 1
    assert cmds[0].startswith("60123456789,buylimit,EURUSD,entry_price=1.085,")


def test_non_signal_messages_return_none(app_module):
    for text in (
        "Good morning traders! Big news day today.",
        "TP1 hit! +100 pips \U0001f680",
        "GOLD is looking bearish on H4",
    ):
        assert app_module.parse_signal(text) is None


def test_signal_without_sl_tp_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="missing SL"):
        app_module.parse_signal("GOLD SELL @ 4099")


def test_entry_line_with_unknown_symbol_and_no_sl_tp_is_rejected(app_module):
    # Not a known symbol, so the trigger dialect can't rescue it either.
    with pytest.raises(app_module.SignalError, match="missing SL or TP"):
        app_module.parse_signal("WIDGET SELL @ 4099")


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


# --- "trigger" dialect ------------------------------------------------------
#
# Verbatim posts from the channel TeleTrader was pointed at, emoji and all.

SELL_TRIGGER_MESSAGE = """XAUUSD Sell Trigger only Below 4792 \U0001f4c9

\U0001f6d1 SL 4808 ⚠️

\U0001f3af Target 4790 4788 4784  4770+ \U0001f3af\U0001f4c9

\U0001f30d FOREX & CRYPTO MARKET \U0001f310\U0001f4ca

\U0001f680 Vantage New (Partner Code: drdvantage): https://vigco.co/la-com/drdvantage"""

BUY_TRIGGER_MESSAGE = """XAUUSD Buy Trigger only Above 4824 \U0001f4c8

SL 4808 \U0001f6d1

Target 4826 4828 4832 4845+ \U0001f3af"""


def test_parses_sell_trigger_ladder(app_module):
    sig = app_module.parse_signal(SELL_TRIGGER_MESSAGE)
    assert sig == {
        "symbol": "XAUUSD",
        "side": "sell",
        "entry": 4792.0,
        "sl": 4808.0,
        "tp": 4790.0,
        "tps": [4790.0, 4788.0, 4784.0, 4770.0],
        # "Sell ... Below" = price breaks down into the entry = sell stop.
        "order_type": "sellstop",
        "second": None,
    }


def test_parses_buy_trigger_ladder(app_module):
    sig = app_module.parse_signal(BUY_TRIGGER_MESSAGE)
    assert sig["order_type"] == "buystop"
    assert sig["entry"] == 4824.0 and sig["sl"] == 4808.0
    assert sig["tps"] == [4826.0, 4828.0, 4832.0, 4845.0]


def test_trigger_order_type_beats_entry_mode(app_module, monkeypatch):
    # ENTRY_MODE only covers signals that don't state their own direction —
    # placing a sell LIMIT at 4792 here would be plain wrong.
    monkeypatch.setattr(app_module, "ENTRY_MODE", "market")
    cmds = app_module.build_commands(app_module.parse_signal(SELL_TRIGGER_MESSAGE))
    assert cmds == [
        "60123456789,sellstop,XAUUSD,entry_price=4792,vol_lots=0.01,sl=4808,tp=4790,comment=tg-ingest,secret=s3cret"
    ]


def test_tp_mode_last_and_ladder(app_module, monkeypatch):
    sig = app_module.parse_signal(SELL_TRIGGER_MESSAGE)

    monkeypatch.setattr(app_module, "TP_MODE", "last")
    assert app_module.build_commands(sig) == [
        "60123456789,sellstop,XAUUSD,entry_price=4792,vol_lots=0.01,sl=4808,tp=4770,comment=tg-ingest,secret=s3cret"
    ]

    monkeypatch.setattr(app_module, "TP_MODE", "ladder")
    cmds = app_module.build_commands(sig)
    assert [c.split("tp=")[1].split(",")[0] for c in cmds] == ["4790", "4788", "4784", "4770"]


def test_labeled_tp_and_colon_stop_loss(app_module):
    sig = app_module.parse_signal("GOLD Sell Below 2345\nSL: 2360\nTP1: 2340 TP2: 2330 TP3: 2310")
    assert sig["order_type"] == "sellstop"
    assert sig["sl"] == 2360.0 and sig["tps"] == [2340.0, 2330.0, 2310.0]
    # GOLD is aliased to XAUUSD even without an operator symbol map entry.
    assert app_module.build_commands(sig)[0].startswith("60123456789,sellstop,XAUUSD,")


def test_symbol_alias_applies_without_operator_map(app_module):
    assert app_module.resolve_symbol("SILVER") == "XAGUSD"
    assert app_module.resolve_symbol("GOLD") == "XAUUSD"  # operator map, same result
    assert app_module.resolve_symbol("EURUSD") == "EURUSD"


def test_trigger_target_on_wrong_side_is_rejected(app_module):
    # A stray number swept out of the promo block lands on the losing side.
    with pytest.raises(app_module.SignalError, match="wrong side of entry"):
        app_module.parse_signal("XAUUSD Sell Below 4792\nSL 4808\nTarget 4790 4788 4900")


def test_trigger_without_targets_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="missing TP/target"):
        app_module.parse_signal("XAUUSD Sell Trigger only Below 4792\nSL 4808")


def test_trigger_with_inverted_stop_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="sanity failed"):
        app_module.parse_signal("XAUUSD Sell Below 4792\nSL 4780\nTarget 4790")


def test_unknown_symbol_trigger_is_not_a_signal(app_module):
    assert app_module.parse_signal("WIDGET Buy Above 100\nSL 90\nTarget 110") is None


# --- "entry range" dialect --------------------------------------------------
#
# The channel quotes a zone instead of a price and puts each target on its own
# line. Both ends of the zone are traded; the nearest target is the one taken.

RANGE_BUY_MESSAGE = """Gold buy limit 4342 - 4339
SL 4336.00
Tp 4347
Tp 4355
Tp 4390
Tp Open"""

RANGE_SELL_MESSAGE = """Gold sell limit 4360 - 4363
SL 4370.00
Tp 4355
Tp 4348
Tp 4320
Tp Open"""


def test_parses_entry_range_buy(app_module):
    sig = app_module.parse_signal(RANGE_BUY_MESSAGE)
    assert sig == {
        "symbol": "GOLD",
        "side": "buy",
        "entry": 4342.0,
        "sl": 4336.0,
        "tp": 4347.0,
        "tps": [4347.0, 4355.0, 4390.0],
        # "limit" is stated, so ENTRY_MODE gets no say.
        "order_type": "buylimit",
        "second": {"kind": "limit", "entry": 4339.0},
    }


def test_entry_range_buy_places_both_legs_at_the_nearest_target(app_module):
    cmds = app_module.build_commands(app_module.parse_signal(RANGE_BUY_MESSAGE))
    assert cmds == [
        "60123456789,buylimit,XAUUSD,entry_price=4342,vol_lots=0.01,sl=4336,tp=4347,comment=tg-ingest,secret=s3cret",
        "60123456789,buylimit,XAUUSD,entry_price=4339,vol_lots=0.01,sl=4336,tp=4347,comment=tg-ingest,secret=s3cret",
    ]


def test_entry_range_sell_mirrors_the_buy(app_module):
    cmds = app_module.build_commands(app_module.parse_signal(RANGE_SELL_MESSAGE))
    assert cmds == [
        "60123456789,selllimit,XAUUSD,entry_price=4360,vol_lots=0.01,sl=4370,tp=4355,comment=tg-ingest,secret=s3cret",
        "60123456789,selllimit,XAUUSD,entry_price=4363,vol_lots=0.01,sl=4370,tp=4355,comment=tg-ingest,secret=s3cret",
    ]


def test_entry_range_quote_order_does_not_matter(app_module):
    # "4339 - 4342" is the same zone written the other way round; price still
    # reaches 4342 first on a buy limit, so the same two orders go out.
    flipped = RANGE_BUY_MESSAGE.replace("4342 - 4339", "4339 - 4342")
    assert app_module.build_commands(
        app_module.parse_signal(flipped)
    ) == app_module.build_commands(app_module.parse_signal(RANGE_BUY_MESSAGE))


def test_entry_range_stop_leads_with_the_other_end(app_module):
    # A buy STOP zone is entered from below, so the LOWER price leads.
    sig = app_module.parse_signal("Gold buy stop 4342 - 4339\nSL 4330\nTp 4350\nTp 4360")
    assert sig["order_type"] == "buystop"
    assert sig["entry"] == 4339.0 and sig["second"] == {"kind": "stop", "entry": 4342.0}


def test_entry_range_without_a_kind_keyword_defers_to_entry_mode(app_module):
    sig = app_module.parse_signal("Gold buy 4342 - 4339\nSL 4336\nTp 4347\nTp 4355")
    # Nothing said limit or stop, so ENTRY_MODE decides the leading leg.
    assert sig["order_type"] is None
    assert sig["second"] == {"kind": "limit", "entry": 4339.0}


def test_bare_tp_lines_keep_all_their_digits(app_module):
    # Regression: the "TP1"-label pattern used to eat the 4 of "Tp 4347" and
    # trade a take-profit of 347.
    assert app_module._extract_targets("Tp 4347\nTp 4355\nTp Open") == [4347.0, 4355.0]
    # A single bare TP line goes down the other extraction path — same rule.
    assert app_module._extract_targets("Tp 4347\nTp Open") == [4347.0]
    # Labelled ladders are unchanged.
    assert app_module._extract_targets("TP1: 4790 TP2: 4788") == [4790.0, 4788.0]


def test_entry_range_tp_mode_last_takes_the_furthest_target(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "TP_MODE", "last")
    cmds = app_module.build_commands(app_module.parse_signal(RANGE_BUY_MESSAGE))
    assert all(",tp=4390," in c for c in cmds)


def test_entry_range_targets_are_ordered_by_distance_not_by_typing(app_module):
    # Channel lists the ladder out of order; "nearest" must still win.
    sig = app_module.parse_signal("Gold buy limit 4342 - 4339\nSL 4336\nTp 4390\nTp 4347\nTp 4355")
    assert sig["tps"] == [4347.0, 4355.0, 4390.0]


def test_entry_range_with_inverted_stop_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="sanity failed"):
        app_module.parse_signal("Gold buy limit 4342 - 4339\nSL 4350\nTp 4360")


def test_entry_range_stop_inside_the_zone_is_rejected(app_module):
    # 4340 is above the far leg: that leg would open already stopped out.
    with pytest.raises(app_module.SignalError, match="sanity failed"):
        app_module.parse_signal("Gold buy limit 4342 - 4339\nSL 4340\nTp 4360")


def test_entry_range_without_sl_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="missing SL"):
        app_module.parse_signal("Gold buy limit 4342 - 4339\nTp 4347")


def test_entry_range_without_targets_is_rejected(app_module):
    with pytest.raises(app_module.SignalError, match="missing TP/target"):
        app_module.parse_signal("Gold buy limit 4342 - 4339\nSL 4336\nTp Open")


def test_entry_range_with_unknown_symbol_is_not_a_signal(app_module):
    assert app_module.parse_signal("WIDGET buy limit 4342 - 4339\nSL 4336\nTp 4347") is None


# --- caption lines above the signal -----------------------------------------
#
# Channels number their posts ("SIGNAL 4 🔽") on a line of their own before the
# entry line. The symbol has to lead A line, not THE first line, or every one
# of these posts is dropped as "not a signal".

CAPTIONED_SELL_MESSAGE = """SIGNAL 4\U0001f53d
Gold sell limit 4369 - 4372
SL 4375.99
Tp 4364
Tp 4355
Tp 4343


⚠️ Disclaimer:
This is a reference trading plan shared for educational purposes only.
Please conduct your own analysis and apply proper risk management.
Trade smart not emotional."""


def test_caption_line_above_the_entry_does_not_hide_the_signal(app_module):
    sig = app_module.parse_signal(CAPTIONED_SELL_MESSAGE)
    assert sig == {
        "symbol": "GOLD",
        "side": "sell",
        "entry": 4369.0,
        "sl": 4375.99,
        "tp": 4364.0,
        "tps": [4364.0, 4355.0, 4343.0],
        "order_type": "selllimit",
        "second": {"kind": "limit", "entry": 4372.0},
    }


def test_captioned_signal_is_identical_to_the_bare_one(app_module):
    bare = CAPTIONED_SELL_MESSAGE.split("\n", 1)[1]
    assert app_module.parse_signal(CAPTIONED_SELL_MESSAGE) == app_module.parse_signal(bare)


def test_symbol_named_only_mid_sentence_is_still_not_a_signal(app_module):
    # "Gold" leads no line here, so the recap stays out of the trading path even
    # though a range, an SL and a target are all present in the prose.
    recap = (
        "AZZAM TRADE RECAP\n"
        "Today we traded gold buy limit 4342 - 4339 and it ran.\n"
        "The SL 4336 was never touched and Tp 4347 printed.\n"
    )
    assert app_module.parse_signal(recap) is None


# --- the channel tag carried in the trade comment ---------------------------
#
# MT5's comment field is the only place the originating channel is visible on
# the broker side, and ea_shim scopes amendments by matching it -- so a relayed
# signal must carry "tg-<initials>", never the generic fallback.


def test_relayed_signal_is_tagged_with_the_channel_initials(app_module):
    relayed = "[SRC:AZZAM - Gold Trading Pro]\n" + CAPTIONED_SELL_MESSAGE
    channel_name, text = app_module.strip_source_tag(relayed)
    assert channel_name == "AZZAM - Gold Trading Pro"

    comment = f"tg-{app_module.channel_initials(channel_name)}"
    assert comment == "tg-AGTP"
    cmds = app_module.build_commands(app_module.parse_signal(text), comment=comment)
    assert cmds == [
        "60123456789,selllimit,XAUUSD,entry_price=4369,vol_lots=0.01,sl=4375.99,tp=4364,comment=tg-AGTP,secret=s3cret",
        "60123456789,selllimit,XAUUSD,entry_price=4372,vol_lots=0.01,sl=4375.99,tp=4364,comment=tg-AGTP,secret=s3cret",
    ]


def test_untagged_message_falls_back_to_the_generic_comment(app_module):
    # A signal pasted straight into the bot DM names no channel, so there are
    # no initials to derive and TELEGRAM_INGEST_COMMENT is all there is.
    channel_name, text = app_module.strip_source_tag(CAPTIONED_SELL_MESSAGE)
    assert channel_name is None
    cmds = app_module.build_commands(app_module.parse_signal(text), comment=None)
    assert all(f",comment={app_module.COMMENT}," in c for c in cmds)


def test_tp_amendment_is_not_read_as_an_entry_range(app_module):
    # The follow-up amendment path owns these; parse_signal must stay out.
    assert app_module.parse_signal("Tp set @ 4346 for both trade") is None
    assert app_module.parse_tp_update("Tp set @ 4346 for both trade") == 4346.0


def test_duplicate_messages_are_dropped(app_module):
    key = (-1001234567890, 424242)
    app_module._seen.discard(key)
    if key in app_module._seen_order:
        app_module._seen_order.remove(key)
    assert app_module._mark_seen(key) is True
    assert app_module._mark_seen(key) is False

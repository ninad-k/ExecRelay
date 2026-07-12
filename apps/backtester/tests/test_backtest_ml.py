"""Tests for backtest_ml.py: CSV loading, Option-1 baseline bookkeeping, and
the filtered-vs-unfiltered comparison engine.

`run_comparison`'s real feature computation needs ~300 bars of warm-up
(see feature_builder.py), which would make a hand-verified PnL scenario
enormous. So the comparison-engine tests here monkeypatch `FeatureBuilder`
with a stand-in that always returns a (dummy) complete feature dict --
isolating the position/PnL bookkeeping under test from feature-warm-up
concerns -- and use a scripted stub predictor (mirroring the
`_StubPredictor` pattern in apps/ml-predictor/tests/test_app_http.py) so the
filtered branch's decisions are fully controlled and the resulting PnL/win
rate/drawdown numbers can be checked against a hand worked-out trade log.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backtest_ml
from backtest_ml import (
    BranchState,
    RawSignal,
    _baseline_decision,
    _max_drawdown,
    load_candles,
    load_signals,
    run_comparison,
)
from feature_builder import Candle


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _candle(i: int, o: float) -> Candle:
    """A bar whose OHLC all equal `o` except a tiny high/low wick, so it's
    never mistaken for a doji and division-by-zero never bites."""
    return Candle(timestamp=_ts(i), open=o, high=o + 0.01, low=o - 0.01, close=o)


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------


def test_load_candles_roundtrip(tmp_path: Path):
    csv_path = tmp_path / "candles.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerow(["2026-01-05T00:05:00+00:00", "10", "11", "9", "10.5", "100"])
        w.writerow(["2026-01-05T00:00:00+00:00", "9", "10", "8", "9.5", "50"])

    candles = load_candles(csv_path)
    # Sorted ascending by timestamp even though the file wasn't.
    assert len(candles) == 2
    assert candles[0].timestamp < candles[1].timestamp
    assert candles[0].close == pytest.approx(9.5)
    assert candles[1].volume == pytest.approx(100.0)


def test_load_signals_accepts_buy_sell_and_numeric_direction(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "action"])
        w.writerow(["2026-01-05T00:00:00+00:00", "buy"])
        w.writerow(["2026-01-05T00:05:00+00:00", "sell"])
        w.writerow(["2026-01-05T00:10:00+00:00", "-1"])

    signals = load_signals(csv_path)
    assert [s.direction for s in signals] == [1, -1, -1]


# ---------------------------------------------------------------------------
# Option-1 baseline decision (no ML)
# ---------------------------------------------------------------------------


def test_baseline_opens_when_flat():
    d = _baseline_decision("LONG", None)
    assert d == {"should_close": False, "should_open": True, "open_direction": "LONG"}


def test_baseline_ignores_same_direction_pyramid():
    d = _baseline_decision("LONG", "LONG")
    assert d["should_open"] is False
    assert d["should_close"] is False


def test_baseline_flips_on_opposite_signal():
    d = _baseline_decision("SHORT", "LONG")
    assert d["should_close"] is True
    assert d["should_open"] is True
    assert d["open_direction"] == "SHORT"


# ---------------------------------------------------------------------------
# max drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_hand_computed():
    # cumulative: 0.10, 0.05 (dd=0.05), 0.20 (new peak), 0.02 (dd=0.18)
    pnls = [0.10, -0.05, 0.15, -0.18]
    assert _max_drawdown(pnls) == pytest.approx(0.18)


def test_max_drawdown_all_gains_is_zero():
    assert _max_drawdown([0.01, 0.02, 0.03]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# BranchState.apply skip-accounting (the CLOSE_ONLY-vs-filter-block fix)
# ---------------------------------------------------------------------------


def test_apply_counts_pyramid_skip_not_filter_skip():
    state = BranchState(current_position="LONG")
    # Same-direction signal, filter would technically also "pass" (irrelevant
    # -- Option 1 never pyramids regardless of the filter).
    state.apply(
        "LONG",
        should_close=False,
        should_open=False,
        open_direction=None,
        blocked_by_filter=False,
        index=1,
        ts=_ts(1),
        fill_price=100.0,
    )
    assert state.skipped_pyramid == 1
    assert state.skipped_filter == 0


def test_apply_counts_close_only_as_filter_skip():
    # Regression test: a CLOSE_ONLY decision (should_close=True,
    # should_open=False) where the filter blocked the reopen leg of what
    # would otherwise have been a FLIP must still count as a filter skip.
    trade = backtest_ml.Trade(
        direction="LONG", entry_index=0, entry_time=_ts(0), entry_price=100.0
    )
    state = BranchState(current_position="LONG", open_trade=trade, trades=[trade])
    state.apply(
        "SHORT",
        should_close=True,
        should_open=False,
        open_direction=None,
        blocked_by_filter=True,
        index=2,
        ts=_ts(2),
        fill_price=90.0,
    )
    assert state.closed == 1
    assert state.skipped_filter == 1
    assert state.skipped_pyramid == 0


# ---------------------------------------------------------------------------
# run_comparison: full hand-worked scenario
# ---------------------------------------------------------------------------


class _ScriptedPredictor:
    """Returns pre-scripted decisions in call order, ignoring the actual
    feature payload -- lets the test dictate exactly what the filtered
    branch does at each signal, independent of the real model's behavior."""

    def __init__(self, decisions: list[dict], threshold: float = 0.50):
        self._decisions = list(decisions)
        self.threshold = threshold

    def predict(self, payload, current_position=None):
        return self._decisions.pop(0)


def _decision(should_close, should_open, open_direction) -> dict:
    return {
        "should_close": should_close,
        "should_open": should_open,
        "open_direction": open_direction,
        "prob_win": None,
        "action_summary": "N/A",
    }


def test_run_comparison_hand_worked_scenario(monkeypatch):
    # 5 bars; opens are the entry/exit fill prices (bar following the signal).
    opens = [100, 110, 121, 90, 130]
    candles = [_candle(i, o) for i, o in enumerate(opens)]

    signals = [
        RawSignal(timestamp=_ts(0), direction=1),  # buy
        RawSignal(timestamp=_ts(1), direction=1),  # buy again (pyramid, ignored)
        RawSignal(timestamp=_ts(2), direction=-1),  # sell (closes LONG)
        RawSignal(timestamp=_ts(3), direction=1),  # buy (closes SHORT if opened)
    ]

    # Bypass the real 300-bar feature warm-up: every signal gets a complete
    # (dummy) feature dict, so the comparison purely exercises position/PnL
    # bookkeeping, which is what this test is checking.
    class _FakeFeatureBuilder:
        def __init__(self, candles):
            pass

        def build_at(self, i):
            return {"dummy": 0.0}

    monkeypatch.setattr(backtest_ml, "FeatureBuilder", _FakeFeatureBuilder)

    # Filtered branch script:
    #   sig0 (flat, buy)          -> OPEN_LONG @110
    #   sig1 (LONG, buy)          -> baseline pyramid-skips before the
    #                                predictor is even consulted... but the
    #                                harness always scores + calls predict()
    #                                for every non-skipped-feature signal, so
    #                                script a NOTHING that the real predictor
    #                                would also produce for a same-direction
    #                                signal.
    #   sig2 (LONG, sell)         -> CLOSE_ONLY: filter blocks the SHORT reopen
    #   sig3 (flat, buy)          -> OPEN_LONG @130
    scripted = _ScriptedPredictor(
        [
            _decision(False, True, "LONG"),
            _decision(False, False, None),
            _decision(True, False, None),
            _decision(False, True, "LONG"),
        ]
    )

    report = run_comparison(candles, signals, scripted)

    # ---- unfiltered baseline (always takes every non-pyramid signal) ----
    # sig0: open LONG @110
    # sig1: pyramid, ignored
    # sig2: FLIP -> close LONG(110->90) pnl=(90-110)/110, open SHORT @90
    # sig3: FLIP -> close SHORT(90->130) pnl=-(130-90)/90, open LONG @130 (still open)
    pnl1 = (90 - 110) / 110
    pnl2 = -(130 - 90) / 90
    u = report.unfiltered
    assert u["trades_opened"] == 3
    assert u["trades_closed"] == 2
    assert u["trades_still_open"] == 1
    assert u["trades_skipped_pyramid"] == 1
    assert u["trades_skipped_by_filter"] == 0
    assert u["cumulative_pnl_pct"] == pytest.approx(pnl1 + pnl2)
    assert u["win_rate"] == pytest.approx(0.0)  # both closed trades lost
    assert u["max_drawdown_pct"] == pytest.approx(-(pnl1 + pnl2))  # both legs negative

    # ---- filtered branch (per script) ----
    # sig0: OPEN_LONG @110
    # sig1: NOTHING, same-direction -> pyramid skip
    # sig2: CLOSE_ONLY -> close LONG(110->90) pnl=(90-110)/110, filter-skip the reopen
    # sig3: flat, buy -> OPEN_LONG @130 (still open)
    f = report.filtered
    assert f["trades_opened"] == 2
    assert f["trades_closed"] == 1
    assert f["trades_still_open"] == 1
    assert f["trades_skipped_pyramid"] == 1
    assert f["trades_skipped_by_filter"] == 1
    assert f["cumulative_pnl_pct"] == pytest.approx(pnl1)
    assert f["win_rate"] == pytest.approx(0.0)

    assert report.uplift_pct == pytest.approx(pnl1 - (pnl1 + pnl2))
    assert report.scored_signals == 4
    assert report.skipped_incomplete_features == 0
    assert report.skipped_missing_candle == 0


def test_run_comparison_skips_signals_with_no_matching_candle(monkeypatch):
    candles = [_candle(i, 100 + i) for i in range(3)]

    class _FakeFeatureBuilder:
        def __init__(self, candles):
            pass

        def build_at(self, i):
            return {"dummy": 0.0}

    monkeypatch.setattr(backtest_ml, "FeatureBuilder", _FakeFeatureBuilder)

    signals = [RawSignal(timestamp=_ts(99), direction=1)]  # no such candle
    report = run_comparison(
        candles, signals, _ScriptedPredictor([_decision(False, True, "LONG")])
    )
    assert report.skipped_missing_candle == 1
    assert report.scored_signals == 0

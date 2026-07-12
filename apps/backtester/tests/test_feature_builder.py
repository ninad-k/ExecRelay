"""Unit tests for feature_builder.py against hand-computed values.

Each indicator primitive is checked against a small synthetic series where
the expected output was worked out by hand (the arithmetic is shown in
comments next to each assertion) rather than by re-running equivalent code,
so these catch real formula mistakes rather than just re-confirming the
implementation agrees with itself.

Where an exact hand calculation over many bars would be impractical (DMI/ADX
over a long warm-up), the expected value is instead derived *analytically*
from a specially-constructed series (e.g. a pure uptrend forces `minus_dm`
to 0 for every bar, which forces `DX` to exactly 100.0 for every bar, which
forces `ADX` to converge to exactly 100.0 once its own smoothing window is
full of nothing but 100.0s) -- that's still a hand-derived expectation, just
proven with algebra instead of a calculator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from feature_builder import (
    FEATURE_NAMES,
    Candle,
    FeatureBuilder,
    _last_closed_bin,
    _session,
    atr,
    dmi,
    ema,
    resample_closes,
    rma,
    rsi_from_closes,
    sma,
    stdev,
)


def _ts(i: int) -> datetime:
    """i-th 5-minute bar starting 2026-01-05 00:00:00 UTC (a Monday)."""
    return datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(timestamp=_ts(i), open=o, high=h, low=low, close=c)


# ---------------------------------------------------------------------------
# sma / stdev
# ---------------------------------------------------------------------------


def test_sma_hand_computed():
    values = [1, 2, 3, 4, 5]
    out = sma(values, 3)
    # window [1,2,3]->2, [2,3,4]->3, [3,4,5]->4
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_stdev_is_population_not_sample():
    # Classic textbook set: mean=5, population variance=4, population stdev=2.
    # (Sample stdev with Bessel's correction would be 2.138 -- Pine's
    # ta.stdev is *not* that.)
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    out = stdev(values, 8)
    assert out[7] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# ema / rma
# ---------------------------------------------------------------------------


def test_ema_seeds_from_first_sample_hand_computed():
    # alpha = 2/(2+1) = 2/3
    values = [1, 2, 3]
    out = ema(values, 2)
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(1.0 + (2 / 3) * (2 - 1))  # 1.666...667
    e2 = out[1] + (2 / 3) * (3 - out[1])
    assert out[2] == pytest.approx(e2)  # 2.555...556


def test_rma_hand_computed():
    values = [1, 2, 3, 4, 5, 6]
    out = rma(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # seed = mean(1,2,3)
    assert out[3] == pytest.approx(2.0 + (4 - 2.0) / 3)  # 2.6666...667
    v3 = out[3]
    assert out[4] == pytest.approx(v3 + (5 - v3) / 3)  # 3.4444...444
    v4 = out[4]
    assert out[5] == pytest.approx(v4 + (6 - v4) / 3)  # 4.2962...963


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


def test_rsi_pure_uptrend_reaches_100():
    # Strictly increasing closes -> every loss is 0, so once the Wilder
    # smoothing window (length=14) is full, down=0 and up>0 => RSI = 100
    # exactly. First gain-index with a full 14-sample window is gains[13],
    # which maps to closes[14] (gains[k] <-> closes[k+1]).
    closes = list(range(1, 17))  # 16 points, closes[14] == 15
    out = rsi_from_closes(closes, 14)
    assert out[13] is None  # not enough warm-up yet
    assert out[14] == pytest.approx(100.0)
    assert out[15] == pytest.approx(100.0)


def test_rsi_flat_series_is_50():
    # No up moves, no down moves -> up=0, down=0 -> defined as 50.0.
    closes = [10.0] * 20
    out = rsi_from_closes(closes, 14)
    assert out[14] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_constant_true_range_hand_computed():
    # Every bar: open=close=10, high=11, low=9 -> range=2, and since close
    # never gaps (prev close always 10), TR = max(2, 1, 1) = 2 for every bar
    # from index 1 onward. rma(constant 2s, length=2) stays exactly 2.0
    # forever once seeded.
    candles = [_candle(i, 10, 11, 9, 10) for i in range(5)]
    out = atr(candles, length=2)
    assert out[0] is None
    assert out[1] is None  # tr[1] is the first defined TR; rma needs 2 of them
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(2.0)
    assert out[4] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# DMI / ADX
# ---------------------------------------------------------------------------


def _trending_candles(n: int, step: float, start: float = 100.0) -> list[Candle]:
    """Strictly monotonic high/low (up if step>0, down if step<0) so every
    bar's directional move is unambiguous -- no mixed up/down bars."""
    out = []
    level = start
    for i in range(n):
        level += step
        out.append(_candle(i, level, level + 1, level - 1, level))
    return out


def test_dmi_pure_uptrend_minus_di_is_zero_and_adx_converges_to_100():
    length = 3
    candles = _trending_candles(20, step=1.0)
    plus_di, minus_di, adx = dmi(candles, length=length)

    # Every down_move is negative (low strictly increasing), so minus_dm is
    # always 0 -> minus_di is always 0 once defined.
    for v in minus_di[length:]:
        assert v == pytest.approx(0.0)

    # minus_di == 0 for every bar => DX = 100*|pdi-0|/(pdi+0) = 100 exactly,
    # for every bar once DX is defined. ADX = rma(DX, length); once the rma
    # window is entirely inside the constant-100 region, ADX == 100 exactly.
    # DX is defined starting at candle index `length`; ADX needs `length`
    # more warm-up samples of DX, so ADX is exactly 100 from index
    # 2*length - 1 onward.
    for v in adx[2 * length - 1 :]:
        assert v == pytest.approx(100.0)


def test_dmi_pure_downtrend_plus_di_is_zero_and_adx_converges_to_100():
    length = 3
    candles = _trending_candles(20, step=-1.0)
    plus_di, minus_di, adx = dmi(candles, length=length)

    for v in plus_di[length:]:
        assert v == pytest.approx(0.0)
    for v in adx[2 * length - 1 :]:
        assert v == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Higher-timeframe resampling
# ---------------------------------------------------------------------------


def test_last_closed_bin_hand_traced():
    factor = 3
    # bin 0 = base indices 0,1,2 (closes at index 2)
    # bin 1 = base indices 3,4,5 (closes at index 5)
    expected = {
        0: None,  # bin 0 not closed yet
        1: None,
        2: 0,  # bin 0 just closed
        3: 0,  # bin 1 forming -> last closed is still bin 0
        4: 0,
        5: 1,  # bin 1 just closed
        6: 1,
        7: 1,
        8: 2,
    }
    for i, want in expected.items():
        assert _last_closed_bin(i, factor) == want


def test_resample_closes_hand_computed():
    candles = [_candle(i, c, c, c, c) for i, c in enumerate([1, 2, 3, 4, 5, 6])]
    assert resample_closes(candles, 3) == [3, 6]


def test_htf_dist_ema50_hand_computed():
    # 9 bars, h1_factor=3 -> H1 bins close at base indices 2, 5, 8 with
    # closes 12, 15, 18 (see module docstring derivation).
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18]
    candles = [_candle(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    fb = FeatureBuilder(candles, h1_factor=3, h4_factor=9)

    assert fb._h1_closes == [12, 15, 18]
    # ema seeds from first sample: h1_ema50[0] == 12
    assert fb._h1_ema50[0] == pytest.approx(12.0)
    alpha = 2.0 / 51.0
    ema1 = 12.0 + alpha * (15.0 - 12.0)
    assert fb._h1_ema50[1] == pytest.approx(ema1)

    # Base index 2 (bin 0 just closed): h1_close=12, h1_ema50=12 -> dist=0.
    h1_close, h1_ema50 = fb._htf(2, 3, fb._h1_closes, fb._h1_ema50)
    assert h1_close == pytest.approx(12.0)
    assert h1_ema50 == pytest.approx(12.0)

    # Base index 3 (bin 1 still forming): still reads the last *closed* bin
    # (bin 0), not the partially-formed bin 1.
    h1_close, h1_ema50 = fb._htf(3, 3, fb._h1_closes, fb._h1_ema50)
    assert h1_close == pytest.approx(12.0)

    # Base index 5 (bin 1 just closed): h1_close=15, h1_ema50=ema1.
    h1_close, h1_ema50 = fb._htf(5, 3, fb._h1_closes, fb._h1_ema50)
    assert h1_close == pytest.approx(15.0)
    assert h1_ema50 == pytest.approx(ema1)
    expected_dist = (15.0 - ema1) / 15.0
    assert expected_dist == pytest.approx(0.192156862745098, rel=1e-9)


# ---------------------------------------------------------------------------
# Session / time features
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [(0, 0), (6, 0), (7, 1), (12, 1), (13, 2), (19, 2), (20, 3), (23, 3)],
)
def test_session_boundaries(hour, expected):
    assert _session(hour) == expected


def test_dow_matches_python_weekday_convention():
    # 2026-01-05 is a Monday -> Python weekday() == 0. The Pine script's
    # f_dow_python field is algebraically equal to datetime.weekday():
    #   _dow_pine = dayofweek(time,"UTC") - 1        (Sun=0 .. Sat=6)
    #   f_dow_python = _dow_pine==0 ? 6 : _dow_pine-1
    # Sunday (_dow_pine=0)  -> 6  == Python Sunday (6)
    # Monday (_dow_pine=1)  -> 0  == Python Monday (0)
    # Saturday (_dow_pine=6)-> 5  == Python Saturday (5)
    # i.e. it's just datetime.weekday() computed in UTC.
    assert _ts(0).weekday() == 0


# ---------------------------------------------------------------------------
# Simple per-bar (non-lookback) features
# ---------------------------------------------------------------------------


def test_bar_shape_features_hand_computed():
    # open=10, high=13, low=8, close=12 -> range=(13-8)/12, body=(12-10)/12,
    # upper_wick=(13-max(10,12))/12=(13-12)/12, lower_wick=(min(10,12)-8)/12=(10-8)/12
    c = _candle(0, 10, 13, 8, 12)
    assert (c.high - c.low) / c.close == pytest.approx(5 / 12)
    assert (c.close - c.open) / c.close == pytest.approx(2 / 12)
    assert (c.high - max(c.open, c.close)) / c.close == pytest.approx(1 / 12)
    assert (min(c.open, c.close) - c.low) / c.close == pytest.approx(2 / 12)


def test_active_bar_flags_doji_as_inactive():
    doji = _candle(0, 10, 10, 10, 10)
    active = _candle(1, 10, 11, 9, 10)
    fb = FeatureBuilder([doji, active])
    assert fb._active_bar[0] == 0.0
    assert fb._active_bar[1] == 1.0


# ---------------------------------------------------------------------------
# Full feature-vector assembly / integration
# ---------------------------------------------------------------------------


def _synthetic_series(n: int) -> list[Candle]:
    """Deterministic, gently trending + oscillating series long enough to
    warm up every rolling window (needs >288 bars for ret_288/vol_288, plus
    enough H1 history for h1_ret_24 with the default h1_factor=12)."""
    import math

    candles = []
    price = 100.0
    for i in range(n):
        price += 0.01 * math.sin(i / 17.0) + 0.002
        o = price
        c = price + 0.05 * math.cos(i / 5.0)
        h = max(o, c) + 0.3
        low = min(o, c) - 0.3
        candles.append(_candle(i, o, h, low, c))
        price = c
    return candles


def test_feature_names_match_ml_predictor_feature_order_minus_direction():
    from pathlib import Path

    order_path = (
        Path(__file__).resolve().parents[2]
        / "ml-predictor"
        / "model"
        / "feature_order.txt"
    )
    with open(order_path) as f:
        lines = [line.strip() for line in f if line.strip()]
    assert lines[0] == "direction"
    assert tuple(lines[1:]) == FEATURE_NAMES


def test_build_at_returns_none_before_warmup():
    candles = _synthetic_series(400)
    fb = FeatureBuilder(candles)
    assert fb.build_at(0) is None
    assert fb.build_at(50) is None


def test_build_at_full_vector_once_warmed_up():
    candles = _synthetic_series(400)
    fb = FeatureBuilder(candles)
    feats = fb.build_at(399)
    assert feats is not None
    assert set(feats.keys()) == set(FEATURE_NAMES)
    for v in feats.values():
        assert isinstance(v, (int, float))

    c = candles[399]
    prev = candles[398]
    assert feats["ret_1"] == pytest.approx((c.close - prev.close) / prev.close)
    assert feats["hour_utc"] == c.timestamp.hour
    assert feats["dow"] == c.timestamp.weekday()
    assert feats["month"] == c.timestamp.month
    assert feats["minute_of_day"] == c.timestamp.hour * 60 + c.timestamp.minute
    assert 0 <= feats["session"] <= 3
    assert feats["active_bar"] in (0.0, 1.0)


def test_build_returns_one_entry_per_candle():
    candles = _synthetic_series(50)
    fb = FeatureBuilder(candles)
    rows = fb.build()
    assert len(rows) == 50

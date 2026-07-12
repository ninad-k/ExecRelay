"""Reconstructs the 35 XGBoost model features from historical OHLCV candles.

The live path computes these features on TradingView (see
``pine/Combo_Webhook_Pine.pine``) and ships them in the webhook payload's
``features`` object. The backtester has no TradingView runtime, so to score
historical signals with the same model it must recompute the identical
feature set from a plain OHLCV candle series.

Every formula here is a direct Python port of the corresponding ``f_*``
assignment in ``pine/Combo_Webhook_Pine.pine``. Where Pine's built-ins have a
specific (and sometimes non-obvious) definition, that definition is called
out in a comment:

  - ``ta.ema`` seeds from the *first* sample (``ema[0] = src[0]``) -- no
    warm-up delay, unlike an SMA-seeded EMA.
  - ``ta.rma`` (Wilder's smoothing, used by ATR/RSI/DMI) seeds with a plain
    SMA over the first ``length`` samples and is undefined before that.
  - ``ta.stdev`` is the *population* standard deviation (divide by N, not
    N-1).
  - ``request.security(..., barmerge.lookahead_off)`` on a higher timeframe
    never returns the still-forming HTF bar's live value for a historical
    bar strictly inside it -- it returns the last *closed* HTF bar. That is
    reproduced here by ``_last_closed_bin``.
  - The Pine script's ``dow`` field already converts TradingView's
    ``dayofweek()`` (Sunday=1..Saturday=7) to Python's weekday convention
    (Monday=0..Sunday=6); the conversion is verified algebraically in the
    module docstring of the test file, so this module just calls
    ``datetime.weekday()`` directly.

Assumes 5-minute base candles (so the 288-bar lookback windows in the model
== 24h, and H1/H4 are 12x/48x the base bar), matching the Pine script's own
"288 bars/day" cadence. If your candle feed uses a different base timeframe,
override ``h1_factor``/``h4_factor`` on `FeatureBuilder` accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

# Order matches apps/ml-predictor/model/feature_order.txt minus the injected
# `direction` column (direction comes from the signal, not the candles).
FEATURE_NAMES: tuple[str, ...] = (
    "ret_1",
    "ret_3",
    "ret_12",
    "ret_36",
    "ret_72",
    "ret_288",
    "range_pct",
    "body_pct",
    "upper_wick",
    "lower_wick",
    "atr_pct",
    "vol_72",
    "vol_288",
    "dist_ema_9",
    "dist_ema_21",
    "dist_ema_50",
    "ema_50_slope",
    "dist_ema_200",
    "ema_200_slope",
    "rsi_14",
    "plus_di",
    "minus_di",
    "adx_14",
    "bb_z",
    "bb_width",
    "active_bar",
    "active_rate_288",
    "h1_dist_ema50",
    "h1_ret_24",
    "h4_dist_ema50",
    "hour_utc",
    "dow",
    "month",
    "minute_of_day",
    "session",
)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # timezone-aware, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ---------------------------------------------------------------------------
# Indicator primitives (pure Python, no pandas/numpy -- these are simple
# recursive filters and keeping them dependency-free makes the hand-computed
# unit tests easy to follow).
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], length: int) -> list[float | None]:
    """Simple moving average. `None` for indices before `length-1`."""
    out: list[float | None] = [None] * len(values)
    window_sum = 0.0
    for i, v in enumerate(values):
        window_sum += v
        if i >= length:
            window_sum -= values[i - length]
        if i >= length - 1:
            out[i] = window_sum / length
    return out


def stdev(values: Sequence[float], length: int) -> list[float | None]:
    """Population standard deviation (Pine `ta.stdev` default), rolling window."""
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i >= length - 1:
            window = values[i - length + 1 : i + 1]
            mean = sum(window) / length
            var = sum((x - mean) ** 2 for x in window) / length
            out[i] = var**0.5
    return out


def ema(values: Sequence[float], length: int) -> list[float]:
    """Exponential moving average, Pine-style: seeded from the first sample
    (`ema[0] = src[0]`), so it is defined for every index -- no warm-up."""
    alpha = 2.0 / (length + 1)
    out: list[float] = [0.0] * len(values)
    if not values:
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rma(values: Sequence[float], length: int) -> list[float | None]:
    """Wilder's smoothing, Pine-style: `na` until `length` samples have
    accumulated, then seeded with a plain SMA of the first `length` values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = prev + (values[i] - prev) / length
        out[i] = prev
    return out


def rsi_from_closes(closes: Sequence[float], length: int = 14) -> list[float | None]:
    """RSI via Wilder smoothing of gains/losses (Pine `ta.rsi`)."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < 2:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    up = rma(gains, length)
    down = rma(losses, length)
    for i in range(1, n):
        u, d = up[i - 1], down[i - 1]  # gains/losses are 1-indexed vs closes
        if u is None or d is None:
            continue
        if d == 0:
            out[i] = 100.0 if u > 0 else 50.0
        else:
            rs = u / d
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def true_range(candles: Sequence[Candle]) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        out[i] = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
    return out


def _align_from_first_defined(values, length, fn):
    """Run a rolling-window function (`rma` or `stdev`) over the maximal
    contiguous defined suffix of `values` (skipping a leading run of
    `None`), then map the result back onto the original index space.

    Bookkeeping every warm-up offset by hand (e.g. "+1 for the previous-close
    shift, +1 more for the smoothing pass") is exactly the kind of thing that
    is easy to get subtly wrong once two `None`-producing stages compose
    (DM/TR needing a previous bar, then DX needing its own RMA warm-up on top
    of that). This finds where real data starts and lets `fn` do its own
    indexing from a clean, `None`-free array instead.
    """
    n = len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    out: list[float | None] = [None] * n
    if start is None:
        return out
    clean = list(values[start:])
    if any(v is None for v in clean):
        raise ValueError("values must be contiguous once defined")
    computed = fn(clean, length)
    for offset, v in enumerate(computed):
        out[start + offset] = v
    return out


def atr(candles: Sequence[Candle], length: int = 14) -> list[float | None]:
    tr = true_range(candles)
    return _align_from_first_defined(tr, length, rma)


def dmi(
    candles: Sequence[Candle], length: int = 14
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Wilder's DMI/ADX: returns (plus_di, minus_di, adx), all indexed like
    `candles` (i.e. `None` until enough warm-up bars have accumulated)."""
    n = len(candles)
    plus_dm: list[float | None] = [None] * n
    minus_dm: list[float | None] = [None] * n
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    tr = true_range(candles)
    tr_rma = _align_from_first_defined(tr, length, rma)
    plus_dm_rma = _align_from_first_defined(plus_dm, length, rma)
    minus_dm_rma = _align_from_first_defined(minus_dm, length, rma)

    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx: list[float | None] = [None] * n
    for i in range(n):
        if tr_rma[i] is None or tr_rma[i] == 0:
            continue
        if plus_dm_rma[i] is None or minus_dm_rma[i] is None:
            continue
        pdi = 100.0 * plus_dm_rma[i] / tr_rma[i]
        mdi = 100.0 * minus_dm_rma[i] / tr_rma[i]
        plus_di[i] = pdi
        minus_di[i] = mdi
        denom = pdi + mdi
        dx[i] = 0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom

    adx = _align_from_first_defined(dx, length, rma)
    return plus_di, minus_di, adx


# ---------------------------------------------------------------------------
# Higher-timeframe (H1/H4) resampling, emulating request.security(...,
# barmerge.lookahead_off) -- never exposes the still-forming HTF bar to a
# base bar strictly inside it.
# ---------------------------------------------------------------------------


def _last_closed_bin(i: int, factor: int) -> int | None:
    """Index (0-based, in units of `factor`-bar bins) of the last HTF bin
    that has fully closed as of base-bar `i`, or `None` if none has yet."""
    bin_idx = i // factor
    bin_closed = (i % factor) == factor - 1
    closed_bin = bin_idx if bin_closed else bin_idx - 1
    return closed_bin if closed_bin >= 0 else None


def resample_closes(candles: Sequence[Candle], factor: int) -> list[float]:
    """Close price of each fully-formed `factor`-bar HTF bin, in chrono order."""
    closes = []
    for start in range(0, len(candles) - factor + 1, factor):
        closes.append(candles[start + factor - 1].close)
    return closes


# ---------------------------------------------------------------------------
# Feature assembly
# ---------------------------------------------------------------------------

_SESSION_BOUNDARIES = (7, 13, 20)  # hour_utc < 7 -> 0, <13 -> 1, <20 -> 2, else 3


def _session(hour_utc: int) -> int:
    for idx, bound in enumerate(_SESSION_BOUNDARIES):
        if hour_utc < bound:
            return idx
    return len(_SESSION_BOUNDARIES)


class FeatureBuilder:
    """Precomputes every rolling indicator once over a full candle series,
    then serves per-bar feature dicts in O(1).

    Usage::

        fb = FeatureBuilder(candles)
        feats = fb.build_at(i)   # dict of 35 features, or None if not enough
                                  # history yet (mirrors Pine's `featuresOk`
                                  # NaN guard on the alert condition).
    """

    def __init__(
        self,
        candles: Sequence[Candle],
        h1_factor: int = 12,
        h4_factor: int = 48,
    ):
        self.candles = list(candles)
        self.h1_factor = h1_factor
        self.h4_factor = h4_factor
        n = len(self.candles)
        closes = [c.close for c in self.candles]

        self._closes = closes
        self._ema9 = ema(closes, 9)
        self._ema21 = ema(closes, 21)
        self._ema50 = ema(closes, 50)
        self._ema200 = ema(closes, 200)
        self._atr14 = atr(self.candles, 14)
        self._rsi14 = rsi_from_closes(closes, 14)
        self._plus_di, self._minus_di, self._adx14 = dmi(self.candles, 14)
        self._sma20 = sma(closes, 20)
        self._stdev20 = stdev(closes, 20)

        ret1 = [None] + [
            (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, n)
        ]
        self._ret1 = ret1
        # ret1[0] is undefined (no previous close); _align_from_first_defined
        # skips that leading None so the 72/288-window stdevs never include
        # a faked value.
        self._vol72 = _align_from_first_defined(ret1, 72, stdev)
        self._vol288 = _align_from_first_defined(ret1, 288, stdev)

        active_bar = [1.0 if c.high != c.low else 0.0 for c in self.candles]
        self._active_bar = active_bar
        self._active_rate288 = sma(active_bar, 288)

        self._h1_closes = resample_closes(self.candles, h1_factor)
        self._h1_ema50 = ema(self._h1_closes, 50)
        self._h4_closes = resample_closes(self.candles, h4_factor)
        self._h4_ema50 = ema(self._h4_closes, 50)

    def __len__(self) -> int:
        return len(self.candles)

    def _htf(self, i: int, factor: int, closes: list[float], ema50: list[float]):
        closed_bin = _last_closed_bin(i, factor)
        if closed_bin is None or closed_bin >= len(closes):
            return None, None
        return closes[closed_bin], ema50[closed_bin]

    def build_at(self, i: int) -> dict[str, float] | None:
        """Feature dict for candle index `i`, or `None` if any required
        lookback isn't available yet (mirrors Pine's `featuresOk` guard)."""
        n = len(self.candles)
        if i < 0 or i >= n:
            raise IndexError(i)
        c = self.candles[i]

        def ret(lag: int) -> float | None:
            if i < lag:
                return None
            prev = self._closes[i - lag]
            return (c.close - prev) / prev if prev else None

        ret_1 = ret(1)
        ret_3 = ret(3)
        ret_12 = ret(12)
        ret_36 = ret(36)
        ret_72 = ret(72)
        ret_288 = ret(288)

        range_pct = (c.high - c.low) / c.close if c.close else None
        body_pct = (c.close - c.open) / c.close if c.close else None
        upper_wick = (c.high - max(c.open, c.close)) / c.close if c.close else None
        lower_wick = (min(c.open, c.close) - c.low) / c.close if c.close else None

        atr14 = self._atr14[i]
        atr_pct = atr14 / c.close if (atr14 is not None and c.close) else None

        vol_72 = self._vol72[i]
        vol_288 = self._vol288[i]

        ema9v, ema21v, ema50v, ema200v = (
            self._ema9[i],
            self._ema21[i],
            self._ema50[i],
            self._ema200[i],
        )
        dist_ema_9 = (c.close - ema9v) / c.close if c.close else None
        dist_ema_21 = (c.close - ema21v) / c.close if c.close else None
        dist_ema_50 = (c.close - ema50v) / c.close if c.close else None
        dist_ema_200 = (c.close - ema200v) / c.close if c.close else None

        ema_50_slope = None
        if i >= 12 and ema50v:
            ema_50_slope = (ema50v - self._ema50[i - 12]) / ema50v
        ema_200_slope = None
        if i >= 48 and ema200v:
            ema_200_slope = (ema200v - self._ema200[i - 48]) / ema200v

        rsi_14 = self._rsi14[i]
        plus_di = self._plus_di[i]
        minus_di = self._minus_di[i]
        adx_14 = self._adx14[i]

        bb_basis = self._sma20[i]
        bb_sd = self._stdev20[i]
        bb_z = None
        bb_width = None
        if bb_basis is not None and bb_sd is not None:
            bb_z = (c.close - bb_basis) / bb_sd if bb_sd != 0 else None
            bb_width = (4 * bb_sd) / bb_basis if bb_basis != 0 else None

        active_bar = self._active_bar[i]
        active_rate_288 = self._active_rate288[i]

        h1_close, h1_ema50 = self._htf(
            i, self.h1_factor, self._h1_closes, self._h1_ema50
        )
        h1_dist_ema50 = (
            (h1_close - h1_ema50) / h1_close if h1_close not in (None, 0) else None
        )
        h1_closed_bin = _last_closed_bin(i, self.h1_factor)
        h1_ret_24 = None
        if h1_closed_bin is not None and h1_closed_bin >= 24:
            prev = self._h1_closes[h1_closed_bin - 24]
            h1_ret_24 = (h1_close - prev) / prev if prev else None

        h4_close, h4_ema50 = self._htf(
            i, self.h4_factor, self._h4_closes, self._h4_ema50
        )
        h4_dist_ema50 = (
            (h4_close - h4_ema50) / h4_close if h4_close not in (None, 0) else None
        )

        ts = c.timestamp
        hour_utc = ts.hour
        dow = ts.weekday()  # Pine's f_dow_python is algebraically datetime.weekday()
        month = ts.month
        minute_of_day = hour_utc * 60 + ts.minute
        session = _session(hour_utc)

        feats: dict[str, float | None] = {
            "ret_1": ret_1,
            "ret_3": ret_3,
            "ret_12": ret_12,
            "ret_36": ret_36,
            "ret_72": ret_72,
            "ret_288": ret_288,
            "range_pct": range_pct,
            "body_pct": body_pct,
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "atr_pct": atr_pct,
            "vol_72": vol_72,
            "vol_288": vol_288,
            "dist_ema_9": dist_ema_9,
            "dist_ema_21": dist_ema_21,
            "dist_ema_50": dist_ema_50,
            "ema_50_slope": ema_50_slope,
            "dist_ema_200": dist_ema_200,
            "ema_200_slope": ema_200_slope,
            "rsi_14": rsi_14,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "adx_14": adx_14,
            "bb_z": bb_z,
            "bb_width": bb_width,
            "active_bar": active_bar,
            "active_rate_288": active_rate_288,
            "h1_dist_ema50": h1_dist_ema50,
            "h1_ret_24": h1_ret_24,
            "h4_dist_ema50": h4_dist_ema50,
            "hour_utc": hour_utc,
            "dow": dow,
            "month": month,
            "minute_of_day": minute_of_day,
            "session": session,
        }

        if any(v is None for v in feats.values()):
            return None
        return feats  # type: ignore[return-value]

    def build(self) -> list[dict[str, float] | None]:
        return [self.build_at(i) for i in range(len(self.candles))]

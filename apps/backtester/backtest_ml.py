"""ML-filter backtest harness: quantifies the XGBoost filter's PnL uplift.

Why this exists
----------------
The live path (``apps/ml-predictor``) scores every candidate entry signal
against a trained XGBoost model and applies "Option 1" open/close/flip
guidance (see ``xgb_predictor.py``). To know whether that filter is actually
worth anything, you need to replay a historical signal stream twice -- once
unfiltered, once through the model -- and compare PnL, win rate, and
drawdown side by side.

What this module needs that the live ``apps/backtester`` service doesn't have
--------------------------------------------------------------------------
``apps/backtester/app.py`` replays ``accepted_signals`` + ``fills`` from
Postgres, but neither table stores OHLCV candle history or the 35 Pine
features, and there is no candles table anywhere in the schema (see
``docs/data-model.md``). The only place features could ride along today is
inside a signal's raw JSONB ``payload`` if it arrived via the *proposed but
unimplemented* ``POST /webhook/ml`` path (ADR 0008 -- there is no
``/webhook/ml`` handler in ``apps/ingress`` yet, and it isn't wired through
`docker-compose.yml`` either). So there is currently no reliable path to
either "recompute features from stored candles" or "read features already
attached to a stored signal."

This harness is therefore a **standalone CLI tool**: give it a CSV of OHLCV
candles and a CSV of signals (timestamp + direction), and it recomputes the
35 features locally (``feature_builder.py``, a Python port of the Pine
formulas), scores each signal with the real shipped XGBoost artifact
(``xgb_predictor.py``, a packaging-driven copy of the live predictor), and
replays the same Option-1 position bookkeeping used in production -- once
with the filter applied, once without -- to produce a side-by-side
comparison report.

What is NOT wired here (and what full DB integration would still need)
------------------------------------------------------------------------
- ``apps/backtester/app.py``'s ``/backtest`` endpoint is untouched: it still
  does the simplified ``accepted_signals`` + ``fills`` replay it always did.
  Wiring this harness into that endpoint would need (a) a candles table (or
  an external OHLCV source) keyed by symbol/timestamp, and (b) `/webhook/ml`
  actually landing so the DB path carries features. Neither exists yet.
- Trade simulation here is a simplified stand-in, not the real
  fills/execution model: entry fills at the *next* candle's open after a
  signal (to avoid lookahead), exits happen at the next opposite signal's
  following-candle open (or end-of-data), and PnL is a percentage return,
  not a lot-sized dollar P&L with spread/slippage/fees. It's a fair,
  identical-methodology comparison between filtered and unfiltered, not a
  broker-accurate simulator.

CLI usage
---------
    python backtest_ml.py --candles candles.csv --signals signals.csv \\
        [--threshold 0.50] [--model-path PATH] [--feature-order-path PATH] \\
        [--output report.json]

``candles.csv`` columns: ``timestamp,open,high,low,close[,volume]``
(ISO-8601 UTC timestamps, ascending, evenly spaced 5-minute bars -- see
``feature_builder.py`` for why 5-minute bars matter to the 288-bar lookback
windows).

``signals.csv`` columns: ``timestamp,action`` where ``action`` is
``buy``/``sell`` (or ``1``/``-1``). Each signal's timestamp must exactly
match a candle timestamp (the bar the signal closed on).
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from feature_builder import Candle, FeatureBuilder
from xgb_predictor import XGBPredictor

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.strip())
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts


def load_candles(path: str | Path) -> list[Candle]:
    candles: list[Candle] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(
                Candle(
                    timestamp=_parse_ts(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0.0) or 0.0),
                )
            )
    candles.sort(key=lambda c: c.timestamp)
    return candles


@dataclass(frozen=True)
class RawSignal:
    timestamp: datetime
    direction: int  # 1 = buy/long, -1 = sell/short


_ACTION_DIRECTION = {"buy": 1, "long": 1, "sell": -1, "short": -1}


def load_signals(path: str | Path) -> list[RawSignal]:
    signals: list[RawSignal] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            action = row["action"].strip().lower()
            if action in _ACTION_DIRECTION:
                direction = _ACTION_DIRECTION[action]
            else:
                direction = int(action)
            signals.append(
                RawSignal(timestamp=_parse_ts(row["timestamp"]), direction=direction)
            )
    signals.sort(key=lambda s: s.timestamp)
    return signals


# ---------------------------------------------------------------------------
# Option-1 position bookkeeping shared by both branches
# ---------------------------------------------------------------------------


@dataclass
class Trade:
    direction: str  # "LONG" / "SHORT"
    entry_index: int
    entry_time: datetime
    entry_price: float
    exit_index: int | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def pnl_pct(self) -> float | None:
        if self.exit_price is None:
            return None
        sign = 1.0 if self.direction == "LONG" else -1.0
        return sign * (self.exit_price - self.entry_price) / self.entry_price


def _baseline_decision(sig_dir: str, current_position: str | None) -> dict:
    """Same Option-1 open/close bookkeeping as `XGBPredictor.predict`, but
    with the ML filter always passing -- i.e. "take every entry signal"."""
    should_close = (current_position == "LONG" and sig_dir == "SHORT") or (
        current_position == "SHORT" and sig_dir == "LONG"
    )
    will_be_flat = current_position is None or should_close
    same_direction = current_position == sig_dir
    should_open = (not same_direction) and will_be_flat
    return {
        "should_close": should_close,
        "should_open": should_open,
        "open_direction": sig_dir if should_open else None,
    }


@dataclass
class BranchState:
    """Tracks one branch's (filtered or unfiltered) running position + trades."""

    current_position: str | None = None
    open_trade: Trade | None = None
    trades: list[Trade] = field(default_factory=list)
    signals_seen: int = 0
    opened: int = 0
    closed: int = 0
    skipped_pyramid: int = 0  # same-direction signal while already in position
    skipped_filter: int = 0  # ML filter blocked the open (filtered branch only)

    def apply(
        self,
        sig_dir: str,
        should_close: bool,
        should_open: bool,
        open_direction: str | None,
        blocked_by_filter: bool,
        index: int,
        ts: datetime,
        fill_price: float | None,
    ) -> None:
        self.signals_seen += 1
        pre_position = self.current_position  # snapshot before any mutation below

        if should_close and self.open_trade is not None:
            self.open_trade.exit_index = index
            self.open_trade.exit_time = ts
            self.open_trade.exit_price = fill_price
            self.closed += 1
            self.open_trade = None
            self.current_position = None

        if should_open and open_direction is not None:
            self.open_trade = Trade(
                direction=open_direction,
                entry_index=index,
                entry_time=ts,
                entry_price=fill_price,
            )
            self.trades.append(self.open_trade)
            self.current_position = open_direction
            self.opened += 1
        elif not should_close and pre_position == sig_dir:
            # Already in a position in the signal's own direction -- Option 1
            # never pyramids, so this signal is ignored regardless of filter.
            self.skipped_pyramid += 1
        elif blocked_by_filter:
            # Covers both "flat, filter blocked the open" (NOTHING) and
            # "closed on the opposite signal, filter blocked the reopen leg"
            # (CLOSE_ONLY instead of a FLIP) -- both are opens the ML gate
            # prevented that the unfiltered baseline would have taken.
            self.skipped_filter += 1


# ---------------------------------------------------------------------------
# Stats / report
# ---------------------------------------------------------------------------


def _max_drawdown(pnl_series: Sequence[float]) -> float:
    """Max drawdown of the cumulative-PnL equity curve (same units as the
    PnL values themselves -- fractional returns here, so e.g. 0.05 == 5%)."""
    peak = 0.0
    cumulative = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return max_dd


def _branch_stats(state: BranchState) -> dict:
    closed_trades = [t for t in state.trades if not t.is_open]
    pnls = [t.pnl_pct() for t in closed_trades]
    wins = [p for p in pnls if p is not None and p > 0]
    win_rate = (len(wins) / len(pnls)) if pnls else None
    cumulative_pnl = sum(p for p in pnls if p is not None)
    return {
        "signals_seen": state.signals_seen,
        "trades_opened": state.opened,
        "trades_closed": state.closed,
        "trades_still_open": sum(1 for t in state.trades if t.is_open),
        "trades_skipped_pyramid": state.skipped_pyramid,
        "trades_skipped_by_filter": state.skipped_filter,
        "win_rate": win_rate,
        "cumulative_pnl_pct": cumulative_pnl,
        "max_drawdown_pct": _max_drawdown([p for p in pnls if p is not None]),
    }


@dataclass
class ComparisonReport:
    threshold: float
    total_candidate_signals: int
    scored_signals: int
    skipped_missing_candle: int
    skipped_incomplete_features: int
    unfiltered: dict
    filtered: dict
    uplift_pct: float

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "total_candidate_signals": self.total_candidate_signals,
            "scored_signals": self.scored_signals,
            "skipped_missing_candle": self.skipped_missing_candle,
            "skipped_incomplete_features": self.skipped_incomplete_features,
            "unfiltered": self.unfiltered,
            "filtered": self.filtered,
            "uplift_pct": self.uplift_pct,
        }


# ---------------------------------------------------------------------------
# Core comparison run
# ---------------------------------------------------------------------------


def run_comparison(
    candles: Sequence[Candle],
    signals: Sequence[RawSignal],
    predictor: XGBPredictor,
) -> ComparisonReport:
    """Replay `signals` over `candles` twice (filtered vs unfiltered) using
    identical Option-1 position bookkeeping, differing only in whether the
    ML filter gates new opens. Returns a side-by-side comparison."""
    ts_index = {c.timestamp: i for i, c in enumerate(candles)}
    fb = FeatureBuilder(candles)

    unfiltered = BranchState()
    filtered = BranchState()

    scored = 0
    skipped_missing_candle = 0
    skipped_incomplete_features = 0

    for sig in signals:
        idx = ts_index.get(sig.timestamp)
        if idx is None:
            skipped_missing_candle += 1
            continue

        sig_dir = "LONG" if sig.direction == 1 else "SHORT"
        fill_index = idx + 1  # enter/exit at the *next* bar's open -- no lookahead
        fill_price = candles[fill_index].open if fill_index < len(candles) else None
        fill_ts = (
            candles[fill_index].timestamp
            if fill_index < len(candles)
            else sig.timestamp
        )

        # ---- unfiltered branch: every signal opens/closes per Option 1 ----
        base = _baseline_decision(sig_dir, unfiltered.current_position)
        unfiltered.apply(
            sig_dir,
            base["should_close"],
            base["should_open"],
            base["open_direction"],
            blocked_by_filter=False,
            index=fill_index,
            ts=fill_ts,
            fill_price=fill_price,
        )

        # ---- filtered branch: score with the real model ----
        feats = fb.build_at(idx)
        if feats is None:
            skipped_incomplete_features += 1
            continue
        scored += 1

        decision = predictor.predict(
            {"direction": sig.direction, "features": feats},
            current_position=filtered.current_position,
        )
        # "Blocked by filter" = the Option-1 rules (ignoring the ML gate)
        # would have opened a position here, but the model's probability
        # didn't clear the threshold. Comparing against what the *filtered*
        # branch's own baseline would have done (not the unfiltered branch's
        # state) keeps this correct even after the two branches' position
        # histories diverge.
        would_open_unfiltered = _baseline_decision(sig_dir, filtered.current_position)[
            "should_open"
        ]
        blocked_by_filter = would_open_unfiltered and not decision["should_open"]
        filtered.apply(
            sig_dir,
            decision["should_close"],
            decision["should_open"],
            decision["open_direction"],
            blocked_by_filter=blocked_by_filter,
            index=fill_index,
            ts=fill_ts,
            fill_price=fill_price,
        )

    unfiltered_stats = _branch_stats(unfiltered)
    filtered_stats = _branch_stats(filtered)
    uplift = (
        filtered_stats["cumulative_pnl_pct"] - unfiltered_stats["cumulative_pnl_pct"]
    )

    return ComparisonReport(
        threshold=predictor.threshold,
        total_candidate_signals=len(signals),
        scored_signals=scored,
        skipped_missing_candle=skipped_missing_candle,
        skipped_incomplete_features=skipped_incomplete_features,
        unfiltered=unfiltered_stats,
        filtered=filtered_stats,
        uplift_pct=uplift,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_model_dir() -> Path:
    candidates = [
        HERE / "model",  # co-located, e.g. copied into a container image
        HERE.parent / "ml-predictor" / "model",  # monorepo dev checkout
    ]
    for c in candidates:
        if (c / "xgb_production.json").exists():
            return c
    raise FileNotFoundError(
        "Could not find xgb_production.json in any of: "
        + ", ".join(str(c) for c in candidates)
        + ". Pass --model-path/--feature-order-path explicitly."
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backtest_ml",
        description="Compare filtered vs unfiltered PnL for the XGBoost trade filter.",
    )
    parser.add_argument("--candles", required=True, help="CSV of OHLCV candles")
    parser.add_argument(
        "--signals", required=True, help="CSV of timestamp,action signals"
    )
    parser.add_argument(
        "--model-path", default=None, help="Path to xgb_production.json"
    )
    parser.add_argument(
        "--feature-order-path", default=None, help="Path to feature_order.txt"
    )
    parser.add_argument(
        "--threshold", type=float, default=0.50, help="Win-probability threshold"
    )
    parser.add_argument(
        "--output", default=None, help="Write JSON report here instead of stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> ComparisonReport:
    args = build_arg_parser().parse_args(argv)

    if args.model_path and args.feature_order_path:
        model_path, feature_order_path = args.model_path, args.feature_order_path
    else:
        model_dir = _default_model_dir()
        model_path = args.model_path or str(model_dir / "xgb_production.json")
        feature_order_path = args.feature_order_path or str(
            model_dir / "feature_order.txt"
        )

    predictor = XGBPredictor(model_path, feature_order_path, threshold=args.threshold)
    candles = load_candles(args.candles)
    signals = load_signals(args.signals)

    report = run_comparison(candles, signals, predictor)
    text = json.dumps(report.to_dict(), indent=2)

    if args.output:
        Path(args.output).write_text(text + "\n")
    else:
        print(text)

    return report


if __name__ == "__main__":
    main()

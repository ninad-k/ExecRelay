"""Tests for report_export.py's HTML/CSV rendering of a ComparisonReport."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_ml import ComparisonReport, Trade
from report_export import render_html_report, write_trades_csv


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _sample_report() -> ComparisonReport:
    closed_trade = Trade(
        direction="LONG",
        entry_index=1,
        entry_time=_ts(1),
        entry_price=100.0,
        exit_index=5,
        exit_time=_ts(5),
        exit_price=101.0,
    )
    open_trade = Trade(
        direction="SHORT",
        entry_index=8,
        entry_time=_ts(8),
        entry_price=102.0,
    )
    stats_template = {
        "signals_seen": 3,
        "trades_opened": 2,
        "trades_closed": 1,
        "trades_still_open": 1,
        "trades_skipped_pyramid": 0,
        "trades_skipped_by_filter": 0,
        "win_rate": 1.0,
        "cumulative_pnl_pct": 0.01,
        "max_drawdown_pct": 0.0,
    }
    return ComparisonReport(
        threshold=0.5,
        total_candidate_signals=3,
        scored_signals=3,
        skipped_missing_candle=0,
        skipped_incomplete_features=0,
        unfiltered=dict(stats_template),
        filtered=dict(stats_template),
        uplift_pct=0.0,
        unfiltered_trades=[closed_trade, open_trade],
        filtered_trades=[closed_trade],
    )


def test_render_html_report_contains_key_figures():
    report = _sample_report()
    out = render_html_report(report)

    assert "<!doctype html>" in out.lower()
    assert "ExecRelay ML Backtest Report" in out
    # summary figures show up
    assert "1.00%" in out  # cumulative_pnl_pct formatted as a percentage
    # both branches' trade rows are present
    assert out.count("<tr>") >= 1 + len(report.unfiltered_trades) + len(
        report.filtered_trades
    )
    assert "&mdash; (open)" in out  # the still-open SHORT trade has no exit


def test_write_trades_csv_round_trips(tmp_path: Path):
    report = _sample_report()
    out_path = tmp_path / "trades.csv"

    write_trades_csv(report, out_path)

    with open(out_path, newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == len(report.unfiltered_trades) + len(report.filtered_trades)
    assert {r["branch"] for r in rows} == {"unfiltered", "filtered"}

    closed_rows = [r for r in rows if r["exit_time"]]
    assert len(closed_rows) == 2  # the closed LONG trade appears in both branches
    for row in closed_rows:
        assert row["direction"] == "LONG"
        assert float(row["pnl_pct"]) > 0

    open_rows = [r for r in rows if not r["exit_time"]]
    assert len(open_rows) == 1
    assert open_rows[0]["exit_price"] == ""
    assert open_rows[0]["pnl_pct"] == ""

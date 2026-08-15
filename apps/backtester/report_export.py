"""HTML + CSV export for backtest_ml.py's ComparisonReport.

Kept as a separate module (rather than inlined in backtest_ml.py) so the
core comparison engine has no string-templating/formatting concerns, and so
this can be reused if another entry point wants the same output shape later.

The HTML report is a single self-contained file (inline CSS, no external
requests, no build step) styled with the same design tokens as
scripts/trade_dashboard.py and static/css/dashboard.css, so it looks at home
if opened next to the rest of ExecRelay's operator tooling. See
apps/backtester/results/README.md for where these are conventionally written.
"""

from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backtest_ml import ComparisonReport, Trade

_STYLE = """
:root {
  --color-background: #020c1b; --color-surface: #061526; --color-surface-2: #0b2040;
  --color-border: #123060; --color-primary: #00c2e0; --color-profit: #05e8a4;
  --color-profit-dim: rgb(5 232 164 / 0.14); --color-loss: #ff3d5f;
  --color-loss-dim: rgb(255 61 95 / 0.14); --color-text: #cde4ff; --color-text-muted: #4e7aab;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, "Segoe UI", Roboto, sans-serif;
  background: var(--color-background); color: var(--color-text); }
header { padding: 1rem 1.5rem; background: var(--color-surface); border-bottom: 1px solid var(--color-border); }
header h1 { margin: 0; font-size: 1.1rem; }
header .meta { color: var(--color-text-muted); font-size: 0.8rem; margin-top: 0.25rem; }
main { padding: 1.5rem; max-width: 1000px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.card { background: var(--color-surface); border: 1px solid var(--color-border);
  border-radius: 0.625rem; padding: 1rem 1.25rem; margin-bottom: 1rem; }
.card h2 { margin: 0 0 0.75rem; font-size: 0.75rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--color-text-muted); }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--color-border); }
th { color: var(--color-text-muted); font-weight: 500; text-transform: uppercase; font-size: 0.68rem; }
.number { font-variant-numeric: tabular-nums; }
.pos { color: var(--color-profit); } .neg { color: var(--color-loss); }
.uplift { font-size: 1.75rem; font-weight: 700; }
"""


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _pnl_class(value: float | None) -> str:
    if value is None:
        return ""
    return "pos" if value >= 0 else "neg"


def _stats_table(stats: dict) -> str:
    rows = [
        ("Signals seen", str(stats["signals_seen"]), ""),
        ("Trades opened", str(stats["trades_opened"]), ""),
        ("Trades closed", str(stats["trades_closed"]), ""),
        ("Trades still open", str(stats["trades_still_open"]), ""),
        ("Skipped (pyramid)", str(stats["trades_skipped_pyramid"]), ""),
        ("Skipped (filter)", str(stats["trades_skipped_by_filter"]), ""),
        ("Win rate", _fmt_pct(stats["win_rate"]), ""),
        (
            "Cumulative PnL",
            _fmt_pct(stats["cumulative_pnl_pct"]),
            _pnl_class(stats["cumulative_pnl_pct"]),
        ),
        ("Max drawdown", _fmt_pct(stats["max_drawdown_pct"]), ""),
    ]
    body = "".join(
        f"<tr><td>{html.escape(label)}</td>"
        f'<td class="number {css_class}">{html.escape(value)}</td></tr>'
        for label, value, css_class in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


def _trades_table(trades: list["Trade"], branch: str) -> str:
    rows = []
    for t in trades:
        pnl = t.pnl_pct()
        exit_time = (
            html.escape(t.exit_time.isoformat()) if t.exit_time else "&mdash; (open)"
        )
        exit_price = f"{t.exit_price:.5f}" if t.exit_price is not None else "&mdash;"
        rows.append(
            "<tr>"
            f"<td>{html.escape(branch)}</td>"
            f"<td>{html.escape(t.direction)}</td>"
            f"<td>{html.escape(t.entry_time.isoformat())}</td>"
            f'<td class="number">{t.entry_price:.5f}</td>'
            f"<td>{exit_time}</td>"
            f'<td class="number">{exit_price}</td>'
            f'<td class="number {_pnl_class(pnl)}">{_fmt_pct(pnl)}</td>'
            "</tr>"
        )
    return "".join(rows)


def render_html_report(report: "ComparisonReport") -> str:
    """Render a self-contained HTML dashboard for a ComparisonReport."""
    trades_rows = _trades_table(report.unfiltered_trades, "unfiltered") + _trades_table(
        report.filtered_trades, "filtered"
    )
    uplift_class = _pnl_class(report.uplift_pct)

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>ExecRelay ML Backtest Report</title>
<style>{_STYLE}</style>
</head><body>
<header>
  <h1>ML filter backtest &mdash; filtered vs. unfiltered</h1>
  <div class="meta">
    threshold={report.threshold:.2f} &middot;
    candidate signals={report.total_candidate_signals} &middot;
    scored={report.scored_signals} &middot;
    skipped (missing candle)={report.skipped_missing_candle} &middot;
    skipped (incomplete features)={report.skipped_incomplete_features}
  </div>
</header>
<main>
  <div class="card">
    <h2>Uplift</h2>
    <div class="uplift {uplift_class} number">{_fmt_pct(report.uplift_pct)}</div>
    <div class="meta">filtered cumulative PnL minus unfiltered cumulative PnL</div>
  </div>
  <div class="grid">
    <div class="card"><h2>Unfiltered (every signal)</h2>{_stats_table(report.unfiltered)}</div>
    <div class="card"><h2>Filtered (XGBoost gated)</h2>{_stats_table(report.filtered)}</div>
  </div>
  <div class="card">
    <h2>Trades</h2>
    <table>
      <thead><tr><th>Branch</th><th>Dir</th><th>Entry time</th><th>Entry</th>
        <th>Exit time</th><th>Exit</th><th>PnL</th></tr></thead>
      <tbody>{trades_rows}</tbody>
    </table>
  </div>
</main>
</body></html>
"""


def write_trades_csv(report: "ComparisonReport", path: str | Path) -> None:
    """Write every trade from both branches to a single CSV."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "branch",
                "direction",
                "entry_time",
                "entry_price",
                "exit_time",
                "exit_price",
                "pnl_pct",
            ]
        )
        for branch, trades in (
            ("unfiltered", report.unfiltered_trades),
            ("filtered", report.filtered_trades),
        ):
            for t in trades:
                writer.writerow(
                    [
                        branch,
                        t.direction,
                        t.entry_time.isoformat(),
                        f"{t.entry_price:.6f}",
                        t.exit_time.isoformat() if t.exit_time else "",
                        f"{t.exit_price:.6f}" if t.exit_price is not None else "",
                        f"{t.pnl_pct():.6f}" if t.pnl_pct() is not None else "",
                    ]
                )

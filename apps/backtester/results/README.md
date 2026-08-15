# apps/backtester/results/

Conventional output directory for `backtest_ml.py` runs. Nothing in this
directory except this file and `.gitkeep` is tracked — generated reports are
gitignored (see the repo-root `.gitignore`) because they're reproducible from
`--candles`/`--signals` inputs plus the tracked model artifact
(`apps/ml-predictor/model/`).

## Generate a report

```sh
python apps/backtester/backtest_ml.py \
  --candles path/to/candles.csv \
  --signals path/to/signals.csv \
  --output   apps/backtester/results/report.json \
  --html     apps/backtester/results/report.html \
  --trades-csv apps/backtester/results/trades.csv
```

- `report.json` — machine-readable summary (`ComparisonReport.to_dict()`):
  threshold, signal counts, and filtered/unfiltered stats (win rate,
  cumulative PnL, max drawdown).
- `report.html` — the same summary rendered as a single self-contained HTML
  page (inline CSS, no external requests) plus a full trade-by-trade table.
  Open it directly in a browser.
- `trades.csv` — every trade from both branches (`branch,direction,
  entry_time,entry_price,exit_time,exit_price,pnl_pct`), for further analysis
  in a spreadsheet or notebook.

All three flags are independent and optional — pass any subset. See
`apps/backtester/report_export.py` for the rendering code and
`apps/backtester/backtest_ml.py`'s module docstring for what this harness
does and does not simulate (it's a methodology-comparison tool, not a
broker-accurate fills simulator).

## Naming convention

No enforced naming scheme, but for anything you intend to keep around or
attach to a PR/ticket, prefix with the date and threshold so results don't
collide: `2026-08-15_threshold-050_report.html`.

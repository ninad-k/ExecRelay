"""Shared pytest setup for apps/backtester tests.

Adds the app directory to `sys.path` so tests can `import feature_builder`,
`import xgb_predictor`, and `import backtest_ml` the same flat, no-package
way those modules import each other -- mirrors how
apps/ml-predictor/tests/conftest.py does this for that sibling app.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

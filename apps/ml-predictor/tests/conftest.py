"""Shared fixtures for ml-predictor tests.

app.py registers its Prometheus metrics against the global default
CollectorRegistry at import time, so it must be executed at most once per
test session -- re-importing it a second time (even via a fresh importlib
spec, which bypasses sys.modules caching) raises "Duplicated timeseries in
CollectorRegistry". A single session-scoped fixture loads it once and hands
the same module object to every test file that needs it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
# app.py does `from xgb_predictor import XGBPredictor`, so the app dir must
# be importable regardless of test collection order.
sys.path.insert(0, str(APP_DIR))


@pytest.fixture(scope="session")
def app_module():
    spec = importlib.util.spec_from_file_location(
        "ml_predictor_app_under_test", APP_DIR / "app.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

"""Smoke test: app module imports cleanly.

Catches missing deps, syntax errors past the top of app.py, and import-time
exceptions. Real unit tests should be added alongside this file.
"""

import importlib.util
import sys
from pathlib import Path


def test_app_module_imports() -> None:
    app_dir = Path(__file__).resolve().parent.parent
    # app.py does `from xgb_predictor import XGBPredictor`, so the app dir must
    # be importable regardless of test collection order.
    sys.path.insert(0, str(app_dir))
    app_path = app_dir / "app.py"
    spec = importlib.util.spec_from_file_location("app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

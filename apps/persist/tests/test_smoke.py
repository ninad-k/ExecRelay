"""Smoke test: app module imports cleanly.

Catches missing deps, syntax errors past the top of app.py, and import-time
exceptions. Real unit tests should be added alongside this file.
"""

import importlib.util
import sys
from pathlib import Path


def test_app_module_imports() -> None:
    # Wipe prometheus registry so re-imports across test modules don't trip
    # the "duplicated timeseries" check on Counter/Histogram registration.
    try:
        from prometheus_client import REGISTRY
        for collector in list(REGISTRY._collector_to_names.keys()):
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass
    except Exception:
        pass
    sys.modules.pop("app", None)

    app_path = Path(__file__).resolve().parent.parent / "app.py"
    spec = importlib.util.spec_from_file_location("app", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

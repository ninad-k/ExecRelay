"""Smoke test: app module imports cleanly.

Catches missing deps, syntax errors past the top of app.py, and import-time
exceptions. Real unit tests should be added alongside this file.

Uses the shared `app_module` fixture (see conftest.py) rather than loading
app.py itself -- app.py registers Prometheus metrics against the global
default CollectorRegistry at import time, so it can only be executed once
per test session across every test file.
"""


def test_app_module_imports(app_module) -> None:
    assert app_module is not None

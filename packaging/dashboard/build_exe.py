"""Build script for a standalone Windows executable of the Rey Capital trade
dashboard (``scripts/trade_dashboard.py``).

Why this exists
----------------
``scripts/trade_dashboard.py`` is normally launched by ``run.ps1`` /
``scripts/local-stack.ps1`` inside the repo's Python venv. That's fine for
development, but the dashboard is also useful standalone -- e.g. handed to a
trader who just wants "double-click, see positions" against a running MT5
terminal, with no Python install, no venv, no clone of this repo. This script
packages it (plus its assets and the MetaTrader5 SDK) into one .exe with
PyInstaller, the same way the legacy prototype dashboard was packaged before
ExecRelay existed.

Usage
-----
    pip install -r packaging/dashboard/requirements-build.txt
    python packaging/dashboard/build_exe.py

Output: dist/ExecRelayDashboard.exe (relative to the repo root). The build/
and dist/ directories are gitignored -- see .gitignore.

Notes
-----
- Windows only (MetaTrader5 has no Linux/macOS wheel).
- The dashboard is stdlib-only at runtime (``http.server``) plus the
  optional ``MetaTrader5`` package -- no Flask/Jinja, so there is nothing
  under templates/ or static/ to bundle for *this* target. If you package a
  Flask/Jinja-based tool from packaging/dashboard/ in the future, add
  ``--add-data`` entries the same way the repo-root templates/README.md
  documents.
- ``scripts/dashboard-assets/`` (currently just favicon.png) IS bundled,
  since trade_dashboard.py serves it from a relative path at runtime.
"""
from __future__ import annotations

import os

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
ENTRY_POINT = os.path.join(REPO_ROOT, "scripts", "trade_dashboard.py")
ASSETS_DIR = os.path.join(REPO_ROOT, "scripts", "dashboard-assets")
APP_NAME = "ExecRelayDashboard"

args = [
    ENTRY_POINT,
    f"--name={APP_NAME}",
    "--onefile",
    "--console",  # keep console: dashboard logs startup/health info to stdout
    f"--add-data={ASSETS_DIR};dashboard-assets",
    "--hidden-import=MetaTrader5",
    "--collect-all=MetaTrader5",
    f"--distpath={os.path.join(REPO_ROOT, 'dist')}",
    f"--workpath={os.path.join(REPO_ROOT, 'build')}",
    f"--specpath={HERE}",
]

if __name__ == "__main__":
    PyInstaller.__main__.run(args)
    exe_path = os.path.join(REPO_ROOT, "dist", f"{APP_NAME}.exe")
    print("\n" + "=" * 60)
    print("BUILD COMPLETE")
    print(f"EXE location: {exe_path}")
    print("Run it from the directory you want config.json / DASHBOARD_ADDR")
    print("etc. picked up from -- it behaves the same as `python")
    print("scripts/trade_dashboard.py` (see scripts/trade_dashboard.py docstring).")
    print("=" * 60)

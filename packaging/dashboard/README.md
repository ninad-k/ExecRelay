# Packaging: standalone dashboard executable

Builds `scripts/trade_dashboard.py` (the Rey Capital combined trade
dashboard normally launched by `run.ps1` / `scripts/local-stack.ps1`) into a
single Windows executable, for traders who want to run it against a live MT5
terminal without a Python install or a full repo checkout.

## Build

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\Activate.ps1
pip install -r packaging/dashboard/requirements-build.txt
python packaging/dashboard/build_exe.py
```

Output: `dist/ExecRelayDashboard.exe` at the repo root. `build/` and `dist/`
are gitignored — see the `.gitignore` entries added alongside this directory.

`ExecRelayDashboard.spec` is the PyInstaller spec `build_exe.py` drives under
the hood; it's committed so CI or a teammate can rebuild with
`pyinstaller packaging/dashboard/ExecRelayDashboard.spec` directly if they
need to tweak Analysis/EXE options the CLI wrapper doesn't expose.

## Running the exe

```powershell
# From the directory you want config.json / .local-stack/ to live under:
.\ExecRelayDashboard.exe
```

It behaves exactly like `python scripts/trade_dashboard.py` — same
`DASHBOARD_ADDR` / `DASHBOARD_TOKEN` / `EA_SHIM_MAGIC` environment variables
(see the docstring at the top of `scripts/trade_dashboard.py`).

**Caveat:** the dashboard's MT5-fill history pane reads
from `.local-stack/logs/transactions/` and `.local-stack/journal.json`
*relative to the repo root it was launched from*. When frozen, those paths
resolve relative to the PyInstaller temp extraction directory, not a real
repo checkout — so run the exe from inside a checked-out ExecRelay repo (or
symlink/copy a `.local-stack/` directory next to it) if you want that
history to show up. The MT5-terminal-derived panes (open positions, closed
deals) don't depend on this and work standalone.

## Extending this to other tools

If you package a Flask/Jinja-based tool from this directory in the future
(as opposed to `trade_dashboard.py`'s stdlib `http.server` + inline HTML),
add `--add-data=<templates dir>;templates` and `--add-data=<static
dir>;static` the way the [repo-root `templates/README.md`](../../templates/README.md)
describes, following the pattern in `templates/README.md`'s "packaging a
Jinja app" section.

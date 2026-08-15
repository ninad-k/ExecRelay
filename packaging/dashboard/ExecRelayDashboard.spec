# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the standalone ExecRelay trade dashboard exe.
# Generated to mirror packaging/dashboard/build_exe.py's PyInstaller
# invocation -- prefer running build_exe.py (it computes paths relative to
# the repo root so this works from any clone location). Run this spec
# directly with `pyinstaller packaging/dashboard/ExecRelayDashboard.spec`
# only if you need to tweak PyInstaller options this spec format exposes
# that the CLI wrapper doesn't.
from PyInstaller.utils.hooks import collect_all
import os

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
REPO_ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))
ENTRY_POINT = os.path.join(REPO_ROOT, "scripts", "trade_dashboard.py")
ASSETS_DIR = os.path.join(REPO_ROOT, "scripts", "dashboard-assets")

datas = [(ASSETS_DIR, "dashboard-assets")]
binaries = []
hiddenimports = ["MetaTrader5"]
tmp_ret = collect_all("MetaTrader5")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [ENTRY_POINT],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ExecRelayDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

# customtkinter ships theme/image assets that must be bundled explicitly.
customtkinter_datas = collect_data_files('customtkinter')

# Build the GUI entry. Backtest remains a normal CLI script and is not bundled
# into this windowed executable.
a = Analysis(
    ['run_gui.py'],
    pathex=[],
    binaries=[],
    datas=customtkinter_datas,
    # The refactor moved source into sz002796; keep the package modules explicit
    # so PyInstaller does not miss lazy imports used by the GUI worker thread.
    hiddenimports=[
        'sz002796.config',
        'sz002796.data_quality',
        'sz002796.execution',
        'sz002796.factors',
        'sz002796.fetcher',
        'sz002796.gui',
        'sz002796.market_data',
        'sz002796.position',
        'sz002796.regime',
        'sz002796.state_store',
        'sz002796.strategy_v6',
        'sz002796.tick_writer',
    ],
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
    name='世嘉科技策略监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

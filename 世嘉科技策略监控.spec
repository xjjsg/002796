# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('customtkinter')


# The Windows release uses onedir packaging. It starts much faster and avoids
# long UPX compression hangs seen in the local Anaconda environment.
a = Analysis(
    ['run_gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['sz002796.config', 'sz002796.data_quality', 'sz002796.execution', 'sz002796.factors', 'sz002796.fetcher', 'sz002796.gui', 'sz002796.market_data', 'sz002796.position', 'sz002796.realtime_sources', 'sz002796.regime', 'sz002796.state_store', 'sz002796.strategy_v6', 'sz002796.tick_writer', 'sz002796.trade_records', 'qmt.adapter', 'qmt.config', 'qmt.live_data', 'qmt.xtquant_env'],
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
    [],
    exclude_binaries=True,
    name='世嘉科技策略监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='世嘉科技策略监控',
)

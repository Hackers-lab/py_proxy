# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

datas = [('icon.ico', '.'), ('icon.png', '.')]
binaries = []
# The UI is imported lazily inside net_tunnel.main(); collect the whole package
# explicitly so every nst submodule is bundled regardless of import location.
# PyQt6 itself (and its Qt plugins) is handled by PyInstaller's bundled hooks.
hiddenimports = collect_submodules('nst')


a = Analysis(
    ['net_tunnel.py'],
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
    name='NetSplitTunnel_v4.8',
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
    uac_admin=True,
    icon=['icon.ico'],
)

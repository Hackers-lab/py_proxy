# -*- mode: python ; coding: utf-8 -*-
import re
from PyInstaller.utils.hooks import collect_submodules

# Keep the EXE name in lock-step with the package version so each build/release
# is named NetSplitTunnel_v<version>.exe automatically.
with open('nst/__init__.py', encoding='utf-8') as _f:
    _VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', _f.read()).group(1)

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
    name=f'NetSplitTunnel_v{_VERSION}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is disabled on purpose: in a one-file build every compressed binary has
    # to be decompressed into a temp dir on each launch, which noticeably slows
    # cold start. Skipping UPX trades a larger .exe for a faster startup.
    upx=False,
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

# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

# One-folder (onedir) build: the app is distributed via the per-user installer,
# so there is no need for a single self-extracting exe. onedir avoids the
# temp-dir extraction on every launch (faster restart) and, crucially, removes
# the "Failed to load python3xx.dll from _MEI..." race that one-file builds hit
# when they relaunch themselves during a self-update.

datas = [('icon.ico', '.'), ('icon.png', '.')]
binaries = []
# The UI is imported lazily inside net_tunnel.main(); collect the whole package
# explicitly so every nst submodule is bundled regardless of import location.
# PyQt6 itself (and its Qt plugins) is handled by PyInstaller's bundled hooks.
hiddenimports = collect_submodules('nst')


# Only QtCore/QtGui/QtWidgets are imported; drop the rest of Qt plus heavy
# unused stdlib so the bundled folder stays small.
excludes = [
    'PyQt6.QtNetwork', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets',
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebChannel', 'PyQt6.QtPdf',
    'PyQt6.QtPdfWidgets', 'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtOpenGL',
    'PyQt6.QtOpenGLWidgets', 'PyQt6.QtPrintSupport', 'PyQt6.QtBluetooth',
    'PyQt6.QtPositioning', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort',
    'PyQt6.QtCharts', 'PyQt6.QtDataVisualization', 'PyQt6.QtSvg',
    'PyQt6.QtSvgWidgets', 'PyQt6.QtDBus',
    'tkinter', 'unittest', 'pydoc', 'lib2to3', 'test', 'distutils',
    'xmlrpc', 'pdb', 'doctest',
]


a = Analysis(
    ['net_tunnel.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)
# Drop large binaries this widgets-only app never loads:
#   opengl32sw.dll  — Qt's ~20MB software-OpenGL fallback (desktop GPU rendering
#                     doesn't need it); Qt6Pdf — the unused PDF module pulled in
#                     transitively. Halves the one-file exe.
_DROP_BINARIES = {'opengl32sw.dll', 'qt6pdf.dll', 'd3dcompiler_47.dll'}
a.binaries = [b for b in a.binaries
              if os.path.basename(b[0]).lower() not in _DROP_BINARIES]
# Qt ships ~3MB of UI translations we don't use.
a.datas = [d for d in a.datas
           if not d[0].lower().replace('\\', '/').startswith('pyqt6/qt6/translations/')]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: binaries/datas live in the COLLECT folder
    name='NetSplitTunnel',
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
    # Run as a normal user — no UAC on launch. Admin is requested on demand
    # only for the split-tunnel route (see nst/routing.py).
    uac_admin=False,
    icon=['icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='NetSplitTunnel',
)

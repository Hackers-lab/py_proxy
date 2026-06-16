"""
Network Split-Tunneler & Proxy Sharing Tool  v3.2
Windows 10/11 only — Python 3.10+

Thin launcher. The application is implemented as the ``nst`` package:
    nst.ui.app.App      — the main window
    nst.proxy_server    — HTTP/HTTPS proxy
    nst.beacon          — host discovery
    nst.chat            — LAN peer discovery + messaging
    nst.theme           — light/dark theming

Third-party deps (pip install before running / bundle with PyInstaller):
    pip install psutil pystray pillow
"""

import sys

from nst.win_utils import (
    check_single_instance,
    elevate,
    hide_console,
    is_admin,
    show_already_running_dialog,
)


def main() -> None:
    hide_console()
    if not is_admin():
        elevate()  # relaunches elevated, then exits this process

    if not check_single_instance():
        show_already_running_dialog()
        sys.exit(0)

    # Import the UI lazily — only the surviving, elevated, single instance needs it.
    from nst.ui.app import HAS_TRAY, App

    if not HAS_TRAY:
        import warnings
        warnings.warn(
            "pystray / Pillow not installed — system tray disabled. "
            "Run: pip install pystray pillow",
            stacklevel=1,
        )

    app = App()
    w, h = 540, 600
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    app.mainloop()


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────────────────────────────────────
#  BUILD (run once, in the project directory):
#
#  pip install psutil pystray pillow pyinstaller
#  pyinstaller NetSplitTunnel.spec
#
#  Output: dist\NetSplitTunnel_v3.exe
#  The uac_admin manifest means Windows prompts for elevation on every launch.
# ──────────────────────────────────────────────────────────────────────────────

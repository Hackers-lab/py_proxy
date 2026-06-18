"""
Network Split-Tunneler & Proxy Sharing Tool
Windows 10/11 only — Python 3.10+

Thin launcher. The application is implemented as the ``nst`` package:
    nst.qt.app.run      — PyQt6 application bootstrap
    nst.proxy_server    — HTTP/HTTPS proxy
    nst.beacon          — host discovery
    nst.chat            — LAN peer discovery + messaging
    nst.qt.theme        — light/dark theming (QSS)

Third-party deps (pip install before running / bundle with PyInstaller):
    pip install psutil PyQt6
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
    from nst.qt.app import run

    run()


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────────────────────────────────────
#  BUILD (run once, in the project directory):
#
#  pip install psutil PyQt6 pyinstaller
#  pyinstaller NetSplitTunnel.spec
#
#  Output: dist\NetSplitTunnel_v4.5.exe
#  The uac_admin manifest means Windows prompts for elevation on every launch.
# ──────────────────────────────────────────────────────────────────────────────

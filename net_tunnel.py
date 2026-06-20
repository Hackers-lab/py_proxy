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
    hide_console,
    show_already_running_dialog,
)


def _route_cli() -> bool:
    """Handle the elevated route-helper invocations.

    The app no longer runs elevated; the split-tunnel route (the only operation
    that needs admin) is performed by relaunching this exe elevated with one of
    these flags. These branches do the route op and exit — no UI, no mutex.
    Returns True if a route command was handled (caller should not continue).
    """
    from nst import routing

    if "--add-route" in sys.argv:
        i = sys.argv.index("--add-route")
        gateway = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        sys.exit(routing._do_add_route(gateway))
    if "--del-route" in sys.argv:
        sys.exit(routing._do_del_route())
    return False


def main() -> None:
    hide_console()

    # Elevated route helper (relaunched via UAC by nst.routing); never opens UI.
    _route_cli()

    if not check_single_instance():
        show_already_running_dialog()
        sys.exit(0)

    # Import the UI lazily — only the surviving, single instance needs it.
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
#  Output: dist\NetSplitTunnel_v<version>.exe
#  The app runs as a normal user (no UAC). Admin is requested on demand only
#  when the split-tunnel route is toggled. Distributed via the per-user
#  Inno Setup installer (installer.iss); see the release workflow.
# ──────────────────────────────────────────────────────────────────────────────

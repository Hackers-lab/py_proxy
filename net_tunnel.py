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
        network = sys.argv[i + 2] if i + 2 < len(sys.argv) else "10.0.0.0"
        sys.exit(routing._do_add_route(gateway, network))
    if "--del-route" in sys.argv:
        i = sys.argv.index("--del-route")
        network = sys.argv[i + 1] if i + 1 < len(sys.argv) else "10.0.0.0"
        sys.exit(routing._do_del_route(network))

    from nst import dual_access
    if "--dual-enable" in sys.argv:
        i = sys.argv.index("--dual-enable")
        a = sys.argv
        sys.exit(dual_access._do_enable(
            intranet_gw = a[i+1] if i+1 < len(a) else "",
            internet_ip = a[i+2] if i+2 < len(a) else "",
            internet_gw = a[i+3] if i+3 < len(a) else "",
            adapter     = a[i+4] if i+4 < len(a) else "",
            dns_csv     = a[i+5] if i+5 < len(a) else "",
            domain_csv  = a[i+6] if i+6 < len(a) else "",
        ))
    if "--dual-disable" in sys.argv:
        i = sys.argv.index("--dual-disable")
        a = sys.argv
        sys.exit(dual_access._do_disable(
            internet_ip = a[i+1] if i+1 < len(a) else "",
            adapter     = a[i+2] if i+2 < len(a) else "",
            domain_csv  = a[i+3] if i+3 < len(a) else "",
        ))

    if "--apply-profile" in sys.argv:
        from nst import ipswitch
        i = sys.argv.index("--apply-profile")
        a = sys.argv
        sys.exit(ipswitch._do_apply(
            adapter = a[i+1] if i+1 < len(a) else "",
            mode    = a[i+2] if i+2 < len(a) else "static",
            ip      = a[i+3] if i+3 < len(a) else "",
            mask    = a[i+4] if i+4 < len(a) else "255.255.255.0",
            gateway = a[i+5] if i+5 < len(a) else "",
            dns     = a[i+6] if i+6 < len(a) else "",
        ))
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

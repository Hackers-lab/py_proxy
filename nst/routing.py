"""Persistent 10.0.0.0/8 route management used by the split-tunnel feature.

Adding/removing a persistent route is the *only* operation in the whole app
that requires administrator rights. Rather than run the entire process
elevated, we elevate just this one action on demand: when not already admin,
the public helpers relaunch this exe with ``--add-route``/``--del-route`` via
UAC (see :func:`nst.win_utils.run_elevated_and_wait` and the CLI dispatch in
``net_tunnel.main``).
"""

import sys

from .netinfo import run_cmd
from .win_utils import (ELEVATION_CANCELLED, is_admin, run_elevated_and_wait,
                        self_relaunch_cmd)


def _network_from_ip(ip: str) -> str:
    """Return the /8 network address for *ip* (e.g. '15.3.4.68' → '15.0.0.0')."""
    return ip.split(".")[0] + ".0.0.0"


def check_route_exists(network: str = "") -> bool:
    """True if a /8 persistent route for *network* is present.

    If *network* is empty the current intranet IP is auto-detected.
    """
    if not network:
        from .netinfo import get_intranet_ip
        ip = get_intranet_ip()
        network = _network_from_ip(ip) if ip else "10.0.0.0"
    _, out, _ = run_cmd(["route", "print", network])
    return "255.0.0.0" in out and network in out


# ── Privileged primitives (must run elevated) ─────────────────────────────────

def _do_add_route(gateway: str, network: str) -> int:
    """Add the persistent /8 route. Returns the ``route`` exit code."""
    code, _, _ = run_cmd(
        ["route", "add", network, "mask", "255.0.0.0", gateway, "-p"]
    )
    return code


def _do_del_route(network: str) -> int:
    """Delete the persistent /8 route. Returns the ``route`` exit code."""
    code, _, _ = run_cmd(["route", "delete", network])
    return code


# ── Public helpers (elevate on demand) ────────────────────────────────────────

def add_intranet_route(gateway: str, network: str) -> tuple[bool, str]:
    if is_admin():
        code = _do_add_route(gateway, network)
    else:
        code = run_elevated_and_wait(
            self_relaunch_cmd() + ["--add-route", gateway, network])
    if code == 0:
        return True, f"Route {network}/8 → {gateway} added (persistent)."
    if code == ELEVATION_CANCELLED:
        return False, "Route add cancelled (administrator approval required)."
    return False, f"route add failed (exit {code})."


def delete_intranet_route(network: str) -> tuple[bool, str]:
    if is_admin():
        code = _do_del_route(network)
    else:
        code = run_elevated_and_wait(
            self_relaunch_cmd() + ["--del-route", network])
    if code == 0:
        return True, f"Route {network}/8 removed."
    if code == ELEVATION_CANCELLED:
        return False, "Route removal cancelled (administrator approval required)."
    return False, f"route delete failed (exit {code})."

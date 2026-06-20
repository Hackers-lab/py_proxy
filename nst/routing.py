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
from .win_utils import ELEVATION_CANCELLED, is_admin, run_elevated_and_wait


def check_route_exists() -> bool:
    """True if a 10.0.0.0 mask 255.0.0.0 route is present."""
    _, out, _ = run_cmd(["route", "print", "10.0.0.0"])
    return "255.0.0.0" in out and "10.0.0.0" in out


# ── Privileged primitives (must run elevated) ─────────────────────────────────

def _do_add_route(gateway: str) -> int:
    """Add the persistent route. Returns the ``route`` exit code."""
    code, _, _ = run_cmd(
        ["route", "add", "10.0.0.0", "mask", "255.0.0.0", gateway, "-p"]
    )
    return code


def _do_del_route() -> int:
    """Delete the persistent route. Returns the ``route`` exit code."""
    code, _, _ = run_cmd(["route", "delete", "10.0.0.0"])
    return code


# ── Public helpers (elevate on demand) ────────────────────────────────────────

def add_intranet_route(gateway: str) -> tuple[bool, str]:
    if is_admin():
        code = _do_add_route(gateway)
    else:
        code = run_elevated_and_wait([sys.executable, "--add-route", gateway])
    if code == 0:
        return True, f"Route 10.0.0.0/8 → {gateway} added (persistent)."
    if code == ELEVATION_CANCELLED:
        return False, "Route add cancelled (administrator approval required)."
    return False, f"route add failed (exit {code})."


def delete_intranet_route() -> tuple[bool, str]:
    if is_admin():
        code = _do_del_route()
    else:
        code = run_elevated_and_wait([sys.executable, "--del-route"])
    if code == 0:
        return True, "Route 10.0.0.0/8 removed."
    if code == ELEVATION_CANCELLED:
        return False, "Route removal cancelled (administrator approval required)."
    return False, f"route delete failed (exit {code})."

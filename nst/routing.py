"""Persistent 10.0.0.0/8 route management used by the split-tunnel feature."""

from .netinfo import run_cmd


def check_route_exists() -> bool:
    """True if a 10.0.0.0 mask 255.0.0.0 route is present."""
    _, out, _ = run_cmd(["route", "print", "10.0.0.0"])
    return "255.0.0.0" in out and "10.0.0.0" in out


def add_intranet_route(gateway: str) -> tuple[bool, str]:
    code, _, err = run_cmd(
        ["route", "add", "10.0.0.0", "mask", "255.0.0.0", gateway, "-p"]
    )
    if code == 0:
        return True, f"Route 10.0.0.0/8 → {gateway} added (persistent)."
    return False, f"route add failed: {err}"


def delete_intranet_route() -> tuple[bool, str]:
    code, _, err = run_cmd(["route", "delete", "10.0.0.0"])
    if code == 0:
        return True, "Route 10.0.0.0/8 removed."
    return False, f"route delete failed: {err}"

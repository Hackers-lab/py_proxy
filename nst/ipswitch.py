"""IP profile switching — apply a saved static/DHCP profile to an adapter.

Each profile stores: adapter name, mode (static|dhcp), IP, mask, gateway, DNS.
Applying a profile requires admin (netsh). Uses the same UAC-relaunch pattern
as nst.routing: the UI calls apply_profile() which elevates to --apply-profile.
"""

import socket
import subprocess

import psutil

from .netinfo import run_cmd
from .win_utils import (ELEVATION_CANCELLED, is_admin, run_elevated_and_wait,
                        self_relaunch_cmd)


def list_adapters() -> list[str]:
    """Return all non-loopback adapter names visible to psutil."""
    return [name for name, addrs in psutil.net_if_addrs().items()
            if not any(a.address.startswith("127.") for a in addrs
                       if a.family == socket.AF_INET)]


def get_adapter_current_ip(adapter: str) -> str:
    """Return the first IPv4 address on *adapter*, or empty string."""
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == adapter:
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("169.254."):
                    return a.address
    return ""


def is_dhcp_active(adapter: str) -> bool:
    """True if *adapter* is currently set to DHCP."""
    _, out, _ = run_cmd(["netsh", "interface", "ip", "show", "address", adapter])
    return "dhcp" in out.lower() and "yes" in out.lower()


def is_profile_active(adapter: str, mode: str, ip: str) -> bool:
    """True if the adapter currently matches the profile's expected state."""
    if not adapter:
        return False
    if mode == "dhcp":
        return is_dhcp_active(adapter)
    return get_adapter_current_ip(adapter) == ip if ip else False


# ── Privileged primitive (runs elevated via --apply-profile) ──────────────────

def _do_apply(adapter: str, mode: str, ip: str, mask: str,
              gateway: str, dns: str) -> int:
    if mode == "dhcp":
        run_cmd(["netsh", "interface", "ip", "set", "address", adapter, "dhcp"])
        run_cmd(["netsh", "interface", "ip", "set", "dns",     adapter, "dhcp"])
        return 0

    # Static
    run_cmd(["netsh", "interface", "ip", "set", "address",
             adapter, "static", ip, mask, gateway])
    servers = [s.strip() for s in dns.split(",") if s.strip()]
    if servers:
        run_cmd(["netsh", "interface", "ip", "set", "dns",
                 adapter, "static", servers[0], "primary"])
        for idx, srv in enumerate(servers[1:], start=2):
            run_cmd(["netsh", "interface", "ip", "add", "dns",
                     adapter, srv, f"index={idx}"])
    else:
        run_cmd(["netsh", "interface", "ip", "set", "dns", adapter, "dhcp"])
    return 0


# ── Public helper ─────────────────────────────────────────────────────────────

import sys   # noqa: E402


def apply_profile(adapter: str, mode: str, ip: str, mask: str,
                  gateway: str, dns: str) -> tuple[bool, str]:
    """Apply a profile. Elevates via UAC if needed. Returns (ok, message)."""
    if not adapter:
        return False, "No adapter configured for this profile."

    args = [adapter, mode, ip, mask, gateway, dns]
    if is_admin():
        code = _do_apply(*args)
    else:
        code = run_elevated_and_wait(
            self_relaunch_cmd() + ["--apply-profile"] + args)

    label = f"DHCP" if mode == "dhcp" else ip
    if code == 0:
        return True, f"Switched {adapter} → {label}"
    if code == ELEVATION_CANCELLED:
        return False, "Profile switch cancelled (admin required)."
    return False, f"Profile switch failed (exit {code})."

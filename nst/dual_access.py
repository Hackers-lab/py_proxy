"""Dual-access: LAN intranet + internet simultaneously over one cable.

Adds a secondary IP, an internet default route, the intranet /8 route (if
missing), two NRPT DNS rules and updated DNS search suffixes so both the
corporate intranet and the public internet are reachable at the same time.

All privileged operations are delegated to an elevated subprocess (same UAC
pattern as nst.routing). The public helpers below are called from the UI;
the _do_* primitives are called from the elevated CLI in net_tunnel.py.
"""

import socket
import subprocess
import winreg

import psutil

from .netinfo import run_cmd, is_valid_ipv4
from .win_utils import (ELEVATION_CANCELLED, is_admin, run_elevated_and_wait,
                        self_relaunch_cmd)

# ── Defaults (wbsedcl specific; user can override in config) ──────────────────
DEFAULT_DNS     = ["10.251.33.80", "10.251.33.90"]
DEFAULT_DOMAINS = ["wbsedcl.in", "wbsedcl.co.in"]
INTERNET_DNS    = "8.8.8.8"


# ── Adapter helpers ───────────────────────────────────────────────────────────

def get_adapter_for_ip(ip: str) -> str | None:
    """Return the Windows adapter name whose current IPv4 address is *ip*."""
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.address == ip:
                return iface
    return None


def _get_adapter_guid(adapter_name: str) -> str | None:
    """Look up the registry GUID for a named adapter."""
    net_key = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, net_key) as nk:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(nk, i); i += 1
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                            rf"{net_key}\{guid}\Connection") as ck:
                            name, _ = winreg.QueryValueEx(ck, "Name")
                            if name == adapter_name:
                                return guid
                    except OSError:
                        pass
                except OSError:
                    break
    except OSError:
        pass
    return None


def get_dhcp_cached_ip(adapter_name: str) -> str | None:
    """Return the last DHCP-assigned IP for *adapter_name* from the registry.

    Windows caches this even after the adapter is switched to static,
    so we can suggest the internet IP without any network disruption.
    """
    guid = _get_adapter_guid(adapter_name)
    if not guid:
        return None
    key = rf"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\{guid}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            ip, _ = winreg.QueryValueEx(k, "DhcpIPAddress")
            return ip if ip and ip not in ("0.0.0.0", "") else None
    except OSError:
        return None


def suggest_internet_ip(intranet_ip: str) -> str:
    """Return the most likely internet IP for the user, or empty string.

    Checks the DHCP cache first; falls back to any other non-intranet IP
    visible on any adapter.
    """
    adapter = get_adapter_for_ip(intranet_ip)
    if adapter:
        cached = get_dhcp_cached_ip(adapter)
        if cached and cached != intranet_ip:
            return cached
    # Secondary fallback: any non-intranet IPv4 on any adapter
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if (addr.family == socket.AF_INET
                    and not addr.address.startswith(("127.", "169.254."))
                    and addr.address != intranet_ip):
                return addr.address
    return ""



def get_adapter_dns_servers(adapter: str) -> list[str]:
    """Read DNS server IPs currently configured on *adapter* via netsh."""
    import re as _re
    _, out, _ = run_cmd(["netsh", "interface", "ip", "show", "dns", adapter])
    found = _re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', out)
    return found if found else DEFAULT_DNS


def get_adapter_dns_config(adapter: str) -> tuple[str, list[str]]:
    """Return the adapter's *current* DNS setup as (mode, servers).

    mode is "dhcp" (servers obtained automatically) or "static" (manually set).
    Captured before enabling dual access so it can be restored exactly on
    disable — corporate PCs usually have static intranet DNS we must not lose.
    """
    import re as _re
    _, out, _ = run_cmd(
        ["netsh", "interface", "ipv4", "show", "dnsservers", adapter])
    servers = _re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', out)
    mode = "static" if "statically configured" in out.lower() else "dhcp"
    return mode, servers


def get_adapter_gateway(adapter: str) -> str | None:
    """Read the default gateway IP configured on *adapter* via PowerShell, with netsh fallback."""
    try:
        r = subprocess.run(
            ["powershell", "-NonInteractive", "-NoProfile", "-Command",
             f"(Get-NetIPConfiguration -InterfaceAlias '{adapter}').IPv4DefaultGateway.NextHop"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        gw = r.stdout.decode(errors="replace").strip()
        if gw and is_valid_ipv4(gw):
            return gw
    except Exception:
        pass

    try:
        import re as _re
        _, out, _ = run_cmd(["netsh", "interface", "ipv4", "show", "config", f"name={adapter}"])
        m = _re.search(r"(?:Default Gateway|Gateway|Passerelle):\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out, _re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def get_adapter_ip_config(adapter: str) -> tuple[str, str, str, str]:
    """Return (mode, ip, mask, gateway) for the adapter's current configuration.

    mode is "dhcp" or "static".
    """
    import re as _re
    _, out, _ = run_cmd(["netsh", "interface", "ipv4", "show", "config", f"name={adapter}"])

    dhcp_enabled = True
    m_dhcp = _re.search(r"DHCP enabled:\s*(Yes|No)", out, _re.IGNORECASE)
    if m_dhcp:
        dhcp_enabled = (m_dhcp.group(1).lower() == "yes")

    mode = "dhcp" if dhcp_enabled else "static"

    ip = ""
    m_ip = _re.search(r"IP Address:\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out, _re.IGNORECASE)
    if m_ip:
        ip = m_ip.group(1)

    mask = "255.255.255.0"
    m_mask = _re.search(r"Subnet Prefix:.*mask\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out, _re.IGNORECASE)
    if m_mask:
        mask = m_mask.group(1)

    gateway = ""
    m_gw = _re.search(r"Default Gateway:\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", out, _re.IGNORECASE)
    if m_gw:
        gateway = m_gw.group(1)
    else:
        gateway = get_adapter_gateway(adapter) or ""

    return mode, ip, mask, gateway


# ── Status checks ─────────────────────────────────────────────────────────────

def check_secondary_ip(internet_ip: str) -> bool:
    """True if *internet_ip* is currently assigned to any local adapter."""
    for _, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.address == internet_ip:
                return True
    return False


def check_internet_route(internet_gw: str) -> bool:
    """True if a 0.0.0.0 default route via *internet_gw* exists."""
    _, out, _ = run_cmd(["route", "print", "0.0.0.0"])
    return internet_gw in out and "0.0.0.0" in out


def check_intranet_route() -> bool:
    """True if the 10.0.0.0/8 intranet route exists."""
    _, out, _ = run_cmd(["route", "print", "10.0.0.0"])
    return "255.0.0.0" in out and "10.0.0.0" in out


def check_nrpt(domain: str) -> bool:
    """True if an NRPT rule for *.domain already exists."""
    r = subprocess.run(
        ["powershell", "-NonInteractive", "-NoProfile", "-Command",
         f'Get-DnsClientNrptRule | Where-Object {{$_.Namespace -eq ".{domain}"}}'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return bool(r.stdout.strip())


def status(internet_ip: str, internet_gw: str, domains: list[str]) -> dict:
    """Return a dict of component → bool for the UI status labels."""
    return {
        "intranet_route": check_intranet_route(),
        "secondary_ip":   check_secondary_ip(internet_ip),
        "internet_route": check_internet_route(internet_gw),
        "nrpt":           any(check_nrpt(d) for d in domains),
    }


# ── Privileged primitives (run elevated) ──────────────────────────────────────

def _ps(command: str) -> int:
    r = subprocess.run(
        ["powershell", "-NonInteractive", "-NoProfile", "-Command", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return r.returncode


def _do_enable(intranet_gw: str, internet_ip: str, internet_gw: str,
               adapter: str, dns_csv: str, domain_csv: str,
               old_internet_ip: str = "", ip_mode: str = "dhcp",
               curr_ip: str = "", curr_mask: str = "") -> int:
    dns_servers = [d.strip() for d in dns_csv.split(",") if d.strip()]
    domains     = [d.strip() for d in domain_csv.split(",") if d.strip()]

    # 0. Convert to static if currently in DHCP mode
    # This prevents Windows from losing our primary intranet address
    # when we add the secondary IP.
    if ip_mode == "dhcp" and curr_ip:
        mask = curr_mask or "255.255.255.0"
        if intranet_gw:
            run_cmd(["netsh", "interface", "ip", "set", "address",
                     adapter, "static", curr_ip, mask, intranet_gw])
        else:
            run_cmd(["netsh", "interface", "ip", "set", "address",
                     adapter, "static", curr_ip, mask])

    # Remove a previously-bound secondary IP that differs from the new one,
    # so changing the internet IP doesn't leave stale addresses stacked on
    # the adapter (re-adding the same IP is harmless and needs no delete).
    old_internet_ip = (old_internet_ip or "").strip()
    if old_internet_ip and old_internet_ip != internet_ip:
        run_cmd(["netsh", "interface", "ip", "delete", "address",
                 adapter, old_internet_ip])
        run_cmd(["route", "delete", "0.0.0.0", "mask", "0.0.0.0",
                 _derive_gw(old_internet_ip)])

    # 1. Add secondary internet IP to the adapter
    run_cmd(["netsh", "interface", "ip", "add", "address",
             adapter, internet_ip, "255.255.255.0"])

    # 2. Add internet default route with a low metric so it wins over the
    #    adapter's existing default (e.g. the intranet gateway, which has no
    #    internet). Non-persistent — removed on disable/reboot.
    run_cmd(["route", "add", "0.0.0.0", "mask", "0.0.0.0",
             internet_gw, "metric", "5"])

    # 3. Add intranet route if not already present
    _, out, _ = run_cmd(["route", "print", "10.0.0.0"])
    if "255.0.0.0" not in out or "10.0.0.0" not in out:
        run_cmd(["route", "add", "10.0.0.0", "mask", "255.0.0.0",
                 intranet_gw, "-p"])

    # 4. Add NRPT rules — one per domain
    dns_str = ",".join(f'"{s}"' for s in dns_servers)
    for domain in domains:
        _ps(f'Add-DnsClientNrptRule -Namespace ".{domain}" '
            f'-NameServers {dns_str} -ErrorAction SilentlyContinue')

    # 5. Set adapter DNS: internet first, intranet as fallback
    run_cmd(["netsh", "interface", "ip", "set", "dns",
             adapter, "static", INTERNET_DNS, "primary"])
    for idx, srv in enumerate(dns_servers, start=2):
        run_cmd(["netsh", "interface", "ip", "add", "dns",
                 adapter, srv, f"index={idx}"])

    # Flush DNS Cache to apply changes immediately
    run_cmd(["ipconfig", "/flushdns"])

    return 0


def _do_disable(internet_ip: str, adapter: str, domain_csv: str,
                prev_dns_mode: str = "dhcp", prev_dns_csv: str = "",
                prev_ip_mode: str = "dhcp", prev_ip_address: str = "",
                prev_ip_mask: str = "", prev_ip_gateway: str = "") -> int:
    domains  = [d.strip() for d in domain_csv.split(",") if d.strip()]
    prev_dns = [d.strip() for d in prev_dns_csv.split(",") if d.strip()]

    # 1. Remove secondary IP
    run_cmd(["netsh", "interface", "ip", "delete", "address",
             adapter, internet_ip])

    # 2. Remove ONLY the internet default route we added (via the internet
    #    gateway). Deleting all 0.0.0.0 routes would also wipe the adapter's
    #    own default gateway and break connectivity until a reboot.
    internet_gw = _derive_gw(internet_ip)
    run_cmd(["route", "delete", "0.0.0.0", "mask", "0.0.0.0", internet_gw])

    # 2.5 Force delete the intranet route we added to prevent it from blackholing corporate LAN
    run_cmd(["route", "delete", "10.0.0.0", "mask", "255.0.0.0"])

    # 3. Remove NRPT rules
    for domain in domains:
        _ps(f'Remove-DnsClientNrptRule -Namespace ".{domain}" '
            f'-Force -ErrorAction SilentlyContinue')

    # 4. Restore IP address mode (DHCP vs Static)
    if prev_ip_mode == "dhcp":
        run_cmd(["netsh", "interface", "ip", "set", "address", adapter, "dhcp"])
    else:
        # If it was originally static, netsh interface ip delete address already removed the secondary IP,
        # but we can make sure the primary IP/gateway are correctly set just in case.
        if prev_ip_address:
            mask = prev_ip_mask or "255.255.255.0"
            if prev_ip_gateway:
                run_cmd(["netsh", "interface", "ip", "set", "address",
                         adapter, "static", prev_ip_address, mask, prev_ip_gateway])
            else:
                run_cmd(["netsh", "interface", "ip", "set", "address",
                         adapter, "static", prev_ip_address, mask])

    # 5. Restore DNS exactly as it was before dual access was enabled
    if prev_dns_mode == "static" and prev_dns:
        run_cmd(["netsh", "interface", "ip", "set", "dns",
                 adapter, "static", prev_dns[0], "primary"])
        for idx, srv in enumerate(prev_dns[1:], start=2):
            run_cmd(["netsh", "interface", "ip", "add", "dns",
                     adapter, srv, f"index={idx}"])
    else:
        run_cmd(["netsh", "interface", "ip", "set", "dns", adapter, "dhcp"])

    # Flush DNS Cache to restore correct resolution immediately
    run_cmd(["ipconfig", "/flushdns"])

    return 0


# ── Public helpers (call from UI; elevate on demand) ─────────────────────────

import sys   # noqa: E402  (after the primitives so circular-import risk is low)


def detect_internet_ip(intranet_ip: str) -> tuple[str, str]:
    """Return (ip, message) for the most likely internet IP.

    Reads the Windows DHCP cache for the adapter — instant, no network
    disruption. Works even when the adapter is currently set to static,
    as Windows keeps the last DHCP-assigned IP in the registry.
    """
    ip = suggest_internet_ip(intranet_ip)
    if ip:
        return ip, f"Internet IP auto-detected: {ip}"
    return "", "Could not auto-detect — enter the internet IP manually."


def enable_dual_access(intranet_ip: str, internet_ip: str,
                       domains: list[str]) -> tuple[bool, str]:
    adapter = get_adapter_for_ip(intranet_ip)
    if not adapter:
        return False, f"Cannot find adapter for intranet IP {intranet_ip}."

    # Get current IP configuration of the adapter
    ip_mode, curr_ip, curr_mask, curr_gw = get_adapter_ip_config(adapter)

    # Remember original DNS and IP setups so disable can restore them exactly.
    # Only capture it the first time (when dual access isn't already on) so a
    # re-enable doesn't overwrite the real previous DNS with our own settings.
    from . import config
    old_internet_ip = config.load_dual_active_ip()
    if not old_internet_ip:
        config.save_dual_prev_ip(ip_mode, curr_ip, curr_mask, curr_gw)
        prev_dns_mode, prev_dns_servers = get_adapter_dns_config(adapter)
        config.save_dual_prev_dns(prev_dns_mode, prev_dns_servers)

    intranet_gw = curr_gw or _derive_gw(intranet_ip)
    internet_gw = _derive_gw(internet_ip)
    # Read DNS already configured on this adapter; fall back to built-in defaults
    dns_servers  = get_adapter_dns_servers(adapter) or DEFAULT_DNS
    dns_csv      = ",".join(dns_servers)
    domain_csv   = ",".join(domains)

    args = [intranet_gw, internet_ip, internet_gw, adapter, dns_csv, domain_csv,
            old_internet_ip, ip_mode, curr_ip, curr_mask]
    if is_admin():
        code = _do_enable(*args)
    else:
        code = run_elevated_and_wait(
            self_relaunch_cmd() + ["--dual-enable"] + args)

    if code == 0:
        config.save_dual_active_ip(internet_ip)
        return True, (f"Dual access enabled — internet via {internet_gw}, "
                      f"intranet via {intranet_gw}.")
    if code == ELEVATION_CANCELLED:
        return False, "Dual access cancelled (administrator approval required)."
    return False, f"Dual access enable failed (exit {code})."


def disable_dual_access(intranet_ip: str, internet_ip: str,
                        domains: list[str]) -> tuple[bool, str]:
    adapter = get_adapter_for_ip(intranet_ip)
    if not adapter:
        return False, f"Cannot find adapter for intranet IP {intranet_ip}."

    domain_csv = ",".join(domains)
    from . import config
    # Remove the IP actually bound at enable time, not whatever is currently in
    # the edit field — they differ if the user edited it after enabling.
    active_ip = config.load_dual_active_ip() or internet_ip
    # Restore the DNS setup captured when dual access was enabled
    prev_dns_mode, prev_dns_servers = config.load_dual_prev_dns()
    prev_dns_csv = ",".join(prev_dns_servers)
    # Restore the IP configuration setup captured when dual access was enabled
    prev_ip_mode, prev_ip_address, prev_ip_mask, prev_ip_gateway = config.load_dual_prev_ip()
    args = [active_ip, adapter, domain_csv, prev_dns_mode, prev_dns_csv,
            prev_ip_mode, prev_ip_address, prev_ip_mask, prev_ip_gateway]
    if is_admin():
        code = _do_disable(*args)
    else:
        code = run_elevated_and_wait(
            self_relaunch_cmd() + ["--dual-disable"] + args)

    if code == 0:
        config.save_dual_active_ip("")
        return True, "Dual access disabled — intranet only."
    if code == ELEVATION_CANCELLED:
        return False, "Dual access disable cancelled."
    return False, f"Dual access disable failed (exit {code})."


def _derive_gw(ip: str) -> str:
    parts = ip.split(".")
    parts[-1] = "1"
    return ".".join(parts)

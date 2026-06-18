"""Network information helpers: local IP detection, silent command runner,
connectivity checks and human-readable speed formatting."""

import socket
import struct
import subprocess

import psutil


def is_private_ipv4(ip: str) -> bool:
    """True for RFC-1918 private ranges: 10/8, 172.16–31/12, 192.168/16.

    Discovery and cross-subnet chat work on any of these, not just 10.x.
    """
    if not ip or not is_valid_ipv4(ip):
        return False
    a, b, *_ = (int(p) for p in ip.split("."))
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def list_local_ipv4() -> list[tuple[str, str, str]]:
    """Return ``[(iface, ip, netmask), …]`` for every usable IPv4 interface.

    Loopback (127.x) and link-local (169.254.x) addresses are skipped. No
    assumption is made about which private range the network uses.
    """
    out: list[tuple[str, str, str]] = []
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address
                if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                out.append((iface, ip, addr.netmask or "255.255.255.0"))
    except Exception:
        pass
    return out


def get_all_local_ips() -> list[str]:
    """All local private IPv4 addresses (every interface). Used by LAN chat."""
    ips = [ip for _iface, ip, _mask in list_local_ipv4() if is_private_ipv4(ip)]
    if not ips:
        # No private address — fall back to whatever we can find.
        ips = [ip for _iface, ip, _mask in list_local_ipv4()]
    # De-dupe, preserve order.
    return list(dict.fromkeys(ips))


def get_intranet_ip() -> str | None:
    """Return a private intranet IPv4 address (fast, no DNS lookup).

    Prefers 10.x (the corporate range the proxy split-tunnel route targets),
    then falls back to any other private range so the proxy panel still shows
    a sensible address on 172.16/12 or 192.168/16 networks.
    """
    privs = get_all_local_ips()
    for ip in privs:
        if ip.startswith("10."):
            return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip.startswith("10."):
                return ip
    except Exception:
        pass
    return privs[0] if privs else None


def get_local_ip() -> str | None:
    """Return the primary LAN IPv4 address for chat.

    Prefers 10.x (corporate/school LANs), then 172.16.x, then 192.168.x.
    Does NOT use the connect-to-internet trick: on machines with both a LAN
    adapter and a hotspot/VPN the OS routes internet traffic via the secondary
    interface, so that trick returns the hotspot IP instead of the LAN IP.
    """
    privs = get_all_local_ips()
    for ip in privs:
        if ip.startswith("10."):
            return ip
    for ip in privs:
        parts = ip.split(".")
        if parts[0] == "172" and 16 <= int(parts[1]) <= 31:
            return ip
    return privs[0] if privs else None


def get_my_broadcast(my_ip: str) -> str:
    """Return the subnet broadcast address for the interface that holds *my_ip*.

    Used so the chat broadcaster only advertises on the LAN the user is actually
    on, not on every adapter (hotspot, VPN, etc.) that happens to be up.
    """
    try:
        my_int = struct.unpack("!I", socket.inet_aton(my_ip))[0]
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET or addr.address != my_ip:
                    continue
                netmask = addr.netmask or "255.255.255.0"
                mask_int = struct.unpack("!I", socket.inet_aton(netmask))[0]
                bcast_int = my_int | (~mask_int & 0xFFFFFFFF)
                return socket.inet_ntoa(struct.pack("!I", bcast_int))
    except Exception:
        pass
    return "255.255.255.255"


def calculate_gateway(ip: str) -> str:
    parts = ip.split(".")
    parts[-1] = "1"
    return ".".join(parts)


def run_cmd(args: list[str]) -> tuple[int, str, str]:
    """Run silently — no console window, no shell. Returns (code, stdout, stderr)."""
    r = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return (r.returncode,
            r.stdout.decode(errors="replace").strip(),
            r.stderr.decode(errors="replace").strip())


def check_internet_connection() -> bool:
    """True if a public DNS server is reachable."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect(("8.8.8.8", 53))
            return True
    except Exception:
        return False


def check_host_reachable(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=1.5):
            return True
    except Exception:
        return False


def check_internet_via_proxy(proxy_host: str, proxy_port: int) -> bool:
    """Verify the proxy can tunnel to a public host via a CONNECT request."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((proxy_host, proxy_port))
            s.sendall(b"CONNECT 8.8.8.8:53 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            return b"200" in resp
    except Exception:
        return False


def is_valid_ipv4(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(
        p.isdigit() and 0 <= int(p) <= 255 for p in parts
    )


def format_speed(bps: float) -> str:
    """Full label, e.g. '12.3 KB/s'."""
    if bps < 1024:
        return f"{bps:.1f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.1f} MB/s"

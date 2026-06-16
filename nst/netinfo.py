"""Network information helpers: local IP detection, silent command runner,
connectivity checks and human-readable speed formatting."""

import socket
import struct
import subprocess

import psutil


def get_intranet_ip() -> str | None:
    """Return the first 10.x.x.x address on this host (fast, no DNS lookup)."""
    try:
        for _interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address.startswith("10."):
                    return addr.address
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip.startswith("10."):
                return ip
    except Exception:
        pass
    return None


def get_subnet_broadcasts() -> list[str]:
    """Return subnet broadcast addresses for every 10.x.x.x interface.

    Uses the actual netmask from ``psutil`` so both /16 and /24 networks
    are handled correctly.  Falls back to 255.255.255.255 on error.
    """
    results: list[str] = []
    try:
        for _iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                if not addr.address.startswith("10."):
                    continue
                netmask = addr.netmask
                if not netmask:
                    continue
                # ip | ~mask  →  broadcast
                ip_int = struct.unpack("!I", socket.inet_aton(addr.address))[0]
                mask_int = struct.unpack("!I", socket.inet_aton(netmask))[0]
                bcast_int = ip_int | (~mask_int & 0xFFFFFFFF)
                bcast = socket.inet_ntoa(struct.pack("!I", bcast_int))
                if bcast not in results:
                    results.append(bcast)
    except Exception:
        pass
    if not results:
        results.append("255.255.255.255")
    return results


def get_local_ip() -> str | None:
    """Return the primary outbound IPv4 address (any subnet). Used by LAN chat."""
    ip = get_intranet_ip()
    if ip:
        return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


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


def format_speed_short(bps: float) -> str:
    """Compact label for the tray icon, e.g. '12K' / '1.2M'."""
    if bps < 1024:
        return f"{int(bps)}"
    if bps < 1024 * 1024:
        kb = bps / 1024
        return f"{kb:.1f}K" if kb < 10 else f"{int(kb)}K"
    mb = bps / (1024 * 1024)
    return f"{mb:.1f}M" if mb < 10 else f"{int(mb)}M"

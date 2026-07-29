import select
import socket
import threading
import time
from collections import deque
from . import config
from .constants import BUFFER_SIZE, CONN_TIMEOUT, PROXY_PORT

# Thread-safe client connection and link tracking data structures
_lock = threading.Lock()

# client_ip -> dict(first_seen, last_seen, active_conns, bytes_transferred)
_connected_clients: dict[str, dict] = {}

# Deque of dicts: timestamp, client_ip, method, host, path, blocked, bytes
_MAX_LINK_LOGS = 500
_LOG_TTL_SECONDS = 24 * 60 * 60  # 1 day
_link_logs: deque = deque(maxlen=_MAX_LINK_LOGS)


def _prune_old_logs() -> None:
    now = time.time()
    global _link_logs
    _link_logs = deque([log for log in _link_logs if now - log["timestamp"] < _LOG_TTL_SECONDS], maxlen=_MAX_LINK_LOGS)


def get_active_clients() -> list[dict]:
    """Return summary of all client IPs that have connected to the proxy."""
    with _lock:
        clients = []
        for ip, info in _connected_clients.items():
            clients.append({
                "ip": ip,
                "first_seen": info["first_seen"],
                "last_seen": info["last_seen"],
                "active_conns": info["active_conns"],
                "bytes": info["bytes"],
            })
        return clients


def get_link_logs() -> list[dict]:
    """Return copy of real-time link inspection logs."""
    with _lock:
        _prune_old_logs()
        return list(_link_logs)


def clear_link_logs() -> None:
    """Clear in-memory link inspection logs."""
    with _lock:
        _link_logs.clear()


def _is_ip_allowed(client_ip: str) -> bool:
    """Check IP against Whitelist and Blacklist."""
    blocked_ips = config.load_proxy_blocked_ips()
    if client_ip in blocked_ips:
        return False

    allowed_ips = config.load_proxy_allowed_ips()
    if allowed_ips and client_ip not in allowed_ips:
        return False

    return True


def _is_domain_blocked(host: str) -> bool:
    """Check if target host matches blocked domain list (e.g. youtube.com, instagram.com)."""
    blocked_domains = config.load_proxy_blocked_domains()
    host_lower = host.lower().split(":")[0]
    for domain in blocked_domains:
        domain_lower = domain.lower()
        if host_lower == domain_lower or host_lower.endswith("." + domain_lower):
            return True
    return False


def _pipe_bidirectional(s1: socket.socket, s2: socket.socket, tracker: dict | None = None) -> None:
    """Pump data bidirectionally between s1 and s2 in a single thread using select."""
    s1.setblocking(False)
    s2.setblocking(False)
    sockets = [s1, s2]
    bytes_transferred = 0
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], CONN_TIMEOUT)
            if not readable:
                break
            for src in readable:
                dst = s2 if src is s1 else s1
                try:
                    chunk = src.recv(BUFFER_SIZE)
                except (socket.error, OSError):
                    chunk = b""
                if not chunk:
                    return
                dst.sendall(chunk)
                bytes_transferred += len(chunk)
    except Exception:
        pass
    finally:
        if tracker is not None and bytes_transferred > 0:
            with _lock:
                tracker["bytes"] += bytes_transferred
        for s in (s1, s2):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def _handle_client(client: socket.socket, client_ip: str) -> None:
    # 1. IP ACL Check
    if not _is_ip_allowed(client_ip):
        try:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nAccess Denied: Your IP is not authorized to use this proxy.\r\n")
            client.close()
        except Exception:
            pass
        return

    # Track active client
    now = time.time()
    with _lock:
        if client_ip not in _connected_clients:
            _connected_clients[client_ip] = {
                "first_seen": now,
                "last_seen": now,
                "active_conns": 1,
                "bytes": 0,
            }
        else:
            _connected_clients[client_ip]["last_seen"] = now
            _connected_clients[client_ip]["active_conns"] += 1

    client_tracker = _connected_clients[client_ip]

    try:
        client.settimeout(CONN_TIMEOUT)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = client.recv(4096)
            if not chunk:
                return
            raw += chunk

        first = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first.split()
        if len(parts) < 3:
            return
        method, url = parts[0].upper(), parts[1]

        if method == "CONNECT":
            hp = url.rsplit(":", 1)
            host, port = hp[0], int(hp[1]) if len(hp) > 1 else 443
            path = "/"
        else:
            stripped = url[7:] if url.startswith("http://") else url
            idx = stripped.find("/")
            host_part = stripped[:idx] if idx != -1 else stripped
            path = stripped[idx:] if idx != -1 else "/"
            hp2 = host_part.rsplit(":", 1)
            host, port = hp2[0], int(hp2[1]) if len(hp2) > 1 else 80

        # 2. Domain Block Check
        is_blocked = _is_domain_blocked(host)

        # Log link if tracking enabled
        if config.load_proxy_link_tracking():
            with _lock:
                _link_logs.appendleft({
                    "timestamp": time.time(),
                    "client_ip": client_ip,
                    "method": method,
                    "host": host,
                    "path": path,
                    "blocked": is_blocked,
                    "bytes": len(raw),
                })

        if is_blocked:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nBlocked: Access to this website is restricted by proxy admin.\r\n")
            return

        # 3. Forward request to remote host
        if method == "CONNECT":
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            lines = raw.split(b"\r\n")
            lines[0] = f"{method} {path} HTTP/1.1".encode()
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            remote.sendall(b"\r\n".join(lines))

        _pipe_bidirectional(client, remote, client_tracker)
    except Exception:
        pass
    finally:
        with _lock:
            if client_ip in _connected_clients:
                _connected_clients[client_ip]["active_conns"] = max(0, _connected_clients[client_ip]["active_conns"] - 1)
                _connected_clients[client_ip]["last_seen"] = time.time()
        try:
            client.close()
        except Exception:
            pass


class ProxyServer:
    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self.running = False

    def start(self) -> tuple[bool, str]:
        if self.running:
            return False, "Proxy already running."
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", PROXY_PORT))
            self._sock.listen(256)
            self._sock.settimeout(1.0)
            self.running = True
            threading.Thread(target=self._loop, daemon=True).start()
            return True, f"Proxy listening on 0.0.0.0:{PROXY_PORT}."
        except Exception as exc:
            return False, f"Failed to start proxy: {exc}"

    def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Proxy is not running."
        self.running = False
        try:
            self._sock.close()
        except Exception:
            pass
        return True, "Proxy stopped."

    def _loop(self) -> None:
        while self.running:
            try:
                client, addr = self._sock.accept()
                client_ip = addr[0]
                threading.Thread(target=_handle_client, args=(client, client_ip),
                                 daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

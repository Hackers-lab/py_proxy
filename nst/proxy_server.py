"""A minimal forwarding HTTP/HTTPS proxy (host side)."""

import socket
import threading

from .constants import BUFFER_SIZE, CONN_TIMEOUT, PROXY_PORT


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(BUFFER_SIZE):
            dst.sendall(chunk)
    except Exception:
        pass
    for s in (src, dst):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


def _handle_client(client: socket.socket) -> None:
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
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            stripped = url[7:] if url.startswith("http://") else url
            idx = stripped.find("/")
            host_part = stripped[:idx] if idx != -1 else stripped
            path = stripped[idx:] if idx != -1 else "/"
            hp2 = host_part.rsplit(":", 1)
            host, port = hp2[0], int(hp2[1]) if len(hp2) > 1 else 80
            lines = raw.split(b"\r\n")
            lines[0] = f"{method} {path} HTTP/1.1".encode()
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            remote.sendall(b"\r\n".join(lines))

        t1 = threading.Thread(target=_pipe, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(remote, client), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
    except Exception:
        pass
    finally:
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
                client, _ = self._sock.accept()
                threading.Thread(target=_handle_client, args=(client,),
                                 daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

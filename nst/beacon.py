"""Host discovery for the proxy feature.

The host broadcasts a small UDP beacon so clients can auto-detect its IP and
internet status without manual entry.
"""

import socket
import threading
import time

from .constants import BEACON_MAGIC, BEACON_PORT


class HostBeacon:
    """Broadcasts ``BEACON_MAGIC|ip|internet`` on UDP every 2 s."""

    def __init__(self, get_internet_status_cb) -> None:
        self.running = False
        self._ip: str = ""
        self._get_internet_status = get_internet_status_cb

    def start(self, ip: str) -> None:
        self._ip = ip
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    @property
    def ip(self) -> str:
        return self._ip

    @ip.setter
    def ip(self, value: str) -> None:
        self._ip = value

    def _loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.0)
            while self.running:
                try:
                    internet_status = "1" if self._get_internet_status() else "0"
                    payload = (BEACON_MAGIC + b"|" + self._ip.encode()
                               + b"|" + internet_status.encode())
                    s.sendto(payload, ("<broadcast>", BEACON_PORT))
                except Exception:
                    pass
                time.sleep(2)
            s.close()
        except Exception:
            pass


class ClientScanner:
    """Listens for host beacons; calls ``callback(ip, has_internet)`` on each."""

    def __init__(self, callback) -> None:
        self._cb = callback
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", BEACON_PORT))
            s.settimeout(1.0)
            while self.running:
                try:
                    data, _addr = s.recvfrom(256)
                    parts = data.split(b"|")
                    if len(parts) >= 2 and parts[0] == BEACON_MAGIC:
                        ip = parts[1].decode(errors="replace")
                        has_internet = True
                        if len(parts) >= 3:
                            has_internet = (parts[2] == b"1")
                        self._cb(ip, has_internet)
                except socket.timeout:
                    continue
                except Exception:
                    continue
            s.close()
        except Exception:
            pass

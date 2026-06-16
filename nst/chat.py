"""LAN chat: peer presence discovery + point-to-point text messaging.

Design mirrors :mod:`nst.beacon`:

* a **presence broadcaster** announces ``CHAT_MAGIC|name|ip`` over UDP so every
  app on the subnet learns who is online and what they call themselves;
* a **presence listener** maintains a roster of peers, expiring silent ones;
* a **TCP server** receives one JSON message per connection;
* :meth:`ChatService.send` opens a short-lived TCP connection to deliver a message.

Only PCs running this app appear in the roster. All threads are daemons with
socket timeouts so shutdown is clean. History is kept by the UI, not here.
"""

import json
import socket
import threading
import time
from dataclasses import dataclass

from .constants import (
    CHAT_MAGIC,
    CHAT_PEER_TIMEOUT,
    CHAT_PRESENCE_EVERY,
    CHAT_PRESENCE_PORT,
    CHAT_TCP_PORT,
)
from .netinfo import get_local_ip


@dataclass
class Peer:
    ip: str
    name: str
    last_seen: float


class ChatService:
    def __init__(self, my_name: str,
                 on_roster_change=None,
                 on_message=None) -> None:
        """
        on_roster_change(peers: list[Peer]) -- called when the roster changes.
        on_message(ip, name, text, ts)      -- called on an incoming message.
        Both are invoked from background threads; the UI must marshal to the
        main thread (e.g. via ``Tk.after``).
        """
        self.my_name = my_name
        self.my_ip: str = get_local_ip() or "127.0.0.1"
        self._on_roster_change = on_roster_change
        self._on_message = on_message

        self._peers: dict[str, Peer] = {}
        self._virtual: dict[str, "DemoBot"] = {}   # ip -> bot (demo / loopback)
        self._lock = threading.Lock()
        self.running = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.my_ip = get_local_ip() or self.my_ip
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._presence_loop, daemon=True).start()
        threading.Thread(target=self._server_loop, daemon=True).start()
        threading.Thread(target=self._reaper_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def set_name(self, name: str) -> None:
        self.my_name = name  # picked up on the next presence broadcast

    def peers(self) -> list[Peer]:
        with self._lock:
            merged = list(self._peers.values())
            merged += [b.peer for b in self._virtual.values()]
        return sorted(merged, key=lambda p: p.name.lower())

    # ── demo / virtual peers ──────────────────────────────────────────────────
    def add_demo_bot(self) -> "DemoBot":
        """Add a simulated peer so the chat UX can be tried on a single PC.

        The bot greets you (triggering a notification) and auto-replies to your
        messages. It bypasses the network entirely.
        """
        bot = DemoBot(self)
        with self._lock:
            self._virtual[bot.peer.ip] = bot
        self._emit_roster()
        bot.greet()
        return bot

    def remove_demo_bots(self) -> None:
        with self._lock:
            had = bool(self._virtual)
            self._virtual.clear()
        if had:
            self._emit_roster()

    def has_demo(self) -> bool:
        return bool(self._virtual)

    # ── presence: outgoing ────────────────────────────────────────────────────
    def _broadcast_loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.0)
            while self.running:
                try:
                    payload = (CHAT_MAGIC + b"|" + self.my_name.encode("utf-8")
                               + b"|" + self.my_ip.encode("utf-8"))
                    s.sendto(payload, ("<broadcast>", CHAT_PRESENCE_PORT))
                except Exception:
                    pass
                time.sleep(CHAT_PRESENCE_EVERY)
            s.close()
        except Exception:
            pass

    # ── presence: incoming ────────────────────────────────────────────────────
    def _presence_loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", CHAT_PRESENCE_PORT))
            s.settimeout(1.0)
            while self.running:
                try:
                    data, _addr = s.recvfrom(512)
                    parts = data.split(b"|", 2)
                    if len(parts) != 3 or parts[0] != CHAT_MAGIC:
                        continue
                    name = parts[1].decode("utf-8", errors="replace").strip()
                    ip = parts[2].decode("utf-8", errors="replace").strip()
                    if not ip or ip == self.my_ip:
                        continue  # ignore self
                    self._touch_peer(ip, name)
                except socket.timeout:
                    continue
                except Exception:
                    continue
            s.close()
        except Exception:
            pass

    def _touch_peer(self, ip: str, name: str) -> None:
        changed = False
        with self._lock:
            existing = self._peers.get(ip)
            if existing is None or existing.name != name:
                changed = True
            self._peers[ip] = Peer(ip=ip, name=name or ip, last_seen=time.time())
        if changed:
            self._emit_roster()

    def _reaper_loop(self) -> None:
        while self.running:
            time.sleep(2)
            now = time.time()
            dropped = False
            with self._lock:
                stale = [ip for ip, p in self._peers.items()
                         if now - p.last_seen > CHAT_PEER_TIMEOUT]
                for ip in stale:
                    del self._peers[ip]
                    dropped = True
            if dropped:
                self._emit_roster()

    def _emit_roster(self) -> None:
        if self._on_roster_change:
            try:
                self._on_roster_change(self.peers())
            except Exception:
                pass

    # ── messaging: incoming ───────────────────────────────────────────────────
    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # No SO_REUSEADDR: on Windows it lets another process co-bind and
            # hijack connections. Single-instance is enforced elsewhere, so an
            # exclusive bind correctly surfaces a genuine port conflict instead
            # of silently swallowing incoming messages.
            srv.bind(("0.0.0.0", CHAT_TCP_PORT))
            srv.listen(16)
            srv.settimeout(1.0)
            while self.running:
                try:
                    conn, addr = srv.accept()
                    threading.Thread(target=self._handle_conn,
                                     args=(conn, addr), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break
            srv.close()
        except Exception:
            pass

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            msg = json.loads(line.decode("utf-8", errors="replace"))
            text = str(msg.get("text", ""))
            name = str(msg.get("from_name", "")) or addr[0]
            ip = str(msg.get("from_ip", "")) or addr[0]
            ts = float(msg.get("ts", time.time()))
            # Refresh roster from the sender even if no presence yet.
            self._touch_peer(ip, name)
            if text and self._on_message:
                self._on_message(ip, name, text, ts)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── messaging: outgoing ───────────────────────────────────────────────────
    def send(self, ip: str, text: str) -> bool:
        """Deliver a message synchronously. Returns True on success.

        Call from a worker thread to avoid blocking the UI.
        """
        bot = self._virtual.get(ip)
        if bot is not None:
            bot.on_user_message(text)
            return True

        payload = json.dumps({
            "from_name": self.my_name,
            "from_ip": self.my_ip,
            "text": text,
            "ts": time.time(),
        }).encode("utf-8") + b"\n"
        try:
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=3.0) as s:
                s.sendall(payload)
            return True
        except Exception:
            return False


class DemoBot:
    """A scripted local peer used by the in-app chat demo (no networking)."""

    IP = "demo.local"
    NAME = "Demo Bot 🤖"

    _REPLIES = [
        "Nice — that's exactly how chatting works here! 🎉",
        "Every PC running this app shows up in the list on the left.",
        "Try renaming yourself with the box above the peer list.",
        "Messages you send appear on the right in blue bubbles.",
        "When a message arrives on another chat, a toast pops up bottom-right.",
        "On a real LAN, open this app on a second PC and it'll appear here.",
        "Got it 👍",
    ]

    def __init__(self, service: "ChatService") -> None:
        self.service = service
        self.peer = Peer(ip=self.IP, name=self.NAME, last_seen=time.time())
        self._i = 0

    def _say(self, text: str, delay: float) -> None:
        def fire():
            if self.service._on_message and self.IP in self.service._virtual:
                self.service._on_message(self.IP, self.NAME, text, time.time())
        threading.Timer(delay, fire).start()

    def greet(self) -> None:
        self._say("👋 Hi! I'm a demo peer. Send me a message to see chat in action.", 1.2)

    def on_user_message(self, _text: str) -> None:
        reply = self._REPLIES[self._i % len(self._REPLIES)]
        self._i += 1
        self._say(reply, 0.9)

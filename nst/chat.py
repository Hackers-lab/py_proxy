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
import struct
import threading
import time
from dataclasses import dataclass

import psutil

from .constants import (
    CHAT_MAGIC,
    CHAT_PEER_TIMEOUT,
    CHAT_PRESENCE_EVERY,
    CHAT_PRESENCE_PORT,
    CHAT_TCP_PORT,
)
from .netinfo import get_local_ip, get_subnet_broadcasts


@dataclass
class Peer:
    ip: str
    name: str
    last_seen: float


class ChatService:
    def __init__(self, my_name: str,
                 on_roster_change=None,
                 on_message=None,
                 on_file_offer=None,
                 on_file_accept=None,
                 on_file_reject=None,
                 on_chat_request=None) -> None:
        """
        on_roster_change(peers: list[Peer])          -- roster changed.
        on_message(ip, name, text, ts)               -- incoming chat message.
        on_file_offer(ip, name, msg_dict)            -- incoming file offer.
        on_file_accept(ip, name, msg_dict)           -- peer accepted our offer.
        on_file_reject(ip, name, msg_dict)           -- peer rejected our offer.
        on_chat_request(ip, name, msg_dict)          -- first message from unknown external IP.
        All callbacks are invoked from background threads; marshal to main thread.
        """
        self.my_name = my_name
        self.my_ip: str = get_local_ip() or "127.0.0.1"
        self._on_roster_change = on_roster_change
        self._on_message = on_message
        self._on_file_offer = on_file_offer
        self._on_file_accept = on_file_accept
        self._on_file_reject = on_file_reject
        self._on_chat_request = on_chat_request

        # IP chat access control
        self.ip_chat_enabled: bool = True
        self._approved_ips: set[str] = set()   # approved external IPs
        self._blocked_ips: set[str] = set()    # permanently blocked IPs
        self._pending_requests: dict[str, list[dict]] = {}  # buffered msgs awaiting approval

        self._peers: dict[str, Peer] = {}
        self._virtual: dict[str, "DemoBot"] = {}   # ip -> bot (demo / loopback)
        self._manual: set[str] = set()  # IPs added manually (never reaped)
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
        threading.Thread(target=self._manual_probe_loop, daemon=True).start()

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

    # ── manual peers (cross-subnet) ──────────────────────────────────────────
    def add_manual_peer(self, ip: str) -> None:
        """Register a peer by IP for cross-subnet chat.

        The peer is added with its IP as the display name (updated once a
        message is received), and is exempt from the reaper.
        We also auto-approve the IP since the user explicitly initiated contact.
        """
        with self._lock:
            self._manual.add(ip)
            self._approved_ips.add(ip)   # user initiated — auto-approve their replies
            if ip not in self._peers:
                self._peers[ip] = Peer(ip=ip, name=ip, last_seen=0.0)
        self._emit_roster()
        threading.Thread(target=self._probe_one_manual, args=(ip,), daemon=True).start()

    def approve_ip(self, ip: str) -> None:
        """Approve an external IP and deliver any buffered messages."""
        with self._lock:
            self._approved_ips.add(ip)
            self._blocked_ips.discard(ip)
            pending = self._pending_requests.pop(ip, [])
        for msg in pending:
            self._dispatch_msg(msg, ip, msg.get("from_name", ip))

    def block_ip(self, ip: str) -> None:
        """Block an external IP and discard buffered messages."""
        with self._lock:
            self._blocked_ips.add(ip)
            self._approved_ips.discard(ip)
            self._pending_requests.pop(ip, None)

    def _is_same_subnet(self, remote_ip: str) -> bool:
        """True if remote_ip shares a subnet with any local interface."""
        try:
            remote_int = struct.unpack("!I", socket.inet_aton(remote_ip))[0]
            for _iface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family != socket.AF_INET or not addr.netmask:
                        continue
                    ip_int = struct.unpack("!I", socket.inet_aton(addr.address))[0]
                    mask_int = struct.unpack("!I", socket.inet_aton(addr.netmask))[0]
                    if (ip_int & mask_int) == (remote_int & mask_int):
                        return True
        except Exception:
            pass
        return False

    def is_manual_peer(self, ip: str) -> bool:
        """True if *ip* was added manually (not auto-discovered)."""
        return ip in self._manual

    def is_peer_online(self, ip: str) -> bool:
        """True if the peer is within the timeout window of last being seen."""
        if ip == DemoBot.IP:
            return True
        with self._lock:
            p = self._peers.get(ip)
            if p is None:
                return False
            return (time.time() - p.last_seen) <= CHAT_PEER_TIMEOUT

    def _manual_probe_loop(self) -> None:
        """Periodically check if manual peers are reachable."""
        while self.running:
            with self._lock:
                ips = list(self._manual)
            for ip in ips:
                threading.Thread(target=self._probe_one_manual, args=(ip,), daemon=True).start()
            time.sleep(5)

    def _probe_one_manual(self, ip: str) -> None:
        """Probes a manual IP to verify if the app's chat service is running."""
        try:
            # Try connecting to the peer's CHAT_TCP_PORT
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=1.0):
                with self._lock:
                    peer = self._peers.get(ip)
                    name = peer.name if peer else ip
                self._touch_peer(ip, name)
        except Exception:
            # Connection failed. We don't touch their last_seen, so they will stay/become offline.
            pass

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
                    # Send to the generic broadcast *and* every subnet-specific
                    # broadcast address.  This ensures peers see us even when
                    # an extra 10.0.0.0/8 route alters default broadcast.
                    targets = {"<broadcast>", "255.255.255.255"}
                    targets.update(get_subnet_broadcasts())
                    for addr in targets:
                        try:
                            s.sendto(payload, (addr, CHAT_PRESENCE_PORT))
                        except Exception:
                            pass
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
            now = time.time()
            was_online = (existing is not None
                          and (now - existing.last_seen) <= CHAT_PEER_TIMEOUT)
            # Emit roster when peer is new, name changed, or was offline (coming online)
            if existing is None or existing.name != name or not was_online:
                changed = True
            self._peers[ip] = Peer(ip=ip, name=name or ip, last_seen=now)
        if changed:
            self._emit_roster()

    def _reaper_loop(self) -> None:
        while self.running:
            time.sleep(2)
            now = time.time()
            dropped = False
            with self._lock:
                stale = [ip for ip, p in self._peers.items()
                         if now - p.last_seen > CHAT_PEER_TIMEOUT
                         and ip not in self._manual]
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
            name = str(msg.get("from_name", "")) or addr[0]
            ip = str(msg.get("from_ip", "")) or addr[0]
            msg_type = msg.get("type", "chat")

            # file_accept / file_reject are responses to OUR offers — always trusted
            if msg_type not in ("file_accept", "file_reject"):
                if not self._is_same_subnet(ip) and ip not in self._approved_ips:
                    if not self.ip_chat_enabled or ip in self._blocked_ips:
                        return  # silently drop
                    # First contact from external IP — buffer and request approval
                    with self._lock:
                        self._pending_requests.setdefault(ip, []).append(msg)
                    if self._on_chat_request:
                        self._on_chat_request(ip, name, msg)
                    return

            self._dispatch_msg(msg, ip, name)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch_msg(self, msg: dict, ip: str, name: str) -> None:
        """Deliver a pre-approved message to the appropriate callback."""
        msg_type = msg.get("type", "chat")
        if msg_type == "file_offer":
            self._touch_peer(ip, name)
            if self._on_file_offer:
                self._on_file_offer(ip, name, msg)
        elif msg_type == "file_accept":
            if self._on_file_accept:
                self._on_file_accept(ip, name, msg)
        elif msg_type == "file_reject":
            if self._on_file_reject:
                self._on_file_reject(ip, name, msg)
        else:
            text = str(msg.get("text", ""))
            ts = float(msg.get("ts", time.time()))
            self._touch_peer(ip, name)
            if text and self._on_message:
                self._on_message(ip, name, text, ts)

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

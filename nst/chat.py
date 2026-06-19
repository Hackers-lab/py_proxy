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
import uuid
from dataclasses import dataclass

import psutil

from . import config
from .constants import (
    CHAT_AWAY_AFTER,
    CHAT_MAGIC,
    CHAT_PEER_TIMEOUT,
    CHAT_PRESENCE_EVERY,
    CHAT_PRESENCE_PORT,
    CHAT_TCP_PORT,
)
from .netinfo import get_all_local_ips, get_local_ip, get_my_broadcast
from .win_utils import get_idle_seconds


@dataclass
class Peer:
    ip: str
    name: str
    last_seen: float
    uid: str = ""
    device: str = ""
    status: str = "online"        # "online" | "away"  (offline is derived from last_seen)
    ips: tuple = ()               # all advertised IPs of this peer


class ChatService:
    @property
    def presence_online(self) -> bool:
        return getattr(self, "my_status", "online") != "invisible"

    @presence_online.setter
    def presence_online(self, val: bool):
        self.my_status = "online" if val else "invisible"

    def __init__(self, my_name: str,
                 on_roster_change=None,
                 on_message=None,
                 on_file_offer=None,
                 on_file_accept=None,
                 on_file_reject=None,
                 on_chat_request=None,
                 on_group_message=None,
                 on_channel_message=None,
                 on_receipt=None,
                 on_delete=None,
                 on_typing=None,
                 on_reaction=None,
                 on_queue_flush=None,
                 on_group_kick=None) -> None:
        """
        on_roster_change(peers: list[Peer])          -- roster changed.
        on_message(ip, name, text, ts, reply, mid)   -- incoming chat message.
        on_file_offer(ip, name, msg_dict)            -- incoming file offer.
        on_file_accept(ip, name, msg_dict)           -- peer accepted our offer.
        on_file_reject(ip, name, msg_dict)           -- peer rejected our offer.
        on_chat_request(ip, name, msg_dict)          -- first message from unknown external IP.
        on_group_message(group, ip, name, text, ts, reply, mid) -- message to a group.
        on_receipt(ip, mid, state)                   -- peer acked one of our messages.
        on_delete(from_ip, mid)                      -- peer deleted a message for everyone.
        on_typing(ip, name, gid, is_typing)          -- peer started/stopped typing.
        on_reaction(from_ip, mid, emoji)             -- peer added/toggled a reaction.
        on_queue_flush(ip, mids)                     -- queued messages finally delivered.
        All callbacks are invoked from background threads; marshal to main thread.
        """
        self.my_name = my_name
        self.my_ip: str = get_local_ip() or "127.0.0.1"
        self.my_uid: str = config.load_device_id()
        self.my_device: str = config.get_device_name()
        self._on_roster_change = on_roster_change
        self._on_message = on_message
        self._on_file_offer = on_file_offer
        self._on_file_accept = on_file_accept
        self._on_file_reject = on_file_reject
        self._on_chat_request = on_chat_request
        self._on_group_message = on_group_message
        self._on_channel_message = on_channel_message
        self._on_receipt = on_receipt
        self._on_delete = on_delete
        self._on_typing = on_typing
        self._on_reaction = on_reaction
        self._on_queue_flush = on_queue_flush
        self._on_group_kick = on_group_kick

        # Offline sender-retained queues (update.md #14): undelivered text/group
        # messages are held in memory and retried when the peer is reachable.
        # Lost if we shut down before delivery (no central server, by design).
        self._outbox: dict[str, list[dict]] = {}   # ip -> [{payload, mid, ts}]

        # Manual status: 'online', 'away', or 'invisible'.
        # 'invisible' stops advertising presence so peers reap us.
        self.my_status: str = "online"

        # IP chat access control
        self.ip_chat_enabled: bool = True
        self._approved_ips: set[str] = set()   # approved external IPs
        self._blocked_ips: set[str] = set()    # permanently blocked IPs
        self._pending_requests: dict[str, list[dict]] = {}  # buffered msgs awaiting approval

        self._peers: dict[str, Peer] = {}
        self._virtual: dict[str, "DemoBot"] = {}   # ip -> bot (demo / loopback)
        self._manual: set[str] = set()  # IPs added manually (never reaped)
        # last_seen survives reaping so the UI can show "Last seen …" for peers
        # that have gone offline. Seeded by the UI from saved history on launch.
        self._last_seen: dict[str, float] = {}
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
        threading.Thread(target=self._outbox_loop, daemon=True).start()

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

    def remove_peer(self, ip: str) -> None:
        """Forget a peer entirely: stop probing it (if manual), drop its presence
        entry and any access-control state.

        Auto-discovered peers on the local subnet may reappear on their next
        presence broadcast — deletion is only permanent for manual IP peers.
        """
        with self._lock:
            self._peers.pop(ip, None)
            self._virtual.pop(ip, None)
            self._manual.discard(ip)
            self._approved_ips.discard(ip)
            self._blocked_ips.discard(ip)
            self._pending_requests.pop(ip, None)
            self._outbox.pop(ip, None)
        self._emit_roster()

    def approve_ip(self, ip: str) -> None:
        """Approve an external IP and deliver any buffered messages.

        The IP is also added to the manual set so the reaper never drops it and
        the manual-probe loop keeps checking its online status — otherwise a
        cross-subnet peer would appear offline shortly after the chat request is
        accepted (their UDP beacon never reaches us through the subnet filter).
        """
        with self._lock:
            self._approved_ips.add(ip)
            self._blocked_ips.discard(ip)
            pending = self._pending_requests.pop(ip, [])
            if ip not in self._peers:
                self._peers[ip] = Peer(ip=ip, name=ip, last_seen=0.0)
            self._manual.add(ip)
        for msg in pending:
            self._dispatch_msg(msg, ip, msg.get("from_name", ip))
        threading.Thread(target=self._probe_one_manual, args=(ip,), daemon=True).start()

    def block_ip(self, ip: str) -> None:
        """Block an external IP and discard buffered messages."""
        with self._lock:
            self._blocked_ips.add(ip)
            self._approved_ips.discard(ip)
            self._pending_requests.pop(ip, None)
            self._outbox.pop(ip, None)

    def unblock_ip(self, ip: str) -> None:
        """Lift a block on *ip* (does not auto-approve — first contact re-prompts)."""
        with self._lock:
            self._blocked_ips.discard(ip)
            self._pending_requests.pop(ip, None)

    def blocked_ips(self) -> list[str]:
        with self._lock:
            return sorted(self._blocked_ips)

    def pending_request_ips(self) -> list[str]:
        with self._lock:
            return sorted(self._pending_requests.keys())

    def is_local_ip(self, ip: str) -> bool:
        """True if *ip* shares a subnet with any of our local interfaces.

        Used by the UI to decide whether to show a peer under LOCAL or IP/MANUAL
        — based on actual network topology, not on how they were added.
        """
        return self._is_same_subnet(ip)

    def _is_same_subnet(self, remote_ip: str) -> bool:
        """True if remote_ip shares a subnet with ANY local interface.

        Used for the IP-chat external-IP approval gate (broad check so manual
        peers added on the same physical LAN are auto-trusted regardless of
        which adapter they arrive on).
        """
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

    def _on_my_subnet(self, remote_ip: str) -> bool:
        """True only if *remote_ip* is on the same subnet as *self.my_ip*.

        Stricter than _is_same_subnet: this checks only the primary LAN
        interface, so a 192.168 hotspot adapter doesn't accidentally let
        hotspot peers appear in the LAN roster.
        """
        try:
            remote_int = struct.unpack("!I", socket.inet_aton(remote_ip))[0]
            my_int = struct.unpack("!I", socket.inet_aton(self.my_ip))[0]
            for _iface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if (addr.family != socket.AF_INET
                            or addr.address != self.my_ip
                            or not addr.netmask):
                        continue
                    mask_int = struct.unpack("!I", socket.inet_aton(addr.netmask))[0]
                    return (my_int & mask_int) == (remote_int & mask_int)
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
                    # Honour the appear-offline toggle: skip advertising but keep
                    # the loop alive so flipping back online resumes instantly.
                    if self.my_status != "invisible":
                        payload = CHAT_MAGIC + b"|" + json.dumps({
                            "v": 2,
                            "uid": self.my_uid,
                            "name": self.my_name,
                            "device": self.my_device,
                            "ip": self.my_ip,
                            "ips": get_all_local_ips(),
                            "status": self.my_status,
                        }).encode("utf-8")
                        # Broadcast only on the primary LAN subnet so the
                        # presence beacon doesn't leak onto hotspot/VPN adapters.
                        bcast = get_my_broadcast(self.my_ip)
                        for addr in {bcast, "255.255.255.255"}:
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
                    data, _addr = s.recvfrom(1024)
                    parts = data.split(b"|", 1)
                    if len(parts) != 2 or parts[0] != CHAT_MAGIC:
                        continue
                    info = json.loads(parts[1].decode("utf-8", errors="replace"))
                    ip = str(info.get("ip", "")).strip()
                    name = str(info.get("name", "")).strip()
                    uid = str(info.get("uid", "")).strip()
                    if not ip or ip == self.my_ip or uid == self.my_uid:
                        continue  # ignore self
                    # Only accept peers on our primary LAN subnet; beacons from
                    # hotspot/VPN adapters on other subnets are silently ignored.
                    if not self._on_my_subnet(ip):
                        continue
                    self._touch_peer(
                        ip, name,
                        uid=uid,
                        device=str(info.get("device", "")).strip(),
                        status=str(info.get("status", "online")).strip() or "online",
                        ips=tuple(str(x) for x in info.get("ips", []) if x),
                    )
                except socket.timeout:
                    continue
                except Exception:
                    continue
            s.close()
        except Exception:
            pass

    def _touch_peer(self, ip: str, name: str, uid: str = "", device: str = "",
                    status: str = "online", ips: tuple = ()) -> None:
        changed = False
        with self._lock:
            existing = self._peers.get(ip)
            now = time.time()
            was_online = (existing is not None
                          and (now - existing.last_seen) <= CHAT_PEER_TIMEOUT)
            # Carry forward identity fields when a touch omits them (e.g. a manual
            # probe or an incoming message that didn't carry full presence info).
            if existing is not None:
                uid = uid or existing.uid
                device = device or existing.device
                ips = ips or existing.ips
            # Emit roster when peer is new, identity changed, status changed, or
            # the peer transitioned offline→online.
            if (existing is None or existing.name != name
                    or existing.status != status or existing.device != device
                    or not was_online):
                changed = True
            self._peers[ip] = Peer(ip=ip, name=name or ip, last_seen=now,
                                   uid=uid, device=device, status=status, ips=ips)
            self._last_seen[ip] = now
        if changed:
            self._emit_roster()

    # ── last-seen / status accessors (for the UI) ─────────────────────────────
    def seed_last_seen(self, ip: str, ts: float) -> None:
        """Restore a saved last-seen timestamp (called by the UI on launch)."""
        if ts:
            with self._lock:
                self._last_seen[ip] = max(ts, self._last_seen.get(ip, 0.0))

    def last_seen_of(self, ip: str) -> float:
        with self._lock:
            return self._last_seen.get(ip, 0.0)

    def peer_status(self, ip: str) -> str:
        """Return 'online', 'away' or 'offline' for *ip*."""
        if ip == DemoBot.IP:
            return "online"
        with self._lock:
            p = self._peers.get(ip)
            if p is None or (time.time() - p.last_seen) > CHAT_PEER_TIMEOUT:
                return "offline"
            return p.status if p.status in ("online", "away") else "online"

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

            # Control messages reference something we already own (an offer we
            # made, a message id we sent, an ephemeral typing ping) so they're
            # always trusted and bypass the first-contact approval gate.
            _trusted = ("file_accept", "file_reject", "receipt", "delete",
                        "typing", "reaction", "group_kick")
            if msg_type not in _trusted:
                if ip in self._blocked_ips:
                    return  # silently drop — applies to LAN and external alike
                if not self._is_same_subnet(ip) and ip not in self._approved_ips:
                    if not self.ip_chat_enabled:
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
        elif msg_type == "receipt":
            if self._on_receipt:
                self._on_receipt(ip, str(msg.get("mid", "")),
                                 str(msg.get("state", "delivered")))
        elif msg_type == "delete":
            if self._on_delete:
                self._on_delete(ip, str(msg.get("mid", "")))
        elif msg_type == "group_kick":
            gid = str(msg.get("gid", ""))
            if gid and self._on_group_kick:
                self._on_group_kick(ip, gid)
        elif msg_type == "typing":
            gid = msg.get("gid") or None
            if self._on_typing:
                self._on_typing(ip, name, gid, bool(msg.get("is_typing")))
        elif msg_type == "reaction":
            mid = str(msg.get("mid", ""))
            emoji = str(msg.get("emoji", ""))
            if mid and emoji and self._on_reaction:
                self._on_reaction(ip, mid, emoji)
        elif msg_type in ("group", "group_invite"):
            # Synced group: every message carries the group identity + member
            # list so the receiving app can register the thread and route the
            # reply back to all members.
            group = msg.get("group")
            if not isinstance(group, dict) or not group.get("gid"):
                return
            self._touch_peer(ip, name)
            text = str(msg.get("text", ""))
            ts = float(msg.get("ts", time.time()))
            reply = msg.get("reply") if isinstance(msg.get("reply"), dict) else None
            mid = str(msg.get("mid", ""))
            if self._on_group_message:
                self._on_group_message(group, ip, name, text, ts, reply, mid)
        elif msg_type in ("channel", "channel_meta"):
            channel = msg.get("channel")
            if not isinstance(channel, dict) or not channel.get("cid"):
                return
            self._touch_peer(ip, name)
            text = str(msg.get("text", ""))
            ts = float(msg.get("ts", time.time()))
            reply = msg.get("reply") if isinstance(msg.get("reply"), dict) else None
            mid = str(msg.get("mid", ""))
            if self._on_channel_message:
                self._on_channel_message(channel, ip, name, text, ts, reply, mid)
        else:
            text = str(msg.get("text", ""))
            ts = float(msg.get("ts", time.time()))
            reply = msg.get("reply") if isinstance(msg.get("reply"), dict) else None
            mid = str(msg.get("mid", ""))
            self._touch_peer(ip, name)
            # Auto-acknowledge delivery the moment we hand the message to the UI.
            if mid and msg_type == "chat":
                threading.Thread(target=self.send_receipt,
                                 args=(ip, mid, "delivered"), daemon=True).start()
            if text and self._on_message:
                self._on_message(ip, name, text, ts, reply, mid)

    # ── messaging: outgoing ───────────────────────────────────────────────────
    def send(self, ip: str, text: str, reply: dict | None = None,
             group: dict | None = None, msg_type: str = "chat",
             mid: str = "", channel: dict | None = None) -> bool:
        """Deliver a message synchronously. Returns True on success.

        ``reply`` is an optional ``{"sender", "text"}`` snippet of the message
        being replied to. ``group`` carries the synced-group identity so the
        peer can route the reply back to every member. ``mid`` is the stable
        message id used for receipts / delete-for-everyone. Call from a worker
        thread to avoid blocking the UI.
        """
        bot = self._virtual.get(ip)
        if bot is not None:
            bot.on_user_message(text)
            return True

        msg: dict = {
            "from_name": self.my_name,
            "from_ip": self.my_ip,
            "text": text,
            "ts": time.time(),
        }
        if mid:
            msg["mid"] = mid
        if reply:
            msg["reply"] = reply
        if group:
            msg["group"] = group
            msg["type"] = msg_type if msg_type in ("group", "group_invite") else "group"
        elif channel:
            msg["channel"] = channel
            msg["type"] = msg_type if msg_type in ("channel", "channel_meta") else "channel"
        payload = json.dumps(msg).encode("utf-8") + b"\n"
        if self._deliver(ip, payload):
            return True
        # Peer unreachable — retain locally and retry when they reappear.
        self._enqueue(ip, payload, mid)
        return False

    # ── offline sender-retained queue (update.md #14) ─────────────────────────
    def _deliver(self, ip: str, payload: bytes) -> bool:
        """Open a short-lived connection and push one framed message. No queueing."""
        try:
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=3.0) as s:
                s.sendall(payload)
            return True
        except Exception:
            return False

    def _enqueue(self, ip: str, payload: bytes, mid: str) -> None:
        key = mid or str(hash(payload))
        with self._lock:
            q = self._outbox.setdefault(ip, [])
            if any(item["key"] == key for item in q):
                return
            q.append({"payload": payload, "key": key, "mid": mid, "ts": time.time()})

    def pending_count(self) -> int:
        """Total messages waiting to be delivered across all peers."""
        with self._lock:
            return sum(len(q) for q in self._outbox.values())

    def _outbox_loop(self) -> None:
        while self.running:
            time.sleep(3)
            with self._lock:
                targets = list(self._outbox.keys())
            for ip in targets:
                if not self.is_peer_online(ip) and not self._reachable(ip):
                    continue
                with self._lock:
                    items = list(self._outbox.get(ip, []))
                delivered: list[str] = []
                for item in items:
                    if self._deliver(ip, item["payload"]):
                        if item["mid"]:
                            delivered.append(item["mid"])
                        keep_key = item["key"]
                        with self._lock:
                            q = self._outbox.get(ip, [])
                            self._outbox[ip] = [i for i in q if i["key"] != keep_key]
                            if not self._outbox[ip]:
                                del self._outbox[ip]
                    else:
                        break   # peer flaky again — leave the rest queued
                if delivered and self._on_queue_flush:
                    try:
                        self._on_queue_flush(ip, delivered)
                    except Exception:
                        pass

    def _reachable(self, ip: str) -> bool:
        try:
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=1.0):
                return True
        except Exception:
            return False

    def send_group(self, group: dict, text: str, reply: dict | None = None,
                   msg_type: str = "group", mid: str = "") -> dict[str, bool]:
        """Fan a message out to every member of ``group`` except ourselves.

        Returns ``{ip: delivered}`` so the UI can flag members that were
        offline. Members are auto-approved for inbound replies. The same ``mid``
        is used for every member so delete-for-everyone targets one logical msg.
        """
        members = [ip for ip in group.get("members", []) if ip and ip != self.my_ip]
        with self._lock:
            self._approved_ips.update(members)
        results: dict[str, bool] = {}
        for ip in members:
            results[ip] = self.send(ip, text, reply=reply, group=group,
                                    msg_type=msg_type, mid=mid)
        return results

    def send_channel(self, channel: dict, text: str, reply: dict | None = None,
                     msg_type: str = "channel", mid: str = "") -> dict[str, bool]:
        """Broadcast a channel post to every subscriber except ourselves.

        Channels are admin-post / member-read (update.md #8); membership routing
        works exactly like a group. Returns ``{ip: delivered}``.
        """
        members = [ip for ip in channel.get("members", []) if ip and ip != self.my_ip]
        with self._lock:
            self._approved_ips.update(members)
        results: dict[str, bool] = {}
        for ip in members:
            results[ip] = self.send(ip, text, reply=reply, channel=channel,
                                    msg_type=msg_type, mid=mid)
        return results

    # ── control messages (receipts / delete / typing) ─────────────────────────
    def _send_json(self, ip: str, payload: dict) -> bool:
        try:
            data = json.dumps(payload).encode("utf-8") + b"\n"
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=3.0) as s:
                s.sendall(data)
            return True
        except Exception:
            return False

    def send_receipt(self, ip: str, mid: str, state: str) -> bool:
        """Tell *ip* that one of their messages was delivered/read by us."""
        if not mid or ip in self._virtual:
            return False
        return self._send_json(ip, {
            "type": "receipt", "mid": mid, "state": state,
            "from_name": self.my_name, "from_ip": self.my_ip,
        })

    def send_delete(self, ip: str, mid: str, gid: str = "") -> bool:
        """Ask *ip* to remove message *mid* for everyone."""
        if not mid or ip in self._virtual:
            return False
        return self._send_json(ip, {
            "type": "delete", "mid": mid, "gid": gid,
            "from_name": self.my_name, "from_ip": self.my_ip,
        })

    def send_typing(self, ip: str, is_typing: bool, gid: str = "") -> bool:
        """Send an ephemeral typing ping to *ip*."""
        if ip in self._virtual:
            return False
        return self._send_json(ip, {
            "type": "typing", "is_typing": bool(is_typing), "gid": gid,
            "from_name": self.my_name, "from_ip": self.my_ip,
        })

    def send_group_kick(self, ip: str, gid: str) -> bool:
        """Tell *ip* they have been removed from group *gid* (they lose it locally)."""
        if not gid or ip in self._virtual:
            return False
        return self._send_json(ip, {
            "type": "group_kick", "gid": gid,
            "from_name": self.my_name, "from_ip": self.my_ip,
        })

    def send_reaction(self, ip: str, mid: str, emoji: str, gid: str = "") -> bool:
        """Toggle an emoji reaction on a message and notify *ip*."""
        if not mid or not emoji or ip in self._virtual:
            return False
        return self._send_json(ip, {
            "type": "reaction", "mid": mid, "emoji": emoji, "gid": gid,
            "from_name": self.my_name, "from_ip": self.my_ip,
        })


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
                self.service._on_message(self.IP, self.NAME, text, time.time(),
                                         None, uuid.uuid4().hex[:16])
        threading.Timer(delay, fire).start()

    def greet(self) -> None:
        self._say("👋 Hi! I'm a demo peer. Send me a message to see chat in action.", 1.2)

    def on_user_message(self, _text: str) -> None:
        reply = self._REPLIES[self._i % len(self._REPLIES)]
        self._i += 1
        self._say(reply, 0.9)

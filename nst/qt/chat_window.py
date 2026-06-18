"""The modern LAN-chat window (PyQt6).

A messaging-app style two-pane layout: a searchable roster of peers and groups
on the left, a smooth bubble conversation + composer on the right. All chat
state, history files and the synced-group / reply / file-transfer protocols are
shared unchanged with the service layer.
"""

import json
import os
import subprocess
import threading
import time
import uuid

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFrame,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QScrollArea, QSizePolicy, QToolButton,
                             QVBoxLayout, QWidget)

from .. import config
from ..chat import DemoBot
from ..constants import CHAT_TCP_PORT, MOBILE_HTTP_PORT
from ..filetransfer import FileTransferService
from ..netinfo import check_host_reachable, is_valid_ipv4
from .theme import theme
from .widgets import Avatar, Dot, ToggleSwitch, hline

_PLACEHOLDER = "Type a message…"
_MAX_HISTORY = 200
_BUBBLE_MAX = 420


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def _fmt_speed(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


def _fmt_eta(secs: float) -> str:
    s = int(secs)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60}s"


def _fmt_last_seen(ts: float) -> str:
    if not ts:
        return "offline"
    d = time.time() - ts
    if d < 60:
        return "last seen just now"
    if d < 3600:
        return f"last seen {int(d // 60)}m ago"
    if d < 86400:
        return f"last seen {int(d // 3600)}h ago"
    if d < 7 * 86400:
        return f"last seen {int(d // 86400)}d ago"
    return "last seen " + time.strftime("%b %d", time.localtime(ts))


_DELETE_WINDOW = 180   # seconds you may still "delete for everyone"


def _mk_id() -> str:
    return uuid.uuid4().hex[:16]


def _mk_entry(kind: str, sender: str, text: str, ts: float, *,
              mid: str = "", reply: dict | None = None,
              status: str = "sent", fwd: bool = False, **extra) -> dict:
    """Build a canonical message entry dict.

    kind: out | in | sys | file_out | file_in_offer | chat_req
    """
    e: dict = {"kind": kind, "mid": mid or _mk_id(),
               "sender": sender, "text": text, "ts": float(ts)}
    if reply:
        e["reply"] = reply
    if kind == "out":
        e["status"] = status
    if fwd:
        e["fwd"] = True
    e.update(extra)
    return e


def _migrate_entry(item) -> dict:
    """Coerce a stored item (new dict, or legacy tuple/list) into an entry dict.

    Legacy outgoing messages are marked already-'read' so they don't show stale
    single ticks after upgrading (see update.md migration decision)."""
    if isinstance(item, dict):
        item.setdefault("mid", _mk_id())
        return item
    try:
        kind = item[0]
        ts = float(item[3]) if len(item) > 3 else time.time()
    except (IndexError, TypeError, ValueError):
        return _mk_entry("sys", "", "", time.time())
    if kind in ("out", "in"):
        reply = item[4] if len(item) > 4 and isinstance(item[4], dict) else None
        return _mk_entry(kind, item[1], item[2], ts, reply=reply, status="read")
    if kind == "sys":
        return _mk_entry("sys", "", item[2], ts)
    # file_/chat_req were never persisted; ignore quietly.
    return _mk_entry("sys", "", "", ts)


def _repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


class _Scroll(QScrollArea):
    """A vertical scroll area exposing a ``body`` VBox to add widgets to."""

    def __init__(self, autostick: bool = False) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(6, 6, 6, 6)
        self.box.setSpacing(2)
        self.box.addStretch(1)
        self.setWidget(self.body)
        self._autostick = autostick
        self._stick = True
        if autostick:
            bar = self.verticalScrollBar()
            bar.rangeChanged.connect(self._on_range)
            bar.valueChanged.connect(self._on_value)

    def add(self, w: QWidget) -> None:
        # Insert before the trailing stretch.
        self.box.insertWidget(self.box.count() - 1, w)

    def clear(self) -> None:
        while self.box.count() > 1:
            item = self.box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _on_value(self, v: int) -> None:
        bar = self.verticalScrollBar()
        self._stick = v >= bar.maximum() - 4

    def _on_range(self, _min: int, _max: int) -> None:
        if self._stick:
            self.verticalScrollBar().setValue(_max)

    def scroll_to_bottom(self) -> None:
        self._stick = True
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))


class _RosterRow(QFrame):
    clicked = pyqtSignal(str)
    deleted = pyqtSignal(str)

    def __init__(self, key, title, subtitle, status, unread, is_group, deletable):
        super().__init__()
        self.key = key
        self.setObjectName("rosterRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(9)

        av = Avatar("👥" if is_group else title, 38)
        lay.addWidget(av)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        name = QLabel(title)
        name.setStyleSheet("font-weight:700;")
        mid.addWidget(name)
        sub = QHBoxLayout()
        sub.setSpacing(4)
        if is_group:
            g = QLabel("👥 " + subtitle)
            g.setObjectName("muted")
            g.setStyleSheet("font-size:11px; color:%s;" % theme.color("text_sec"))
            sub.addWidget(g)
        else:
            sub.addWidget(Dot(status, 9))
            s = QLabel(subtitle)
            s.setObjectName("muted")
            s.setStyleSheet("font-size:11px; color:%s;" % theme.color("text_sec"))
            sub.addWidget(s)
        sub.addStretch(1)
        mid.addLayout(sub)
        lay.addLayout(mid, 1)

        if unread:
            b = QLabel(str(unread))
            b.setObjectName("unread")
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(b)
        if deletable:
            x = QPushButton("✕")
            x.setFixedSize(22, 22)
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.setToolTip("Remove")
            # Explicit style: the base button padding (8px 14px) would otherwise
            # squeeze the glyph out of this 22px box and make it look invisible.
            x.setStyleSheet(
                "QPushButton{background:transparent; border:none; padding:0;"
                " font-size:13px; font-weight:700; color:%s;}"
                "QPushButton:hover{color:#fff; background:%s; border-radius:11px;}"
                % (theme.color("text_sec"), theme.color("danger")))
            x.clicked.connect(lambda: self.deleted.emit(self.key))
            lay.addWidget(x)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        _repolish(self)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(e)


class ChatWindow(QWidget):
    """Standalone chat window. Closing hides it so conversations persist."""

    activity = pyqtSignal(str)     # background message arrived on this key

    def __init__(self, chat_service, toasts, mobile_server=None,
                 log_fn=lambda m: None) -> None:
        super().__init__(None)
        self.chat = chat_service
        self._toasts = toasts
        self._log = log_fn
        self._mobile = mobile_server
        self.setWindowTitle("LAN Chat — Net Split-Tunneler")
        self.resize(900, 600)
        self.setMinimumSize(720, 480)

        # Each conversation is a list of message dicts (see _mk_entry). Keyed by
        # peer IP or "group:<gid>".
        self._conversations: dict[str, list[dict]] = {}
        self._names: dict[str, str] = {}
        self._devices: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._unread: dict[str, int] = {}
        self._groups: dict[str, dict] = {}
        self._active: str | None = None
        self._visible = False
        self._peer_filter = ""
        self._reply_to: dict | None = None
        self._notifications_enabled = config.load_notifications_enabled()
        self._last_online_sig: frozenset = frozenset()
        self._rows: dict[str, _RosterRow] = {}

        # mobile sessions (keyed by sid; cleared on leave)
        self._mobile_sessions: dict[str, object] = {}       # sid -> MobileSession

        # message-id bookkeeping (receipts, delete-for-everyone, reactions)
        self._mid_index: dict[str, tuple[str, dict]] = {}   # mid -> (key, entry)
        self._status_lbls: dict[str, QLabel] = {}           # mid -> tick label
        self._seen_lbls: dict[str, QPushButton] = {}        # mid -> "X/Y Seen" button (group out)
        self._reaction_rows: dict[str, QWidget] = {}        # mid -> reaction pill container
        self._read_sent: set[str] = set()                   # mids we've acked "read"

        # typing indicators
        self._typers: dict[str, dict[str, float]] = {}      # key -> {ip: expiry}
        self._typing_last_sent = 0.0                        # throttle outgoing pings
        self._typing_active = False

        # file transfer state
        self._progress_text: dict[str, str] = {}
        self._progress_lbls: dict[str, QLabel] = {}
        self._offer_states: dict[str, str] = {}
        self._transfer_paths: dict[str, str] = {}
        self._chat_req_states: dict[str, str] = {}

        self._ft = FileTransferService(chat_service)
        self._ft.start()

        self._build()
        self._load_history()
        theme.changed.connect(self._on_theme)
        self.update_roster(self.chat.peers())
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._roster_tick)
        self._tick.start(3000)

        # Typing: a one-shot timer that fires "stopped typing" after a pause, and
        # a periodic sweep that expires stale remote typers.
        self._typing_stop_timer = QTimer(self)
        self._typing_stop_timer.setSingleShot(True)
        self._typing_stop_timer.timeout.connect(self._stop_typing)
        self._typing_sweep = QTimer(self)
        self._typing_sweep.timeout.connect(self._typing_tick)
        self._typing_sweep.start(2000)

    # ── construction ────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget()
        side.setObjectName("card")
        side.setFixedWidth(270)
        s = QVBoxLayout(side)
        s.setContentsMargins(12, 12, 12, 12)
        s.setSpacing(8)

        # YOU header
        you = QHBoxLayout()
        lbl = QLabel("YOU")
        lbl.setObjectName("section")
        you.addWidget(lbl)
        you.addStretch(1)
        self._self_dot = Dot(self.chat.presence_online, 9)
        you.addWidget(self._self_dot)
        gear = QToolButton()
        gear.setText("⚙")
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.clicked.connect(self._open_settings)
        you.addWidget(gear)
        s.addLayout(you)

        idrow = QHBoxLayout()
        self._self_avatar = Avatar(self.chat.my_name, 34)
        idrow.addWidget(self._self_avatar)
        self._name_edit = QLineEdit(self.chat.my_name)
        self._name_edit.setStyleSheet("font-weight:700;")
        # editingFinished already fires on both Enter and focus-out; connecting
        # returnPressed too made _rename run twice per save.
        self._name_edit.editingFinished.connect(self._rename)
        idrow.addWidget(self._name_edit, 1)
        s.addLayout(idrow)

        # connect by IP
        ciprow = QHBoxLayout()
        cip = QLabel("CONNECT BY IP")
        cip.setObjectName("section")
        ciprow.addWidget(cip)
        ciprow.addStretch(1)
        self._ip_toggle = ToggleSwitch(self.chat.ip_chat_enabled)
        self._ip_toggle.toggled.connect(self._toggle_ip_chat)
        ciprow.addWidget(self._ip_toggle)
        s.addLayout(ciprow)

        conrow = QHBoxLayout()
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("e.g. 192.168.1.20")
        self._ip_edit.returnPressed.connect(self._connect_manual_ip)
        conrow.addWidget(self._ip_edit, 1)
        go = QPushButton("➜")
        go.setProperty("variant", "accent")
        go.setFixedWidth(40)
        go.clicked.connect(self._connect_manual_ip)
        conrow.addWidget(go)
        s.addLayout(conrow)

        # peers header + new group
        phrow = QHBoxLayout()
        ph = QLabel("PEERS")
        ph.setObjectName("section")
        phrow.addWidget(ph)
        phrow.addStretch(1)
        newg = QPushButton("＋ Group")
        newg.setProperty("variant", "ghost")
        newg.setStyleSheet("color:%s; font-weight:700;" % theme.color("accent"))
        newg.clicked.connect(self._new_group_dialog)
        phrow.addWidget(newg)
        s.addLayout(phrow)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search peers…")
        self._search.textChanged.connect(self._on_search)
        s.addWidget(self._search)

        self._roster = _Scroll()
        s.addWidget(self._roster, 1)

        demo = QPushButton("Try Demo Chat")
        demo.setProperty("variant", "accent")
        demo.clicked.connect(self._start_demo)
        s.addWidget(demo)

        root.addWidget(side)

        # right pane
        right = QWidget()
        r = QVBoxLayout(right)
        r.setContentsMargins(14, 12, 14, 12)
        r.setSpacing(8)

        head = QHBoxLayout()
        self._head_avatar = Avatar("LAN", 40)
        head.addWidget(self._head_avatar)
        htext = QVBoxLayout()
        htext.setSpacing(0)
        self._head_name = QLabel("LAN Chat")
        self._head_name.setObjectName("title")
        self._head_sub = QLabel("Select a peer on the left")
        self._head_sub.setObjectName("muted")
        htext.addWidget(self._head_name)
        htext.addWidget(self._head_sub)
        head.addLayout(htext)
        head.addStretch(1)
        self._btn_add = QPushButton("＋ Add")
        self._btn_add.setProperty("variant", "accent")
        self._btn_add.clicked.connect(self._add_group_members)
        head.addWidget(self._btn_add)
        self._btn_save = QPushButton("✎ Save name")
        self._btn_save.clicked.connect(self._edit_alias)
        head.addWidget(self._btn_save)
        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._clear_chat)
        head.addWidget(self._btn_clear)
        r.addLayout(head)
        r.addWidget(hline())

        self._messages = _Scroll(autostick=True)
        r.addWidget(self._messages, 1)

        # typing indicator
        self._typing_lbl = QLabel("")
        self._typing_lbl.setObjectName("muted")
        self._typing_lbl.setStyleSheet("font-style:italic; font-size:11px; color:%s;"
                                       % theme.color("text_sec"))
        self._typing_lbl.hide()
        r.addWidget(self._typing_lbl)

        # reply bar
        self._reply_bar = QFrame()
        self._reply_bar.setObjectName("replyBar")
        rb = QHBoxLayout(self._reply_bar)
        rb.setContentsMargins(10, 6, 8, 6)
        stripe = QFrame()
        stripe.setFixedWidth(3)
        stripe.setStyleSheet("background:%s; border-radius:2px;" % theme.color("accent"))
        rb.addWidget(stripe)
        rbt = QVBoxLayout()
        rbt.setSpacing(0)
        self._reply_who = QLabel("")
        self._reply_who.setObjectName("accent")
        self._reply_prev = QLabel("")
        self._reply_prev.setObjectName("muted")
        rbt.addWidget(self._reply_who)
        rbt.addWidget(self._reply_prev)
        rb.addLayout(rbt, 1)
        rbx = QPushButton("✕")
        rbx.setFixedSize(22, 22)
        rbx.setCursor(Qt.CursorShape.PointingHandCursor)
        rbx.setStyleSheet(
            "QPushButton{background:transparent; border:none; padding:0;"
            " font-size:13px; color:%s;}"
            "QPushButton:hover{color:%s;}"
            % (theme.color("text_sec"), theme.color("text_pri")))
        rbx.clicked.connect(self._cancel_reply)
        rb.addWidget(rbx)
        self._reply_bar.hide()
        r.addWidget(self._reply_bar)

        comp = QHBoxLayout()
        self._entry = QLineEdit()
        self._entry.setPlaceholderText(_PLACEHOLDER)
        self._entry.returnPressed.connect(self._send)
        self._entry.textChanged.connect(self._on_typing_edit)
        comp.addWidget(self._entry, 1)
        self._btn_file = QPushButton("📎")
        self._btn_file.setFixedWidth(44)
        self._btn_file.clicked.connect(self._attach_file)
        comp.addWidget(self._btn_file)
        self._btn_send = QPushButton("Send")
        self._btn_send.setProperty("variant", "accent")
        self._btn_send.clicked.connect(self._send)
        comp.addWidget(self._btn_send)
        self._composer = QWidget()
        self._composer.setLayout(comp)
        r.addWidget(self._composer)

        root.addWidget(right, 1)
        self._show_empty_state()
        self._set_composer_visible(False)

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _is_group(key: str) -> bool:
        return bool(key) and key.startswith("group:")

    @staticmethod
    def _is_mobile(key: str) -> bool:
        return bool(key) and key.startswith("mobile:")

    def _display_name(self, key: str) -> str:
        if self._is_group(key):
            return self._groups.get(key[6:], {}).get("name", "Group")
        if self._is_mobile(key):
            sid = key[7:]
            sess = self._mobile_sessions.get(sid)
            return f"{sess.name} 📱" if sess else self._names.get(key, "Mobile 📱")
        return self._aliases.get(key) or self._names.get(key, key)

    def _group_meta(self, gid: str) -> dict:
        g = self._groups.get(gid, {})
        members = list(g.get("members", []))
        if self.chat.my_ip not in members:
            members = members + [self.chat.my_ip]
        return {"gid": gid, "name": g.get("name", "Group"), "members": members}

    def _last_activity(self, key: str) -> float:
        msgs = self._conversations.get(key)
        if not msgs:
            return 0.0
        try:
            return float(msgs[-1].get("ts", 0))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    # ── window visibility ─────────────────────────────────────────────────────
    def open(self, key: str | None = None) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._visible = True
        if key:
            self.select_peer(key)

    def closeEvent(self, e) -> None:
        e.ignore()
        self._visible = False
        self.hide()

    def showEvent(self, e) -> None:
        self._visible = True
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._visible = False
        super().hideEvent(e)

    def changeEvent(self, e) -> None:
        from PyQt6.QtCore import QEvent
        if e.type() == QEvent.Type.ActivationChange:
            self._visible = self.isActiveWindow() and self.isVisible()
            # Only rebuild the roster when there was actually unread to clear.
            # Otherwise every modal dialog (Save name, Connect by IP, New group)
            # opening/closing would tear down and re-add every row twice, which
            # flashes the peer list behind the dialog.
            if self._visible and self._active:
                if self._unread.get(self._active):
                    self._unread[self._active] = 0
                    self.update_roster(self.chat.peers())
                    self._render(self._active)
                self._mark_read(self._active)
        super().changeEvent(e)

    # ── self identity / settings ──────────────────────────────────────────────
    def _rename(self) -> None:
        new = self._name_edit.text().strip()[:32]
        if not new:
            self._name_edit.setText(self.chat.my_name)
            return
        if new == self.chat.my_name:
            return
        self.chat.set_name(new)
        config.save_display_name(new)
        self._self_avatar.set_name(new)
        self._log(f"Chat display name set to '{new}'.")

    def _open_settings(self) -> None:
        m = QMenu(self)
        if self.chat.presence_online:
            m.addAction("● Online — appear offline", lambda: self._set_presence(False))
        else:
            m.addAction("○ Offline — appear online", lambda: self._set_presence(True))
        m.addSeparator()
        if self._notifications_enabled:
            m.addAction("🔔 Popups on — pause popups", lambda: self._set_notify(False))
        else:
            m.addAction("🔕 Popups paused — enable popups", lambda: self._set_notify(True))
        m.addSeparator()
        m.addAction("📱 Mobile Access — Show QR", self._show_qr_dialog)
        m.exec(self.sender().mapToGlobal(QPoint(0, self.sender().height())))

    def _set_presence(self, online: bool) -> None:
        self.chat.presence_online = online
        config.save_presence_online(online)
        self._self_dot.set_online(online)
        self._log(f"You now appear {'online' if online else 'offline'} to peers.")

    def _set_notify(self, enabled: bool) -> None:
        self._notifications_enabled = enabled
        config.save_notifications_enabled(enabled)
        self._log(f"Message popups {'enabled' if enabled else 'paused'}.")

    @property
    def notifications_enabled(self) -> bool:
        return self._notifications_enabled

    def _toggle_ip_chat(self, enabled: bool) -> None:
        self.chat.ip_chat_enabled = enabled
        config.save_ip_chat_enabled(enabled)
        self._log(f"External IP chat {'enabled' if enabled else 'disabled'}.")

    def _on_search(self, text: str) -> None:
        self._peer_filter = text.strip().lower()
        self.update_roster(self.chat.peers())

    def _on_theme(self) -> None:
        if self._active:
            self._render(self._active)
        self.update_roster(self.chat.peers())

    # ── roster ────────────────────────────────────────────────────────────────
    def _status_of(self, ip: str) -> str:
        """'online' | 'away' | 'offline' for a peer."""
        if ip == DemoBot.IP:
            return "online"
        if self._is_mobile(ip):
            sid = ip[7:]
            sess = self._mobile_sessions.get(sid)
            return "online" if (sess and sess.state == "approved") else "offline"
        return self.chat.peer_status(ip)

    def _is_online(self, ip: str) -> bool:
        return self._status_of(ip) in ("online", "away")

    def _visible_peers(self, peers) -> set[str]:
        """Peers to list: everyone currently seen, plus anyone we have history
        with (shown offline with a last-seen) — never groups or ourselves."""
        cands = {p.ip for p in peers}
        cands |= {c for c in self._conversations if not self._is_group(c)}
        cands.discard(self.chat.my_ip)
        return cands

    def _peer_subtitle(self, ip: str, status: str) -> str:
        if ip == DemoBot.IP:
            return "demo peer"
        if self._is_mobile(ip):
            sid = ip[7:]
            sess = self._mobile_sessions.get(sid)
            if not sess:
                return "📱 disconnected"
            if sess.state == "pending":
                return f"📱 {sess.ip} · waiting for approval"
            if sess.state == "approved":
                return f"📱 {sess.ip} · connected"
            return f"📱 {sess.ip} · {sess.state}"
        if status == "offline":
            return _fmt_last_seen(self.chat.last_seen_of(ip))
        dev = self._devices.get(ip)
        label = f"{dev} · {ip}" if dev else ip
        return f"away · {label}" if status == "away" else label

    def _matches(self, key: str) -> bool:
        if not self._peer_filter:
            return True
        hay = f"{self._display_name(key)} {key} {self._devices.get(key, '')}".lower()
        return self._peer_filter in hay

    def _status_sig(self, peers) -> frozenset:
        return frozenset((ip, self._status_of(ip)) for ip in self._visible_peers(peers))

    def _roster_tick(self) -> None:
        peers = self.chat.peers()
        sig = self._status_sig(peers)
        if sig != self._last_online_sig:
            self.update_roster(peers)

    def update_roster(self, peers) -> None:
        for p in peers:
            self._names[p.ip] = p.name
            if getattr(p, "device", ""):
                self._devices[p.ip] = p.device

        self._last_online_sig = self._status_sig(peers)

        self._roster.clear()
        self._rows = {}

        groups = [f"group:{g}" for g in self._groups if self._matches(f"group:{g}")]
        peers_f = [ip for ip in self._visible_peers(peers) if self._matches(ip)]

        if not groups and not peers_f:
            hint = QLabel("No matches." if self._peer_filter
                          else "Looking for people on your network…\nOpen the app on another PC, or Try Demo Chat.")
            hint.setObjectName("muted")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            self._roster.add(hint)
            return

        for key in sorted(groups, key=lambda x: (-self._last_activity(x),
                                                 self._display_name(x).lower())):
            gid = key[6:]
            n = len(self._group_meta(gid).get("members", []))
            self._add_row(key, self._display_name(key), f"{n} members",
                          "online", self._unread.get(key, 0), True, True)

        # Online/away first, then offline; within a group, most-recent first.
        _rank = {"online": 0, "away": 1, "offline": 2}
        for ip in sorted(peers_f, key=lambda x: (_rank.get(self._status_of(x), 2),
                                                 -self._last_activity(x),
                                                 self._display_name(x).lower())):
            status = self._status_of(ip)
            self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, status),
                          status, self._unread.get(ip, 0), False, ip != DemoBot.IP)

        if self._active:
            self._update_header_sub(peers)

    def _add_row(self, key, title, sub, status, unread, is_group, deletable) -> None:
        row = _RosterRow(key, title, sub, status, unread, is_group, deletable)
        row.set_active(key == self._active)
        row.clicked.connect(self.select_peer)
        row.deleted.connect(self._delete_group if is_group else self._delete_peer)
        self._roster.add(row)
        self._rows[key] = row

    def _update_header_sub(self, peers) -> None:
        key = self._active
        if self._is_group(key):
            n = len(self._group_meta(key[6:]).get("members", []))
            self._head_sub.setText(f"Group · {n} members")
        elif key == DemoBot.IP:
            self._head_sub.setText("demo peer")
        elif self._is_mobile(key):
            sid = key[7:]
            sess = self._mobile_sessions.get(sid)
            if sess and sess.state == "approved":
                self._head_sub.setText(f"📱 {sess.ip} · connected")
            elif sess:
                self._head_sub.setText(f"📱 {sess.ip} · {sess.state}")
            else:
                self._head_sub.setText("📱 mobile · disconnected")
        else:
            status = self._status_of(key)
            dev = self._devices.get(key)
            ident = f"{dev}  ·  {key}" if dev else key
            if status == "offline":
                self._head_sub.setText(f"{ident}  ·  {_fmt_last_seen(self.chat.last_seen_of(key))}")
            else:
                self._head_sub.setText(f"{ident}  ·  {status.capitalize()}")

    # ── selection ─────────────────────────────────────────────────────────────
    def select_peer(self, key: str) -> None:
        prev = self._active
        self._active = key
        self._unread[key] = 0
        self._cancel_reply()
        for k, row in self._rows.items():
            row.set_active(k == key)
        self._head_avatar.set_name("👥" if self._is_group(key) else self._display_name(key))
        self._head_name.setText(self._display_name(key))
        self._update_header_sub(self.chat.peers())
        is_grp = self._is_group(key)
        is_mob = self._is_mobile(key)
        self._btn_add.setVisible(is_grp)
        self._btn_save.setVisible(not is_grp and not is_mob and key != DemoBot.IP)
        self._btn_file.setVisible(not is_grp)
        self._set_composer_visible(True)
        self._render(key)
        self._refresh_typing()
        self._mark_read(key)
        self._entry.setFocus()
        if prev != key:
            self.update_roster(self.chat.peers())

    def _set_composer_visible(self, on: bool) -> None:
        self._composer.setVisible(on)
        self._btn_clear.setVisible(on)
        if not on:
            self._btn_add.setVisible(False)
            self._btn_save.setVisible(False)
            self._cancel_reply()

    def _show_empty_state(self) -> None:
        self._messages.clear()
        w = QLabel("💬\n\nPick someone from the list to start chatting.")
        w.setObjectName("muted")
        w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._messages.add(w)

    # ── message store ─────────────────────────────────────────────────────────
    def _store(self, key: str, entry: dict) -> None:
        """Append an entry to a conversation and index it by message id."""
        self._conversations.setdefault(key, []).append(entry)
        if entry.get("mid"):
            self._mid_index[entry["mid"]] = (key, entry)
        self._trim(key)

    def _persist(self, key: str) -> None:
        if self._is_group(key):
            self._save_group(key[6:])
        else:
            self._save_peer(key)

    def _drop_index(self, key: str) -> None:
        for mid in [m for m, (k, _e) in self._mid_index.items() if k == key]:
            self._mid_index.pop(mid, None)

    @staticmethod
    def _entry_sender(entry: dict) -> str:
        return "You" if entry.get("kind") == "out" else entry.get("sender", "")

    # ── rendering ─────────────────────────────────────────────────────────────
    def _render(self, key: str) -> None:
        self._messages.clear()
        self._progress_lbls.clear()
        self._status_lbls.clear()
        self._seen_lbls.clear()
        self._reaction_rows.clear()
        msgs = self._conversations.get(key, [])
        if not msgs:
            w = QLabel("Say hi! 👋")
            w.setObjectName("muted")
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._messages.add(w)
        else:
            for entry in msgs:
                self._messages.add(self._make_bubble(entry))
        self._messages.scroll_to_bottom()

    def _append(self, entry: dict) -> None:
        self._messages.add(self._make_bubble(entry))
        self._messages.scroll_to_bottom()

    def _tick_parts(self, status: str, is_out: bool) -> tuple[str, str]:
        """Return (glyph, color) for a delivery-status tick on an out bubble."""
        muted = "rgba(255,255,255,0.65)" if is_out else theme.color("text_sec")
        if status == "read":
            return "✓✓", "#a8e0ff" if is_out else theme.color("accent")
        if status == "delivered":
            return "✓✓", muted
        return "✓", muted   # sent

    def _make_bubble(self, entry: dict) -> QWidget:
        kind = entry.get("kind", "sys")
        if kind in ("file_out", "file_in_offer"):
            return self._make_file_bubble(entry)
        if kind == "chat_req":
            return self._make_req_bubble(entry)
        if kind == "mobile_req":
            return self._make_mobile_req_bubble(entry)
        if kind == "sys":
            lbl = QLabel(f"— {entry.get('text', '')} —")
            lbl.setObjectName("muted")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-style:italic; padding:3px; color:%s;" % theme.color("text_sec"))
            return lbl

        sender, text, ts = entry.get("sender", ""), entry.get("text", ""), entry.get("ts", 0)
        reply = entry.get("reply")
        is_out = kind == "out"
        deleted = bool(entry.get("deleted"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 2, 4, 2)
        bubble = QFrame()
        bubble.setProperty("bubble", "out" if is_out else "in")
        bubble.setMaximumWidth(_BUBBLE_MAX)
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(12, 8, 12, 6)
        bv.setSpacing(2)
        txcol = theme.color("bubble_out_tx" if is_out else "bubble_in_tx")

        # Right-click menu (reply / delete) — not on tombstones.
        if not deleted:
            bubble.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            bubble.customContextMenuRequested.connect(
                lambda pos, e=entry, b=bubble: self._msg_menu(e, b.mapToGlobal(pos)))

        if deleted:
            d = QLabel("🚫 This message was deleted")
            d.setStyleSheet("color:%s; font-style:italic; font-size:12px;" % txcol)
            bv.addWidget(d)
            stamp = QLabel(time.strftime("%H:%M", time.localtime(ts)))
            stamp.setStyleSheet("font-size:10px; color:%s;" % txcol)
            bv.addWidget(stamp, alignment=Qt.AlignmentFlag.AlignRight)
            if is_out:
                h.addStretch(1); h.addWidget(bubble)
            else:
                h.addWidget(bubble); h.addStretch(1)
            return row

        if not is_out:
            sl = QLabel(sender)
            sl.setStyleSheet("color:%s; font-weight:700; font-size:11px;" % theme.color("accent"))
            bv.addWidget(sl)
        if entry.get("fwd"):
            fl = QLabel("↪ Forwarded")
            fl.setStyleSheet("color:%s; font-style:italic; font-size:10px;" % txcol)
            bv.addWidget(fl)
        if isinstance(reply, dict) and reply.get("text"):
            # Colour the nested quote to contrast with its own bubble: white on
            # the blue outgoing bubble, accent on the light incoming bubble —
            # never blue-on-blue.
            if is_out:
                q_bg, q_stripe = "rgba(255,255,255,0.20)", "rgba(255,255,255,0.85)"
                q_who, q_tx = "#ffffff", "rgba(255,255,255,0.88)"
            else:
                q_bg, q_stripe = "rgba(127,127,127,0.14)", theme.color("accent")
                q_who, q_tx = theme.color("accent"), txcol
            q = QFrame()
            q.setStyleSheet("background:%s; border-radius:7px;" % q_bg)
            qh = QHBoxLayout(q)
            qh.setContentsMargins(0, 0, 0, 0)
            qh.setSpacing(0)
            stripe = QFrame()
            stripe.setFixedWidth(3)
            stripe.setStyleSheet("background:%s; border-top-left-radius:7px;"
                                 "border-bottom-left-radius:7px;" % q_stripe)
            qh.addWidget(stripe)
            qv = QVBoxLayout()
            qv.setContentsMargins(8, 3, 8, 3)
            qv.setSpacing(0)
            who = QLabel(reply.get("sender", ""))
            who.setStyleSheet("color:%s; font-weight:700; font-size:10px;" % q_who)
            snip = reply["text"]
            snip = snip if len(snip) <= 80 else snip[:77] + "…"
            qt = QLabel(snip)
            qt.setStyleSheet("color:%s; font-size:11px;" % q_tx)
            qt.setWordWrap(True)
            qv.addWidget(who)
            qv.addWidget(qt)
            qh.addLayout(qv, 1)
            bv.addWidget(q)

        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        msg.setStyleSheet("color:%s; font-size:13px;" % txcol)
        bv.addWidget(msg)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        rep = QPushButton("↩ Reply")
        rep.setProperty("variant", "ghost")
        rep.setStyleSheet("font-size:10px; color:%s; padding:0;" % txcol)
        rep.setCursor(Qt.CursorShape.PointingHandCursor)
        snd = "You" if is_out else sender
        rep.clicked.connect(lambda _=False, s=snd, t=text: self._set_reply(s, t))
        stamp = QLabel(time.strftime("%H:%M", time.localtime(ts)))
        stamp.setStyleSheet("font-size:10px; color:%s;" % txcol)
        foot.addWidget(rep)
        foot.addStretch(1)
        foot.addWidget(stamp)
        if is_out:
            glyph, color = self._tick_parts(entry.get("status", "sent"), True)
            tick = QLabel(glyph)
            tick.setStyleSheet("font-size:11px; color:%s;" % color)
            foot.addWidget(tick)
            if entry.get("mid"):
                self._status_lbls[entry["mid"]] = tick
        bv.addLayout(foot)

        # Group outgoing: show seen-by count below the timestamp row.
        mid = entry.get("mid", "")
        if is_out and mid:
            key_now = self._active or ""
            if self._is_group(key_now):
                gid = key_now[6:]
                total = len([m for m in self._group_meta(gid).get("members", [])
                              if m != self.chat.my_ip])
                seen = len(entry.get("seen_by", {}))
                seen_btn = QPushButton(f"✓ {seen}/{total} Seen")
                seen_btn.setFlat(True)
                seen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                seen_btn.setStyleSheet(
                    "QPushButton{font-size:10px; color:%s; padding:0; border:none;"
                    " background:transparent;}" % txcol)
                seen_btn.clicked.connect(
                    lambda _=False, m=mid: self._seen_popup(m))
                bv.addWidget(seen_btn, alignment=Qt.AlignmentFlag.AlignRight)
                self._seen_lbls[mid] = seen_btn

        # Reaction row lives outside/below the bubble frame so it doesn't stretch it.
        reaction_row = self._make_reaction_row(entry)
        if mid:
            self._reaction_rows[mid] = reaction_row
        v_wrap = QWidget()
        v_wrap.setMaximumWidth(_BUBBLE_MAX)
        vv = QVBoxLayout(v_wrap)
        vv.setContentsMargins(0, 0, 0, 0)
        vv.setSpacing(2)
        vv.addWidget(bubble)
        vv.addWidget(reaction_row)

        if is_out:
            h.addStretch(1)
            h.addWidget(v_wrap)
        else:
            h.addWidget(v_wrap)
            h.addStretch(1)
        return row

    # ── reply ─────────────────────────────────────────────────────────────────
    def _set_reply(self, sender: str, text: str) -> None:
        self._reply_to = {"sender": sender, "text": text}
        self._reply_who.setText(f"↩ Replying to {sender}")
        self._reply_prev.setText(text if len(text) <= 80 else text[:77] + "…")
        self._reply_bar.show()
        self._entry.setFocus()

    def _cancel_reply(self) -> None:
        self._reply_to = None
        self._reply_bar.hide()

    # ── send / receive ────────────────────────────────────────────────────────
    def _send(self) -> None:
        key = self._active
        text = self._entry.text().strip()
        if not key or not text:
            return
        self._entry.clear()
        self._stop_typing()
        reply = self._reply_to
        entry = _mk_entry("out", "You", text, time.time(), reply=reply, status="sent")
        mid = entry["mid"]
        self._store(key, entry)
        self._cancel_reply()
        self._persist(key)
        self._append(entry)

        if self._is_group(key):
            meta = self._group_meta(key[6:])
            threading.Thread(target=self._send_group_worker,
                             args=(key, meta, text, reply, mid), daemon=True).start()
        elif self._is_mobile(key):
            sid = key[7:]
            if self._mobile:
                threading.Thread(
                    target=lambda: self._mobile.send_to(sid, self.chat.my_name, text),
                    daemon=True).start()
        else:
            threading.Thread(target=self._send_worker,
                             args=(key, text, reply, mid), daemon=True).start()

    def _send_worker(self, ip, text, reply, mid) -> None:
        ok = self.chat.send(ip, text, reply=reply, mid=mid)
        if not ok:
            QTimer.singleShot(0, lambda: self._sys(ip, "not delivered (peer offline?)"))

    def _send_group_worker(self, key, meta, text, reply, mid) -> None:
        results = self.chat.send_group(meta, text, reply=reply, mid=mid)
        failed = [ip for ip, okk in results.items() if not okk]
        if failed:
            QTimer.singleShot(0, lambda: self._sys(
                key, f"not delivered to {len(failed)} member(s) (offline?)"))

    def _sys(self, key, text) -> None:
        entry = _mk_entry("sys", "", text, time.time())
        self._store(key, entry)
        if key == self._active:
            self._append(entry)

    def receive_message(self, ip, name, text, ts, reply=None, mid="") -> None:
        self._names[ip] = name
        entry = _mk_entry("in", name, text, ts, mid=mid, reply=reply)
        self._store(ip, entry)
        self._save_peer(ip)
        if ip == self._active and self._visible:
            self._append(entry)
            self._mark_read(ip)
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())
            if self._notifications_enabled:
                prev = text if len(text) <= 120 else text[:117] + "…"
                self._toasts.notify(name, prev, ip)
                self.activity.emit(ip)

    def on_group_message(self, group, ip, name, text, ts, reply=None, mid="") -> None:
        gid = group.get("gid")
        if not gid:
            return
        members = [m for m in group.get("members", []) if m]
        g = self._groups.setdefault(gid, {"name": group.get("name", "Group"), "members": members})
        g["name"] = group.get("name", g.get("name", "Group"))
        if members:
            g["members"] = members
        for m in members:
            if m and m != self.chat.my_ip and not self.chat.is_manual_peer(m):
                self.chat.add_manual_peer(m)
        self._names[ip] = name
        key = f"group:{gid}"
        if not text:
            self._save_group(gid)
            self.update_roster(self.chat.peers())
            return
        entry = _mk_entry("in", name, text, ts, mid=mid, reply=reply, from_ip=ip)
        self._store(key, entry)
        self._save_group(gid)
        if key == self._active and self._visible:
            self._append(entry)
        else:
            self._unread[key] = self._unread.get(key, 0) + 1
            self.update_roster(self.chat.peers())
            if self._notifications_enabled:
                prev = text if len(text) <= 100 else text[:97] + "…"
                self._toasts.notify(f"{g['name']} (group)", f"{name}: {prev}", key)
                self.activity.emit(key)

    # ── receipts / read tracking ──────────────────────────────────────────────
    def on_receipt(self, ip, mid, state) -> None:
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        if entry.get("kind") != "out":
            return

        # Group: track per-member seen_by; still advance status for "delivered"
        if self._is_group(key):
            if state == "read":
                entry.setdefault("seen_by", {})[ip] = time.time()
                self._persist(key)
                self._update_seen_lbl(mid, key)
            elif state == "delivered":
                order = {"sent": 0, "delivered": 1, "read": 2}
                if order["delivered"] > order.get(entry.get("status", "sent"), 0):
                    entry["status"] = "delivered"
                    self._persist(key)
                    lbl = self._status_lbls.get(mid)
                    if lbl is not None:
                        try:
                            glyph, color = self._tick_parts("delivered", True)
                            lbl.setText(glyph)
                            lbl.setStyleSheet("font-size:11px; color:%s;" % color)
                        except RuntimeError:
                            self._status_lbls.pop(mid, None)
            return

        # 1:1: single status progression, never regress
        order = {"sent": 0, "delivered": 1, "read": 2}
        if order.get(state, 0) <= order.get(entry.get("status", "sent"), 0):
            return
        entry["status"] = state
        self._persist(key)
        lbl = self._status_lbls.get(mid)
        if lbl is None:
            return
        try:
            glyph, color = self._tick_parts(state, True)
            lbl.setText(glyph)
            lbl.setStyleSheet("font-size:11px; color:%s;" % color)
        except RuntimeError:
            self._status_lbls.pop(mid, None)

    def _update_seen_lbl(self, mid: str, key: str) -> None:
        """Refresh the 'X/Y Seen' button for a group outgoing message."""
        btn = self._seen_lbls.get(mid)
        if btn is None:
            return
        loc = self._mid_index.get(mid)
        if not loc:
            return
        _, entry = loc
        seen = len(entry.get("seen_by", {}))
        gid = key[6:] if self._is_group(key) else ""
        total = len([m for m in self._group_meta(gid).get("members", [])
                     if m != self.chat.my_ip]) if gid else 0
        try:
            btn.setText(f"✓ {seen}/{total} Seen")
        except RuntimeError:
            self._seen_lbls.pop(mid, None)

    def _mark_read(self, key) -> None:
        """Send 'read' receipts once the chat is open+focused."""
        if key == DemoBot.IP or self._is_mobile(key):
            return
        if not (self._visible and self.isActiveWindow() and key == self._active):
            return

        if self._is_group(key):
            # For group messages, send receipt to each original sender's IP.
            to_send: dict[str, list[str]] = {}   # from_ip -> [mid, ...]
            for e in self._conversations.get(key, []):
                if (e.get("kind") == "in" and not e.get("deleted")
                        and e.get("mid") and e["mid"] not in self._read_sent
                        and e.get("from_ip")):
                    to_send.setdefault(e["from_ip"], []).append(e["mid"])
            if not to_send:
                return
            for mids in to_send.values():
                self._read_sent.update(mids)
            snap = dict(to_send)

            def work_grp():
                for from_ip, mids in snap.items():
                    for mid in mids:
                        self.chat.send_receipt(from_ip, mid, "read")
            threading.Thread(target=work_grp, daemon=True).start()
        else:
            pending = [e["mid"] for e in self._conversations.get(key, [])
                       if e.get("kind") == "in" and not e.get("deleted")
                       and e.get("mid") and e["mid"] not in self._read_sent]
            if not pending:
                return
            self._read_sent.update(pending)
            ip = key

            def work():
                for mid in pending:
                    self.chat.send_receipt(ip, mid, "read")
            threading.Thread(target=work, daemon=True).start()

    # ── delete ────────────────────────────────────────────────────────────────
    _REACTION_EMOJIS = ("👍", "❤️", "😂", "😮", "😢", "🙏")

    def _msg_menu(self, entry: dict, gpos) -> None:
        m = QMenu(self)
        m.addAction("↩ Reply", lambda: self._set_reply(self._entry_sender(entry),
                                                       entry.get("text", "")))
        if entry.get("text"):
            m.addAction("➤ Forward", lambda: self._forward(entry))
        react_menu = m.addMenu("React 😊")
        mid = entry.get("mid", "")
        for emoji in self._REACTION_EMOJIS:
            react_menu.addAction(emoji,
                                 lambda _=False, e=emoji, m_=mid: self._toggle_reaction(m_, e))
        m.addSeparator()
        if (entry.get("kind") == "out"
                and time.time() - entry.get("ts", 0) <= _DELETE_WINDOW):
            m.addAction("🚫 Delete for everyone", lambda: self._delete_everyone(entry))
        m.addAction("🗑 Delete for me", lambda: self._delete_for_me(entry))
        m.exec(gpos)

    def _forward(self, entry: dict) -> None:
        text = entry.get("text", "")
        if not text:
            return
        targets: dict[str, str] = {}
        for gid, g in self._groups.items():
            targets[f"👥 {g.get('name', 'Group')}"] = f"group:{gid}"
        for ip in self._visible_peers(self.chat.peers()):
            if ip == DemoBot.IP or ip == self._active:
                continue
            targets[f"{self._display_name(ip)} ({ip})"] = ip
        if not targets:
            self._log("No other chats to forward to yet.")
            return
        items = list(targets.keys())
        choice, ok = QInputDialog.getItem(self, "Forward", "Forward to:", items, 0, False)
        if not ok or not choice:
            return
        key = targets[choice]
        e = _mk_entry("out", "You", text, time.time(), status="sent", fwd=True)
        mid = e["mid"]
        self._store(key, e)
        self._persist(key)
        if self._is_group(key):
            meta = self._group_meta(key[6:])
            threading.Thread(target=lambda: self.chat.send_group(meta, text, mid=mid),
                             daemon=True).start()
        else:
            threading.Thread(target=lambda: self.chat.send(key, text, mid=mid),
                             daemon=True).start()
        self.select_peer(key)
        self._log("Message forwarded.")

    def _delete_for_me(self, entry: dict) -> None:
        mid = entry.get("mid")
        loc = self._mid_index.get(mid)
        key = loc[0] if loc else self._active
        if not key:
            return
        self._conversations[key] = [e for e in self._conversations.get(key, [])
                                    if e.get("mid") != mid]
        self._mid_index.pop(mid, None)
        self._persist(key)
        if key == self._active:
            self._render(key)

    def _delete_everyone(self, entry: dict) -> None:
        mid = entry.get("mid")
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, e = loc
        e["deleted"] = True
        e["text"] = ""
        e.pop("reply", None)
        self._persist(key)
        if key == self._active:
            self._render(key)
        if self._is_group(key):
            gid = key[6:]
            meta = self._group_meta(gid)
            targets = [m for m in meta.get("members", []) if m and m != self.chat.my_ip]
            threading.Thread(target=lambda: [self.chat.send_delete(t, mid, gid=gid)
                                             for t in targets], daemon=True).start()
        else:
            threading.Thread(target=lambda: self.chat.send_delete(key, mid),
                             daemon=True).start()

    def on_remote_delete(self, from_ip, mid) -> None:
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        if entry.get("kind") != "in":
            return   # only the original sender can delete-for-everyone
        entry["deleted"] = True
        entry["text"] = ""
        entry.pop("reply", None)
        self._persist(key)
        if key == self._active and self._visible:
            self._render(key)
        else:
            self.update_roster(self.chat.peers())

    # ── reactions ─────────────────────────────────────────────────────────────
    def _make_reaction_row(self, entry: dict) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)
        reactions = entry.get("reactions", {})
        for emoji, ips in reactions.items():
            lay.addWidget(self._reaction_pill(emoji, len(ips),
                                              self.chat.my_ip in ips,
                                              entry.get("mid", "")))
        lay.addStretch(1)
        if not reactions:
            w.hide()
        return w

    def _reaction_pill(self, emoji: str, count: int, my_reacted: bool,
                       mid: str) -> QPushButton:
        btn = QPushButton(f"{emoji} {count}")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        accent = theme.color("accent")
        border = accent if my_reacted else theme.color("border")
        bg = accent + "33" if my_reacted else "transparent"
        btn.setStyleSheet(
            f"QPushButton{{font-size:12px; padding:2px 7px; border-radius:10px;"
            f" border:1px solid {border}; background:{bg};"
            f" color:{theme.color('text_pri')};}}"
            f"QPushButton:hover{{background:{accent}33;"
            f" border-color:{accent};}}")
        btn.clicked.connect(lambda _=False, e=emoji, m=mid: self._toggle_reaction(m, e))
        return btn

    def _rebuild_reaction_row(self, mid: str) -> None:
        """Repopulate an existing reaction container after a toggle."""
        w = self._reaction_rows.get(mid)
        if w is None:
            return
        loc = self._mid_index.get(mid)
        if not loc:
            return
        _, entry = loc
        lay = w.layout()
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        reactions = entry.get("reactions", {})
        for emoji, ips in reactions.items():
            lay.addWidget(self._reaction_pill(emoji, len(ips),
                                              self.chat.my_ip in ips, mid))
        lay.addStretch(1)
        try:
            w.setVisible(bool(reactions))
        except RuntimeError:
            self._reaction_rows.pop(mid, None)

    def on_reaction(self, from_ip: str, mid: str, emoji: str) -> None:
        """Incoming reaction from a peer — toggle their entry in the reactions map."""
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        reactions = entry.setdefault("reactions", {})
        ips = reactions.setdefault(emoji, [])
        if from_ip in ips:
            ips.remove(from_ip)
            if not ips:
                del reactions[emoji]
        else:
            ips.append(from_ip)
        self._persist(key)
        self._rebuild_reaction_row(mid)

    def _toggle_reaction(self, mid: str, emoji: str) -> None:
        """Toggle our own reaction on a message, then notify peer(s)."""
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        reactions = entry.setdefault("reactions", {})
        ips = reactions.setdefault(emoji, [])
        my_ip = self.chat.my_ip
        if my_ip in ips:
            ips.remove(my_ip)
            if not ips:
                del reactions[emoji]
        else:
            ips.append(my_ip)
        self._persist(key)
        self._rebuild_reaction_row(mid)
        if self._is_group(key):
            gid = key[6:]
            targets = [m for m in self._group_meta(gid).get("members", [])
                       if m and m != self.chat.my_ip]
            threading.Thread(
                target=lambda: [self.chat.send_reaction(t, mid, emoji, gid=gid)
                                for t in targets],
                daemon=True).start()
        else:
            threading.Thread(
                target=lambda: self.chat.send_reaction(key, mid, emoji),
                daemon=True).start()

    def _seen_popup(self, mid: str) -> None:
        """Show who has/hasn't seen a group message."""
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        if not self._is_group(key):
            return
        gid = key[6:]
        all_recipients = [m for m in self._group_meta(gid).get("members", [])
                          if m != self.chat.my_ip]
        seen_by = set(entry.get("seen_by", {}).keys())
        not_seen = [m for m in all_recipients if m not in seen_by]

        popup = QMenu(self)
        hdr = popup.addAction("Seen by:")
        hdr.setEnabled(False)
        for ip in seen_by:
            name = self._aliases.get(ip) or self._names.get(ip, ip)
            popup.addAction(f"  ✓  {name}")
        if not seen_by:
            a = popup.addAction("  (none yet)")
            a.setEnabled(False)
        popup.addSeparator()
        hdr2 = popup.addAction("Not yet seen:")
        hdr2.setEnabled(False)
        for ip in not_seen:
            name = self._aliases.get(ip) or self._names.get(ip, ip)
            popup.addAction(f"  {name}")
        if not not_seen:
            a = popup.addAction("  (everyone has seen it)")
            a.setEnabled(False)
        popup.exec(QCursor.pos())

    # ── mobile web bridge ─────────────────────────────────────────────────────
    def on_mobile_join(self, session) -> None:
        """A phone submitted its name — show approval card or handle auto-approval."""
        sid = session.sid
        self._mobile_sessions[sid] = session
        key = f"mobile:{sid}"
        self._names[key] = f"{session.name} 📱"
        
        if session.state == "approved":
            self._sys(key, f"{session.name} 📱 has connected (auto-approved).")
            self.update_roster(self.chat.peers())
            if key == self._active:
                self._update_header_sub(self.chat.peers())
            return

        entry = _mk_entry("mobile_req", session.name, "", time.time(),
                          sid=sid, from_ip=session.ip, device=session.device)
        self._conversations.setdefault(key, [])
        self._store(key, entry)
        self._unread[key] = self._unread.get(key, 0) + 1
        self.update_roster(self.chat.peers())
        if self._notifications_enabled:
            self._toasts.notify(f"{session.name} 📱",
                                f"Wants to join from {session.ip}", key)
            self.activity.emit(key)
        if key == self._active and self._visible:
            self._append(entry)

    def on_mobile_leave(self, session) -> None:
        """A phone disconnected."""
        sid = session.sid
        key = f"mobile:{sid}"
        self._mobile_sessions.pop(sid, None)
        if session.state == "approved":
            self._sys(key, f"{session.name} 📱 disconnected.")
        self.update_roster(self.chat.peers())
        if key == self._active:
            self._update_header_sub(self.chat.peers())

    def on_mobile_message(self, session, text: str) -> None:
        """Approved mobile user sent a chat message."""
        key = f"mobile:{session.sid}"
        entry = _mk_entry("in", f"{session.name} 📱", text, time.time())
        self._store(key, entry)
        if key == self._active and self._visible:
            self._append(entry)
        else:
            self._unread[key] = self._unread.get(key, 0) + 1
            self.update_roster(self.chat.peers())
            if self._notifications_enabled:
                prev = text if len(text) <= 120 else text[:117] + "…"
                self._toasts.notify(f"{session.name} 📱", prev, key)
                self.activity.emit(key)

    def on_mobile_file(self, session, filename: str, save_path: str, size: int) -> None:
        """Approved mobile user uploaded a file."""
        key = f"mobile:{session.sid}"
        tid = uuid.uuid4().hex[:12]
        self._transfer_paths[tid] = save_path
        self._progress_text[tid] = "Saved!"
        self._add_file_entry(key, "file_in_offer", tid, filename, size, from_ip=session.ip)
        self._offer_states[tid] = "accepted"
        self._rerender_if_active(key)
        if key != self._active or not self._visible:
            self._unread[key] = self._unread.get(key, 0) + 1
            self.update_roster(self.chat.peers())
            if self._notifications_enabled:
                self._toasts.notify(f"{session.name} 📱", f"📎 Sent a file: {filename}", key)
                self.activity.emit(key)

    def on_mobile_download(self, sid: str, tid: str) -> None:
        """Mobile user started/finished downloading a file offered by desktop."""
        key = f"mobile:{sid}"
        self._set_progress(tid, "Sent!")
        self._rerender_if_active(key)

    def _make_mobile_req_bubble(self, entry: dict) -> QWidget:
        sid = entry.get("sid", "")
        sess = self._mobile_sessions.get(sid)
        state = sess.state if sess else "disconnected"
        card = QFrame()
        card.setObjectName("card2")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        hd = QLabel(f"📱 {entry.get('sender', 'Mobile')} wants to join")
        hd.setStyleSheet("font-weight:700; font-size:13px;")
        v.addWidget(hd)
        ip = entry.get("from_ip", "")
        dev = entry.get("device", "")
        if ip:
            info = QLabel(f"From: {ip}" + (f"  ·  {dev[:40]}" if dev else ""))
            info.setObjectName("muted")
            v.addWidget(info)
        if state == "pending":
            brow = QHBoxLayout()
            acc = QPushButton("Approve")
            acc.setProperty("variant", "success")
            acc.clicked.connect(lambda: self._approve_mobile(sid))
            rej = QPushButton("Reject")
            rej.clicked.connect(lambda: self._reject_mobile(sid))
            blk = QPushButton("Block")
            blk.setProperty("variant", "danger")
            blk.clicked.connect(lambda: self._block_mobile(sid))
            brow.addWidget(acc); brow.addWidget(rej); brow.addWidget(blk)
            brow.addStretch(1)
            v.addLayout(brow)
        elif state == "approved":
            lbl = QLabel("✓ Approved — chatting now")
            lbl.setStyleSheet("color:%s;" % theme.color("success"))
            v.addWidget(lbl)
        elif state == "blocked":
            lbl = QLabel("Blocked")
            lbl.setStyleSheet("color:%s;" % theme.color("danger"))
            v.addWidget(lbl)
        else:
            lbl = QLabel("Rejected / Disconnected")
            lbl.setObjectName("muted")
            v.addWidget(lbl)
        return card

    def _approve_mobile(self, sid: str) -> None:
        sess = self._mobile_sessions.get(sid)
        if not sess or not self._mobile:
            return
        key = f"mobile:{sid}"
        
        # Save to registry approved devices list
        from ..config import load_approved_mobile_devices, save_approved_mobile_devices
        approved = load_approved_mobile_devices()
        if sid not in approved:
            approved.append(sid)
            save_approved_mobile_devices(approved)

        history = list(self._conversations.get(key, []))
        self._mobile.approve(sid, history)
        self._sys(key, f"{sess.name} has been approved and joined the chat.")
        self._rerender_if_active(key)
        self.update_roster(self.chat.peers())

    def _reject_mobile(self, sid: str) -> None:
        sess = self._mobile_sessions.get(sid)
        if not sess or not self._mobile:
            return
        self._mobile.reject(sid)
        key = f"mobile:{sid}"
        self._sys(key, f"{sess.name} was rejected.")
        self._rerender_if_active(key)

    def _block_mobile(self, sid: str) -> None:
        sess = self._mobile_sessions.get(sid)
        if not sess or not self._mobile:
            return
        self._mobile.block(sid)
        key = f"mobile:{sid}"
        
        # Remove from registry approved devices list
        from ..config import load_approved_mobile_devices, save_approved_mobile_devices
        approved = load_approved_mobile_devices()
        if sid in approved:
            approved.remove(sid)
            save_approved_mobile_devices(approved)

        self._sys(key, f"{sess.name} ({sess.ip}) has been blocked.")
        self._rerender_if_active(key)
        self._log(f"Mobile IP {sess.ip} blocked.")

    def _show_qr_dialog(self) -> None:
        from PyQt6.QtGui import QPixmap

        if not self._mobile:
            QMessageBox.information(self, "Mobile Access",
                                    "Mobile server is not configured.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Mobile Access")
        dlg.setMinimumWidth(360)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(10)

        # ── server not available (aiohttp missing) ──────────────────────────
        if not self._mobile.running:
            err = QLabel(
                "Mobile server is not available.\n\n"
                "Install required packages and restart:\n"
                "    pip install aiohttp qrcode pillow"
            )
            err.setWordWrap(True)
            err.setStyleSheet("color:#ef4444;font-weight:bold;")
            v.addWidget(err)
            ok = QPushButton("Close")
            ok.clicked.connect(dlg.close)
            v.addWidget(ok)
            dlg.exec()
            return

        # ── server failed to start (port conflict, etc.) ────────────────────
        if self._mobile.start_error:
            err = QLabel(f"Server failed to start:\n{self._mobile.start_error}")
            err.setWordWrap(True)
            err.setStyleSheet("color:#ef4444;font-weight:bold;")
            v.addWidget(err)
            ok = QPushButton("Close")
            ok.clicked.connect(dlg.close)
            v.addWidget(ok)
            dlg.exec()
            return

        # ── QR code ─────────────────────────────────────────────────────────
        qr_url = self._mobile.get_qr_url()
        if qr_url:
            try:
                from ..mobile import make_qr_png
                png = make_qr_png(qr_url)
                if png:
                    v.addWidget(QLabel("Scan with your phone camera to join:"),
                                alignment=Qt.AlignmentFlag.AlignCenter)
                    pm = QPixmap()
                    pm.loadFromData(png)
                    ql = QLabel()
                    ql.setPixmap(pm.scaled(240, 240,
                                           Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
                    ql.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    v.addWidget(ql)
                else:
                    hint = QLabel("QR unavailable — install: pip install qrcode pillow")
                    hint.setObjectName("muted")
                    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    v.addWidget(hint)
            except Exception:
                pass

        # ── URL list ─────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep)

        lbl_hdr = QLabel("Open in phone browser:")
        lbl_hdr.setStyleSheet("font-weight:bold;")
        v.addWidget(lbl_hdr)

        labeled = self._mobile.get_access_urls_labeled()
        if not labeled:
            labeled = [("Network", qr_url)] if qr_url else []

        for net_label, url in labeled:
            row = QWidget()
            row_h = QHBoxLayout(row)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(8)
            tag = QLabel(net_label)
            tag.setObjectName("muted")
            tag.setFixedWidth(100)
            url_lbl = QLabel(url)
            url_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            url_lbl.setStyleSheet("font-weight:bold;font-size:13px;")
            row_h.addWidget(tag)
            row_h.addWidget(url_lbl, 1)
            v.addWidget(row)

        # ── firewall tip ──────────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(sep2)

        fw = QLabel(
            "If your phone says \"site can't be reached\", Windows Firewall is\n"
            "blocking port 8765. Run this once as Administrator:\n\n"
            f"netsh advfirewall firewall add rule name=\"NST Mobile\"\n"
            f" dir=in action=allow protocol=TCP localport={MOBILE_HTTP_PORT}"
        )
        fw.setObjectName("muted")
        fw.setWordWrap(True)
        fw.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fw.setStyleSheet("font-size:11px;")
        v.addWidget(fw)

        ok = QPushButton("Close")
        ok.clicked.connect(dlg.close)
        v.addWidget(ok)
        dlg.adjustSize()
        dlg.exec()

    # ── typing indicators ─────────────────────────────────────────────────────
    def _on_typing_edit(self, _text: str) -> None:
        key = self._active
        if not key or key == DemoBot.IP or self._is_mobile(key) or not self._entry.text().strip():
            return
        now = time.time()
        if not self._typing_active or now - self._typing_last_sent > 2.0:
            self._typing_active = True
            self._typing_last_sent = now
            self._send_typing(key, True)
        self._typing_stop_timer.start(4000)

    def _stop_typing(self) -> None:
        if self._typing_active and self._active:
            self._typing_active = False
            self._send_typing(self._active, False)
        self._typing_stop_timer.stop()

    def _send_typing(self, key, is_typing) -> None:
        if self._is_group(key):
            gid = key[6:]
            targets = [m for m in self._group_meta(gid).get("members", [])
                       if m and m != self.chat.my_ip]
            threading.Thread(target=lambda: [self.chat.send_typing(t, is_typing, gid=gid)
                                             for t in targets], daemon=True).start()
        else:
            threading.Thread(target=lambda: self.chat.send_typing(key, is_typing),
                             daemon=True).start()

    def on_typing(self, ip, name, gid, is_typing) -> None:
        key = f"group:{gid}" if gid else ip
        self._names.setdefault(ip, name)
        typers = self._typers.setdefault(key, {})
        if is_typing:
            typers[ip] = time.time() + 6.0
        else:
            typers.pop(ip, None)
        if key == self._active:
            self._refresh_typing()

    def _typing_tick(self) -> None:
        now = time.time()
        changed = False
        for key, typers in self._typers.items():
            for ip in [i for i, exp in typers.items() if exp <= now]:
                typers.pop(ip, None)
                changed = True
        if changed and self._active:
            self._refresh_typing()

    def _refresh_typing(self) -> None:
        key = self._active
        typers = self._typers.get(key, {}) if key else {}
        live = [ip for ip, exp in typers.items() if exp > time.time()]
        if not live:
            self._typing_lbl.hide()
            return
        if self._is_group(key):
            if len(live) == 1:
                who = self._aliases.get(live[0]) or self._names.get(live[0], live[0])
                txt = f"{who} is typing…"
            else:
                txt = f"{len(live)} people are typing…"
        else:
            txt = "typing…"
        self._typing_lbl.setText(txt)
        self._typing_lbl.show()

    # ── demo ──────────────────────────────────────────────────────────────────
    def _start_demo(self) -> None:
        if not self.chat.has_demo():
            self.chat.add_demo_bot()
            self._log("Demo chat started — say hi to the Demo Bot.")
        QTimer.singleShot(150, lambda: self.select_peer(DemoBot.IP))

    # ── manual IP ─────────────────────────────────────────────────────────────
    def _connect_manual_ip(self) -> None:
        ip = self._ip_edit.text().strip()
        if not ip:
            return
        if not is_valid_ipv4(ip):
            self._log(f"Invalid IP: {ip!r} — enter a valid IPv4 address (e.g. 192.168.1.20).")
            return
        if ip == self.chat.my_ip:
            self._log("Cannot chat with yourself.")
            return
        name, ok = QInputDialog.getText(self, "Name this PC", f"Enter a name for {ip}:")
        if ok and name.strip():
            self._aliases[ip] = name.strip()[:32]
        self.chat.add_manual_peer(ip)
        self._names.setdefault(ip, ip)
        self._ip_edit.clear()
        self.select_peer(ip)
        self._save_peer(ip)
        threading.Thread(target=self._probe_manual, args=(ip,), daemon=True).start()

    def _probe_manual(self, ip) -> None:
        if not check_host_reachable(ip, CHAT_TCP_PORT):
            QTimer.singleShot(0, lambda: self._sys(
                ip, "Not reachable — make sure the app is running on that PC."))

    # ── alias / delete ────────────────────────────────────────────────────────
    def _edit_alias(self) -> None:
        ip = self._active
        if not ip or self._is_group(ip) or ip == DemoBot.IP:
            return
        cur = self._aliases.get(ip, "")
        name, ok = QInputDialog.getText(self, "Save name", f"Name for {ip}:",
                                        text=cur)
        if not ok:
            return
        name = name.strip()[:32]
        if name:
            self._aliases[ip] = name
        else:
            self._aliases.pop(ip, None)
        self._save_peer(ip)
        self._head_name.setText(self._display_name(ip))
        self._head_avatar.set_name(self._display_name(ip))
        self.update_roster(self.chat.peers())
        self._log(f"Saved name for {ip}.")

    def _delete_peer(self, ip) -> None:
        name = self._display_name(ip)
        if self._is_mobile(ip):
            sid = ip[7:]
            if self._mobile:
                self._mobile.disconnect(sid)
            self._mobile_sessions.pop(sid, None)
            self._drop_index(ip)
            for d in (self._conversations, self._unread, self._names, self._typers):
                d.pop(ip, None)
            if self._active == ip:
                self._reset_active()
            self.update_roster(self.chat.peers())
            return
        if QMessageBox.question(self, "Remove peer",
                                f"Remove {name} and delete its chat history?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.chat.remove_peer(ip)
        self._drop_index(ip)
        for d in (self._conversations, self._unread, self._names, self._devices,
                  self._aliases, self._chat_req_states, self._typers):
            d.pop(ip, None)
        self._delete_history_file(ip)
        if self._active == ip:
            self._reset_active()
        self.update_roster(self.chat.peers())
        self._log(f"Removed {name} from the chat list.")

    def _reset_active(self) -> None:
        self._active = None
        self._head_name.setText("LAN Chat")
        self._head_sub.setText("Select a peer on the left")
        self._head_avatar.set_name("LAN")
        self._set_composer_visible(False)
        self._show_empty_state()

    # ── groups ────────────────────────────────────────────────────────────────
    def _member_dialog(self, title, exclude) -> list[str] | None:
        cands = [ip for ip in (set(self._names) | set(self._aliases) | set(self._conversations))
                 if ip not in exclude and ip != self.chat.my_ip
                 and ip != DemoBot.IP and not self._is_group(ip)]
        cands.sort(key=lambda x: self._display_name(x).lower())
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(360, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Select members"))
        lst = QListWidget()
        boxes = {}
        for ip in cands:
            it = QListWidgetItem()
            cb = QCheckBox(f"{self._display_name(ip)}  ({ip})")
            boxes[ip] = cb
            lst.addItem(it)
            lst.setItemWidget(it, cb)
        v.addWidget(lst, 1)
        v.addWidget(QLabel("Add an IP (optional)"))
        extra = QLineEdit()
        extra.setPlaceholderText("e.g. 192.168.1.20")
        v.addWidget(extra)
        brow = QHBoxLayout()
        brow.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(dlg.reject)
        okb = QPushButton("OK")
        okb.setProperty("variant", "accent")
        okb.clicked.connect(dlg.accept)
        brow.addWidget(cancel)
        brow.addWidget(okb)
        v.addLayout(brow)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        chosen = [ip for ip, cb in boxes.items() if cb.isChecked()]
        ex = extra.text().strip()
        if ex and is_valid_ipv4(ex) and ex != self.chat.my_ip and ex not in exclude:
            chosen.append(ex)
        return list(dict.fromkeys(chosen))

    def _new_group_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        if not ok or not name.strip():
            return
        members = self._member_dialog("New group members", {self.chat.my_ip})
        if not members:
            return
        gid = uuid.uuid4().hex[:12]
        self._groups[gid] = {"name": name.strip()[:32], "members": members}
        self._conversations.setdefault(f"group:{gid}", [])
        for ip in members:
            if not self.chat.is_manual_peer(ip):
                self.chat.add_manual_peer(ip)
        meta = self._group_meta(gid)
        threading.Thread(
            target=lambda: self.chat.send_group(
                meta, f"{self.chat.my_name} created group \"{name.strip()}\"",
                msg_type="group_invite"), daemon=True).start()
        self._save_group(gid)
        self.update_roster(self.chat.peers())
        self.select_peer(f"group:{gid}")
        self._log(f"Group \"{name.strip()}\" created with {len(members)} member(s).")

    def _add_group_members(self) -> None:
        key = self._active
        if not key or not self._is_group(key):
            return
        gid = key[6:]
        existing = set(self._group_meta(gid).get("members", []))
        new = self._member_dialog("Add members", existing)
        if not new:
            return
        new = [ip for ip in new if ip not in existing]
        if not new:
            return
        g = self._groups.get(gid)
        g["members"] = list(dict.fromkeys(list(g.get("members", [])) + new))
        for ip in new:
            if not self.chat.is_manual_peer(ip):
                self.chat.add_manual_peer(ip)
        meta = self._group_meta(gid)
        threading.Thread(
            target=lambda: self.chat.send_group(
                meta, f"{self.chat.my_name} added {len(new)} member(s)",
                msg_type="group_invite"), daemon=True).start()
        self._save_group(gid)
        self._update_header_sub(self.chat.peers())
        self.update_roster(self.chat.peers())
        self._log(f"Added {len(new)} member(s) to \"{g.get('name', 'Group')}\".")

    def _delete_group(self, key) -> None:
        gid = key[6:]
        name = self._display_name(key)
        if QMessageBox.question(self, "Leave group",
                                f"Leave \"{name}\" and delete its history here?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._drop_index(key)
        self._groups.pop(gid, None)
        self._conversations.pop(key, None)
        self._unread.pop(key, None)
        self._typers.pop(key, None)
        self._delete_history_file(key)
        if self._active == key:
            self._reset_active()
        self.update_roster(self.chat.peers())
        self._log(f"Left group \"{name}\".")

    # ── clear ─────────────────────────────────────────────────────────────────
    def _clear_chat(self) -> None:
        key = self._active
        if not key:
            return
        self._drop_index(key)
        if self._is_group(key):
            self._conversations[key] = []
            self._unread.pop(key, None)
            self._save_group(key[6:])
        else:
            self._conversations.pop(key, None)
            self._unread.pop(key, None)
            self._save_peer(key)
        self._render(key)
        self._log(f"Chat with {self._display_name(key)} cleared.")

    # ── file transfer ─────────────────────────────────────────────────────────
    def _make_file_bubble(self, entry: dict) -> QWidget:
        kind = entry["kind"]
        tid = entry["tid"]
        ts = entry.get("ts", 0)
        meta = {"filename": entry.get("filename", ""), "size": entry.get("size", 0),
                "from_ip": entry.get("from_ip")}
        is_out = kind == "file_out"
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 2, 4, 2)
        bubble = QFrame()
        bubble.setProperty("bubble", "out" if is_out else "in")
        bubble.setMaximumWidth(_BUBBLE_MAX)
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(12, 8, 12, 8)
        bv.setSpacing(3)
        txcol = theme.color("bubble_out_tx" if is_out else "bubble_in_tx")

        title = QLabel(f"📎 {meta['filename']}")
        title.setStyleSheet("color:%s; font-weight:700;" % txcol)
        bv.addWidget(title)
        bv.addWidget(QLabel(_fmt_size(meta["size"])))

        state = self._offer_states.get(tid, "pending")
        if kind == "file_in_offer" and state == "pending" and (time.time() - ts <= 60):
            brow = QHBoxLayout()
            acc = QPushButton("Accept")
            acc.setProperty("variant", "success")
            acc.clicked.connect(lambda: self._accept_file(tid, meta["from_ip"],
                                                          meta["filename"], meta["size"]))
            rej = QPushButton("Reject")
            rej.setProperty("variant", "danger")
            rej.clicked.connect(lambda: self._reject_file(tid, meta["from_ip"]))
            brow.addWidget(acc)
            brow.addWidget(rej)
            brow.addStretch(1)
            bv.addLayout(brow)
        else:
            prog = QLabel(self._progress_text.get(tid, "…"))
            prog.setStyleSheet("color:%s; font-size:11px;" % txcol)
            bv.addWidget(prog)
            self._progress_lbls[tid] = prog
            done = self._transfer_paths.get(tid)
            if done is None:
                cancel = QPushButton("Cancel")
                cancel.setProperty("variant", "danger")
                cancel.clicked.connect(lambda: self._cancel_file(tid))
                bv.addWidget(cancel)
            elif done:
                orow = QHBoxLayout()
                of = QPushButton("Open File")
                of.clicked.connect(lambda: os.startfile(done))
                ofd = QPushButton("Open Folder")
                ofd.clicked.connect(lambda: subprocess.Popen(f'explorer /select,"{done}"', shell=True))
                orow.addWidget(of)
                orow.addWidget(ofd)
                orow.addStretch(1)
                bv.addLayout(orow)

        stamp = QLabel(time.strftime("%H:%M", time.localtime(ts)))
        stamp.setStyleSheet("font-size:10px; color:%s;" % txcol)
        bv.addWidget(stamp, alignment=Qt.AlignmentFlag.AlignRight)
        if is_out:
            h.addStretch(1); h.addWidget(bubble)
        else:
            h.addWidget(bubble); h.addStretch(1)
        return row

    def _set_progress(self, tid: str, text: str) -> None:
        self._progress_text[tid] = text
        lbl = self._progress_lbls.get(tid)
        if lbl is not None:
            try:
                lbl.setText(text)
            except RuntimeError:
                self._progress_lbls.pop(tid, None)

    def _attach_file(self) -> None:
        ip = self._active
        if not ip or self._is_group(ip):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Send file")
        if not path:
            return
        filename = os.path.basename(path)
        size = os.path.getsize(path)

        tid = uuid.uuid4().hex[:12]
        if self._is_mobile(ip):
            self._transfer_paths[tid] = path
            self._progress_text[tid] = "Serving to mobile..."
        else:
            self._transfer_paths[tid] = None
            self._progress_text[tid] = f"Waiting for {self._display_name(ip)} to accept…"
        self._add_file_entry(ip, "file_out", tid, filename, size)

        if self._is_mobile(ip):
            sid = ip[7:]
            if self._mobile:
                self._mobile.register_pending_file(tid, path)
                self._mobile.send_file_offer(sid, tid, filename, size)
            return

        threading.Thread(target=self._offer_worker,
                         args=(ip, path, filename, size, tid), daemon=True).start()

    def _offer_worker(self, ip, path, filename, size, tid) -> None:
        holder = {"tid": tid}

        def progress(done, total, speed, elapsed, eta):
            if holder["tid"]:
                pct = int(done * 100 / total) if total else 0
                def main_prog():
                    self._set_progress(holder["tid"], f"Sending {pct}%  {_fmt_speed(speed)}  ETA {_fmt_eta(eta)}")
                QTimer.singleShot(0, main_prog)

        def done():
            tid = holder["tid"]
            if tid:
                self._transfer_paths[tid] = path
                def main_done():
                    self._set_progress(tid, "Sent!")
                    self._rerender_if_active(ip)
                QTimer.singleShot(0, main_done)

        def error(msg):
            tid = holder["tid"]
            if tid:
                self._transfer_paths[tid] = ""
                def main_error():
                    self._set_progress(tid, f"Failed: {msg}")
                    self._rerender_if_active(ip)
                QTimer.singleShot(0, main_error)

        def expire():
            tid = holder["tid"]
            if tid:
                def main_expire():
                    self._set_progress(tid, "No response — expired")
                QTimer.singleShot(0, main_expire)

        try:
            self._ft.offer_file(ip, path, tid=tid, progress_cb=progress, done_cb=done,
                                error_cb=error, expire_cb=expire)
        except Exception as e:
            self._transfer_paths[tid] = ""
            def main_exc():
                self._set_progress(tid, f"Failed: {e}")
                self._rerender_if_active(ip)
            QTimer.singleShot(0, main_exc)

    def _add_file_entry(self, ip, kind, tid, filename, size, from_ip=None) -> None:
        entry = _mk_entry(kind, "", "", time.time(), tid=tid, filename=filename,
                          size=size, from_ip=from_ip)
        self._store(ip, entry)
        if ip == self._active and (kind == "file_out" or self._visible):
            self._append(entry)
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())

    def _rerender_if_active(self, ip) -> None:
        if ip == self._active:
            self._render(ip)

    def on_file_offer_received(self, ip, name, msg) -> None:
        tid = msg["transfer_id"]
        self._names[ip] = name
        self._offer_states[tid] = "pending"
        self._add_file_entry(ip, "file_in_offer", tid, msg["filename"], msg["size"], from_ip=ip)
        if not (ip == self._active and self._visible) and self._notifications_enabled:
            self._toasts.notify(name, f"📎 Wants to send: {msg['filename']}", ip)
            self.activity.emit(ip)

    def _accept_file(self, tid, from_ip, filename, size) -> None:
        from_ip = from_ip or self._active
        if not from_ip:
            return
        self._offer_states[tid] = "accepted"
        self._transfer_paths[tid] = None
        self._set_progress(tid, "Connecting…")
        self._rerender_if_active(from_ip)

        def progress(done, total, speed, elapsed, eta):
            pct = int(done * 100 / total) if total else 0
            def main_prog():
                self._set_progress(tid, f"Receiving {pct}%  {_fmt_speed(speed)}  ETA {_fmt_eta(eta)}")
            QTimer.singleShot(0, main_prog)

        def fdone(save_path):
            self._transfer_paths[tid] = save_path
            def main_fdone():
                self._set_progress(tid, "Saved!")
                self._rerender_if_active(from_ip)
            QTimer.singleShot(0, main_fdone)

        def ferr(msg):
            self._transfer_paths[tid] = ""
            def main_ferr():
                self._set_progress(tid, f"Failed: {msg}")
                self._rerender_if_active(from_ip)
            QTimer.singleShot(0, main_ferr)

        def work():
            self._ft.send_accept(from_ip, tid)
            self._ft.receive_file(tid, from_ip, progress_cb=progress,
                                  done_cb=fdone, error_cb=ferr)
        threading.Thread(target=work, daemon=True).start()

    def _reject_file(self, tid, from_ip) -> None:
        from_ip = from_ip or self._active
        if not from_ip:
            return
        self._offer_states[tid] = "rejected"
        self._transfer_paths[tid] = ""
        self._set_progress(tid, "Rejected")
        self._rerender_if_active(from_ip)
        threading.Thread(target=lambda: self._ft.send_reject(from_ip, tid), daemon=True).start()

    def _cancel_file(self, tid) -> None:
        self._ft.cancel_transfer(tid)
        self._transfer_paths[tid] = ""
        self._set_progress(tid, "Cancelled")
        if self._active:
            self._render(self._active)

    def on_file_accepted(self, ip, name, msg) -> None:
        self._set_progress(msg["transfer_id"], f"{name} accepted — sending…")

    def on_file_rejected(self, ip, name, msg) -> None:
        tid = msg["transfer_id"]
        self._ft.cancel_offer(tid)
        self._transfer_paths[tid] = ""
        self._set_progress(tid, f"Rejected by {name}")
        self._rerender_if_active(ip)

    # ── chat requests (external IP first contact) ─────────────────────────────
    def _make_req_bubble(self, entry: dict) -> QWidget:
        ip = entry.get("from_ip", "")
        meta = {"from_name": entry.get("sender", ip), "first_msg": entry.get("text", "")}
        state = self._chat_req_states.get(ip, "pending")
        card = QFrame()
        card.setObjectName("card2")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.addWidget(QLabel(f"{meta['from_name']} ({ip}) wants to chat"))
        if meta.get("first_msg"):
            q = QLabel(f"\"{meta['first_msg'][:80]}\"")
            q.setObjectName("muted")
            v.addWidget(q)
        if state == "pending":
            brow = QHBoxLayout()
            acc = QPushButton("Accept")
            acc.setProperty("variant", "success")
            acc.clicked.connect(lambda: self._accept_chat(ip))
            blk = QPushButton("Block")
            blk.setProperty("variant", "danger")
            blk.clicked.connect(lambda: self._block_chat(ip))
            brow.addWidget(acc); brow.addWidget(blk); brow.addStretch(1)
            v.addLayout(brow)
        elif state == "accepted":
            ok = QLabel("Accepted — messages will now appear normally.")
            ok.setStyleSheet("color:%s;" % theme.color("success"))
            v.addWidget(ok)
        else:
            bl = QLabel("Blocked — messages from this IP are discarded.")
            bl.setStyleSheet("color:%s;" % theme.color("danger"))
            v.addWidget(bl)
        return card

    def on_chat_request_received(self, ip, name, msg) -> None:
        if ip in self._chat_req_states:
            if self._chat_req_states[ip] == "accepted":
                self.chat.approve_ip(ip)
            return
        self._names[ip] = name
        self._chat_req_states[ip] = "pending"
        entry = _mk_entry("chat_req", name, str(msg.get("text", "")), time.time(),
                          from_ip=ip)
        self._store(ip, entry)
        if ip == self._active and self._visible:
            self._append(entry)
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())
        if self._notifications_enabled:
            self._toasts.notify(name, "Wants to chat — tap to respond", ip)
            self.activity.emit(ip)

    def _accept_chat(self, ip) -> None:
        self._chat_req_states[ip] = "accepted"
        self.chat.approve_ip(ip)
        self._save_peer(ip)
        self._rerender_if_active(ip)

    def _block_chat(self, ip) -> None:
        self._chat_req_states[ip] = "blocked"
        self.chat.block_ip(ip)
        self._save_peer(ip)
        self._rerender_if_active(ip)

    # ── persistence (JSON entry-dicts, with legacy-tuple migration) ───────────
    def _trim(self, key) -> None:
        m = self._conversations.get(key)
        if m and len(m) > _MAX_HISTORY:
            dropped, self._conversations[key] = m[:-_MAX_HISTORY], m[-_MAX_HISTORY:]
            for e in dropped:
                if isinstance(e, dict):
                    self._mid_index.pop(e.get("mid"), None)

    def _index_conversation(self, key) -> None:
        for e in self._conversations.get(key, []):
            if isinstance(e, dict) and e.get("mid"):
                self._mid_index[e["mid"]] = (key, e)

    def _load_history(self) -> None:
        try:
            d = config.get_peer_chat_dir()
            for fname in os.listdir(d):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ip = data.get("ip")
                    if not ip:
                        continue
                    self._conversations[ip] = [_migrate_entry(m) for m in
                                               data.get("messages", [])[-_MAX_HISTORY:]]
                    self._index_conversation(ip)
                    if self._is_group(ip) and isinstance(data.get("group"), dict):
                        gid = ip[6:]
                        g = data["group"]
                        self._groups[gid] = {"name": g.get("name", "Group"),
                                             "members": [m for m in g.get("members", []) if m]}
                        for m in self._groups[gid]["members"]:
                            if m and m != self.chat.my_ip and not self.chat.is_manual_peer(m):
                                self.chat.add_manual_peer(m)
                        continue
                    if data.get("name"):
                        self._names[ip] = data["name"]
                    if data.get("device"):
                        self._devices[ip] = data["device"]
                    if data.get("alias"):
                        self._aliases[ip] = data["alias"]
                    if data.get("manual"):
                        self.chat.add_manual_peer(ip)
                    if data.get("approved"):
                        self.chat.approve_ip(ip)
                        self._chat_req_states[ip] = "accepted"
                    elif data.get("blocked"):
                        self.chat.block_ip(ip)
                        self._chat_req_states[ip] = "blocked"
                    # Restore last-seen so the peer shows "last seen …" until it
                    # comes back online; fall back to the newest message time.
                    ls = data.get("last_seen") or 0.0
                    if not ls:
                        try:
                            ls = max((float(m.get("ts", 0)) for m in self._conversations[ip]),
                                     default=0.0)
                        except (TypeError, ValueError):
                            ls = 0.0
                    self.chat.seed_last_seen(ip, ls)
                except Exception:
                    pass
        except Exception:
            pass

    def _save_peer(self, ip) -> None:
        msgs = list(self._conversations.get(ip, []))
        name = self._names.get(ip, ip)
        device = self._devices.get(ip)
        alias = self._aliases.get(ip)
        manual = self.chat.is_manual_peer(ip)
        last_seen = self.chat.last_seen_of(ip)

        def write():
            try:
                safe = ip.replace(".", "_").replace(":", "_")
                kept = [m for m in msgs
                        if m.get("kind") not in ("file_out", "file_in_offer", "chat_req")]
                data = {"ip": ip, "name": name,
                        "messages": kept[-_MAX_HISTORY:]}
                if device:
                    data["device"] = device
                if alias:
                    data["alias"] = alias
                if manual:
                    data["manual"] = True
                if last_seen:
                    data["last_seen"] = last_seen
                if ip in self.chat._approved_ips:
                    data["approved"] = True
                if ip in self.chat._blocked_ips:
                    data["blocked"] = True
                with open(os.path.join(config.get_peer_chat_dir(), f"{safe}.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass
        threading.Thread(target=write, daemon=True).start()

    def _save_group(self, gid) -> None:
        key = f"group:{gid}"
        msgs = list(self._conversations.get(key, []))
        group = dict(self._groups.get(gid, {}))

        def write():
            try:
                kept = [m for m in msgs
                        if m.get("kind") not in ("file_out", "file_in_offer", "chat_req")]
                data = {"ip": key,
                        "group": {"name": group.get("name", "Group"),
                                  "members": group.get("members", [])},
                        "messages": kept[-_MAX_HISTORY:]}
                with open(os.path.join(config.get_peer_chat_dir(), f"group_{gid}.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass
        threading.Thread(target=write, daemon=True).start()

    def _delete_history_file(self, key) -> None:
        def rm():
            try:
                safe = key.replace(".", "_").replace(":", "_")
                p = os.path.join(config.get_peer_chat_dir(), f"{safe}.json")
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        threading.Thread(target=rm, daemon=True).start()

    def shutdown(self) -> None:
        try:
            self._ft.stop()
        except Exception:
            pass

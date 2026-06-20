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

from PyQt6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog,
                             QFrame, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QToolButton, QVBoxLayout, QWidget)

from .. import __version__, config
from ..chat import DemoBot
from ..constants import CHAT_TCP_PORT
from ..filetransfer import FileTransferService
from ..netinfo import check_host_reachable, is_valid_ipv4
from . import sound
from .settings_dialog import SettingsDialog
from .theme import theme
from .widgets import Avatar, AvatarWithStatus, Dot, ToggleSwitch, hline

_PLACEHOLDER = "Type a message..."
_MAX_HISTORY = 200
_BUBBLE_MAX = 420  # fallback before the window is realized


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


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def _fmt_progress(verb: str, done: int, total: int,
                  speed: float, elapsed: float, eta: float) -> str:
    """One-line transfer status: e.g. 'Receiving 42% · 4.2/10.0 MB · 2.1 MB/s · 2s · ETA 3s'."""
    pct = int(done * 100 / total) if total else 0
    return (f"{verb} {pct}% · {_fmt_size(done)}/{_fmt_size(total)} · "
            f"{_fmt_speed(speed)} · {_fmt_eta(elapsed)} · ETA {_fmt_eta(eta)}")


def _xfer_fail_text(msg: str) -> str:
    """Friendly terminal status for a failed transfer ('Cancelled' vs 'Failed: ...')."""
    low = (msg or "").lower()
    if "cancel" in low or "interrupt" in low:
        return "Cancelled"
    return f"Failed: {msg}"


def _reveal_in_explorer(path: str) -> None:
    """Open Explorer with *path* selected, without flashing a console window."""
    try:
        norm = os.path.normpath(path)
        subprocess.Popen(["explorer", f"/select,{norm}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def _open_file(path: str) -> None:
    """Open *path* with its default application; show a message if missing."""
    try:
        os.startfile(path)
    except FileNotFoundError:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(None, "File not found",
                            f"The file could not be found:\n{path}")
    except Exception as exc:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(None, "Cannot open file", str(exc))


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
                # NB: do NOT setParent(None) here -- that momentarily promotes the
                # (still-visible) child to a top-level window, which flashes on
                # screen before deleteLater() runs. hide() + deleteLater() keeps
                # the parent intact so nothing is ever shown as its own window.
                w.hide()
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
    menu = pyqtSignal(str, QPoint)

    def __init__(self, key, title, subtitle, status, unread, kind, deletable):
        super().__init__()
        self.key = key
        self.setObjectName("rosterRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.menu.emit(self.key, self.mapToGlobal(pos)))
        is_room = kind in ("group", "channel")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 2, 8, 2)
        lay.setSpacing(8)

        if is_room:
            av = Avatar(title, 26)
            lay.addWidget(av)
        else:
            self._av_status = AvatarWithStatus(title, 26, status)
            lay.addWidget(self._av_status)

        name_lbl = QLabel(title)
        name_lbl.setStyleSheet("font-weight:600; font-size:12px; background:transparent;")
        lay.addWidget(name_lbl, 1)
        if is_room:
            tag = QLabel("📢" if kind == "channel" else "👥")
            tag.setStyleSheet("font-size:11px; background:transparent;")
            lay.addWidget(tag)

        if unread:
            b = QLabel(str(unread) if unread < 100 else "99+")
            b.setObjectName("unread")
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(b)

        self._del_btn: QPushButton | None = None
        if deletable:
            x = QPushButton("✕")
            x.setFixedSize(20, 20)
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.setStyleSheet(
                "QPushButton{background:transparent; border:none; padding:0;"
                " font-size:11px; font-weight:700; color:%s;}"
                "QPushButton:hover{color:#fff; background:%s; border-radius:10px;}"
                % (theme.color("text_sec"), theme.color("danger")))
            x.clicked.connect(lambda: self.deleted.emit(self.key))
            x.hide()
            lay.addWidget(x)
            self._del_btn = x

        if subtitle:
            self.setToolTip(subtitle)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        _repolish(self)

    def enterEvent(self, e) -> None:
        if self._del_btn:
            self._del_btn.show()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if self._del_btn:
            self._del_btn.hide()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mousePressEvent(e)


class _Composer(QPlainTextEdit):
    """Multi-line message input: Enter sends, Shift+Enter inserts a newline.

    Auto-grows from one line up to a few lines, then scrolls (update.md #9).
    """

    submit = pyqtSignal()

    def __init__(self, max_lines: int = 6) -> None:
        super().__init__()
        self.setObjectName("composer")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        # Inner inset on all sides (vertical centring + comfortable left margin),
        # so the height maths below can fit a line without clipping it.
        self.document().setDocumentMargin(7)
        self._max_lines = max_lines
        self.textChanged.connect(self._auto_height)
        self._auto_height()

    def text(self) -> str:
        return self.toPlainText()

    def clear(self) -> None:
        super().clear()
        self._auto_height()

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(e)        # Shift+Enter → newline
            else:
                self.submit.emit()              # Enter → send
            return
        super().keyPressEvent(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._auto_height()

    def _auto_height(self) -> None:
        # Grow with the number of (wrapped) lines, capped at max_lines. For a
        # QPlainTextEdit document().size().height() is the LINE COUNT, not
        # pixels, so multiply by the line height. The scrollbar stays off until
        # the content genuinely overflows -- so one line never clips or shows the
        # stray scrollbar.
        doc = self.document()
        if self.viewport().width() > 0:
            doc.setTextWidth(self.viewport().width())
        line = self.fontMetrics().lineSpacing()
        dm = int(doc.documentMargin())
        fr = self.frameWidth()
        visual_lines = max(1.0, doc.size().height())
        shown = min(visual_lines, self._max_lines)
        target = int(round(shown * line)) + 2 * dm + 2 * fr + 4
        if self.height() != target:
            self.setFixedHeight(target)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if visual_lines > self._max_lines + 0.01
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.height())


class _SectionHeader(QFrame):
    """Clickable, collapsible section header (LOCAL / GROUPS / IP / OFFLINE)."""

    toggled = pyqtSignal(str)

    def __init__(self, label: str, count: int = 0, collapsed: bool = False):
        super().__init__()
        self.label = label
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 5, 10, 1)
        h.setSpacing(5)
        self._chev = QLabel("▸" if collapsed else "▾")
        self._chev.setStyleSheet(
            "font-size:9px; color:%s; background:transparent;" % theme.color("text_sec"))
        h.addWidget(self._chev)
        lbl = QLabel(label)
        lbl.setObjectName("section")
        lbl.setStyleSheet("font-size:9px; font-weight:800; letter-spacing:1px;")
        h.addWidget(lbl)
        if count:
            cnt = QLabel(str(count))
            cnt.setStyleSheet(
                "font-size:9px; font-weight:700; color:%s;"
                " background:%s; border-radius:6px; padding:0 4px;"
                % (theme.color("accent"), theme.color("panel2")))
            h.addWidget(cnt)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("hdivider")
        h.addWidget(line, 1)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggled.emit(self.label)
        super().mousePressEvent(e)


class ChatWindow(QWidget):
    """Standalone chat window. Closing hides it so conversations persist."""

    activity = pyqtSignal(str)     # background message arrived on this key
    # File-transfer callbacks fire on worker threads; these signals marshal them
    # back onto the GUI thread (QTimer.singleShot from a worker thread never
    # fires -- the worker has no Qt event loop).
    _xfer_progress = pyqtSignal(str, str)            # tid, status text
    _xfer_finished = pyqtSignal(str, str, str, str)  # tid, ip, path(""=failed), status text
    _sys_sig = pyqtSignal(str, str)                  # key, text -- post a system line
    _queued_sig = pyqtSignal(str)                    # mid -- message held in offline queue

    def __init__(self, chat_service, toasts,
                 log_fn=lambda m: None) -> None:
        super().__init__(None)
        self.chat = chat_service
        self._toasts = toasts
        self._log = log_fn
        self.setWindowTitle(f"LAN Chat — Net Split-Tunneler v{__version__}")
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
        self._channels: dict[str, dict] = {}   # cid -> {name, admins, members}
        self._active: str | None = None
        self._visible = False
        self._peer_filter = ""
        self._collapsed: set[str] = set()   # roster sections the user folded away
        self._reply_to: dict | None = None
        self._notifications_enabled = config.load_notifications_enabled()
        self._last_online_sig: frozenset = frozenset()
        self._rows: dict[str, _RosterRow] = {}
        # Peers the user deleted -- hidden from the roster until they contact us.
        self._hidden: set[str] = set(config.load_hidden_peers())

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

        # Deliver worker-thread transfer updates onto the GUI thread.
        self._xfer_progress.connect(self._on_xfer_progress)
        self._xfer_finished.connect(self._on_xfer_finished)
        self._sys_sig.connect(self._sys)
        self._queued_sig.connect(self._on_queued)

        self._build()
        self._load_history()
        theme.changed.connect(self._on_theme)
        self.update_roster(self.chat.peers())
        # Restore the previously open conversation (update.md General settings).
        if config.load_restore_session():
            last = config.load_last_active_chat()
            if last and last in self._conversations:
                QTimer.singleShot(0, lambda k=last: self.select_peer(k))
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
        self._self_dot = Dot(self.chat.my_status, 9)
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
        newg = QToolButton()
        newg.setText("＋ New")
        newg.setCursor(Qt.CursorShape.PointingHandCursor)
        newg.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        newg.setStyleSheet("QToolButton{color:%s; font-weight:700;}"
                           "QToolButton::menu-indicator{image:none;}"
                           % theme.color("accent"))
        newmenu = QMenu(newg)
        newmenu.addAction("👥  New group", self._new_group_dialog)
        newmenu.addAction("📢  New broadcast channel", self._new_channel_dialog)
        newg.setMenu(newmenu)
        phrow.addWidget(newg)
        s.addLayout(phrow)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Search peers...")
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
        self._btn_search = QPushButton("🔍")
        self._btn_search.setProperty("variant", "ghost")
        self._btn_search.setFixedWidth(40)
        self._btn_search.setToolTip("Search messages and files")
        self._btn_search.clicked.connect(self._open_search)
        head.addWidget(self._btn_search)
        self._btn_manage = QPushButton("⚙ Manage")
        self._btn_manage.clicked.connect(self._manage_active)
        head.addWidget(self._btn_manage)
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

        # Drop-zone overlay parented to _messages so it sits on top of it.
        self._drop_overlay = QFrame(self._messages)
        self._drop_overlay.setObjectName("dropZone")
        drop_v = QVBoxLayout(self._drop_overlay)
        drop_v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_icon = QLabel("📂")
        drop_icon.setStyleSheet("font-size:40px; background:transparent;")
        drop_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_v.addWidget(drop_icon)
        drop_lbl = QLabel("Drop file to send")
        drop_lbl.setObjectName("dropZoneLabel")
        drop_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_v.addWidget(drop_lbl)
        self._drop_lbl_peer = QLabel("")
        self._drop_lbl_peer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_lbl_peer.setStyleSheet(
            "font-size:12px; color:%s; background:transparent;" % theme.color("text_sec"))
        drop_v.addWidget(self._drop_lbl_peer)
        self._drop_overlay.hide()
        self.setAcceptDrops(True)
        # The scroll area + its viewport sit above the window, so they must accept
        # drops and forward the events to us via the installed event filter.
        for w in (self._messages, self._messages.viewport()):
            w.setAcceptDrops(True)
            w.installEventFilter(self)

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

        # read-only notice (broadcast channels where you're not an admin)
        self._readonly_lbl = QLabel("📢  Only channel admins can post here.")
        self._readonly_lbl.setObjectName("muted")
        self._readonly_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._readonly_lbl.setStyleSheet(
            "font-style:italic; padding:8px; color:%s;" % theme.color("text_sec"))
        self._readonly_lbl.hide()
        r.addWidget(self._readonly_lbl)

        comp = QHBoxLayout()
        self._entry = _Composer()
        self._entry.setPlaceholderText(_PLACEHOLDER + "   (Enter = send · Shift+Enter = new line)")
        self._entry.submit.connect(self._send)
        self._entry.textChanged.connect(self._on_typing_edit)
        comp.addWidget(self._entry, 1)
        self._btn_emoji = QPushButton("😊")
        self._btn_emoji.setFixedWidth(38)
        self._btn_emoji.setToolTip("Open emoji picker  (Win + .)")
        self._btn_emoji.clicked.connect(self._open_emoji_picker)
        comp.addWidget(self._btn_emoji, alignment=Qt.AlignmentFlag.AlignBottom)
        self._btn_file = QPushButton("📎")
        self._btn_file.setFixedWidth(38)
        self._btn_file.clicked.connect(self._attach_file)
        comp.addWidget(self._btn_file, alignment=Qt.AlignmentFlag.AlignBottom)
        self._btn_send = QPushButton("Send")
        self._btn_send.setProperty("variant", "accent")
        self._btn_send.clicked.connect(self._send)
        comp.addWidget(self._btn_send, alignment=Qt.AlignmentFlag.AlignBottom)
        self._composer = QWidget()
        self._composer.setLayout(comp)
        r.addWidget(self._composer)

        root.addWidget(right, 1)
        self._show_empty_state()
        self._set_composer_visible(False)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _bubble_max(self) -> int:
        """Maximum bubble width: ~78 % of the chat viewport, clamped 280-760 px."""
        vp = self._messages.viewport().width()
        if vp < 10:
            return _BUBBLE_MAX  # not yet realized -- use module fallback
        return max(280, min(760, int(vp * 0.78)))

    @staticmethod
    def _is_group(key: str) -> bool:
        return bool(key) and key.startswith("group:")

    @staticmethod
    def _is_channel(key: str) -> bool:
        return bool(key) and key.startswith("channel:")

    def _is_room(self, key: str) -> bool:
        return self._is_group(key) or self._is_channel(key)

    def _display_name(self, key: str) -> str:
        if self._is_group(key):
            return self._groups.get(key[6:], {}).get("name", "Group")
        if self._is_channel(key):
            return self._channels.get(key[8:], {}).get("name", "Channel")
        return self._aliases.get(key) or self._names.get(key, key)

    def _group_meta(self, gid: str) -> dict:
        g = self._groups.get(gid, {})
        members = list(g.get("members", []))
        if self.chat.my_ip not in members:
            members = members + [self.chat.my_ip]
        # Groups must never be admin-less (update.md #7): fall back to ourselves.
        admins = [a for a in g.get("admins", []) if a in members] or [self.chat.my_ip]
        return {"gid": gid, "name": g.get("name", "Group"),
                "members": members, "admins": admins}

    def _channel_meta(self, cid: str) -> dict:
        c = self._channels.get(cid, {})
        members = list(c.get("members", []))
        if self.chat.my_ip not in members:
            members = members + [self.chat.my_ip]
        admins = [a for a in c.get("admins", []) if a in members] or [self.chat.my_ip]
        return {"cid": cid, "name": c.get("name", "Channel"),
                "members": members, "admins": admins}

    def _is_admin(self, key: str) -> bool:
        """True if the local user may *manage* (rename/kick/delete) this conversation."""
        if self._is_group(key):
            return self.chat.my_ip in self._group_meta(key[6:])["admins"]
        if self._is_channel(key):
            return self.chat.my_ip in self._channel_meta(key[8:])["admins"]
        return True   # private chats / demo: always postable

    def _can_post(self, key: str) -> bool:
        """True if the local user may send a message here.

        Groups are many-to-many: every member posts. Channels are broadcast:
        only admins may post. Private chats are always postable.
        """
        if self._is_channel(key):
            return self.chat.my_ip in self._channel_meta(key[8:])["admins"]
        return True

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

    def set_on_closed(self, cb) -> None:
        """Register a callback fired whenever the user closes (hides) the chat."""
        self._on_closed = cb

    def closeEvent(self, e) -> None:
        e.ignore()
        self._visible = False
        self.hide()
        cb = getattr(self, "_on_closed", None)
        if cb:
            try:
                cb()
            except Exception:
                pass

    def showEvent(self, e) -> None:
        self._visible = True
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self._visible = False
        super().hideEvent(e)

    def changeEvent(self, e) -> None:
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
        m.addAction("⚙  Settings...", self._open_full_settings)
        m.addSeparator()
        status = self.chat.my_status
        m.addAction("● Online" + (" ✓" if status == "online" else ""), lambda: self.set_status("online"))
        m.addAction("🌙 Away" + (" ✓" if status == "away" else ""), lambda: self.set_status("away"))
        m.addAction("○ Invisible (appear offline)" + (" ✓" if status == "invisible" else ""), lambda: self.set_status("invisible"))
        m.addSeparator()
        if self._notifications_enabled:
            m.addAction("🔔 Notifications on -- pause", lambda: self._set_notify(False))
        else:
            m.addAction("🔕 Notifications paused -- enable", lambda: self._set_notify(True))
        m.exec(self.sender().mapToGlobal(QPoint(0, self.sender().height())))

    def _open_full_settings(self) -> None:
        SettingsDialog(self, self).exec()

    def set_status(self, status: str) -> None:
        self.chat.my_status = status
        config.save_my_status(status)
        self._self_dot.set_status(status)
        self._log(f"You now appear {status} to peers.")

    def _set_notify(self, enabled: bool) -> None:
        self._notifications_enabled = enabled
        config.save_notifications_enabled(enabled)
        self._log(f"Notifications {'enabled' if enabled else 'paused'}.")

    # ── settings-dialog callbacks ─────────────────────────────────────────────
    def apply_display_name(self, name: str) -> None:
        name = (name or "").strip()[:32]
        if not name or name == self.chat.my_name:
            return
        self.chat.set_name(name)
        config.save_display_name(name)
        self._name_edit.setText(name)
        self._self_avatar.set_name(name)
        self._log(f"Chat display name set to '{name}'.")

    def on_settings_changed(self) -> None:
        """Re-read live-affecting settings after the Settings dialog changes them."""
        self._notifications_enabled = config.load_notifications_enabled()
        self._self_dot.set_status(self.chat.my_status)

    def clear_all_history(self) -> int:
        """Clear every local conversation (keeps peers, drops messages). Returns count."""
        keys = list(self._conversations.keys())
        for key in keys:
            self._drop_index(key)
            if self._is_group(key):
                self._conversations[key] = []
                self._save_group(key[6:])
            elif self._is_channel(key):
                self._conversations[key] = []
                self._save_channel(key[8:])
            else:
                self._conversations.pop(key, None)
                self._save_peer(key)
            self._unread.pop(key, None)
        if self._active:
            self._render(self._active)
        self.update_roster(self.chat.peers())
        self._log(f"Cleared local history for {len(keys)} conversation(s).")
        return len(keys)

    def block_user(self, ip: str, name: str = "") -> None:
        """Permanently block a peer (update.md #12) and persist to the block list."""
        self.chat.block_ip(ip)
        users = config.load_blocked_users()
        if not any(u["ip"] == ip for u in users):
            users.append({"ip": ip, "name": name or self._display_name(ip)})
            config.save_blocked_users(users)
        self._chat_req_states[ip] = "blocked"
        self._log(f"Blocked {name or ip}.")

    def unblock_user(self, ip: str) -> None:
        self.chat.unblock_ip(ip)
        users = [u for u in config.load_blocked_users() if u["ip"] != ip]
        config.save_blocked_users(users)
        self._chat_req_states.pop(ip, None)
        self._log(f"Unblocked {self._display_name(ip)}.")

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
        return self.chat.peer_status(ip)

    def _is_online(self, ip: str) -> bool:
        return self._status_of(ip) in ("online", "away")

    def _visible_peers(self, peers) -> set[str]:
        """Peers to list: everyone currently seen, plus anyone we have history
        with (shown offline with a last-seen) -- never groups, ourselves, or
        peers the user deleted (hidden until they contact us again)."""
        cands = {p.ip for p in peers}
        cands |= {c for c in self._conversations if not self._is_room(c)}
        cands.discard(self.chat.my_ip)
        cands -= self._hidden
        return cands

    def _unhide(self, ip: str) -> None:
        """A hidden (deleted) peer made contact -- bring it back into the roster."""
        if ip in self._hidden:
            self._hidden.discard(ip)
            config.save_hidden_peers(list(self._hidden))

    def _peer_subtitle(self, ip: str, status: str) -> str:
        if ip == DemoBot.IP:
            return "demo peer"
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
        channels = [f"channel:{c}" for c in self._channels if self._matches(f"channel:{c}")]
        peers_f = [ip for ip in self._visible_peers(peers) if self._matches(ip)]

        if not groups and not channels and not peers_f:
            hint = QLabel("No matches." if self._peer_filter
                          else "Looking for people on your network...\nOpen the app on another PC, or Try Demo Chat.")
            hint.setObjectName("muted")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            self._roster.add(hint)
            return

        grp_channels = sorted(channels, key=lambda x: (-self._last_activity(x),
                                                       self._display_name(x).lower()))
        grp_groups = sorted(groups, key=lambda x: (-self._last_activity(x),
                                                   self._display_name(x).lower()))
        if (grp_channels or grp_groups) and not self._add_section(
                "GROUPS", len(grp_channels) + len(grp_groups)):
            for key in grp_channels:
                n = len(self._channel_meta(key[8:]).get("members", []))
                badge = "read-only" if not self._is_admin(key) else f"{n} members"
                self._add_row(key, self._display_name(key), badge,
                              "online", self._unread.get(key, 0), "channel", True)
            for key in grp_groups:
                gid = key[6:]
                n = len(self._group_meta(gid).get("members", []))
                self._add_row(key, self._display_name(key), f"{n} members",
                              "online", self._unread.get(key, 0), "group", True)

        _rank = {"online": 0, "away": 1, "offline": 2}
        def _peer_sort(x):
            return (0 if self._unread.get(x, 0) else 1,
                    -self._unread.get(x, 0),
                    _rank.get(self._status_of(x), 2),
                    -self._last_activity(x),
                    self._display_name(x).lower())

        # Online/away peers go in LOCAL / IP-MANUAL; everything offline (no matter
        # the origin) collapses into a single OFFLINE section at the bottom.
        online_f = [ip for ip in peers_f if self._status_of(ip) != "offline"]
        offline_f = [ip for ip in peers_f if self._status_of(ip) == "offline"]
        local_peers = [ip for ip in online_f if self.chat.is_local_ip(ip)]
        manual_peers = [ip for ip in online_f if not self.chat.is_local_ip(ip)]

        if local_peers and not self._add_section("LOCAL", len(local_peers)):
            for ip in sorted(local_peers, key=_peer_sort):
                status = self._status_of(ip)
                self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, status),
                              status, self._unread.get(ip, 0), "peer", ip != DemoBot.IP)

        if manual_peers and not self._add_section("IP / MANUAL", len(manual_peers)):
            for ip in sorted(manual_peers, key=_peer_sort):
                status = self._status_of(ip)
                self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, status),
                              status, self._unread.get(ip, 0), "peer", True)

        if offline_f and not self._add_section("OFFLINE", len(offline_f)):
            for ip in sorted(offline_f, key=_peer_sort):
                self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, "offline"),
                              "offline", self._unread.get(ip, 0), "peer", ip != DemoBot.IP)

        if self._active:
            self._update_header_sub(peers)

    def _add_section(self, label: str, count: int) -> bool:
        """Add a collapsible section header; return True if it is collapsed."""
        collapsed = label in self._collapsed
        hdr = _SectionHeader(label, count, collapsed)
        hdr.toggled.connect(self._toggle_section)
        self._roster.add(hdr)
        return collapsed

    def _toggle_section(self, label: str) -> None:
        if label in self._collapsed:
            self._collapsed.discard(label)
        else:
            self._collapsed.add(label)
        self.update_roster(self.chat.peers())

    def _add_row(self, key, title, sub, status, unread, kind, deletable) -> None:
        row = _RosterRow(key, title, sub, status, unread, kind, deletable)
        row.set_active(key == self._active)
        row.clicked.connect(self.select_peer)
        if kind == "group":
            row.deleted.connect(self._delete_group)
        elif kind == "channel":
            row.deleted.connect(self._delete_channel)
        else:
            row.deleted.connect(self._delete_peer)
        row.menu.connect(self._roster_menu)
        self._roster.add(row)
        self._rows[key] = row

    def _update_header_sub(self, peers) -> None:
        key = self._active
        if self._is_group(key):
            meta = self._group_meta(key[6:])
            n = len(meta.get("members", []))
            role = " · admin" if self.chat.my_ip in meta["admins"] else ""
            self._head_sub.setText(f"Group · {n} members{role}")
        elif self._is_channel(key):
            meta = self._channel_meta(key[8:])
            n = len(meta.get("members", []))
            role = "admin" if self.chat.my_ip in meta["admins"] else "read-only"
            self._head_sub.setText(f"📢 Broadcast channel · {n} members · {role}")
        elif key == DemoBot.IP:
            self._head_sub.setText("demo peer")
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
        self._head_avatar.set_name(self._display_name(key))
        self._head_name.setText(self._display_name(key))
        self._update_header_sub(self.chat.peers())
        is_grp = self._is_group(key)
        is_chan = self._is_channel(key)
        is_room = is_grp or is_chan
        can_post = self._is_admin(key)
        self._btn_add.setVisible(is_room and can_post)
        self._btn_manage.setVisible(is_room)
        self._btn_save.setVisible(not is_room and key != DemoBot.IP)
        # File send and emoji: 1:1 peers only.
        self._btn_file.setVisible(not is_room)
        self._btn_emoji.setVisible(not is_room)
        self._set_composer_visible(True)
        # Broadcast channels are post-only for admins; members read.
        read_only = is_chan and not can_post
        self._composer.setVisible(not read_only)
        self._readonly_lbl.setVisible(read_only)
        self._render(key)
        self._refresh_typing()
        self._mark_read(key)
        if not read_only:
            self._entry.setFocus()
        config.save_last_active_chat(key)
        if prev != key:
            self.update_roster(self.chat.peers())

    def _set_composer_visible(self, on: bool) -> None:
        self._composer.setVisible(on)
        self._btn_clear.setVisible(on)
        if not on:
            self._btn_add.setVisible(False)
            self._btn_save.setVisible(False)
            self._btn_manage.setVisible(False)
            self._readonly_lbl.setVisible(False)
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
        elif self._is_channel(key):
            self._save_channel(key[8:])
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
        if status == "queued":
            return "🕓", muted   # held in offline queue, awaiting peer
        return "✓", muted   # sent

    def _make_bubble(self, entry: dict) -> QWidget:
        kind = entry.get("kind", "sys")
        if kind in ("file_out", "file_in_offer"):
            return self._make_file_bubble(entry)
        if kind == "chat_req":
            return self._make_req_bubble(entry)
        if kind == "sys":
            lbl = QLabel(f"-- {entry.get('text', '')} --")
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
        bubble.setMaximumWidth(self._bubble_max())
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(12, 8, 12, 6)
        bv.setSpacing(2)
        txcol = theme.color("bubble_out_tx" if is_out else "bubble_in_tx")

        # Right-click menu (reply / delete) -- not on tombstones.
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
            # the blue outgoing bubble, accent on the light incoming bubble --
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
            snip = snip if len(snip) <= 80 else snip[:77] + "..."
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
        v_wrap.setMaximumWidth(self._bubble_max())
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
        self._reply_prev.setText(text if len(text) <= 80 else text[:77] + "...")
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
        if not self._can_post(key):
            return   # broadcast channel: only admins may post
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
        elif self._is_channel(key):
            meta = self._channel_meta(key[8:])
            threading.Thread(target=self._send_channel_worker,
                             args=(key, meta, text, reply, mid), daemon=True).start()
        else:
            threading.Thread(target=self._send_worker,
                             args=(key, text, reply, mid), daemon=True).start()

    def _send_worker(self, ip, text, reply, mid) -> None:
        ok = self.chat.send(ip, text, reply=reply, mid=mid)
        if not ok:
            # Held in the offline queue; mark the bubble as queued (🕓).
            self._queued_sig.emit(mid)

    def _send_group_worker(self, key, meta, text, reply, mid) -> None:
        results = self.chat.send_group(meta, text, reply=reply, mid=mid)
        failed = [ip for ip, okk in results.items() if not okk]
        if failed:
            self._queued_sig.emit(mid)

    def _send_channel_worker(self, key, meta, text, reply, mid) -> None:
        results = self.chat.send_channel(meta, text, reply=reply, mid=mid)
        failed = [ip for ip, okk in results.items() if not okk]
        if failed:
            self._queued_sig.emit(mid)

    def _sys(self, key, text) -> None:
        entry = _mk_entry("sys", "", text, time.time())
        self._store(key, entry)
        if key == self._active:
            self._append(entry)

    def _notify_background(self, scope: str, key: str, title: str, body: str) -> None:
        """Alert for a background message, honouring the per-type toggles.

        "Show window" (popup) raises the chat window without switching the active
        conversation -- the unread badge in the roster tells you who sent. If the
        window is already visible (you're in another chat), we show a toast
        instead so you aren't interrupted. Toast fires unconditionally when the
        popup toggle is off. Sound/taskbar are independent of both.
        """
        notifs_ok = config.load_notifications_enabled() and not config.load_do_not_disturb()
        window_up = self.isVisible() and not self.isMinimized()

        if sound.should_notify(scope, "popup"):
            if window_up:
                # Already visible in another chat -- raising does nothing; show a
                # toast so the user sees who messaged without hijacking their view.
                if notifs_ok:
                    self._toasts.notify(title, body, key)
            else:
                # Raise the window to the *current* active chat; do not switch to
                # the sender -- the roster badge is the cue for who needs attention.
                self.showNormal()
                self.raise_()
                self.activateWindow()
                self._visible = True
        elif notifs_ok:
            self._toasts.notify(title, body, key)
            if sound.should_notify(scope, "taskbar") and not self.isActiveWindow():
                QApplication.alert(self, 3000)
        if sound.should_notify(scope, "sound"):
            sound.play_sound()

    def receive_message(self, ip, name, text, ts, reply=None, mid="") -> None:
        self._unhide(ip)
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
            prev = text if len(text) <= 120 else text[:117] + "..."
            self._notify_background("private", ip, name, prev)

    def on_group_message(self, group, ip, name, text, ts, reply=None, mid="") -> None:
        gid = group.get("gid")
        if not gid:
            return
        members = [m for m in group.get("members", []) if m]
        admins = [a for a in group.get("admins", []) if a]
        g = self._groups.setdefault(gid, {"name": group.get("name", "Group"),
                                          "members": members, "admins": admins})
        g["name"] = group.get("name", g.get("name", "Group"))
        if members:
            g["members"] = members
        # Admin set is authoritative from the sender so promote/demote propagate.
        if admins:
            g["admins"] = admins
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
            self._mark_read(key)
        else:
            self._unread[key] = self._unread.get(key, 0) + 1
            self.update_roster(self.chat.peers())
            prev = text if len(text) <= 100 else text[:97] + "..."
            self._notify_background("group", key, f"{g['name']} (group)", f"{name}: {prev}")

    def on_channel_message(self, channel, ip, name, text, ts, reply=None, mid="") -> None:
        cid = channel.get("cid")
        if not cid:
            return
        members = [m for m in channel.get("members", []) if m]
        admins = [a for a in channel.get("admins", []) if a]
        c = self._channels.setdefault(cid, {"name": channel.get("name", "Channel"),
                                            "members": members, "admins": admins})
        c["name"] = channel.get("name", c.get("name", "Channel"))
        if members:
            c["members"] = members
        if admins:
            c["admins"] = admins
        for m in members:
            if m and m != self.chat.my_ip and not self.chat.is_manual_peer(m):
                self.chat.add_manual_peer(m)
        self._names[ip] = name
        key = f"channel:{cid}"
        if not text:
            self._save_channel(cid)
            self.update_roster(self.chat.peers())
            return
        entry = _mk_entry("in", name, text, ts, mid=mid, reply=reply, from_ip=ip)
        self._store(key, entry)
        self._save_channel(cid)
        if key == self._active and self._visible:
            self._append(entry)
        else:
            self._unread[key] = self._unread.get(key, 0) + 1
            self.update_roster(self.chat.peers())
            prev = text if len(text) <= 100 else text[:97] + "..."
            self._notify_background("broadcast", key, f"📢 {c['name']}", f"{name}: {prev}")

    # ── offline queue + group removal callbacks ───────────────────────────────
    def _refresh_tick(self, mid: str, status: str) -> None:
        lbl = self._status_lbls.get(mid)
        if lbl is None:
            return
        try:
            glyph, color = self._tick_parts(status, True)
            lbl.setText(glyph)
            lbl.setStyleSheet("font-size:11px; color:%s;" % color)
        except RuntimeError:
            self._status_lbls.pop(mid, None)

    def _on_queued(self, mid: str) -> None:
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        if entry.get("kind") != "out" or entry.get("status") in ("delivered", "read"):
            return
        entry["status"] = "queued"
        self._persist(key)
        self._refresh_tick(mid, "queued")

    def on_queue_flush(self, ip, mids) -> None:
        for mid in mids:
            loc = self._mid_index.get(mid)
            if not loc:
                continue
            key, entry = loc
            if entry.get("kind") == "out" and entry.get("status") == "queued":
                entry["status"] = "sent"
                self._persist(key)
                self._refresh_tick(mid, "sent")

    def on_group_kicked(self, from_ip, gid) -> None:
        key = f"group:{gid}"
        if gid not in self._groups:
            return
        name = self._display_name(key)
        self._drop_index(key)
        self._groups.pop(gid, None)
        self._conversations.pop(key, None)
        self._unread.pop(key, None)
        self._typers.pop(key, None)
        self._delete_history_file(key)
        if self._active == key:
            self._reset_active()
        self.update_roster(self.chat.peers())
        self._toasts.notify("Removed from group",
                            f'You were removed from "{name}".', "")

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
        if key == DemoBot.IP or self._is_channel(key):
            return   # broadcast channels are read-only; no receipts
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
        """Incoming reaction from a peer -- toggle their entry in the reactions map."""
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

    # ── typing indicators ─────────────────────────────────────────────────────
    def _on_typing_edit(self) -> None:
        key = self._active
        if (not key or key == DemoBot.IP or self._is_channel(key)
                or not self._entry.text().strip()):
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
                txt = f"{who} is typing..."
            else:
                txt = f"{len(live)} people are typing..."
        else:
            txt = "typing..."
        self._typing_lbl.setText(txt)
        self._typing_lbl.show()

    # ── demo ──────────────────────────────────────────────────────────────────
    def _start_demo(self) -> None:
        if not self.chat.has_demo():
            self.chat.add_demo_bot()
            self._log("Demo chat started -- say hi to the Demo Bot.")
        QTimer.singleShot(150, lambda: self.select_peer(DemoBot.IP))

    # ── manual IP ─────────────────────────────────────────────────────────────
    def _connect_manual_ip(self) -> None:
        ip = self._ip_edit.text().strip()
        if not ip:
            return
        if not is_valid_ipv4(ip):
            self._log(f"Invalid IP: {ip!r} -- enter a valid IPv4 address (e.g. 192.168.1.20).")
            return
        if ip == self.chat.my_ip:
            self._log("Cannot chat with yourself.")
            return
        name, ok = QInputDialog.getText(self, "Name this PC", f"Enter a name for {ip}:")
        if ok and name.strip():
            self._aliases[ip] = name.strip()[:32]
        self._unhide(ip)   # explicit re-add overrides a prior deletion
        self.chat.add_manual_peer(ip)
        self._names.setdefault(ip, ip)
        self._ip_edit.clear()
        self.select_peer(ip)
        self._save_peer(ip)
        threading.Thread(target=self._probe_manual, args=(ip,), daemon=True).start()

    def _probe_manual(self, ip) -> None:
        if not check_host_reachable(ip, CHAT_TCP_PORT):
            self._sys_sig.emit(
                ip, "Not reachable -- make sure the app is running on that PC.")

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
        # Remember the deletion so a live peer's next broadcast (or a reload of
        # its group membership) doesn't silently bring it back.
        self._hidden.add(ip)
        config.save_hidden_peers(list(self._hidden))
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
        # Blocked users can't be added to new groups/channels by the blocker (#12).
        blocked = set(self.chat.blocked_ips())
        cands = [ip for ip in (set(self._names) | set(self._aliases) | set(self._conversations))
                 if ip not in exclude and ip != self.chat.my_ip
                 and ip != DemoBot.IP and not self._is_room(ip) and ip not in blocked]
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
        # Creator is the first admin (update.md #7).
        self._groups[gid] = {"name": name.strip()[:32], "members": members,
                             "admins": [self.chat.my_ip]}
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
        """Header ＋ Add -- works for both groups and channels (admins only)."""
        key = self._active
        if self._is_channel(key):
            return self._add_channel_members(key[8:])
        if not key or not self._is_group(key):
            return
        gid = key[6:]
        if not self._is_admin(key):
            return
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
        self._broadcast_group_meta(gid, f"{self.chat.my_name} added {len(new)} member(s)")
        self._save_group(gid)
        self._update_header_sub(self.chat.peers())
        self.update_roster(self.chat.peers())
        self._log(f"Added {len(new)} member(s) to \"{g.get('name', 'Group')}\".")

    # ── group admin (update.md #7) ────────────────────────────────────────────
    def _broadcast_group_meta(self, gid, system_text="") -> None:
        meta = self._group_meta(gid)
        threading.Thread(
            target=lambda: self.chat.send_group(meta, system_text,
                                                msg_type="group_invite"),
            daemon=True).start()

    def _ensure_group_admin(self, gid) -> None:
        """Guarantee the group always has at least one admin (#7)."""
        g = self._groups.get(gid)
        if not g:
            return
        admins = [a for a in g.get("admins", []) if a in g.get("members", [])]
        if not admins and g.get("members"):
            admins = [g["members"][0]]
        g["admins"] = admins

    def _manage_active(self) -> None:
        key = self._active
        if self._is_group(key):
            self._manage_group_dialog(key[6:])
        elif self._is_channel(key):
            self._manage_channel_dialog(key[8:])

    def _manage_group_dialog(self, gid) -> None:
        if gid not in self._groups:
            return
        am_admin = self.chat.my_ip in self._group_meta(gid)["admins"]
        dlg = QDialog(self)
        dlg.setWindowTitle("Manage group")
        dlg.resize(420, 500)
        v = QVBoxLayout(dlg)

        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("Name:"))
        name_edit = QLineEdit(self._groups[gid].get("name", "Group"))
        name_edit.setEnabled(am_admin)
        nrow.addWidget(name_edit, 1)
        if am_admin:
            rb = QPushButton("Rename")
            rb.clicked.connect(lambda: self._group_rename(gid, name_edit.text()))
            nrow.addWidget(rb)
        v.addLayout(nrow)

        v.addWidget(QLabel("Members"))
        lst = QListWidget()
        v.addWidget(lst, 1)

        def refresh():
            lst.clear()
            meta = self._group_meta(gid)
            for ip in meta["members"]:
                tag = " · admin" if ip in meta["admins"] else ""
                me = " (you)" if ip == self.chat.my_ip else ""
                it = QListWidgetItem(f"{self._display_name(ip)}{me}{tag}")
                it.setData(Qt.ItemDataRole.UserRole, ip)
                lst.addItem(it)
        refresh()

        def selected_ip():
            it = lst.currentItem()
            return it.data(Qt.ItemDataRole.UserRole) if it else None

        if am_admin:
            arow = QHBoxLayout()
            add = QPushButton("＋ Add")
            add.clicked.connect(lambda: (self._add_group_members_for(gid), refresh()))
            promote = QPushButton("Promote")
            promote.clicked.connect(lambda: (self._group_set_admin(gid, selected_ip(), True), refresh()))
            demote = QPushButton("Demote")
            demote.clicked.connect(lambda: (self._group_set_admin(gid, selected_ip(), False), refresh()))
            remove = QPushButton("Remove")
            remove.setProperty("variant", "danger")
            remove.clicked.connect(lambda: (self._group_remove_member(gid, selected_ip()), refresh()))
            for b in (add, promote, demote, remove):
                arow.addWidget(b)
            v.addLayout(arow)

        brow = QHBoxLayout()
        leave = QPushButton("Leave group")
        leave.setProperty("variant", "danger")
        leave.clicked.connect(lambda: (dlg.accept(), self._delete_group(f"group:{gid}")))
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        brow.addWidget(leave)
        brow.addStretch(1)
        brow.addWidget(close)
        v.addLayout(brow)
        dlg.exec()

    def _add_group_members_for(self, gid) -> None:
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
        self._broadcast_group_meta(gid, f"{self.chat.my_name} added {len(new)} member(s)")
        self._save_group(gid)
        self.update_roster(self.chat.peers())

    def _group_rename(self, gid, name) -> None:
        name = (name or "").strip()[:32]
        if not name or gid not in self._groups:
            return
        self._groups[gid]["name"] = name
        self._broadcast_group_meta(gid, f"{self.chat.my_name} renamed the group to \"{name}\"")
        self._save_group(gid)
        if self._active == f"group:{gid}":
            self._head_name.setText(name)
            self._head_avatar.set_name(name)
        self.update_roster(self.chat.peers())

    def _group_set_admin(self, gid, ip, make_admin: bool) -> None:
        if not ip or gid not in self._groups:
            return
        g = self._groups[gid]
        admins = [a for a in g.get("admins", [])]
        if make_admin and ip not in admins:
            admins.append(ip)
        elif not make_admin and ip in admins:
            admins.remove(ip)
        g["admins"] = admins
        self._ensure_group_admin(gid)   # never leave it admin-less
        verb = "promoted" if make_admin else "demoted"
        self._broadcast_group_meta(gid, f"{self._display_name(ip)} was {verb}")
        self._save_group(gid)
        self.update_roster(self.chat.peers())

    def _group_remove_member(self, gid, ip) -> None:
        if not ip or gid not in self._groups or ip == self.chat.my_ip:
            return
        g = self._groups[gid]
        g["members"] = [m for m in g.get("members", []) if m != ip]
        g["admins"] = [a for a in g.get("admins", []) if a != ip]
        self._ensure_group_admin(gid)
        # Tell the removed member (they lose the group + history, #7) ...
        threading.Thread(target=lambda: self.chat.send_group_kick(ip, gid),
                         daemon=True).start()
        # ... and sync the smaller roster to everyone who remains.
        self._broadcast_group_meta(gid, f"{self._display_name(ip)} was removed")
        self._save_group(gid)
        self._update_header_sub(self.chat.peers())
        self.update_roster(self.chat.peers())

    def _delete_group(self, key) -> None:
        gid = key[6:]
        name = self._display_name(key)
        if QMessageBox.question(self, "Leave group",
                                f"Leave \"{name}\" and delete its history here?") \
                != QMessageBox.StandardButton.Yes:
            return
        meta = self._group_meta(gid)
        others = [m for m in meta["members"] if m != self.chat.my_ip]
        admins = [a for a in meta["admins"] if a != self.chat.my_ip]
        if others and not admins:
            admins = [others[0]]   # ownership transfers automatically (#7)
        if others:
            new_meta = {"gid": gid, "name": meta["name"],
                        "members": others, "admins": admins}
            threading.Thread(
                target=lambda: self.chat.send_group(
                    new_meta, f"{self.chat.my_name} left the group",
                    msg_type="group_invite"), daemon=True).start()
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

    # ── broadcast channels (update.md #8) ─────────────────────────────────────
    def _new_channel_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "New broadcast channel", "Channel name:")
        if not ok or not name.strip():
            return
        members = self._member_dialog("Add channel members", {self.chat.my_ip})
        if members is None:
            return
        cid = uuid.uuid4().hex[:12]
        self._channels[cid] = {"name": name.strip()[:32], "members": members,
                               "admins": [self.chat.my_ip]}
        self._conversations.setdefault(f"channel:{cid}", [])
        for ip in members:
            if not self.chat.is_manual_peer(ip):
                self.chat.add_manual_peer(ip)
        meta = self._channel_meta(cid)
        threading.Thread(
            target=lambda: self.chat.send_channel(
                meta, f"{self.chat.my_name} created channel \"{name.strip()}\"",
                msg_type="channel_meta"), daemon=True).start()
        self._save_channel(cid)
        self.update_roster(self.chat.peers())
        self.select_peer(f"channel:{cid}")
        self._log(f"Broadcast channel \"{name.strip()}\" created.")

    def _broadcast_channel_meta(self, cid, system_text="") -> None:
        meta = self._channel_meta(cid)
        threading.Thread(
            target=lambda: self.chat.send_channel(meta, system_text,
                                                  msg_type="channel_meta"),
            daemon=True).start()

    def _add_channel_members(self, cid) -> None:
        if cid not in self._channels or not self._is_admin(f"channel:{cid}"):
            return
        existing = set(self._channel_meta(cid).get("members", []))
        new = self._member_dialog("Add channel members", existing)
        if not new:
            return
        new = [ip for ip in new if ip not in existing]
        if not new:
            return
        c = self._channels[cid]
        c["members"] = list(dict.fromkeys(list(c.get("members", [])) + new))
        for ip in new:
            if not self.chat.is_manual_peer(ip):
                self.chat.add_manual_peer(ip)
        self._broadcast_channel_meta(cid, f"{self.chat.my_name} added {len(new)} member(s)")
        self._save_channel(cid)
        self._update_header_sub(self.chat.peers())
        self.update_roster(self.chat.peers())

    def _manage_channel_dialog(self, cid) -> None:
        if cid not in self._channels:
            return
        am_admin = self.chat.my_ip in self._channel_meta(cid)["admins"]
        dlg = QDialog(self)
        dlg.setWindowTitle("Manage channel")
        dlg.resize(420, 500)
        v = QVBoxLayout(dlg)

        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("Name:"))
        name_edit = QLineEdit(self._channels[cid].get("name", "Channel"))
        name_edit.setEnabled(am_admin)
        nrow.addWidget(name_edit, 1)
        if am_admin:
            rb = QPushButton("Rename")
            rb.clicked.connect(lambda: self._channel_rename(cid, name_edit.text()))
            nrow.addWidget(rb)
        v.addLayout(nrow)

        v.addWidget(QLabel("Members (admins can post)"))
        lst = QListWidget()
        v.addWidget(lst, 1)

        def refresh():
            lst.clear()
            meta = self._channel_meta(cid)
            for ip in meta["members"]:
                tag = " · admin" if ip in meta["admins"] else ""
                me = " (you)" if ip == self.chat.my_ip else ""
                it = QListWidgetItem(f"{self._display_name(ip)}{me}{tag}")
                it.setData(Qt.ItemDataRole.UserRole, ip)
                lst.addItem(it)
        refresh()

        def selected_ip():
            it = lst.currentItem()
            return it.data(Qt.ItemDataRole.UserRole) if it else None

        if am_admin:
            arow = QHBoxLayout()
            add = QPushButton("＋ Add")
            add.clicked.connect(lambda: (self._add_channel_members(cid), refresh()))
            promote = QPushButton("Make admin")
            promote.clicked.connect(lambda: (self._channel_set_admin(cid, selected_ip(), True), refresh()))
            demote = QPushButton("Remove admin")
            demote.clicked.connect(lambda: (self._channel_set_admin(cid, selected_ip(), False), refresh()))
            remove = QPushButton("Remove")
            remove.setProperty("variant", "danger")
            remove.clicked.connect(lambda: (self._channel_remove_member(cid, selected_ip()), refresh()))
            for b in (add, promote, demote, remove):
                arow.addWidget(b)
            v.addLayout(arow)

        brow = QHBoxLayout()
        leave = QPushButton("Delete/leave channel")
        leave.setProperty("variant", "danger")
        leave.clicked.connect(lambda: (dlg.accept(), self._delete_channel(f"channel:{cid}")))
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        brow.addWidget(leave)
        brow.addStretch(1)
        brow.addWidget(close)
        v.addLayout(brow)
        dlg.exec()

    def _channel_rename(self, cid, name) -> None:
        name = (name or "").strip()[:32]
        if not name or cid not in self._channels:
            return
        self._channels[cid]["name"] = name
        self._broadcast_channel_meta(cid, f"Channel renamed to \"{name}\"")
        self._save_channel(cid)
        if self._active == f"channel:{cid}":
            self._head_name.setText(name)
            self._head_avatar.set_name(name)
        self.update_roster(self.chat.peers())

    def _channel_set_admin(self, cid, ip, make_admin: bool) -> None:
        if not ip or cid not in self._channels:
            return
        c = self._channels[cid]
        admins = [a for a in c.get("admins", [])]
        if make_admin and ip not in admins:
            admins.append(ip)
        elif not make_admin and ip in admins:
            admins.remove(ip)
        if not admins:
            admins = [self.chat.my_ip]
        c["admins"] = admins
        self._broadcast_channel_meta(cid)
        self._save_channel(cid)
        self.update_roster(self.chat.peers())

    def _channel_remove_member(self, cid, ip) -> None:
        if not ip or cid not in self._channels or ip == self.chat.my_ip:
            return
        c = self._channels[cid]
        c["members"] = [m for m in c.get("members", []) if m != ip]
        c["admins"] = [a for a in c.get("admins", []) if a != ip]
        self._broadcast_channel_meta(cid)
        self._save_channel(cid)
        self._update_header_sub(self.chat.peers())
        self.update_roster(self.chat.peers())

    def _delete_channel(self, key) -> None:
        cid = key[8:]
        name = self._display_name(key)
        verb = "Delete" if self._is_admin(key) else "Leave"
        if QMessageBox.question(self, f"{verb} channel",
                                f"{verb} \"{name}\" and remove it here?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._drop_index(key)
        self._channels.pop(cid, None)
        self._conversations.pop(key, None)
        self._unread.pop(key, None)
        self._typers.pop(key, None)
        self._delete_history_file(key)
        if self._active == key:
            self._reset_active()
        self.update_roster(self.chat.peers())
        self._log(f"{verb}d channel \"{name}\".")

    # ── roster context menu / blocking / search ───────────────────────────────
    def _roster_menu(self, key, gpos) -> None:
        m = QMenu(self)
        m.addAction("Open", lambda: self.select_peer(key))
        if self._is_group(key):
            m.addAction("⚙ Manage group", lambda: self._manage_group_dialog(key[6:]))
            m.addAction("Leave group", lambda: self._delete_group(key))
        elif self._is_channel(key):
            if self._is_admin(key):
                m.addAction("⚙ Manage channel", lambda: self._manage_channel_dialog(key[8:]))
            m.addAction("Delete/leave channel", lambda: self._delete_channel(key))
        elif key != DemoBot.IP:
            m.addAction("✎ Save name", lambda: (self.select_peer(key), self._edit_alias()))
            m.addSeparator()
            if key in self.chat.blocked_ips():
                m.addAction("Unblock", lambda: (self.unblock_user(key),
                                                self.update_roster(self.chat.peers())))
            else:
                m.addAction("🚫 Block user", lambda: self._block_peer(key))
            m.addAction("Remove", lambda: self._delete_peer(key))
        m.exec(gpos)

    def _block_peer(self, ip) -> None:
        name = self._display_name(ip)
        if QMessageBox.question(self, "Block user",
                                f"Block {name}? They won't be able to message "
                                "you or send files.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.block_user(ip, name)
        if self._active == ip:
            self._rerender_if_active(ip)
        self.update_roster(self.chat.peers())

    def _open_search(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Search messages & files")
        dlg.resize(540, 500)
        v = QVBoxLayout(dlg)
        field = QLineEdit()
        field.setPlaceholderText("🔍  Search message text and file names...")
        v.addWidget(field)
        results = QListWidget()
        v.addWidget(results, 1)
        info = QLabel("")
        info.setObjectName("muted")
        v.addWidget(info)

        def run(text):
            results.clear()
            q = text.strip().lower()
            if len(q) < 2:
                info.setText("Type at least 2 characters.")
                return
            count = 0
            for key, msgs in self._conversations.items():
                cname = self._display_name(key)
                for e in msgs:
                    if not isinstance(e, dict) or e.get("deleted"):
                        continue
                    kind = e.get("kind", "")
                    if kind in ("out", "in"):
                        hay = e.get("text", "")
                    elif kind in ("file_out", "file_in_offer"):
                        hay = e.get("filename", "")
                    else:
                        continue
                    if q not in hay.lower():
                        continue
                    who = "You" if kind == "out" else e.get("sender", cname)
                    ts = time.strftime("%b %d %H:%M", time.localtime(e.get("ts", 0)))
                    icon = "📎" if kind.startswith("file") else "💬"
                    snip = hay if len(hay) <= 64 else hay[:61] + "..."
                    it = QListWidgetItem(f"{icon}  {cname} -- {who}: {snip}\n        {ts}")
                    it.setData(Qt.ItemDataRole.UserRole, key)
                    results.addItem(it)
                    count += 1
                    if count >= 200:
                        break
                if count >= 200:
                    break
            info.setText(f"{count} match(es)" + (" (showing first 200)" if count >= 200 else ""))

        def open_result(it):
            key = it.data(Qt.ItemDataRole.UserRole)
            dlg.accept()
            self.open(key)

        field.textChanged.connect(run)
        results.itemActivated.connect(open_result)
        results.itemDoubleClicked.connect(open_result)
        field.setFocus()
        dlg.exec()

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
        elif self._is_channel(key):
            self._conversations[key] = []
            self._unread.pop(key, None)
            self._save_channel(key[8:])
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
        bubble.setMaximumWidth(self._bubble_max())
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(12, 8, 12, 8)
        bv.setSpacing(3)
        txcol = theme.color("bubble_out_tx" if is_out else "bubble_in_tx")

        title = QLabel(f"📎 {meta['filename']}")
        title.setStyleSheet("color:%s; font-weight:700;" % txcol)
        bv.addWidget(title)
        bv.addWidget(QLabel(_fmt_size(meta["size"])))

        state = self._offer_states.get(tid, "pending")
        expiry_secs = config.load_file_expiry_min() * 60
        if kind == "file_in_offer" and state == "pending" and (time.time() - ts <= expiry_secs):
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
            prog = QLabel(self._progress_text.get(tid, "..."))
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
                thumb = self._make_thumbnail(done)
                if thumb is not None:
                    bv.addWidget(thumb)
                orow = QHBoxLayout()
                of = QPushButton("Open File")
                of.clicked.connect(lambda _=False, p=done: _open_file(p))
                ofd = QPushButton("Open Folder")
                ofd.clicked.connect(lambda _=False, p=done: _reveal_in_explorer(p))
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

    def _make_thumbnail(self, path: str) -> QLabel | None:
        """Return a clickable image preview for *path*, or None if not an image.

        Uses Qt's built-in image readers (png/jpg/gif/bmp/webp) -- no extra deps.
        """
        if not path or not path.lower().endswith(_IMAGE_EXTS):
            return None
        pm = QPixmap(path)
        if pm.isNull():
            return None
        thumb = QLabel()
        thumb.setPixmap(pm.scaled(260, 260, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("Click to open")
        thumb.mousePressEvent = lambda _e, p=path: os.startfile(p)
        return thumb

    def _set_progress(self, tid: str, text: str) -> None:
        self._progress_text[tid] = text
        lbl = self._progress_lbls.get(tid)
        if lbl is not None:
            try:
                lbl.setText(text)
            except RuntimeError:
                self._progress_lbls.pop(tid, None)

    def _open_emoji_picker(self) -> None:
        """Trigger the Windows built-in emoji picker (Win + .) focused on the composer."""
        import ctypes
        self._entry.setFocus()
        VK_LWIN, VK_PERIOD = 0x5B, 0xBE
        KEYEVENTF_KEYUP = 0x0002
        kbi = ctypes.windll.user32.keybd_event
        kbi(VK_LWIN, 0, 0, 0)
        kbi(VK_PERIOD, 0, 0, 0)
        kbi(VK_PERIOD, 0, KEYEVENTF_KEYUP, 0)
        kbi(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)

    def _attach_file(self) -> None:
        ip = self._active
        if not ip or self._is_room(ip):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Send file")
        if path:
            self._attach_file_path(path)

    def _attach_file_path(self, path: str) -> None:
        """Send a file by path (used by both file picker and drag-and-drop)."""
        ip = self._active
        if not ip or self._is_room(ip):
            return
        if not os.path.isfile(path):
            return
        filename = os.path.basename(path)
        size = os.path.getsize(path)

        max_mb = config.load_max_file_mb()
        if max_mb and size > max_mb * 1024 * 1024:
            QMessageBox.warning(
                self, "File too large",
                f'"{filename}" is {_fmt_size(size)}, over the {max_mb} MB limit set '
                "in Settings -> File Transfer.")
            return

        tid = uuid.uuid4().hex[:12]
        self._transfer_paths[tid] = None
        self._progress_text[tid] = f"Waiting for {self._display_name(ip)} to accept..."
        self._add_file_entry(ip, "file_out", tid, filename, size)

        threading.Thread(target=self._offer_worker,
                         args=(ip, path, filename, size, tid), daemon=True).start()

    # -- drag & drop -------------------------------------------------------
    def _can_drop(self) -> bool:
        return bool(self._active) and not self._is_room(self._active)

    def _show_drop_overlay(self) -> None:
        name = self._display_name(self._active)
        self._drop_lbl_peer.setText(f"->  {name}")
        self._drop_overlay.resize(self._messages.viewport().size())
        self._drop_overlay.move(0, 0)
        self._drop_overlay.show()
        self._drop_overlay.raise_()

    def _handle_drag_enter(self, e) -> bool:
        if e.mimeData().hasUrls() and self._can_drop():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
            self._show_drop_overlay()
            return True
        e.ignore()
        return False

    def _handle_drop(self, e) -> bool:
        self._drop_overlay.hide()
        md = e.mimeData()
        if md.hasUrls() and self._can_drop():
            for url in md.urls():
                if url.isLocalFile():
                    self._attach_file_path(url.toLocalFile())
                    break
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
            return True
        e.ignore()
        return False

    # Drag events are delivered to the inner scroll-area / viewport (which sit on
    # top of this window), so we capture them with an event filter rather than the
    # window-level drag*Event overrides (those never fire while a child is hovered).
    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.Type.DragEnter:
            if self._handle_drag_enter(e):
                return True
        elif t == QEvent.Type.DragMove:
            if e.mimeData().hasUrls() and self._can_drop():
                e.setDropAction(Qt.DropAction.CopyAction)
                e.accept()
                return True
            e.ignore()
        elif t == QEvent.Type.DragLeave:
            self._drop_overlay.hide()
        elif t == QEvent.Type.Drop:
            if self._handle_drop(e):
                return True
        return super().eventFilter(obj, e)

    # Keep window-level handlers too, for drops that land on the window chrome.
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        self._handle_drag_enter(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasUrls() and self._can_drop():
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e: QDragLeaveEvent) -> None:
        self._drop_overlay.hide()
        super().dragLeaveEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:
        self._handle_drop(e)

    def _offer_worker(self, ip, path, filename, size, tid) -> None:
        # Callbacks run on a transfer worker thread -- emit signals (queued to the
        # GUI thread) rather than touching widgets or using QTimer here.
        throttle = {"t": 0.0, "pct": -1}

        def progress(done, total, speed, elapsed, eta):
            pct = int(done * 100 / total) if total else 0
            now = time.time()
            if pct != throttle["pct"] or now - throttle["t"] >= 0.12:
                throttle["pct"], throttle["t"] = pct, now
                self._xfer_progress.emit(
                    tid, _fmt_progress("Sending", done, total, speed, elapsed, eta))

        def done():
            self._xfer_finished.emit(tid, ip, path, "Sent ✓")

        def error(msg):
            self._xfer_finished.emit(tid, ip, "", _xfer_fail_text(msg))

        def expire():
            self._xfer_finished.emit(tid, ip, "", "No response -- expired")

        try:
            self._ft.offer_file(ip, path, tid=tid, progress_cb=progress, done_cb=done,
                                error_cb=error, expire_cb=expire)
        except Exception as e:
            self._xfer_finished.emit(tid, ip, "", f"Failed: {e}")

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

    # ── transfer updates (delivered on the GUI thread via signals) ─────────────
    def _on_xfer_progress(self, tid: str, text: str) -> None:
        """Live progress tick: update the label in place (cheap, no re-render)."""
        self._set_progress(tid, text)

    def _on_xfer_finished(self, tid: str, ip: str, path: str, text: str) -> None:
        """Terminal state (done / failed / expired): record result and re-render."""
        self._transfer_paths[tid] = path   # real path = success, "" = failed
        self._offer_states[tid] = "done" if path else "failed"
        self._set_progress(tid, text)
        self._persist(ip)                  # keep this transfer in history
        self._render(ip)

    def on_file_offer_received(self, ip, name, msg) -> None:
        tid = msg["transfer_id"]
        self._unhide(ip)
        self._names[ip] = name
        self._offer_states[tid] = "pending"
        self._add_file_entry(ip, "file_in_offer", tid, msg["filename"], msg["size"], from_ip=ip)
        if not (ip == self._active and self._visible):
            self._notify_background("private", ip, name,
                                    f"📎 Wants to send: {msg['filename']}")

    def _accept_file(self, tid, from_ip, filename, size) -> None:
        from_ip = from_ip or self._active
        if not from_ip:
            return
        self._offer_states[tid] = "accepted"
        self._transfer_paths[tid] = None
        self._set_progress(tid, "Connecting...")
        self._render(from_ip)  # show the progress bubble immediately

        # Callbacks run on a transfer worker thread -- emit signals (queued to the
        # GUI thread) rather than touching widgets or using QTimer here.
        throttle = {"t": 0.0, "pct": -1}

        def progress(done, total, speed, elapsed, eta):
            pct = int(done * 100 / total) if total else 0
            now = time.time()
            if pct != throttle["pct"] or now - throttle["t"] >= 0.12:
                throttle["pct"], throttle["t"] = pct, now
                self._xfer_progress.emit(
                    tid, _fmt_progress("Receiving", done, total, speed, elapsed, eta))

        def fdone(save_path):
            self._xfer_finished.emit(tid, from_ip, save_path, "Saved ✓")

        def ferr(msg):
            self._xfer_finished.emit(tid, from_ip, "", _xfer_fail_text(msg))

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
        self._persist(from_ip)
        self._rerender_if_active(from_ip)

        threading.Thread(target=lambda: self._ft.send_reject(from_ip, tid), daemon=True).start()

    def _cancel_file(self, tid) -> None:
        self._ft.cancel_transfer(tid)
        self._transfer_paths[tid] = ""
        self._offer_states[tid] = "cancelled"
        self._set_progress(tid, "Cancelled")
        if self._active:
            self._persist(self._active)
            self._render(self._active)

    def on_file_accepted(self, ip, name, msg) -> None:
        self._set_progress(msg["transfer_id"], f"{name} accepted -- sending...")
        self._render(ip)  # Always render to show updated status

    def on_file_rejected(self, ip, name, msg) -> None:
        tid = msg["transfer_id"]
        self._ft.cancel_offer(tid)
        self._transfer_paths[tid] = ""
        self._offer_states[tid] = "rejected"
        self._set_progress(tid, f"Rejected by {name}")
        self._persist(ip)
        self._render(ip)  # Always render to show rejection status

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
            ok = QLabel("Accepted -- messages will now appear normally.")
            ok.setStyleSheet("color:%s;" % theme.color("success"))
            v.addWidget(ok)
        else:
            bl = QLabel("Blocked -- messages from this IP are discarded.")
            bl.setStyleSheet("color:%s;" % theme.color("danger"))
            v.addWidget(bl)
        return card

    def on_chat_request_received(self, ip, name, msg) -> None:
        self._unhide(ip)
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
        self._notify_background("private", ip, name, "Wants to chat -- tap to respond")

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

    def _apply_retention(self, msgs: list) -> list:
        """Drop messages older than the configured retention window (#17)."""
        days = config.load_retention_days()
        if not days:
            return msgs   # "Forever"
        cutoff = time.time() - days * 86400
        return [m for m in msgs
                if not isinstance(m, dict) or float(m.get("ts", 0) or 0) >= cutoff]

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
                    msgs = [_migrate_entry(m) for m in
                            data.get("messages", [])[-_MAX_HISTORY:]]
                    self._conversations[ip] = self._apply_retention(msgs)
                    self._index_conversation(ip)
                    self._seed_transfer_state(ip)
                    if self._is_group(ip) and isinstance(data.get("group"), dict):
                        gid = ip[6:]
                        g = data["group"]
                        self._groups[gid] = {
                            "name": g.get("name", "Group"),
                            "members": [m for m in g.get("members", []) if m],
                            "admins": [a for a in g.get("admins", []) if a]}
                        for m in self._groups[gid]["members"]:
                            if m and m != self.chat.my_ip and not self.chat.is_manual_peer(m):
                                self.chat.add_manual_peer(m)
                        continue
                    if self._is_channel(ip) and isinstance(data.get("channel"), dict):
                        cid = ip[8:]
                        c = data["channel"]
                        self._channels[cid] = {
                            "name": c.get("name", "Channel"),
                            "members": [m for m in c.get("members", []) if m],
                            "admins": [a for a in c.get("admins", []) if a]}
                        for m in self._channels[cid]["members"]:
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
                    # Restore last-seen so the peer shows "last seen ..." until it
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

    def _seed_transfer_state(self, key) -> None:
        """Restore the live transfer dicts from persisted file entries on load,
        so reloaded file bubbles render their final state (Open / Cancelled / ...)."""
        for e in self._conversations.get(key, []):
            if not (isinstance(e, dict) and e.get("kind") in ("file_out", "file_in_offer")):
                continue
            tid = e.get("tid")
            if not tid:
                continue
            self._transfer_paths[tid] = e.get("path", "")
            self._progress_text[tid] = e.get("status", "")
            self._offer_states[tid] = e.get("state", "done")

    def _freeze_file_entries(self, key) -> None:
        """Snapshot the live transfer state into each file entry so it survives a
        restart. Pending offers never acted on are marked expired."""
        for e in self._conversations.get(key, []):
            if not (isinstance(e, dict) and e.get("kind") in ("file_out", "file_in_offer")):
                continue
            tid = e.get("tid")
            if not tid:
                continue
            path = self._transfer_paths.get(tid)
            status = self._progress_text.get(tid, e.get("status", ""))
            state = self._offer_states.get(tid, e.get("state", "done"))
            if not path and state in ("pending", "accepted"):
                # interrupted before completion or never answered
                status = status or "Offer expired"
                state = "expired"
            e["path"] = path if path else ""
            e["status"] = status
            e["state"] = state

    def _save_peer(self, ip) -> None:
        self._freeze_file_entries(ip)
        msgs = list(self._conversations.get(ip, []))
        name = self._names.get(ip, ip)
        device = self._devices.get(ip)
        alias = self._aliases.get(ip)
        manual = self.chat.is_manual_peer(ip)
        last_seen = self.chat.last_seen_of(ip)

        def write():
            try:
                safe = ip.replace(".", "_").replace(":", "_")
                kept = [m for m in msgs if m.get("kind") != "chat_req"]
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
                                  "members": group.get("members", []),
                                  "admins": group.get("admins", [])},
                        "messages": kept[-_MAX_HISTORY:]}
                with open(os.path.join(config.get_peer_chat_dir(), f"group_{gid}.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass
        threading.Thread(target=write, daemon=True).start()

    def _save_channel(self, cid) -> None:
        key = f"channel:{cid}"
        msgs = list(self._conversations.get(key, []))
        channel = dict(self._channels.get(cid, {}))

        def write():
            try:
                kept = [m for m in msgs
                        if m.get("kind") not in ("file_out", "file_in_offer", "chat_req")]
                data = {"ip": key,
                        "channel": {"name": channel.get("name", "Channel"),
                                    "members": channel.get("members", []),
                                    "admins": channel.get("admins", [])},
                        "messages": kept[-_MAX_HISTORY:]}
                with open(os.path.join(config.get_peer_chat_dir(), f"channel_{cid}.json"),
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

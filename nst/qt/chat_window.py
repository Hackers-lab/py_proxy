"""The modern LAN-chat window (PyQt6).

A messaging-app style two-pane layout: a searchable roster of peers and groups
on the left, a smooth bubble conversation + composer on the right. All chat
state, history files and the synced-group / reply / file-transfer protocols are
shared unchanged with the service layer.
"""

import base64
import json
import os
import subprocess
import threading
import time
import uuid

from PyQt6.QtCore import (QBuffer, QByteArray, QEvent, QPoint, QSize, Qt, QTimer,
                         pyqtSignal)
from PyQt6.QtGui import (QColor, QCursor, QDragEnterEvent, QDragLeaveEvent,
                        QDropEvent, QIcon, QImage, QPainter, QPainterPath, QPen,
                        QPixmap, QTextCursor)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QDialog,
                             QFileDialog, QFrame, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QStackedWidget, QToolButton,
                             QVBoxLayout, QWidget)

from .. import __version__, antivirus, chatlock, config
from ..chat import DemoBot, UpdatesBot
from ..constants import CHAT_RATE_LIMIT, CHAT_RATE_WINDOW, CHAT_TCP_PORT
from ..filetransfer import FileTransferService
from ..netinfo import check_host_reachable, is_valid_ipv4
from . import sound
from .settings_dialog import SettingsDialog
from .theme import theme
from .widgets import (Avatar, AvatarWithStatus, ToggleSwitch, dot_pixmap,
                      hline)

_PLACEHOLDER = "Type a message..."
_MAX_HISTORY = 200
_BUBBLE_MAX = 420  # fallback before the window is realized
_SAVE_DEBOUNCE = 1.0   # seconds to coalesce rapid history writes into one


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
_EDIT_WINDOW   = 120   # seconds you may still edit a sent message (2 min)


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


# Inline pasted images are downscaled + compressed so one fits in a single chat
# message frame and doesn't bloat history. Full-resolution sharing still goes
# through the file-transfer button.
_INLINE_MAX_EDGE = 1280
_INLINE_MAX_BYTES = 1_200_000   # raw bytes; base64 ~1.33x, well under the 8 MB frame


def _encode_image(img: QImage) -> dict | None:
    """Downscale/compress *img* to a wire dict {mime, data(base64), name}."""
    if img is None or img.isNull():
        return None
    if max(img.width(), img.height()) > _INLINE_MAX_EDGE:
        img = img.scaled(_INLINE_MAX_EDGE, _INLINE_MAX_EDGE,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    # PNG keeps screenshots/text crisp; fall back to progressively smaller JPEG.
    attempts = [("image/png", "PNG", -1, "image.png"),
                ("image/jpeg", "JPG", 82, "image.jpg"),
                ("image/jpeg", "JPG", 60, "image.jpg")]
    for mime, fmt, quality, name in attempts:
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        ok = img.save(buf, fmt, quality)
        buf.close()
        if ok and ba.size() <= _INLINE_MAX_BYTES:
            return {"mime": mime, "name": name,
                    "data": base64.b64encode(bytes(ba)).decode("ascii")}
    # Still too big — shrink harder and accept a lower-quality JPEG.
    img = img.scaled(900, 900, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", 55)
    buf.close()
    return {"mime": "image/jpeg", "name": "image.jpg",
            "data": base64.b64encode(bytes(ba)).decode("ascii")}


def _pixmap_from_image_dict(d: dict) -> QPixmap | None:
    try:
        raw = base64.b64decode(d.get("data", ""))
    except Exception:
        return None
    pm = QPixmap()
    return pm if pm.loadFromData(raw) and not pm.isNull() else None


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


class _ReplyButton(QAbstractButton):
    """A reply arrow drawn with QPainter (not a font glyph, so it always shows).

    Always visible in the accent colour; while the parent row is hovered it
    fills into an accent circle with a white arrow."""

    def __init__(self, accent: str) -> None:
        super().__init__()
        self._accent = QColor(accent)
        self._hover = False
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Reply")

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawEllipse(0, 0, w, h)
            stroke = QColor("#ffffff")
        else:
            stroke = self._accent
        pen = QPen(stroke)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Reply arrow: an arrowhead on the left + an L-shaped shaft going
        # right then up — reads as a "reply/back" curve.
        tip_x, mid_y = w * 0.30, h * 0.56
        head = QPainterPath()
        head.moveTo(w * 0.46, h * 0.36)
        head.lineTo(tip_x, mid_y)
        head.lineTo(w * 0.46, h * 0.72)
        p.drawPath(head)
        shaft = QPainterPath()
        shaft.moveTo(tip_x, mid_y)
        shaft.lineTo(w * 0.66, mid_y)
        shaft.cubicTo(w * 0.80, mid_y, w * 0.80, h * 0.30,
                      w * 0.66, h * 0.30)
        p.drawPath(shaft)


class _ClipButton(QAbstractButton):
    """A solid-coloured 'attach' button with a simple paperclip drawn by
    QPainter (no font glyph). Vertically expands to match the composer."""

    def __init__(self, bg: str) -> None:
        super().__init__()
        self._bg = QColor(bg)
        self._hover = False
        self.setFixedSize(44, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Attach a file")

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # Rounded coloured background.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._bg.lighter(115) if self._hover else self._bg)
        p.drawRoundedRect(0, 0, w, h, 10, 10)
        # Centered paperclip: one continuous rounded wire (outer loop + inner
        # short side), scaled to a fixed icon size regardless of button height.
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = w / 2.0, h / 2.0
        ih = 18.0
        top, bot = cy - ih / 2.0, cy + ih / 2.0
        r = 3.6
        clip = QPainterPath()
        clip.moveTo(cx - r, top + 2)
        clip.lineTo(cx - r, bot - r)
        clip.cubicTo(cx - r, bot, cx + r, bot, cx + r, bot - r)
        clip.lineTo(cx + r, top + r)
        clip.cubicTo(cx + r, top, cx, top, cx, top + r)
        clip.lineTo(cx, bot - r)
        p.drawPath(clip)


class _Composer(QPlainTextEdit):
    """Multi-line message input: Enter sends, Shift+Enter inserts a newline.

    Auto-grows from one line up to a few lines, then scrolls (update.md #9).
    """

    submit = pyqtSignal()
    cancel = pyqtSignal()             # Escape pressed (cancel an active reply/edit)
    imagePasted = pyqtSignal(object)  # QImage pasted/dropped into the composer
    heightChanged = pyqtSignal(int)   # emitted whenever the auto-height changes

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
        self._emoji_btn: QPushButton | None = None
        self.textChanged.connect(self._auto_height)
        self._auto_height()

    def set_emoji_button(self, btn: "QPushButton") -> None:
        """Overlay an emoji button at the inner-right edge, vertically centred,
        and reserve text space on the right so typing never runs under it."""
        self._emoji_btn = btn
        btn.setParent(self)
        self.setViewportMargins(0, 0, 34, 0)
        btn.raise_()
        self._place_emoji()

    def _place_emoji(self) -> None:
        if not self._emoji_btn:
            return
        b = self._emoji_btn
        b.move(self.width() - b.width() - 6, max(0, (self.height() - b.height()) // 2))

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
        if e.key() == Qt.Key.Key_Escape:
            self.cancel.emit()                  # Escape → drop reply/edit
            return
        super().keyPressEvent(e)

    def canInsertFromMimeData(self, source) -> bool:
        if source.hasImage():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        # A pasted picture (e.g. copied from a website or the Snipping Tool)
        # becomes an inline image instead of being dropped on the floor.
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.imagePasted.emit(img)
                return
        super().insertFromMimeData(source)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._auto_height()
        self._place_emoji()

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
            self.heightChanged.emit(target)
        self._place_emoji()
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


class _LockGateDialog(QDialog):
    """Unlock prompt for password-protected chat.

    Two pages: enter the password, or — via "Forgot password?" — answer the
    security questions to authorise a reset. ``action`` is one of "unlocked",
    "reset" or "cancel" after the dialog closes.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chat Locked 🔒")
        self.setModal(True)
        self.action = "cancel"
        self._answers: list[str] = []
        self.resize(420, 200)
        outer = QVBoxLayout(self)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)
        self._stack.addWidget(self._password_page())
        self._stack.addWidget(self._reset_page())

    def _password_page(self) -> QWidget:
        w = QVBoxLayout()
        page = QWidget()
        page.setLayout(w)
        w.addWidget(QLabel("This chat is locked. Enter your password to unlock."))
        self._pw = QLineEdit()
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("Password")
        self._pw.returnPressed.connect(self._try_unlock)
        w.addWidget(self._pw)
        self._err = QLabel("")
        self._err.setStyleSheet("color:#e5534b; font-size:11px;")
        w.addWidget(self._err)
        w.addStretch(1)
        row = QHBoxLayout()
        forgot = QPushButton("Forgot password?")
        forgot.setFlat(True)
        forgot.setCursor(Qt.CursorShape.PointingHandCursor)
        forgot.clicked.connect(self._go_reset)
        row.addWidget(forgot)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        unlock = QPushButton("Unlock")
        unlock.setProperty("variant", "accent")
        unlock.setDefault(True)
        unlock.clicked.connect(self._try_unlock)
        row.addWidget(cancel)
        row.addWidget(unlock)
        w.addLayout(row)
        return page

    def _reset_page(self) -> QWidget:
        w = QVBoxLayout()
        page = QWidget()
        page.setLayout(w)
        if chatlock.has_questions():
            w.addWidget(QLabel("Answer your security questions to reset the "
                               "password.\n⚠ Resetting permanently deletes the "
                               "locked chats."))
            self._answer_edits: list[QLineEdit] = []
            for q in chatlock.questions():
                w.addWidget(QLabel(q))
                e = QLineEdit()
                w.addWidget(e)
                self._answer_edits.append(e)
        else:
            self._answer_edits = []
            w.addWidget(QLabel("No security questions were set, so the password "
                               "can't be recovered.\n\nYou can still reset, which "
                               "permanently deletes the locked chats."))
        self._reset_err = QLabel("")
        self._reset_err.setStyleSheet("color:#e5534b; font-size:11px;")
        w.addWidget(self._reset_err)
        w.addStretch(1)
        row = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        row.addWidget(back)
        row.addStretch(1)
        reset = QPushButton("Reset & delete locked chats")
        reset.setProperty("variant", "danger")
        reset.clicked.connect(self._try_reset)
        row.addWidget(reset)
        w.addLayout(row)
        return page

    def _go_reset(self) -> None:
        self._stack.setCurrentIndex(1)

    def _try_unlock(self) -> None:
        if chatlock.unlock(self._pw.text()):
            self.action = "unlocked"
            self.accept()
        else:
            self._err.setText("Incorrect password.")
            self._pw.selectAll()
            self._pw.setFocus()

    def _try_reset(self) -> None:
        answers = [e.text() for e in self._answer_edits]
        if chatlock.has_questions() and not chatlock.verify_answers(answers):
            self._reset_err.setText("One or more answers are incorrect.")
            return
        if QMessageBox.warning(
                self, "Reset chat lock",
                "This permanently deletes the locked chats and removes the "
                "password. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.action = "reset"
            self.accept()


class _LockSetupDialog(QDialog):
    """Create or change the chat-lock password, scope and security questions."""

    def __init__(self, convos: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chat Lock")
        self.setModal(True)
        self.resize(460, 520)
        v = QVBoxLayout(self)
        changing = chatlock.is_set()
        v.addWidget(QLabel("Set a password" if not changing else "Change password"))
        self._pw = QLineEdit(); self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("New password")
        self._pw2 = QLineEdit(); self._pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw2.setPlaceholderText("Confirm password")
        v.addWidget(self._pw)
        v.addWidget(self._pw2)
        show = QCheckBox("Show password")
        show.toggled.connect(lambda on: [e.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
            for e in (self._pw, self._pw2)])
        v.addWidget(show)

        v.addWidget(hline())
        self._whole = QCheckBox("Lock the entire chat (ask for the password on launch)")
        self._whole.setChecked(chatlock.scope() != "selective")
        v.addWidget(self._whole)
        v.addWidget(QLabel("Or lock only these conversations:"))
        self._list = QListWidget()
        self._list.setMaximumHeight(150)
        locked = chatlock.locked_keys()
        for key, label in convos:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if key in locked
                             else Qt.CheckState.Unchecked)
            self._list.addItem(it)
        v.addWidget(self._list)
        self._whole.toggled.connect(lambda on: self._list.setEnabled(not on))
        self._list.setEnabled(not self._whole.isChecked())

        v.addWidget(hline())
        v.addWidget(QLabel("Security questions (used to reset a forgotten "
                           "password — resetting deletes the locked chats):"))
        self._q_edits: list[tuple[QLineEdit, QLineEdit]] = []
        existing = chatlock.questions()
        for i in range(2):
            qe = QLineEdit(); qe.setPlaceholderText(f"Question {i + 1} (optional)")
            ae = QLineEdit(); ae.setPlaceholderText("Answer")
            if i < len(existing):
                qe.setText(existing[i])
            v.addWidget(qe); v.addWidget(ae)
            self._q_edits.append((qe, ae))

        self._err = QLabel(""); self._err.setStyleSheet("color:#e5534b; font-size:11px;")
        v.addWidget(self._err)
        v.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        save = QPushButton("Save"); save.setProperty("variant", "accent")
        save.setDefault(True); save.clicked.connect(self._save)
        row.addWidget(cancel); row.addWidget(save)
        v.addLayout(row)

    def values(self) -> dict:
        return self._result

    def _save(self) -> None:
        pw = self._pw.text()
        if len(pw) < 4:
            self._err.setText("Password must be at least 4 characters.")
            return
        if pw != self._pw2.text():
            self._err.setText("Passwords don't match.")
            return
        scope = "global" if self._whole.isChecked() else "selective"
        keys = []
        if scope == "selective":
            for i in range(self._list.count()):
                it = self._list.item(i)
                if it.checkState() == Qt.CheckState.Checked:
                    keys.append(it.data(Qt.ItemDataRole.UserRole))
            if not keys:
                self._err.setText("Pick at least one conversation, or lock the whole chat.")
                return
        questions = [(qe.text(), ae.text()) for qe, ae in self._q_edits
                     if qe.text().strip() and ae.text().strip()]
        self._result = {"password": pw, "scope": scope, "keys": keys,
                        "questions": questions}
        self.accept()


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
    _scan_done = pyqtSignal(str, bool, str, str, bool)  # tid, ok, threat, engine, scanned
    _scanned_sig = pyqtSignal(str, str)              # tid, scan-badge label (receiver side)

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
        # Edit-in-progress: (key, mid) of the outgoing message being edited, or
        # None. While set, the composer's Send applies an edit instead of a new
        # message (mirrors the reply bar above the composer).
        self._editing: tuple[str, str] | None = None
        # A pasted image staged for sending with the next message (QImage), shown
        # as a preview chip above the composer until sent or cancelled.
        self._pending_image: QImage | None = None
        self._notifications_enabled = config.load_notifications_enabled()
        self._popup_paused: bool = False   # bell pause: suppresses window-raise only
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
        # tid -> (ip, path, filename, size, mode) for an outgoing file awaiting
        # its antivirus scan result before the offer is sent.
        self._scan_ctx: dict[str, tuple] = {}
        # tid -> "🛡 Scanned by …" badge shown on the file bubble once it passes.
        self._scan_info: dict[str, str] = {}

        # Locked (encrypted) conversations whose files we couldn't decrypt yet
        # because the password hasn't been supplied this session: key -> path.
        self._locked_files: dict[str, str] = {}

        self._ft = FileTransferService(chat_service)
        self._ft.start()

        # Remote-screen viewing: set by app.py once the service exists. The chat
        # window only launches outgoing viewer sessions; the host side lives in
        # RemoteHostController.
        self._remote_service = None
        self._remote_windows: list = []

        # App-level actions injected by app.py so the ⋯ header menu can reach the
        # Network Tools window, the updater and quit without owning those objects.
        self._open_network_tools = None
        self._check_updates_cb = None
        self._quit_cb = None

        # Timed notification pause. _notify_resume_at is the epoch second when
        # the pause expires (0 = paused indefinitely or not paused).
        self._notify_resume_at: float = 0.0
        self._notify_pause_timer = QTimer(self)
        self._notify_pause_timer.setSingleShot(True)
        self._notify_pause_timer.timeout.connect(self._on_notify_timer_expired)

        # Deliver worker-thread transfer updates onto the GUI thread.
        self._xfer_progress.connect(self._on_xfer_progress)
        self._xfer_finished.connect(self._on_xfer_finished)
        self._sys_sig.connect(self._sys)
        self._queued_sig.connect(self._on_queued)
        self._scan_done.connect(self._on_scan_done)
        self._scanned_sig.connect(self._on_scanned)

        # Debounced history writer. Each message used to spawn a fresh thread
        # that rewrote the whole conversation file from scratch; an always-on
        # app in active use churned the disk on every keystroke-reply. Now saves
        # are coalesced per-conversation through one persistent writer: a burst
        # of edits to the same chat collapses into a single atomic write ~1s
        # later, and the per-message thread spawn is gone.
        self._save_lock = threading.Lock()
        self._save_queue: dict[str, tuple] = {}   # stem -> (path, data|None, is_delete)
        self._save_event = threading.Event()
        self._save_running = True
        self._save_thread = threading.Thread(target=self._save_writer, daemon=True)
        self._save_thread.start()

        # Sender-side mirror of the peer anti-flood limit: timestamps of our
        # recent outbound messages per conversation, plus the last time we warned
        # about it (so the warning itself isn't repeated every keystroke).
        self._out_times: dict[str, list[float]] = {}
        self._rate_warned: dict[str, float] = {}

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
        # Animated dots for the "… is typing" indicator.
        self._typing_base = "typing"
        self._typing_phase = 0
        self._typing_anim = QTimer(self)
        self._typing_anim.timeout.connect(self._typing_anim_tick)

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

        # YOU header row: YOU label — [theme] [bell] [status] [gear]
        you = QHBoxLayout()
        you.setSpacing(4)
        lbl = QLabel("YOU")
        lbl.setObjectName("section")
        you.addWidget(lbl)
        you.addStretch(1)
        # Theme toggle (☀ / ☾)
        self._theme_btn = QToolButton()
        self._theme_btn.setObjectName("bellBtn")   # reuse same transparent style
        tf = self._theme_btn.font()
        tf.setPixelSize(15)
        self._theme_btn.setFont(tf)
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.clicked.connect(self._toggle_theme)
        self._refresh_theme_btn()
        you.addWidget(self._theme_btn)
        # Bell — shows popup-pause state; click opens timer menu or resumes.
        self._bell_btn = QToolButton()
        self._bell_btn.setObjectName("bellBtn")
        bf = self._bell_btn.font()
        bf.setPixelSize(15)
        self._bell_btn.setFont(bf)
        self._bell_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bell_btn.clicked.connect(self._open_bell_menu)
        self._refresh_bell_btn()
        you.addWidget(self._bell_btn)
        # Status chip — presence dot + ▾
        self._status_btn = QToolButton()
        self._status_btn.setObjectName("statusChip")
        self._status_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._status_btn.setText("▾")
        self._status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._status_btn.setToolTip("Your status")
        self._status_btn.clicked.connect(self._open_status_menu)
        self._refresh_status_btn()
        you.addWidget(self._status_btn)
        # Settings gear ⚙
        gear = QToolButton()
        gear.setObjectName("gearBtn")
        gear.setText("⚙")
        gf = gear.font()
        gf.setPixelSize(20)
        gf.setBold(True)
        gear.setFont(gf)
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.setToolTip("Menu")
        gear.clicked.connect(self._open_app_menu)
        you.addWidget(gear)
        s.addLayout(you)
        s.addWidget(hline())

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
        self._ip_edit.setPlaceholderText("10.x.x.x")
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
        self._btn_remote = QPushButton("🖥")
        self._btn_remote.setProperty("variant", "ghost")
        self._btn_remote.setFixedWidth(40)
        self._btn_remote.setToolTip("View / control this peer's screen")
        self._btn_remote.clicked.connect(self._open_remote)
        head.addWidget(self._btn_remote)
        self._btn_manage = QPushButton("⚙ Manage")
        self._btn_manage.clicked.connect(self._manage_active)
        head.addWidget(self._btn_manage)
        self._btn_add = QPushButton("＋ Add")
        self._btn_add.setProperty("variant", "accent")
        self._btn_add.clicked.connect(self._add_group_members)
        head.addWidget(self._btn_add)
        self._btn_save = QPushButton("💾")
        self._btn_save.setProperty("variant", "ghost")
        self._btn_save.setFixedWidth(40)
        self._btn_save.setToolTip("Edit display name / alias")
        self._btn_save.clicked.connect(self._edit_alias)
        head.addWidget(self._btn_save)
        self._btn_clear = QPushButton("❌")
        self._btn_clear.setProperty("variant", "ghost")
        self._btn_clear.setFixedWidth(40)
        self._btn_clear.setToolTip("Clear chat history")
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

        # edit bar — shown while editing one of your own recent messages
        self._edit_bar = QFrame()
        self._edit_bar.setObjectName("replyBar")
        eb = QHBoxLayout(self._edit_bar)
        eb.setContentsMargins(10, 6, 8, 6)
        estripe = QFrame()
        estripe.setFixedWidth(3)
        estripe.setStyleSheet("background:%s; border-radius:2px;" % theme.color("accent"))
        eb.addWidget(estripe)
        ebt = QVBoxLayout()
        ebt.setSpacing(0)
        ewho = QLabel("✏ Editing message")
        ewho.setObjectName("accent")
        self._edit_prev = QLabel("")
        self._edit_prev.setObjectName("muted")
        ebt.addWidget(ewho)
        ebt.addWidget(self._edit_prev)
        eb.addLayout(ebt, 1)
        ebx = QPushButton("✕")
        ebx.setFixedSize(22, 22)
        ebx.setCursor(Qt.CursorShape.PointingHandCursor)
        ebx.setStyleSheet(
            "QPushButton{background:transparent; border:none; padding:0;"
            " font-size:13px; color:%s;}"
            "QPushButton:hover{color:%s;}"
            % (theme.color("text_sec"), theme.color("text_pri")))
        ebx.clicked.connect(self._cancel_edit)
        eb.addWidget(ebx)
        self._edit_bar.hide()
        r.addWidget(self._edit_bar)

        # image preview bar — a pasted image staged to send with the next message
        self._img_bar = QFrame()
        self._img_bar.setObjectName("replyBar")
        ib = QHBoxLayout(self._img_bar)
        ib.setContentsMargins(10, 6, 8, 6)
        self._img_thumb = QLabel()
        self._img_thumb.setFixedSize(54, 40)
        self._img_thumb.setScaledContents(False)
        ib.addWidget(self._img_thumb)
        ilbl = QLabel("📷 Image ready — add a caption (optional) and press Send")
        ilbl.setObjectName("muted")
        ib.addWidget(ilbl, 1)
        ibx = QPushButton("✕")
        ibx.setFixedSize(22, 22)
        ibx.setCursor(Qt.CursorShape.PointingHandCursor)
        ibx.setStyleSheet(
            "QPushButton{background:transparent; border:none; padding:0;"
            " font-size:13px; color:%s;}"
            "QPushButton:hover{color:%s;}"
            % (theme.color("text_sec"), theme.color("text_pri")))
        ibx.clicked.connect(self._cancel_image)
        ib.addWidget(ibx)
        self._img_bar.hide()
        r.addWidget(self._img_bar)

        # read-only notice (broadcast channels where you're not an admin)
        self._readonly_lbl = QLabel("📢  Only channel admins can post here.")
        self._readonly_lbl.setObjectName("muted")
        self._readonly_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._readonly_lbl.setStyleSheet(
            "font-style:italic; padding:8px; color:%s;" % theme.color("text_sec"))
        self._readonly_lbl.hide()
        r.addWidget(self._readonly_lbl)

        comp = QHBoxLayout()
        comp.setSpacing(6)
        self._entry = _Composer()
        self._entry.setPlaceholderText(_PLACEHOLDER)
        self._entry.submit.connect(self._send)
        self._entry.cancel.connect(self._on_composer_escape)
        self._entry.imagePasted.connect(self._stage_image)
        self._entry.textChanged.connect(self._on_typing_edit)
        self._entry.textChanged.connect(self._update_send_enabled)
        comp.addWidget(self._entry, 1)

        # Emoji button lives INSIDE the composer at the inner-right edge.
        self._btn_emoji = QPushButton("😊")
        self._btn_emoji.setFixedSize(30, 30)
        self._btn_emoji.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_emoji.setToolTip("Open emoji picker  (Win + .)")
        self._btn_emoji.setStyleSheet(
            "QPushButton{background:transparent; border:none; padding:0;"
            " font-size:18px; color:%s;}"
            "QPushButton:hover{color:%s;}"
            % (theme.color("text_sec"), theme.color("text_pri")))
        self._btn_emoji.clicked.connect(self._open_emoji_picker)
        self._entry.set_emoji_button(self._btn_emoji)

        # File + Send: both solid-coloured and kept exactly the composer's
        # height (synced via the composer's heightChanged signal).
        self._btn_file = _ClipButton(theme.color("accent2"))
        self._btn_file.clicked.connect(self._attach_file)
        comp.addWidget(self._btn_file, alignment=Qt.AlignmentFlag.AlignBottom)

        self._btn_send = QPushButton("➤")
        self._btn_send.setFixedWidth(44)
        self._btn_send.setToolTip("Send  (Enter)")
        self._btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_send.clicked.connect(self._send)
        comp.addWidget(self._btn_send, alignment=Qt.AlignmentFlag.AlignBottom)
        self._entry.heightChanged.connect(self._sync_composer_buttons)
        self._sync_composer_buttons(self._entry.height())
        self._update_send_enabled()
        self._composer = QWidget()
        self._composer.setLayout(comp)
        r.addWidget(self._composer)

        root.addWidget(right, 1)
        self._show_empty_state()
        self._set_composer_visible(False)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _bubble_max(self) -> int:
        """Maximum bubble width: ~88 % of the chat viewport (leaving ~12 % on the
        opposite side for the reply button + breathing room), clamped 280-980 px.
        A long message can therefore fill most of the width instead of stopping
        well short and leaving a big empty gutter down the middle."""
        vp = self._messages.viewport().width()
        if vp < 10:
            return _BUBBLE_MAX  # not yet realized -- use module fallback
        return max(280, min(980, int(vp * 0.88)))

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
    def _is_virtual(self, key: str) -> bool:
        return key in (DemoBot.IP, UpdatesBot.IP)

    def open(self, key: str | None = None) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._visible = True
        self._maybe_unlock_on_open()
        if key == "update":
            self._ensure_updates_bot()
            QTimer.singleShot(100, lambda: self.select_peer(UpdatesBot.IP))
        elif key:
            self.select_peer(key)

    def post_update_notes(self, version: str, bullets: list[str]) -> None:
        """Inject changelog messages into the What's New conversation."""
        self._ensure_updates_bot()
        bot = self.chat.get_updates_bot()
        if bot:
            bot.post_notes(version, bullets)
        QTimer.singleShot(400, lambda: self.select_peer(UpdatesBot.IP))

    def _ensure_updates_bot(self) -> None:
        if not self.chat.has_updates_bot():
            self.chat.add_updates_bot()

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

    def _refresh_theme_btn(self) -> None:
        self._theme_btn.setText("☀️" if theme.is_dark() else "🌙")
        self._theme_btn.setToolTip(
            "Switch to light mode" if theme.is_dark() else "Switch to dark mode")

    def _toggle_theme(self) -> None:
        theme.toggle()
        self._refresh_theme_btn()

    def _refresh_bell_btn(self) -> None:
        if not self._popup_paused:
            self._bell_btn.setText("🔔")
            self._bell_btn.setToolTip("Window pop-up on — click to pause")
        else:
            self._bell_btn.setText("🔕")
            if self._notify_resume_at:
                remaining = max(0, self._notify_resume_at - time.time())
                h, m = int(remaining // 3600), int((remaining % 3600) // 60)
                when = f"{h}h {m}m" if h else f"{m}m"
                self._bell_btn.setToolTip(
                    f"Window pop-up paused — resumes in {when} (click to resume now)")
            else:
                self._bell_btn.setToolTip("Window pop-up paused — click to resume")

    def _open_bell_menu(self) -> None:
        # If paused, a single click resumes — no menu needed.
        if self._popup_paused:
            self._set_popup_paused(False)
            return
        m = QMenu(self)
        m.addAction("Pause for 15 minutes",  lambda: self._pause_popup(15 * 60))
        m.addAction("Pause for 1 hour",      lambda: self._pause_popup(1 * 3600))
        m.addAction("Pause for 2 hours",     lambda: self._pause_popup(2 * 3600))
        m.addAction("Pause for 6 hours",     lambda: self._pause_popup(6 * 3600))
        m.addAction("Pause for 24 hours",    lambda: self._pause_popup(24 * 3600))
        m.exec(self.sender().mapToGlobal(QPoint(0, self.sender().height())))

    def _pause_popup(self, seconds: int) -> None:
        """Pause window-raise for *seconds*; toast + sound keep working."""
        self._notify_pause_timer.stop()
        self._notify_resume_at = time.time() + seconds
        self._set_popup_paused(True)
        self._notify_pause_timer.start(seconds * 1000)

    def _set_popup_paused(self, paused: bool) -> None:
        if not paused:
            self._notify_pause_timer.stop()
            self._notify_resume_at = 0.0
        self._popup_paused = paused
        self._refresh_bell_btn()
        self._log(f"Window pop-up {'paused' if paused else 'resumed'}.")

    def _on_notify_timer_expired(self) -> None:
        self._notify_resume_at = 0.0
        self._set_popup_paused(False)

    def _refresh_status_btn(self) -> None:
        """Repaint the status chip dot to match the current presence."""
        self._status_btn.setIcon(QIcon(dot_pixmap(self.chat.my_status, 13)))
        self._status_btn.setIconSize(QSize(13, 13))

    def _open_status_menu(self) -> None:
        m = QMenu(self)
        status = self.chat.my_status

        # Consistent presence dots (green/amber/grey); active status gets a ✓.
        def _status_item(key: str, label: str) -> None:
            act = m.addAction(label + ("   ✓" if status == key else ""))
            act.setIcon(QIcon(dot_pixmap(key, 12)))
            act.triggered.connect(lambda: self.set_status(key))

        _status_item("online", "Online")
        _status_item("away", "Away")
        _status_item("invisible", "Invisible (appear offline)")
        m.exec(self.sender().mapToGlobal(QPoint(0, self.sender().height())))

    def _open_full_settings(self) -> None:
        SettingsDialog(self, self).exec()

    # ── app menu (⚙) ──────────────────────────────────────────────────────────
    def set_app_actions(self, open_network_tools=None,
                        check_updates=None, quit_app=None) -> None:
        """Wire up app-level actions surfaced by the ⚙ header menu (injected by
        app.py, which owns the Network Tools window, updater and quit)."""
        if open_network_tools is not None:
            self._open_network_tools = open_network_tools
        if check_updates is not None:
            self._check_updates_cb = check_updates
        if quit_app is not None:
            self._quit_cb = quit_app

    def _open_app_menu(self) -> None:
        m = QMenu(self)
        if self._open_network_tools:
            m.addAction("Network Tools", self._open_network_tools)
            m.addSeparator()
        m.addAction("Settings...", self._open_full_settings)
        if self._check_updates_cb:
            m.addAction("Check for Updates", self._check_updates_cb)
        m.addAction("About", self._about)
        if self._quit_cb:
            m.addSeparator()
            m.addAction("Quit", self._quit_cb)
        m.exec(self.sender().mapToGlobal(QPoint(0, self.sender().height())))

    def _about(self) -> None:
        QMessageBox.about(
            self, "About Net Split-Tunneler",
            f"<b>Net Split-Tunneler v{__version__}</b><br>"
            "Proxy Sharing Tool + LAN Chat<br><br>"
            "A lightweight Windows utility to split-tunnel local traffic, share a "
            "proxy connection, and chat across the LAN.<br><br>"
            "Developed by Pramod Verma")

    def set_status(self, status: str) -> None:
        self.chat.my_status = status
        config.save_my_status(status)
        self._refresh_status_btn()
        self._log(f"You now appear {status} to peers.")

    def _set_notify(self, enabled: bool) -> None:
        """Master notification switch (sound + toast). Bell controls popup only."""
        self._notifications_enabled = enabled
        config.save_notifications_enabled(enabled)

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
        self._refresh_bell_btn()
        self._refresh_status_btn()

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
        if ip in (DemoBot.IP, UpdatesBot.IP):
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
        if ip == UpdatesBot.IP:
            return "app updates"
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

        if self._unlock_banner_needed():
            self._add_unlock_banner()

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
                              status, self._unread.get(ip, 0), "peer", not self._is_virtual(ip))

        if manual_peers and not self._add_section("IP / MANUAL", len(manual_peers)):
            for ip in sorted(manual_peers, key=_peer_sort):
                status = self._status_of(ip)
                self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, status),
                              status, self._unread.get(ip, 0), "peer", True)

        if offline_f and not self._add_section("OFFLINE", len(offline_f)):
            for ip in sorted(offline_f, key=_peer_sort):
                self._add_row(ip, self._display_name(ip), self._peer_subtitle(ip, "offline"),
                              "offline", self._unread.get(ip, 0), "peer", not self._is_virtual(ip))

        if self._active:
            self._update_header_sub(peers)

    def _add_unlock_banner(self) -> None:
        """A clickable roster banner to unlock password-protected chats."""
        n = len(self._locked_files)
        banner = QFrame()
        banner.setObjectName("card2")
        banner.setCursor(Qt.CursorShape.PointingHandCursor)
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(10, 8, 10, 8)
        lbl = QLabel(f"🔒  {n} locked chat{'s' if n != 1 else ''} — click to unlock")
        lbl.setStyleSheet("font-weight:700; color:%s;" % theme.color("accent"))
        bl.addWidget(lbl, 1)
        banner.mousePressEvent = lambda _e: self._run_unlock_gate()
        self._roster.add(banner)

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
        elif key == UpdatesBot.IP:
            self._head_sub.setText("app release notes")
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
        if self._editing and self._editing[0] != key:
            self._cancel_edit()   # leaving the chat being edited abandons the edit
        if prev != key and self._pending_image is not None:
            self._cancel_image()  # a staged image belongs to the chat it was pasted in
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
        self._btn_save.setVisible(not is_room and not self._is_virtual(key))
        self._btn_remote.setVisible(not is_room and not self._is_virtual(key))
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

    # ── remote screen ─────────────────────────────────────────────────────────
    def set_remote_service(self, service) -> None:
        """Give the chat window the RemoteScreenService so the 🖥 button works."""
        self._remote_service = service

    def apply_remote_enabled(self, enabled: bool) -> None:
        """Start/stop accepting incoming screen sessions when the user toggles it."""
        if not self._remote_service:
            return
        if enabled:
            self._remote_service.start()   # idempotent
        else:
            self._remote_service.stop()

    def _open_remote(self) -> None:
        key = self._active
        if not key or self._is_room(key) or self._is_virtual(key):
            return
        if not self._remote_service:
            QMessageBox.information(self, "Remote screen",
                                    "Remote screen is not available.")
            return
        from .remote_window import open_viewer
        win = open_viewer(self._remote_service, key, self._display_name(key))
        # Keep a reference so the window isn't garbage-collected; drop it on close.
        self._remote_windows.append(win)
        win.destroyed.connect(lambda *_: self._remote_windows.remove(win)
                              if win in self._remote_windows else None)

    def _set_composer_visible(self, on: bool) -> None:
        self._composer.setVisible(on)
        self._btn_clear.setVisible(on)
        if not on:
            self._btn_add.setVisible(False)
            self._btn_save.setVisible(False)
            self._btn_manage.setVisible(False)
            self._btn_remote.setVisible(False)
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
            prev = None
            for entry in msgs:
                self._messages.add(self._make_bubble(entry, prev))
                prev = entry
        self._messages.scroll_to_bottom()

    def _append(self, entry: dict) -> None:
        conv = self._conversations.get(self._active, [])
        if conv and conv[-1] is entry:
            prev = conv[-2] if len(conv) >= 2 else None
        else:
            prev = conv[-1] if conv else None
        self._messages.add(self._make_bubble(entry, prev))
        self._messages.scroll_to_bottom()

    def _grouped_with(self, entry: dict, prev: dict | None) -> bool:
        """True if *entry* should visually merge with the preceding bubble:
        same sender, same direction, both plain chat, within 5 minutes."""
        if not prev:
            return False
        kind = entry.get("kind")
        if kind not in ("in", "out") or prev.get("kind") != kind:
            return False
        if entry.get("sender") != prev.get("sender"):
            return False
        return abs(float(entry.get("ts", 0)) - float(prev.get("ts", 0))) <= 300

    def _tick_parts(self, status: str, is_out: bool) -> tuple[str, str]:
        """Return (glyph, color) for a delivery-status tick on an out bubble.

        Three clearly distinct states on the blue outgoing bubble:
          sent       single dim tick   ✓
          delivered  double dim tick   ✓✓   (reached their device)
          read       double GREEN tick ✓✓   (they opened it) -- bright green so
                     it is unmistakable against the blue bubble and clearly
                     different from the faded 'delivered' ticks.
        """
        muted = "rgba(255,255,255,0.6)" if is_out else theme.color("text_sec")
        if status == "read":
            return "✓✓", "#3ddc84" if is_out else "#1aa260"
        if status == "delivered":
            return "✓✓", muted
        if status == "queued":
            return "🕓", muted   # held in offline queue, awaiting peer
        return "✓", muted   # sent

    def _make_sys_pill(self, text: str) -> QWidget:
        """A centered rounded 'pill' for system notices (joins, renames, etc.)."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 6, 4, 6)
        h.addStretch(1)
        pill = QLabel(text)
        pill.setWordWrap(True)
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setStyleSheet(
            "background:%s; color:%s; border-radius:11px; padding:4px 12px;"
            " font-size:11px;" % (theme.color("panel2"), theme.color("text_sec")))
        h.addWidget(pill)
        h.addStretch(1)
        return row

    def _make_bubble(self, entry: dict, prev: dict | None = None) -> QWidget:
        kind = entry.get("kind", "sys")
        if kind in ("file_out", "file_in_offer"):
            return self._make_file_bubble(entry)
        if kind == "chat_req":
            return self._make_req_bubble(entry)
        if kind == "sys":
            return self._make_sys_pill(entry.get("text", ""))

        sender, text, ts = entry.get("sender", ""), entry.get("text", ""), entry.get("ts", 0)
        reply = entry.get("reply")
        is_out = kind == "out"
        deleted = bool(entry.get("deleted"))
        grouped = self._grouped_with(entry, prev)
        active = self._active or ""
        in_room = self._is_group(active) or self._is_channel(active)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 1 if grouped else 9, 4, 1)
        h.setSpacing(6)
        bubble = QFrame()
        bubble.setProperty("bubble", "out" if is_out else "in")
        bubble.setMaximumWidth(self._bubble_max())
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(12, 8, 12, 6)
        bv.setSpacing(2)
        txcol = theme.color("bubble_out_tx" if is_out else "bubble_in_tx")
        # Lower-contrast colour for timestamps/ticks so the message text leads.
        meta_col = "rgba(255,255,255,0.6)" if is_out else theme.color("text_sec")

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

        if not is_out and not grouped:
            sl = QLabel(sender)
            sl.setStyleSheet("color:%s; font-weight:700; font-size:11px;" % theme.color("accent"))
            bv.addWidget(sl)
        if entry.get("fwd"):
            fl = QLabel("↪ Forwarded")
            fl.setStyleSheet("color:%s; font-style:italic; font-size:10px;" % txcol)
            bv.addWidget(fl)
        if isinstance(reply, dict) and reply.get("text"):
            # Quoted reply rendered as a clean inset card with a coloured left
            # bar. The outgoing (blue) bubble darkens rather than white-washing;
            # the incoming bubble gets a subtle grey tint. The background is
            # scoped to #quoteCard so it never bleeds onto the child labels
            # (which would otherwise double-paint and look patchy).
            if is_out:
                q_bg, q_stripe = "rgba(0,0,0,0.20)", "rgba(255,255,255,0.92)"
                q_who, q_tx = "#ffffff", "rgba(255,255,255,0.82)"
            else:
                q_bg, q_stripe = "rgba(127,127,127,0.16)", theme.color("accent")
                q_who, q_tx = theme.color("accent"), theme.color("text_sec")
            q = QFrame()
            q.setObjectName("quoteCard")
            q.setStyleSheet(
                "QFrame#quoteCard{background:%s; border-radius:8px;}" % q_bg)
            qh = QHBoxLayout(q)
            qh.setContentsMargins(0, 0, 0, 0)
            qh.setSpacing(0)
            stripe = QFrame()
            stripe.setFixedWidth(4)
            stripe.setStyleSheet(
                "background:%s; border-top-left-radius:8px;"
                "border-bottom-left-radius:8px;" % q_stripe)
            qh.addWidget(stripe)
            qv = QVBoxLayout()
            qv.setContentsMargins(10, 5, 10, 5)
            qv.setSpacing(1)
            who = QLabel(reply.get("sender", ""))
            who.setStyleSheet("color:%s; font-weight:700; font-size:10px;"
                              " background:transparent;" % q_who)
            snip = reply["text"]
            snip = snip if len(snip) <= 80 else snip[:77] + "..."
            qt = QLabel(snip)
            qt.setStyleSheet("color:%s; font-size:11px; background:transparent;" % q_tx)
            qt.setWordWrap(True)
            qv.addWidget(who)
            qv.addWidget(qt)
            qh.addLayout(qv, 1)
            bv.addWidget(q)
            bv.addSpacing(2)

        img_dict = entry.get("image")
        if isinstance(img_dict, dict):
            pm = _pixmap_from_image_dict(img_dict)
            if pm is not None:
                il = QLabel()
                il.setPixmap(pm.scaled(
                    320, 320, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                il.setStyleSheet("border-radius:8px;")
                il.setCursor(Qt.CursorShape.PointingHandCursor)
                il.setToolTip("Click to open")
                il.mousePressEvent = lambda _e, d=img_dict: self._open_inline_image(d)
                bv.addWidget(il)
                if text:
                    bv.addSpacing(2)

        if text:
            msg = QLabel(text)
            msg.setWordWrap(True)
            msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            msg.setStyleSheet("color:%s; font-size:13px;" % txcol)
            bv.addWidget(msg)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        foot.addStretch(1)
        if entry.get("edited"):
            ed = QLabel("edited")
            ed.setStyleSheet("font-size:10px; font-style:italic; color:%s;" % meta_col)
            foot.addWidget(ed)
        stamp = QLabel(time.strftime("%H:%M", time.localtime(ts)))
        stamp.setStyleSheet("font-size:10px; color:%s;" % meta_col)
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

        # Reply affordance: an always-visible accent-coloured arrow on the
        # bubble's inner side (painted, so it never depends on a font glyph),
        # filling into an accent circle while the row is hovered.
        snd = "You" if is_out else sender
        reply_btn = _ReplyButton(theme.color("accent"))
        reply_btn.clicked.connect(lambda _=False, s=snd, t=text: self._set_reply(s, t))
        hslot = QWidget()
        hslot.setFixedWidth(28)
        hsl = QHBoxLayout(hslot)
        hsl.setContentsMargins(1, 0, 1, 0)
        hsl.addWidget(reply_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Avatar beside incoming messages in a group/channel so it's clear who
        # is speaking. Grouped (consecutive) messages get a blank spacer to keep
        # the left edge aligned.
        def _avatar_slot() -> QWidget:
            if not in_room or is_out:
                return None
            if grouped:
                sp = QWidget(); sp.setFixedWidth(30); return sp
            box = QWidget(); box.setFixedWidth(30)
            bl = QHBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0)
            bl.addWidget(Avatar(sender, 28), alignment=Qt.AlignmentFlag.AlignTop)
            return box

        if is_out:
            h.addStretch(1)
            h.addWidget(hslot, alignment=Qt.AlignmentFlag.AlignVCenter)
            h.addWidget(v_wrap)
        else:
            av = _avatar_slot()
            if av is not None:
                h.addWidget(av, alignment=Qt.AlignmentFlag.AlignTop)
            h.addWidget(v_wrap)
            h.addWidget(hslot, alignment=Qt.AlignmentFlag.AlignVCenter)
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

    # ── edit ──────────────────────────────────────────────────────────────────
    def _begin_edit(self, entry: dict) -> None:
        """Load a recent own message back into the composer for editing."""
        mid = entry.get("mid", "")
        loc = self._mid_index.get(mid)
        key = loc[0] if loc else self._active
        if not key or entry.get("kind") != "out" or not entry.get("text"):
            return
        if time.time() - entry.get("ts", 0) > _EDIT_WINDOW:
            self._log("That message is too old to edit (2 minute limit).")
            return
        self._cancel_reply()
        self._editing = (key, mid)
        text = entry.get("text", "")
        self._entry.setPlainText(text)
        self._entry.moveCursor(QTextCursor.MoveOperation.End)
        snip = text if len(text) <= 80 else text[:77] + "..."
        self._edit_prev.setText(snip)
        self._edit_bar.show()
        self._entry.setFocus()

    def _cancel_edit(self) -> None:
        self._editing = None
        self._edit_bar.hide()
        self._entry.clear()

    def _on_composer_escape(self) -> None:
        """Escape in the composer drops an active edit, image, else a reply."""
        if self._editing:
            self._cancel_edit()
        elif self._pending_image is not None:
            self._cancel_image()
        elif self._reply_to:
            self._cancel_reply()

    # ── inline images ─────────────────────────────────────────────────────────
    def _stage_image(self, img: QImage) -> None:
        """Stage a pasted image for the next send (1:1 chats only)."""
        if not self._active or self._is_room(self._active) or self._is_virtual(self._active):
            self._log("Images can only be pasted into a one-to-one chat.")
            return
        if img is None or img.isNull():
            return
        self._pending_image = img
        pm = QPixmap.fromImage(img).scaled(
            54, 40, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._img_thumb.setPixmap(pm)
        self._img_bar.show()
        self._update_send_enabled()
        self._entry.setFocus()

    def _cancel_image(self) -> None:
        self._pending_image = None
        self._img_bar.hide()
        self._update_send_enabled()

    def _open_inline_image(self, d: dict) -> None:
        """Write an inline image to a temp file and open it in the default viewer."""
        try:
            raw = base64.b64decode(d.get("data", ""))
            ext = ".png" if d.get("mime") == "image/png" else ".jpg"
            path = os.path.join(config.load_download_dir(),
                                f"image_{uuid.uuid4().hex[:8]}{ext}")
            with open(path, "wb") as f:
                f.write(raw)
            os.startfile(path)
        except Exception as e:
            self._log(f"Couldn't open image: {e}")

    def _apply_edit(self, key: str, mid: str, new_text: str) -> None:
        """Commit an edit locally and broadcast it to the recipient(s)."""
        loc = self._mid_index.get(mid)
        if not loc:
            self._cancel_edit()
            return
        ekey, entry = loc
        if entry.get("text", "") == new_text:
            self._cancel_edit()
            return
        entry["text"] = new_text
        entry["edited"] = True
        self._persist(ekey)
        if ekey == self._active:
            self._render(ekey)
        if self._is_group(ekey):
            gid = ekey[6:]
            meta = self._group_meta(gid)
            targets = [m for m in meta.get("members", []) if m and m != self.chat.my_ip]
            threading.Thread(
                target=lambda: [self.chat.send_edit(t, mid, new_text, gid=gid)
                                for t in targets], daemon=True).start()
        elif self._is_channel(ekey):
            cid = ekey[8:]
            meta = self._channel_meta(cid)
            targets = [m for m in meta.get("members", []) if m and m != self.chat.my_ip]
            threading.Thread(
                target=lambda: [self.chat.send_edit(t, mid, new_text)
                                for t in targets], daemon=True).start()
        else:
            threading.Thread(target=lambda: self.chat.send_edit(ekey, mid, new_text),
                             daemon=True).start()
        self._cancel_edit()

    def on_remote_edit(self, from_ip, mid, new_text) -> None:
        """Apply an edit a peer made to one of their messages."""
        loc = self._mid_index.get(mid)
        if not loc:
            return
        key, entry = loc
        if entry.get("kind") != "in" or entry.get("deleted"):
            return   # only the original sender may edit; never revive a tombstone
        entry["text"] = str(new_text)
        entry["edited"] = True
        self._persist(key)
        if key == self._active:
            self._render(key)

    def _sync_composer_buttons(self, h: int) -> None:
        """Keep the file + send buttons exactly as tall as the composer."""
        h = max(36, int(h))
        self._btn_file.setFixedSize(44, h)
        self._btn_send.setFixedSize(44, h)

    def _update_send_enabled(self) -> None:
        """Light up the circular Send button when there's text or a staged image."""
        has_text = bool(self._entry.text().strip()) or self._pending_image is not None
        self._btn_send.setEnabled(has_text)
        if has_text:
            bg, fg = theme.color("accent"), "#ffffff"
        else:
            bg, fg = theme.color("panel2"), theme.color("text_sec")
        self._btn_send.setStyleSheet(
            "QPushButton{background:%s; color:%s; border:none; border-radius:10px;"
            " font-size:16px; font-weight:700;}" % (bg, fg))

    # ── send / receive ────────────────────────────────────────────────────────
    def _rate_block(self, key: str) -> bool:
        """True if sending now would exceed the recipient's anti-flood limit.

        Mirrors the per-sender limit the receiving app enforces, so instead of
        letting the recipient silently drop a too-fast message we keep it in the
        box and tell the user to slow down (warning shown at most once per
        window). Virtual peers (demo / What's New) are never limited.
        """
        if self._is_virtual(key):
            return False
        now = time.time()
        cutoff = now - CHAT_RATE_WINDOW
        times = self._out_times.setdefault(key, [])
        times[:] = [t for t in times if t >= cutoff]
        if len(times) >= CHAT_RATE_LIMIT:
            if now - self._rate_warned.get(key, 0) >= CHAT_RATE_WINDOW:
                self._rate_warned[key] = now
                self._sys(key,
                          f"⚠️ You're sending messages too fast — please slow "
                          f"down. Up to {int(CHAT_RATE_LIMIT)} per minute are "
                          f"delivered; faster ones may be dropped.")
            return True
        times.append(now)
        return False

    def _send(self) -> None:
        key = self._active
        text = self._entry.text().strip()
        if not key or (not text and self._pending_image is None):
            return
        # An active edit takes over Send: commit the new text instead of posting
        # a fresh message (the message keeps its id, so receipts/reactions stand).
        if self._editing:
            self._stop_typing()
            self._apply_edit(self._editing[0], self._editing[1], text)
            return
        if not self._can_post(key):
            return   # broadcast channel: only admins may post
        if self._rate_block(key):
            return   # too fast — message kept in the box, user warned
        # Encode a staged inline image (1:1 chats only).
        image = None
        if self._pending_image is not None and not self._is_room(key):
            image = _encode_image(self._pending_image)
        self._cancel_image()
        self._entry.clear()
        self._stop_typing()
        reply = self._reply_to
        extra = {"image": image} if image else {}
        entry = _mk_entry("out", "You", text, time.time(), reply=reply,
                          status="sent", **extra)
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
                             args=(key, text, reply, mid, image), daemon=True).start()

    def _send_worker(self, ip, text, reply, mid, image=None) -> None:
        ok = self.chat.send(ip, text, reply=reply, mid=mid, image=image)
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

        if sound.should_notify(scope, "popup") and not self._popup_paused:
            if window_up:
                if notifs_ok:
                    self._toasts.notify(title, body, key)
            else:
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
        else:
            # Log why sound was skipped so it's diagnosable from the event log.
            reasons = []
            if not config.load_notifications_enabled():
                reasons.append("notifications off")
            if config.load_mute_all():
                reasons.append("mute all")
            if config.load_do_not_disturb():
                reasons.append("do not disturb")
            if config.load_sound_volume() <= 0:
                reasons.append("volume 0")
            prefs = config.load_notify_prefs()
            if not prefs.get(scope, {}).get("sound", True):
                reasons.append(f"{scope} sound off in Settings")
            if reasons:
                self._log(f"[sound skipped: {', '.join(reasons)}]")

    def receive_message(self, ip, name, text, ts, reply=None, mid="", image=None) -> None:
        self._unhide(ip)
        self._names[ip] = name
        extra = {"image": image} if isinstance(image, dict) else {}
        entry = _mk_entry("in", name, text, ts, mid=mid, reply=reply, **extra)
        self._store(ip, entry)
        self._save_peer(ip)
        if ip == self._active and self._visible:
            self._append(entry)
            self._mark_read(ip)
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())
            prev = text if len(text) <= 120 else text[:117] + "..."
            if image and not text:
                prev = "📷 Photo"
            self._notify_background("private", ip, name, prev)

    def on_group_message(self, group, ip, name, text, ts, reply=None, mid="") -> None:
        gid = group.get("gid")
        if not gid:
            return
        claimed_members = [m for m in group.get("members", []) if m]
        claimed_admins = [a for a in group.get("admins", []) if a]
        known = self._groups.get(gid)
        if known is None:
            # First time we hear of this group — this is how being added works.
            # Serverless: with no prior state we trust the inviter's roster.
            g = self._groups[gid] = {"name": group.get("name", "Group"),
                                     "members": claimed_members,
                                     "admins": claimed_admins}
        else:
            g = known
            # Access control: a sender we no longer list as a member — e.g.
            # someone who was kicked — can't post to or modify the group.
            if ip != self.chat.my_ip and ip not in g.get("members", []):
                return
            g["name"] = group.get("name", g.get("name", "Group"))
            # Roster/admin changes are authoritative only from a current admin,
            # so a plain member's (or a kicked user's) payload can't rewrite
            # membership — nobody can silently re-add someone who was removed.
            if ip in g.get("admins", []):
                if claimed_members:
                    g["members"] = claimed_members
                if claimed_admins:
                    g["admins"] = claimed_admins
        for m in g.get("members", []):
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
        claimed_members = [m for m in channel.get("members", []) if m]
        claimed_admins = [a for a in channel.get("admins", []) if a]
        known = self._channels.get(cid)
        if known is None:
            c = self._channels[cid] = {"name": channel.get("name", "Channel"),
                                       "members": claimed_members,
                                       "admins": claimed_admins}
        else:
            c = known
            # A subscriber we've removed can't act on the channel at all.
            if ip != self.chat.my_ip and ip not in c.get("members", []):
                return
            c["name"] = channel.get("name", c.get("name", "Channel"))
            # Only a current admin may change the roster.
            if ip in c.get("admins", []):
                if claimed_members:
                    c["members"] = claimed_members
                if claimed_admins:
                    c["admins"] = claimed_admins
        for m in c.get("members", []):
            if m and m != self.chat.my_ip and not self.chat.is_manual_peer(m):
                self.chat.add_manual_peer(m)
        self._names[ip] = name
        key = f"channel:{cid}"
        if not text:
            self._save_channel(cid)
            self.update_roster(self.chat.peers())
            return
        # Channels are broadcast-only: members read, admins post. Drop a text
        # post that didn't come from a channel admin.
        if ip != self.chat.my_ip and ip not in c.get("admins", []):
            self._save_channel(cid)
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
        if self._is_virtual(key) or self._is_channel(key):
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
        if (entry.get("kind") == "out" and entry.get("text")
                and time.time() - entry.get("ts", 0) <= _EDIT_WINDOW):
            m.addAction("✏ Edit", lambda: self._begin_edit(entry))
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
            if self._is_virtual(ip) or ip == self._active:
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
        if (not key or self._is_virtual(key) or self._is_channel(key)
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
            self._typing_anim.stop()
            return
        if self._is_group(key):
            if len(live) == 1:
                who = self._aliases.get(live[0]) or self._names.get(live[0], live[0])
                self._typing_base = f"{who} is typing"
            else:
                self._typing_base = f"{len(live)} people are typing"
        else:
            self._typing_base = "typing"
        self._typing_phase = 0
        self._typing_anim_tick()
        self._typing_lbl.show()
        if not self._typing_anim.isActive():
            self._typing_anim.start(450)

    def _typing_anim_tick(self) -> None:
        """Cycle the trailing dots (· ·· ···) for a lively typing indicator."""
        self._typing_phase = (self._typing_phase + 1) % 4
        dots = "•" * self._typing_phase
        self._typing_lbl.setText(f"{self._typing_base} {dots}")

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
        if not ip or self._is_group(ip) or self._is_virtual(ip):
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
                 and not self._is_virtual(ip) and not self._is_room(ip) and ip not in blocked]
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
        elif not self._is_virtual(key):
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
        name = self._display_name(key)
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Clear chat")
        confirm.setText(f"Clear all messages with {name}?")
        confirm.setInformativeText(
            "This permanently removes the local message history for this "
            "conversation. It cannot be undone.")
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        confirm.button(QMessageBox.StandardButton.Yes).setText("Clear")
        if confirm.exec() != QMessageBox.StandardButton.Yes:
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

        av_badge = self._scan_info.get(tid) or entry.get("av")
        if av_badge:
            badge = QLabel(av_badge)
            badge.setStyleSheet("font-size:10px; color:%s;" % txcol)
            badge.setToolTip("This file was checked for malware before "
                             "sending/after receiving.")
            bv.addWidget(badge)

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
        mode = config.load_av_mode()
        if mode == "off":
            self._progress_text[tid] = f"Waiting for {self._display_name(ip)} to accept..."
            self._add_file_entry(ip, "file_out", tid, filename, size)
            threading.Thread(target=self._offer_worker,
                             args=(ip, path, filename, size, tid), daemon=True).start()
            return
        # Scan before the offer leaves this PC; show a "Scanning…" bubble and only
        # offer the file once it comes back clean (or the user overrides in warn mode).
        self._scan_ctx[tid] = (ip, path, filename, size, mode)
        self._offer_states[tid] = "scanning"
        self._progress_text[tid] = "🛡 Scanning for threats…"
        self._add_file_entry(ip, "file_out", tid, filename, size)
        threading.Thread(target=self._scan_worker, args=(tid, path), daemon=True).start()

    @staticmethod
    def _scan_badge(engine: str, scanned: bool) -> str:
        """A short 'scanned by …' label for a file bubble."""
        eng = (engine or "").strip()
        if not eng or eng.startswith("heuristics"):
            return "🛡 Scanned (heuristics)"
        if len(eng) > 38:
            eng = eng[:37] + "…"
        return f"🛡 Scanned by {eng}"

    def _on_scanned(self, tid: str, label: str) -> None:
        """Record a file's scan badge (receiver side) and refresh its bubble."""
        self._scan_info[tid] = label
        if self._active:
            self._rerender_if_active(self._active)

    def _scan_worker(self, tid: str, path: str) -> None:
        res = antivirus.scan(path)
        self._scan_done.emit(tid, res.ok, res.threat, res.engine, res.scanned)

    def _on_scan_done(self, tid, ok, threat, engine, scanned) -> None:
        ctx = self._scan_ctx.pop(tid, None)
        if not ctx:
            return   # cancelled while scanning, or already handled
        ip, path, filename, size, mode = ctx
        if not ok:
            if mode == "block" or QMessageBox.warning(
                    self, "Threat detected",
                    f"“{filename}” was flagged by {engine}:\n\n{threat}\n\n"
                    "Send it anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                self._transfer_paths[tid] = ""
                self._offer_states[tid] = "blocked"
                self._set_progress(tid, f"🛡 Blocked — {threat}")
                self._persist(ip)
                self._rerender_if_active(ip)
                return
        # Clean (or user overrode): proceed with the offer, and tag the bubble
        # with the antivirus that cleared it.
        self._scan_info[tid] = self._scan_badge(engine, scanned)
        self._offer_states[tid] = "pending"
        self._set_progress(tid, f"Waiting for {self._display_name(ip)} to accept...")
        self._rerender_if_active(ip)
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
            mode = config.load_av_mode()
            if mode != "off":
                res = antivirus.scan(save_path)
                if not res.ok:
                    if mode == "block":
                        try:
                            os.remove(save_path)
                        except OSError:
                            pass
                        self._xfer_finished.emit(
                            tid, from_ip, "", f"🛡 Blocked — {res.threat}")
                        return
                    # warn mode: keep the file but flag it clearly
                    self._xfer_finished.emit(
                        tid, from_ip, save_path, f"⚠ Flagged — {res.threat}")
                    return
                self._scanned_sig.emit(tid, self._scan_badge(res.engine, res.scanned))
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
        self._scan_ctx.pop(tid, None)   # if still scanning, abandon the pending offer
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
                    path = os.path.join(d, fname)
                    with open(path, "rb") as f:
                        raw = f.read()
                    if chatlock.is_blob(raw):
                        # Encrypted conversation. Decrypt now if the password has
                        # been supplied this session; otherwise remember it as a
                        # locked entry to load once the user unlocks.
                        key = chatlock.blob_key(raw)
                        dec = chatlock.decrypt_payload(raw)
                        if dec is None:
                            if key:
                                self._locked_files[key] = path
                            continue
                        data = json.loads(dec.decode("utf-8"))
                    else:
                        data = json.loads(raw.decode("utf-8"))
                    self._ingest_history(data)
                except Exception:
                    pass
        except Exception:
            pass

    def _ingest_history(self, data: dict) -> None:
        """Populate conversation state from one decoded history record."""
        ip = data.get("ip")
        if not ip:
            return
        msgs = [_migrate_entry(m) for m in data.get("messages", [])[-_MAX_HISTORY:]]
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
            return
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
            return
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
        # Restore last-seen so the peer shows "last seen ..." until it comes back
        # online; fall back to the newest message time.
        ls = data.get("last_seen") or 0.0
        if not ls:
            try:
                ls = max((float(m.get("ts", 0)) for m in self._conversations[ip]),
                         default=0.0)
            except (TypeError, ValueError):
                ls = 0.0
        self.chat.seed_last_seen(ip, ls)

    def _load_locked_after_unlock(self) -> None:
        """After a successful unlock, decrypt and ingest any deferred files."""
        for key in list(self._locked_files.keys()):
            try:
                with open(self._locked_files[key], "rb") as f:
                    raw = f.read()
                dec = chatlock.decrypt_payload(raw)
                if dec is not None:
                    self._ingest_history(json.loads(dec.decode("utf-8")))
                    self._locked_files.pop(key, None)
            except Exception:
                pass
        self.update_roster(self.chat.peers())

    # ── chat lock (encrypted history) ─────────────────────────────────────────
    def _conversation_choices(self) -> list[tuple[str, str]]:
        """(key, label) for every known conversation, for the lock-setup picker."""
        out = []
        for gid in self._groups:
            out.append((f"group:{gid}", f"👥 {self._display_name(f'group:{gid}')}"))
        for cid in self._channels:
            out.append((f"channel:{cid}", f"📢 {self._display_name(f'channel:{cid}')}"))
        for ip in self._conversations:
            if self._is_group(ip) or self._is_channel(ip) or self._is_virtual(ip):
                continue
            out.append((ip, f"{self._display_name(ip)} ({ip})"))
        return out

    def setup_lock(self) -> None:
        """Create or change the chat-lock password (called from Settings)."""
        if chatlock.needs_unlock():
            # Must unlock before changing so existing chats can be re-encrypted.
            if not self._run_unlock_gate():
                return
        dlg = _LockSetupDialog(self._conversation_choices(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        chatlock.set_password(vals["password"])
        chatlock.set_scope(vals["scope"], vals["keys"])
        chatlock.set_questions(vals["questions"])
        # Re-save every now-locked conversation so it is (re-)encrypted on disk,
        # and every formerly-locked one so it drops back to plaintext.
        for key in set(self._conversations) | set(self._locked_files):
            self._persist(key)
        self._log("Chat lock updated.")

    def remove_lock(self) -> None:
        """Remove the password and re-save locked chats as plaintext."""
        if not chatlock.is_set():
            return
        if chatlock.needs_unlock() and not self._run_unlock_gate():
            return
        was_locked = [k for k in self._conversations if chatlock.is_locked(k)]
        chatlock.clear()
        for key in was_locked:
            self._persist(key)   # now rewritten as plaintext (is_locked → False)
        self._log("Chat lock removed.")

    def _run_unlock_gate(self) -> bool:
        """Show the unlock prompt. Returns True once unlocked (or no lock)."""
        if not chatlock.needs_unlock():
            return True
        dlg = _LockGateDialog(self)
        dlg.exec()
        if dlg.action == "unlocked":
            self._load_locked_after_unlock()
            return True
        if dlg.action == "reset":
            self._reset_lock()
            return False
        return False

    def _reset_lock(self) -> None:
        """Delete every locked conversation and forget the password (reset path)."""
        removed = set(self._locked_files)
        for key in list(self._conversations):
            if chatlock.is_locked(key):
                removed.add(key)
        for key in removed:
            self._delete_history_file(key)
            self._conversations.pop(key, None)
            self._drop_index(key)
            self._locked_files.pop(key, None)
            if self._is_group(key):
                self._groups.pop(key[6:], None)
            elif self._is_channel(key):
                self._channels.pop(key[8:], None)
        self._flush_saves()   # apply the deletions immediately, not after debounce
        chatlock.clear()
        if self._active in removed:
            self._active = None
            self._show_empty_state()
            self._set_composer_visible(False)
        self.update_roster(self.chat.peers())
        self._log(f"Chat lock reset — {len(removed)} locked conversation(s) deleted.")

    def _maybe_unlock_on_open(self) -> None:
        """Global-scope gate shown when the window is opened while locked."""
        if chatlock.needs_unlock() and chatlock.scope() == "global":
            self._run_unlock_gate()

    def _unlock_banner_needed(self) -> bool:
        return bool(self._locked_files) and chatlock.needs_unlock()

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
            if e.get("av"):
                self._scan_info[tid] = e["av"]

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
            av = self._scan_info.get(tid)
            if av:
                e["av"] = av

    # ── debounced history persistence ─────────────────────────────────────────
    def _enqueue_save(self, stem: str, path: str, data: dict | None,
                      is_delete: bool = False) -> None:
        """Queue a history write/delete keyed by file *stem*; later calls for the
        same conversation supersede earlier ones, collapsing a burst into one
        disk operation."""
        with self._save_lock:
            self._save_queue[stem] = (path, data, is_delete)
        self._save_event.set()

    def _save_writer(self) -> None:
        while self._save_running:
            self._save_event.wait()
            if not self._save_running:
                break
            self._save_event.clear()
            time.sleep(_SAVE_DEBOUNCE)    # let a burst of edits accumulate
            self._flush_saves()
        self._flush_saves()

    def _flush_saves(self) -> None:
        with self._save_lock:
            pending = self._save_queue
            self._save_queue = {}
        for path, data, is_delete in pending.values():
            try:
                if is_delete:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    key = data.get("ip") if isinstance(data, dict) else None
                    if key and chatlock.is_locked(key):
                        if not chatlock.is_unlocked():
                            continue   # never overwrite ciphertext with plaintext
                        raw = chatlock.encrypt_payload(key, raw)
                    tmp = f"{path}.tmp"
                    with open(tmp, "wb") as f:
                        f.write(raw)
                    os.replace(tmp, path)   # atomic: never leave a half-written file
            except Exception:
                pass

    def _save_peer(self, ip) -> None:
        self._freeze_file_entries(ip)
        safe = ip.replace(".", "_").replace(":", "_")
        # Shallow-copy each entry so a later mutation (receipt/reaction/edit) on
        # the GUI thread can't change the dict while the writer serializes it.
        kept = [dict(m) for m in self._conversations.get(ip, [])
                if m.get("kind") != "chat_req"]
        data = {"ip": ip, "name": self._names.get(ip, ip),
                "messages": kept[-_MAX_HISTORY:]}
        if self._devices.get(ip):
            data["device"] = self._devices[ip]
        if self._aliases.get(ip):
            data["alias"] = self._aliases[ip]
        if self.chat.is_manual_peer(ip):
            data["manual"] = True
        if self.chat.last_seen_of(ip):
            data["last_seen"] = self.chat.last_seen_of(ip)
        if ip in self.chat._approved_ips:
            data["approved"] = True
        if ip in self.chat._blocked_ips:
            data["blocked"] = True
        path = os.path.join(config.get_peer_chat_dir(), f"{safe}.json")
        self._enqueue_save(safe, path, data)

    def _save_group(self, gid) -> None:
        key = f"group:{gid}"
        group = dict(self._groups.get(gid, {}))
        kept = [dict(m) for m in self._conversations.get(key, [])
                if m.get("kind") not in ("file_out", "file_in_offer", "chat_req")]
        data = {"ip": key,
                "group": {"name": group.get("name", "Group"),
                          "members": group.get("members", []),
                          "admins": group.get("admins", [])},
                "messages": kept[-_MAX_HISTORY:]}
        path = os.path.join(config.get_peer_chat_dir(), f"group_{gid}.json")
        self._enqueue_save(f"group_{gid}", path, data)

    def _save_channel(self, cid) -> None:
        key = f"channel:{cid}"
        channel = dict(self._channels.get(cid, {}))
        kept = [dict(m) for m in self._conversations.get(key, [])
                if m.get("kind") not in ("file_out", "file_in_offer", "chat_req")]
        data = {"ip": key,
                "channel": {"name": channel.get("name", "Channel"),
                            "members": channel.get("members", []),
                            "admins": channel.get("admins", [])},
                "messages": kept[-_MAX_HISTORY:]}
        path = os.path.join(config.get_peer_chat_dir(), f"channel_{cid}.json")
        self._enqueue_save(f"channel_{cid}", path, data)

    def _delete_history_file(self, key) -> None:
        safe = key.replace(".", "_").replace(":", "_")
        path = os.path.join(config.get_peer_chat_dir(), f"{safe}.json")
        self._enqueue_save(safe, path, None, is_delete=True)

    def shutdown(self) -> None:
        try:
            self._ft.stop()
        except Exception:
            pass
        # Stop the writer and flush anything still queued so no message is lost
        # if the user quits within the debounce window (e.g. an update restart).
        self._save_running = False
        self._save_event.set()
        try:
            self._save_thread.join(timeout=3)
        except Exception:
            pass
        self._flush_saves()

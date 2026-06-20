"""Remote-screen UI: the viewer window the local user drives, plus the host-side
controller (accept prompt + "you are being viewed" indicator).

Frames and service callbacks arrive on background threads, so everything here is
funnelled through Qt signals before it touches a widget.
"""

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

# Qt key -> Windows virtual-key code for keys that don't produce text (and for
# modifiers). Letters/digits map directly: Qt.Key_A..Z / 0..9 equal their ASCII
# codes, which are also their VK codes.
_QT_VK = {
    Qt.Key.Key_Backspace: 0x08, Qt.Key.Key_Tab: 0x09, Qt.Key.Key_Return: 0x0D,
    Qt.Key.Key_Enter: 0x0D, Qt.Key.Key_Shift: 0x10, Qt.Key.Key_Control: 0x11,
    Qt.Key.Key_Alt: 0x12, Qt.Key.Key_CapsLock: 0x14, Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Space: 0x20, Qt.Key.Key_PageUp: 0x21, Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_End: 0x23, Qt.Key.Key_Home: 0x24, Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26, Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Meta: 0x5B,
}
for _i in range(12):
    _QT_VK[getattr(Qt.Key, f"Key_F{_i + 1}")] = 0x70 + _i

_BTN = {
    Qt.MouseButton.LeftButton: "l",
    Qt.MouseButton.RightButton: "r",
    Qt.MouseButton.MiddleButton: "m",
}


class _ScreenCanvas(QWidget):
    """Paints the remote frames and forwards local input as normalised events."""

    input_event = pyqtSignal(object)   # event dict -> session.send_input

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._img_rect = QRect()
        self._pressed_vks: set[int] = set()
        self.control_enabled = True
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("background:#101216;")
        self.setMinimumSize(320, 200)

    def set_frame(self, pix: QPixmap) -> None:
        self._pix = pix
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if not self._pix or self._pix.isNull():
            return
        scaled = self._pix.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        self._img_rect = QRect(QPoint(x, y), scaled)
        p.drawPixmap(self._img_rect, self._pix)

    # ── coordinate mapping ───────────────────────────────────────────────────
    def _norm(self, pos) -> tuple[float, float] | None:
        r = self._img_rect
        if r.width() <= 0 or r.height() <= 0:
            return None
        nx = (pos.x() - r.x()) / r.width()
        ny = (pos.y() - r.y()) / r.height()
        if nx < 0 or nx > 1 or ny < 0 or ny > 1:
            return None
        return nx, ny

    # ── mouse ────────────────────────────────────────────────────────────────
    def mouseMoveEvent(self, e) -> None:
        if not self.control_enabled:
            return
        n = self._norm(e.position())
        if n:
            self.input_event.emit({"k": "move", "x": n[0], "y": n[1]})

    def mousePressEvent(self, e) -> None:
        self.setFocus()
        if not self.control_enabled:
            return
        n = self._norm(e.position())
        btn = _BTN.get(e.button())
        if n and btn:
            self.input_event.emit({"k": "button", "btn": btn, "down": True,
                                   "x": n[0], "y": n[1]})

    def mouseReleaseEvent(self, e) -> None:
        if not self.control_enabled:
            return
        n = self._norm(e.position())
        btn = _BTN.get(e.button())
        if n and btn:
            self.input_event.emit({"k": "button", "btn": btn, "down": False,
                                   "x": n[0], "y": n[1]})

    def wheelEvent(self, e) -> None:
        if not self.control_enabled:
            return
        self.input_event.emit({"k": "wheel", "delta": e.angleDelta().y()})

    # ── keyboard ─────────────────────────────────────────────────────────────
    def keyPressEvent(self, e) -> None:
        if not self.control_enabled:
            return
        key = e.key()
        mods = e.modifiers()
        combo = mods & (Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.AltModifier
                        | Qt.KeyboardModifier.MetaModifier)
        if key in _QT_VK:
            vk = _QT_VK[key]
        elif combo and (Qt.Key.Key_A <= key <= Qt.Key.Key_Z
                        or Qt.Key.Key_0 <= key <= Qt.Key.Key_9):
            vk = key   # a shortcut such as Ctrl+C — send the raw virtual key
        else:
            text = e.text()
            if text and text.isprintable():
                self.input_event.emit({"k": "text", "ch": text})
            return
        self._pressed_vks.add(vk)
        self.input_event.emit({"k": "key", "vk": vk, "down": True})

    def keyReleaseEvent(self, e) -> None:
        if not self.control_enabled:
            return
        vk = _QT_VK.get(e.key(), e.key())
        if vk in self._pressed_vks:
            self._pressed_vks.discard(vk)
            self.input_event.emit({"k": "key", "vk": vk, "down": False})


class RemoteViewerWindow(QWidget):
    """Top-level window showing one remote host's screen."""

    _frame_in = pyqtSignal(bytes)
    _accepted = pyqtSignal(str, int, int)
    _rejected = pyqtSignal(str)
    _closed = pyqtSignal(str)
    _clip_in = pyqtSignal(str)

    def __init__(self, service, ip: str, host_label: str, secret: str = "") -> None:
        super().__init__()
        self._service = service
        self._ip = ip
        self._secret = secret or ""
        self._session = None
        self.setWindowTitle(f"Remote screen — {host_label}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1100, 720)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        self._status = QLabel(f"Connecting to {host_label} ({ip})…")
        self._status.setStyleSheet("font-weight:600;")
        bar.addWidget(self._status)
        bar.addStretch(1)

        self._control_btn = QPushButton("Control: on")
        self._control_btn.setCheckable(True)
        self._control_btn.setChecked(True)
        self._control_btn.clicked.connect(self._toggle_control)
        bar.addWidget(self._control_btn)

        clip_btn = QPushButton("Send clipboard")
        clip_btn.clicked.connect(self._push_clipboard)
        bar.addWidget(clip_btn)

        cad = QPushButton("Ctrl+Alt+Del")
        cad.clicked.connect(self._send_cad)
        bar.addWidget(cad)

        disc = QPushButton("Disconnect")
        disc.setProperty("variant", "danger")
        disc.clicked.connect(self.close)
        bar.addWidget(disc)
        root.addLayout(bar)

        self._canvas = _ScreenCanvas(self)
        self._canvas.input_event.connect(self._on_input)
        root.addWidget(self._canvas, 1)

        self._frame_in.connect(self._render_frame)
        self._accepted.connect(self._on_accepted)
        self._rejected.connect(self._on_rejected)
        self._closed.connect(self._on_session_closed)
        self._clip_in.connect(self._set_local_clipboard)

    def start(self) -> None:
        self._session = self._service.connect(
            self._ip, secret=self._secret,
            on_frame=lambda b: self._frame_in.emit(b),
            on_accept=lambda name, w, h: self._accepted.emit(name, w, h),
            on_reject=lambda reason: self._rejected.emit(reason),
            on_clipboard=lambda text: self._clip_in.emit(text),
            on_closed=lambda reason: self._closed.emit(reason or ""),
        )

    # ── slots (GUI thread) ───────────────────────────────────────────────────
    def _render_frame(self, data: bytes) -> None:
        img = QImage.fromData(data)
        if not img.isNull():
            self._canvas.set_frame(QPixmap.fromImage(img))

    def _on_accepted(self, name: str, w: int, h: int) -> None:
        self._status.setText(f"Connected — {name}  ({w}×{h})")
        self._canvas.setFocus()

    def _on_rejected(self, reason: str) -> None:
        self._status.setText(f"Declined: {reason}")
        QMessageBox.information(self, "Remote screen",
                                f"The host declined the connection.\n\n{reason}")
        self.close()

    def _on_session_closed(self, reason: str) -> None:
        if self.isVisible():
            self._status.setText("Disconnected" + (f" — {reason}" if reason else ""))

    def _on_input(self, ev: dict) -> None:
        if self._session:
            self._session.send_input(ev)

    def _toggle_control(self) -> None:
        on = self._control_btn.isChecked()
        self._canvas.control_enabled = on
        self._control_btn.setText(f"Control: {'on' if on else 'off'}")

    def _push_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text and self._session:
            self._session.send_clipboard(text)

    def _set_local_clipboard(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    def _send_cad(self) -> None:
        # Ctrl+Alt+Del can't be injected by SendInput on a normal desktop, but
        # Ctrl+Alt+End (the RDP equivalent) reaches most targets; offer that.
        if not self._session:
            return
        for vk, down in ((0x11, True), (0x12, True), (0x23, True),
                         (0x23, False), (0x12, False), (0x11, False)):
            self._session.send_input({"k": "key", "vk": vk, "down": down})

    def closeEvent(self, e) -> None:
        if self._session:
            self._session.close()
            self._session = None
        super().closeEvent(e)


# ── host side ─────────────────────────────────────────────────────────────────

class _SharingIndicator(QWidget):
    """Small always-on-top banner shown while a peer is viewing this screen."""

    def __init__(self, viewer_label: str, on_stop) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        dot = QLabel("🔴")
        lay.addWidget(dot)
        lbl = QLabel(f"{viewer_label} is viewing your screen")
        lbl.setStyleSheet("color:#fff;font-weight:600;")
        lay.addWidget(lbl)
        stop = QPushButton("Stop")
        stop.setProperty("variant", "danger")
        stop.clicked.connect(on_stop)
        lay.addWidget(stop)
        self.setStyleSheet("background:#202632;border-radius:8px;")
        self.adjustSize()
        self._place()

    def _place(self) -> None:
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 20, scr.top() + 20)


class RemoteHostController:
    """Bridges RemoteScreenService host callbacks to dialogs/indicators.

    Construct with the already-wired ScreenSignals so the service's background
    callbacks (which emit those signals) are handled on the GUI thread here.
    """

    def __init__(self, service, signals) -> None:
        self._service = service
        self._indicators: dict[str, _SharingIndicator] = {}
        signals.request.connect(self._on_request)
        signals.share_started.connect(self._on_share_started)
        signals.share_stopped.connect(self._on_share_stopped)
        signals.clipboard_in.connect(self._on_clipboard_in)

    def _on_request(self, name: str, ip: str, respond) -> None:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Screen-share request")
        box.setText(f"{name} ({ip}) wants to view and control your screen.")
        box.setInformativeText("Allow this connection?")
        allow = box.addButton("Allow", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Decline", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(allow)
        box.exec()
        respond(box.clickedButton() is allow)

    def _on_share_started(self, session) -> None:
        ind = _SharingIndicator(
            f"{session.viewer_name} ({session.viewer_ip})",
            on_stop=lambda sid=session.id: self._service.stop_session(sid),
        )
        self._indicators[session.id] = ind
        ind.show()

    def _on_share_stopped(self, session) -> None:
        ind = self._indicators.pop(session.id, None)
        if ind:
            ind.close()

    def _on_clipboard_in(self, text: str) -> None:
        if text:
            QApplication.clipboard().setText(text)


# ── entry points ──────────────────────────────────────────────────────────────

def open_viewer(service, ip: str, host_label: str, secret: str = "") -> RemoteViewerWindow:
    win = RemoteViewerWindow(service, ip, host_label, secret=secret)
    win.start()
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def prompt_and_connect(service, parent=None) -> RemoteViewerWindow | None:
    """Ask for an IP (and optional secret) then open a viewer."""
    ip, ok = QInputDialog.getText(parent, "Connect to screen",
                                  "Host IP address:", QLineEdit.EchoMode.Normal, "")
    ip = (ip or "").strip()
    if not ok or not ip:
        return None
    secret, _ = QInputDialog.getText(parent, "Connect to screen",
                                     "Secret (leave blank to request permission):",
                                     QLineEdit.EchoMode.Password, "")
    return open_viewer(service, ip, ip, secret=(secret or "").strip())

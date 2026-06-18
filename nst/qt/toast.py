"""Bottom-right toast notifications for incoming chat activity.

Toasts stack upward above the tray, fade in/out, auto-dismiss, and emit
``clicked(key)`` so the app can focus the chat on that conversation.
"""

from PyQt6.QtCore import (QEasingCurve, QPropertyAnimation, Qt, QTimer,
                          pyqtSignal)
from PyQt6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                             QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)
from PyQt6.QtGui import QColor

from .theme import theme

_WIDTH = 330
_MARGIN = 16
_GAP = 10
_LIFETIME_MS = 6000
_MAX = 5


class Toast(QWidget):
    def __init__(self, manager: "ToastManager", title: str, body: str, key: str) -> None:
        super().__init__(None)
        self.manager = manager
        self.key = key
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(_WIDTH)

        card = QFrame(self)
        card.setObjectName("card")
        p = theme.p
        card.setStyleSheet(
            f"QFrame#card {{ background: {p['panel']}; border: 1px solid {p['border']};"
            f" border-radius: 12px; }}")
        shadow = QGraphicsDropShadowEffect(blurRadius=28, xOffset=0, yOffset=6)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 11, 12, 12)
        lay.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        icon = QLabel("\U0001F4AC")
        icon.setStyleSheet(f"color: {p['accent']}; font-size: 14px;")
        ttl = QLabel(title)
        ttl.setStyleSheet("font-weight: 700;")
        ttl.setWordWrap(False)
        close = QPushButton("✕")
        close.setProperty("variant", "ghost")
        close.setFixedSize(22, 22)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(lambda: self.manager.dismiss(self))
        head.addWidget(icon)
        head.addWidget(ttl, 1)
        head.addWidget(close)
        lay.addLayout(head)
        msg = QLabel(body)
        msg.setObjectName("muted")
        msg.setWordWrap(True)
        lay.addWidget(msg)

        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(lambda: self.manager.dismiss(self))
        self._life.start(_LIFETIME_MS)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.setWindowOpacity(0.0)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.manager.clicked.emit(self.key)
            self.manager.dismiss(self)

    def fade_in(self) -> None:
        self._fade.stop()
        self._fade.setDuration(160)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()


class ToastManager(QWidget):
    """Lives as a hidden parent so toasts share the GUI thread/event loop."""

    clicked = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._toasts: list[Toast] = []

    def notify(self, title: str, body: str, key: str) -> None:
        while len(self._toasts) >= _MAX:
            self.dismiss(self._toasts[0])
        t = Toast(self, title, body, key)
        self._toasts.append(t)
        t.show()
        t.adjustSize()
        self._reflow()
        t.fade_in()

    def dismiss(self, toast: Toast) -> None:
        if toast not in self._toasts:
            return
        self._toasts.remove(toast)
        toast.close()
        toast.deleteLater()
        self._reflow()

    def _reflow(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        x = geo.right() - _WIDTH - _MARGIN
        y = geo.bottom() - _MARGIN
        for t in reversed(self._toasts):
            t.adjustSize()
            y -= t.height()
            t.move(x, y)
            y -= _GAP

    def destroy_all(self) -> None:
        for t in list(self._toasts):
            self.dismiss(t)

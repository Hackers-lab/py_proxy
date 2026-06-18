"""Small reusable Qt widgets: circular avatars, a presence dot and an
animated on/off toggle switch — all drawn with QPainter so they stay crisp at
any DPI and recolor with the palette."""

from functools import lru_cache

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractButton, QFrame, QLabel

from .theme import avatar_color, theme


@lru_cache(maxsize=256)
def avatar_pixmap(name: str, size: int = 36) -> QPixmap:
    """A circular initials avatar, colored deterministically by name."""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    pr.setPen(Qt.PenStyle.NoPen)
    pr.setBrush(QBrush(QColor(avatar_color(name))))
    pr.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    initial = next((c for c in (name or "?") if c.isalnum()), "?").upper()
    pr.setPen(QPen(QColor("#ffffff")))
    f = QFont("Segoe UI", int(size * 0.4), QFont.Weight.Bold)
    pr.setFont(f)
    pr.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, initial)
    pr.end()
    return pm


_DOT_COLORS = {"online": "#22c55e", "away": "#f59e0b",
               "offline": "#94a3b8", "invisible": "#94a3b8"}


def _dot_color(status) -> str:
    if isinstance(status, bool):
        status = "online" if status else "offline"
    return _DOT_COLORS.get(status, "#94a3b8")


def dot_pixmap(status, size: int = 10) -> QPixmap:
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    pr.setPen(Qt.PenStyle.NoPen)
    pr.setBrush(QBrush(QColor(_dot_color(status))))
    pr.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    pr.end()
    return pm


class Avatar(QLabel):
    def __init__(self, name: str = "?", size: int = 36, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_name(name)

    def set_name(self, name: str) -> None:
        self.setPixmap(avatar_pixmap(name or "?", self._size))


class Dot(QLabel):
    def __init__(self, status=False, size: int = 10, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_status(status)

    def set_status(self, status) -> None:
        """Accepts 'online'/'away'/'offline'/'invisible' or a bool."""
        self.setPixmap(dot_pixmap(status, self._size))

    # Backwards-compatible alias.
    def set_online(self, online: bool) -> None:
        self.set_status(online)


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("hdivider")
    f.setFixedHeight(1)
    return f


def vline() -> QFrame:
    f = QFrame()
    f.setObjectName("divider")
    f.setFixedWidth(1)
    return f


class ToggleSwitch(QAbstractButton):
    """A compact sliding on/off switch (checkable)."""

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._w, self._h = 42, 22
        self.toggled.connect(lambda _=False: self.update())   # repaint thumb on flip

    def sizeHint(self) -> QSize:
        return QSize(self._w, self._h)

    def paintEvent(self, _e) -> None:
        pr = QPainter(self)
        pr.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track = QColor("#22c55e") if on else QColor(theme.color("border"))
        pr.setPen(Qt.PenStyle.NoPen)
        pr.setBrush(track)
        r = self._h / 2
        pr.drawRoundedRect(QRectF(0, 0, self._w, self._h), r, r)
        pr.setBrush(QColor("#ffffff"))
        d = self._h - 6
        x = self._w - self._h + 3 if on else 3
        pr.drawEllipse(QRectF(x, 3, d, d))
        pr.end()

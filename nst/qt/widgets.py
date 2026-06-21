"""Small reusable Qt widgets: circular avatars, a presence dot and an
animated on/off toggle switch — all drawn with QPainter so they stay crisp at
any DPI and recolor with the palette."""

from functools import lru_cache

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
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


def bell_pixmap(enabled: bool, size: int = 18, color: str = "#94a3b8") -> QPixmap:
    """A smooth path-based bell. When disabled a red slash crosses it."""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)
    c = QColor(color)

    # ── bell body ─────────────────────────────────────────────────────────────
    path = QPainterPath()
    # Start at the hanger nub top-center
    cx = s * 0.50
    # Hanger: small rounded rect at top
    hx, hy, hw, hh = s*0.42, s*0.04, s*0.16, s*0.11
    path.addRoundedRect(QRectF(hx, hy, hw, hh), s*0.04, s*0.04)

    # Bell dome: arc that widens from top to bottom, like a D rotated
    body = QPainterPath()
    body.moveTo(cx, s * 0.14)
    # left curve — control points pull it outward
    body.cubicTo(cx - s*0.05, s*0.14,   # cp1
                 cx - s*0.38, s*0.22,   # cp2
                 cx - s*0.38, s*0.58)   # end
    # bottom-left flare
    body.cubicTo(cx - s*0.38, s*0.66,
                 cx - s*0.46, s*0.68,
                 cx - s*0.46, s*0.70)
    # bottom bar (flat-ish)
    body.lineTo(cx + s*0.46, s*0.70)
    # bottom-right flare
    body.cubicTo(cx + s*0.46, s*0.68,
                 cx + s*0.38, s*0.66,
                 cx + s*0.38, s*0.58)
    # right curve
    body.cubicTo(cx + s*0.38, s*0.22,
                 cx + s*0.05, s*0.14,
                 cx,          s*0.14)
    body.closeSubpath()

    pr.setPen(Qt.PenStyle.NoPen)
    pr.setBrush(QBrush(c))
    pr.drawPath(path)    # hanger
    pr.drawPath(body)    # dome + flare

    # Clapper — small filled circle hanging below the flare
    pr.drawEllipse(QRectF(cx - s*0.11, s*0.70, s*0.22, s*0.18))

    # ── slash for muted ───────────────────────────────────────────────────────
    if not enabled:
        pen = QPen(QColor("#ef4444"))
        pen.setWidthF(s * 0.14)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pr.setPen(pen)
        pr.drawLine(int(s * 0.80), int(s * 0.08),
                    int(s * 0.14), int(s * 0.92))

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


class AvatarWithStatus(QAbstractButton):
    """Circular avatar with a presence-indicator dot overlaid at the bottom-right.

    The dot ring is drawn in the theme's panel color so it appears to "cut"
    cleanly from the avatar regardless of the row background.
    """

    def __init__(self, name: str = "?", size: int = 34,
                 status: str = "offline", parent=None) -> None:
        super().__init__(parent)
        self._name = name or "?"
        self._av_size = size
        self._status = status
        self._dot_r = max(6, size // 5)
        total = size + self._dot_r // 2 + 1
        self.setFixedSize(total, total)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_name(self, name: str) -> None:
        self._name = name or "?"
        self.update()

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def paintEvent(self, _e) -> None:
        pr = QPainter(self)
        pr.setRenderHint(QPainter.RenderHint.Antialiasing)
        pr.drawPixmap(0, 0, avatar_pixmap(self._name, self._av_size))
        d = self._dot_r
        x = float(self._av_size - d // 2 - 1)
        y = float(self._av_size - d // 2 - 1)
        pr.setPen(Qt.PenStyle.NoPen)
        # ring — drawn in panel color to visually separate dot from avatar edge
        pr.setBrush(QBrush(QColor(theme.color("panel"))))
        pr.drawEllipse(QRectF(x - 2, y - 2, d + 4, d + 4))
        pr.setBrush(QBrush(QColor(_dot_color(self._status))))
        pr.drawEllipse(QRectF(x, y, d, d))
        pr.end()

    def sizeHint(self) -> QSize:
        return QSize(self.width(), self.height())


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

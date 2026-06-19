"""System-tray icons (proxy + chat) and the click-through speed overlay,
all native Qt — no pystray/PIL dependency."""

import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QLabel, QMenu, QSystemTrayIcon,
                             QVBoxLayout, QWidget)

from ..win_utils import get_resource_path, get_tray_notify_rect


def app_icon() -> QIcon:
    ico = get_resource_path("icon.ico")
    if os.path.exists(ico):
        ic = QIcon(ico)
        if not ic.isNull():
            return ic
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    pr.setPen(Qt.PenStyle.NoPen)
    pr.setBrush(QColor("#3b82f6"))
    pr.drawEllipse(4, 4, 56, 56)
    pr.setBrush(QColor("#15171c"))
    pr.drawEllipse(20, 20, 24, 24)
    pr.end()
    return QIcon(pm)


def chat_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    pr.setPen(Qt.PenStyle.NoPen)
    pr.setBrush(QColor("#10b981"))
    pr.drawRoundedRect(QRectF(4, 4, 56, 44), 12, 12)
    for cx in (22, 32, 42):
        pr.setBrush(QColor(255, 255, 255, 230))
        pr.drawEllipse(QRectF(cx - 3, 22, 6, 6))
    pr.end()
    return QIcon(pm)


def speed_icon(up: str, down: str) -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.Antialiasing)
    pr.setBrush(QColor(21, 23, 28, 235))
    pr.setPen(Qt.PenStyle.NoPen)
    pr.drawRoundedRect(QRectF(0, 0, 31, 31), 5, 5)
    f = QFont("Segoe UI", 7, QFont.Weight.Bold)
    pr.setFont(f)
    pr.setPen(QColor("#f59e0b"))
    pr.drawText(QRectF(2, 1, 30, 15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"▲{up}")
    pr.setPen(QColor("#22c55e"))
    pr.drawText(QRectF(2, 16, 30, 15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"▼{down}")
    pr.end()
    return QIcon(pm)


class TrayManager:
    def __init__(self, on_open_proxy, on_open_chat, on_quit) -> None:
        self._app_icon = app_icon()
        self.proxy = QSystemTrayIcon(self._app_icon)
        self.proxy.setToolTip("Net Split-Tunneler  Proxy")
        pm = QMenu()
        act_p = QAction("Open Proxy", pm)
        act_p.triggered.connect(lambda: on_open_proxy())
        pm.addAction(act_p)
        pm.addSeparator()
        act_pq = QAction("Quit", pm)
        act_pq.triggered.connect(lambda: on_quit())
        pm.addAction(act_pq)
        self.proxy.setContextMenu(pm)
        self.proxy.activated.connect(
            lambda r: on_open_proxy() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._proxy_menu = pm

        self.chat = QSystemTrayIcon(chat_icon())
        self.chat.setToolTip("LAN Chat")
        cm = QMenu()
        act_c = QAction("Open Chat", cm)
        act_c.triggered.connect(lambda: on_open_chat())
        cm.addAction(act_c)
        cm.addSeparator()
        act_cq = QAction("Quit", cm)
        act_cq.triggered.connect(lambda: on_quit())
        cm.addAction(act_cq)
        self.chat.setContextMenu(cm)
        self.chat.activated.connect(
            lambda r: on_open_chat() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._chat_menu = cm

    def show(self) -> None:
        self.proxy.show()
        self.chat.show()

    def hide(self) -> None:
        self.proxy.hide()
        self.chat.hide()

    def set_speed(self, up: str, down: str) -> None:
        self.proxy.setIcon(speed_icon(up, down))
        self.proxy.setToolTip(f"Net Split-Tunneler  Proxy\nUp: {up}\nDown: {down}")

    def set_idle(self) -> None:
        self.proxy.setIcon(self._app_icon)
        self.proxy.setToolTip("Net Split-Tunneler  Proxy")

    def notify(self, title: str, body: str) -> None:
        try:
            self.proxy.showMessage(title, body, self._app_icon, 4000)
        except Exception:
            pass


class SpeedOverlay(QWidget):
    """A small always-on-top, click-through readout pinned beside the clock.

    Windows owns the taskbar clock/date area, so a third-party app can't embed
    a widget there directly. This floats a legible rounded pill over that spot
    instead, staying above the taskbar.
    """

    _W, _H = 108, 38

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # A semi-opaque dark pill so the text is readable over any taskbar colour.
        pill = QWidget()
        pill.setObjectName("speedPill")
        pill.setStyleSheet("QWidget#speedPill { background: rgba(18,20,26,0.90);"
                           " border-radius: 7px; }")
        lay = QVBoxLayout(pill)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(0)
        self._up = QLabel("")
        self._up.setStyleSheet("color:#f59e0b; font:700 10px 'Segoe UI'; background:transparent;")
        self._down = QLabel("")
        self._down.setStyleSheet("color:#22c55e; font:700 10px 'Segoe UI'; background:transparent;")
        lay.addWidget(self._up)
        lay.addWidget(self._down)
        outer.addWidget(pill)
        self.setFixedSize(self._W, self._H)

    def show_speed(self, up: str, down: str) -> None:
        self._up.setText(f"▲ {up}")
        self._down.setText(f"▼ {down}")
        rect = get_tray_notify_rect()
        if not rect:
            self.hide()
            return
        left, top, right, bottom = rect
        ow, oh = self._W, self._H
        if (bottom - top) < (right - left):   # horizontal taskbar
            x, y = left - ow - 8, top + (bottom - top - oh) // 2
        else:
            x, y = left + (right - left - ow) // 2, top - oh - 8
        self.move(int(x), int(y))
        if not self.isVisible():
            self.show()
        self.raise_()

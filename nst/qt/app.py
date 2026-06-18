"""QApplication bootstrap: wires the shared service layer to the Qt windows,
toasts and tray, then runs the event loop."""

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .. import config
from ..chat import ChatService
from ..constants import MOBILE_HTTP_PORT
from ..mobile import MobileServer
from ..win_utils import get_resource_path, set_app_user_model_id
from .chat_window import ChatWindow
from .main_window import MainWindow
from .signals import ChatSignals
from .theme import theme
from .toast import ToastManager
from .tray import SpeedOverlay, TrayManager


def run() -> None:
    set_app_user_model_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Net Split-Tunneler")
    app.setQuitOnLastWindowClosed(False)   # closing windows hides to tray
    app.setStyleSheet(theme.qss())
    theme.changed.connect(lambda: app.setStyleSheet(theme.qss()))

    ico = get_resource_path("icon.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))

    # ── services + signal bridge ──────────────────────────────────────────────
    sig = ChatSignals()
    chat = ChatService(
        config.load_display_name(),
        on_roster_change=lambda peers: sig.roster_changed.emit(peers),
        on_message=lambda ip, name, text, ts, reply, mid:
            sig.message.emit(ip, name, text, ts, reply, mid),
        on_file_offer=lambda ip, name, msg: sig.file_offer.emit(ip, name, msg),
        on_file_accept=lambda ip, name, msg: sig.file_accept.emit(ip, name, msg),
        on_file_reject=lambda ip, name, msg: sig.file_reject.emit(ip, name, msg),
        on_chat_request=lambda ip, name, msg: sig.chat_request.emit(ip, name, msg),
        on_group_message=lambda group, ip, name, text, ts, reply, mid:
            sig.group_message.emit(group, ip, name, text, ts, reply, mid),
        on_receipt=lambda ip, mid, state: sig.receipt.emit(ip, mid, state),
        on_delete=lambda ip, mid: sig.deleted.emit(ip, mid),
        on_typing=lambda ip, name, gid, typing: sig.typing.emit(ip, name, gid, typing),
        on_reaction=lambda ip, mid, emoji: sig.reaction.emit(ip, mid, emoji),
    )
    chat.ip_chat_enabled = config.load_ip_chat_enabled()
    chat.presence_online = config.load_presence_online()

    mobile = MobileServer(
        port=MOBILE_HTTP_PORT,
        on_join=lambda s: sig.mobile_join.emit(s),
        on_leave=lambda s: sig.mobile_leave.emit(s),
        on_message=lambda s, t: sig.mobile_message.emit(s, t),
        on_file=lambda s, fn, p, sz: sig.mobile_file.emit(s, fn, p, sz),
        on_file_downloaded=lambda sid, tid: sig.mobile_download.emit(sid, tid),
    )
    if config.load_mobile_enabled():
        mobile.start()

    toasts = ToastManager()
    _log_holder = {"main": None}
    chat_window = ChatWindow(chat, toasts,
                             mobile_server=mobile,
                             log_fn=lambda m: _log_holder["main"] and _log_holder["main"].log(m))

    sig.roster_changed.connect(chat_window.update_roster)
    sig.message.connect(chat_window.receive_message)
    sig.file_offer.connect(chat_window.on_file_offer_received)
    sig.file_accept.connect(chat_window.on_file_accepted)
    sig.file_reject.connect(chat_window.on_file_rejected)
    sig.chat_request.connect(chat_window.on_chat_request_received)
    sig.group_message.connect(chat_window.on_group_message)
    sig.receipt.connect(chat_window.on_receipt)
    sig.deleted.connect(chat_window.on_remote_delete)
    sig.typing.connect(chat_window.on_typing)
    sig.reaction.connect(chat_window.on_reaction)
    sig.mobile_join.connect(chat_window.on_mobile_join)
    sig.mobile_leave.connect(chat_window.on_mobile_leave)
    sig.mobile_message.connect(chat_window.on_mobile_message)
    sig.mobile_file.connect(chat_window.on_mobile_file)
    sig.mobile_download.connect(chat_window.on_mobile_download)

    chat_window.activity.connect(chat_window.open)
    toasts.clicked.connect(chat_window.open)

    def run_demo():
        chat_window.open()
        chat_window._start_demo()

    def quit_app():
        try:
            chat.stop()
            mobile.stop()
            chat_window.shutdown()
            main.shutdown()
            toasts.destroy_all()
            tray.hide()
            overlay.hide()
        except Exception:
            pass
        app.quit()

    main = MainWindow(open_chat=chat_window.open, run_demo=run_demo, on_quit=quit_app)
    _log_holder["main"] = main

    def open_proxy():
        main.showNormal()
        main.raise_()
        main.activateWindow()

    tray = TrayManager(on_open_proxy=open_proxy,
                       on_open_chat=chat_window.open,
                       on_quit=quit_app)
    overlay = SpeedOverlay()
    main.set_tray(tray)
    main.set_overlay(overlay)
    tray.show()

    chat.start()
    main.show()

    sys.exit(app.exec())

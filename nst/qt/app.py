"""QApplication bootstrap: wires the shared service layer to the Qt windows,
toasts and tray, then runs the event loop."""

import os
import sys

from PyQt6.QtCore import Qt, QTimer, qInstallMessageHandler
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .. import antivirus, config
from ..chat import ChatService
from ..remotescreen import RemoteScreenService
from .. import changelog
from ..updater import UpdateManager, apply_staged_on_launch
from ..win_utils import get_resource_path, set_app_user_model_id
from .chat_window import ChatWindow
from .main_window import MainWindow
from .remote_window import RemoteHostController
from .signals import ChatSignals, ScreenSignals
from .theme import theme
from .toast import ToastManager
from .tray import SpeedOverlay, TrayManager, chat_icon


# Known-benign Qt noise we don't want cluttering the console: the Windows
# platform plugin failing to read a monitor's EDID interface, and the stylesheet
# engine's pointSize complaint when a QSS rule mixes font-weight with a px size.
_QT_NOISE = (
    "Unable to open monitor interface",
    "QFont::setPointSize: Point size <= 0",
)


def _qt_message_filter(mode, context, message) -> None:
    if any(s in message for s in _QT_NOISE):
        return
    sys.stderr.write(message + "\n")


def run() -> None:
    qInstallMessageHandler(_qt_message_filter)

    # Apply a previously staged update before anything else (exits if it runs).
    apply_staged_on_launch()

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
        on_message=lambda ip, name, text, ts, reply, mid, image=None:
            sig.message.emit(ip, name, text, ts, reply, mid, image),
        on_file_offer=lambda ip, name, msg: sig.file_offer.emit(ip, name, msg),
        on_file_accept=lambda ip, name, msg: sig.file_accept.emit(ip, name, msg),
        on_file_reject=lambda ip, name, msg: sig.file_reject.emit(ip, name, msg),
        on_chat_request=lambda ip, name, msg: sig.chat_request.emit(ip, name, msg),
        on_group_message=lambda group, ip, name, text, ts, reply, mid:
            sig.group_message.emit(group, ip, name, text, ts, reply, mid),
        on_channel_message=lambda channel, ip, name, text, ts, reply, mid:
            sig.channel_message.emit(channel, ip, name, text, ts, reply, mid),
        on_receipt=lambda ip, mid, state: sig.receipt.emit(ip, mid, state),
        on_delete=lambda ip, mid: sig.deleted.emit(ip, mid),
        on_edit=lambda ip, mid, text: sig.edited.emit(ip, mid, text),
        on_typing=lambda ip, name, gid, typing: sig.typing.emit(ip, name, gid, typing),
        on_reaction=lambda ip, mid, emoji: sig.reaction.emit(ip, mid, emoji),
        on_queue_flush=lambda ip, mids: sig.queue_flush.emit(ip, mids),
        on_group_kick=lambda ip, gid: sig.group_kick.emit(ip, gid),
    )
    chat.ip_chat_enabled = config.load_ip_chat_enabled()
    chat.my_status = config.load_my_status()

    # Remote-screen service (host + viewer). Host callbacks are marshalled to the
    # GUI thread via ScreenSignals and handled by RemoteHostController.
    screen_sig = ScreenSignals()
    remote = RemoteScreenService(
        chat,
        on_request=lambda name, ip, respond: screen_sig.request.emit(name, ip, respond),
        on_share_started=lambda s: screen_sig.share_started.emit(s),
        on_share_stopped=lambda s: screen_sig.share_stopped.emit(s),
        on_clipboard_from_viewer=lambda text: screen_sig.clipboard_in.emit(text),
        on_server_error=lambda msg: screen_sig.server_error.emit(msg),
    )

    toasts = ToastManager()
    screen_sig.server_error.connect(
        lambda msg: toasts.notify("Remote screen", msg, "remote-error"))
    _log_holder = {"main": None}
    chat_window = ChatWindow(chat, toasts,
                             log_fn=lambda m: _log_holder["main"] and _log_holder["main"].log(m))
    # Give the chat window the green message-bubble icon (matches the tray chat
    # icon) so it's distinct from the proxy/splitter window's app icon.
    chat_window.setWindowIcon(chat_icon())
    chat_window.set_remote_service(remote)
    remote_host = RemoteHostController(remote, screen_sig)

    sig.roster_changed.connect(chat_window.update_roster)
    sig.message.connect(chat_window.receive_message)
    sig.file_offer.connect(chat_window.on_file_offer_received)
    sig.file_accept.connect(chat_window.on_file_accepted)
    sig.file_reject.connect(chat_window.on_file_rejected)
    sig.chat_request.connect(chat_window.on_chat_request_received)
    sig.group_message.connect(chat_window.on_group_message)
    sig.channel_message.connect(chat_window.on_channel_message)
    sig.receipt.connect(chat_window.on_receipt)
    sig.deleted.connect(chat_window.on_remote_delete)
    sig.edited.connect(chat_window.on_remote_edit)
    sig.typing.connect(chat_window.on_typing)
    sig.reaction.connect(chat_window.on_reaction)
    sig.queue_flush.connect(chat_window.on_queue_flush)
    sig.group_kick.connect(chat_window.on_group_kicked)

    chat_window.activity.connect(chat_window.open)
    toasts.clicked.connect(chat_window.open)

    def run_demo():
        chat_window.open()
        chat_window._start_demo()

    def quit_app():
        try:
            remote.stop()
            chat.stop()
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

    # ── silent self-update ────────────────────────────────────────────────────
    # Apply updates only while the chat window is closed, so an active
    # conversation is never interrupted. Found-while-chatting updates are staged
    # and applied the moment the chat closes (see ChatWindow.closeEvent).
    updates = UpdateManager(
        is_chat_open=lambda: chat_window.isVisible() and not chat_window.isMinimized(),
        quit_app=quit_app,
    )
    updates.status.connect(lambda m: toasts.notify("Software update", m, "update"))
    toasts.clicked.connect(lambda key: key == "update" and chat_window._ensure_updates_bot())
    # Mirror update activity into the main window's event log: the detailed
    # progress (update found, installer size, download %) plus the status lines.
    updates.log.connect(main.log)
    updates.status.connect(main.log)
    chat_window.set_on_closed(updates.apply_staged_if_any)
    main.set_update_manager(updates)

    # Chat is the primary window; its ⋯ header menu reaches the Network Tools
    # window (the proxy/splitter), the updater and quit.
    chat_window.set_app_actions(
        open_network_tools=open_proxy,
        check_updates=lambda: updates.check(manual=True),
        quit_app=quit_app,
    )

    chat.start()
    antivirus.prime()   # warm the active-AV name cache off the GUI thread
    if config.load_remote_enabled():
        remote.start()

    # ── launch mode ───────────────────────────────────────────────────────────
    # --autostart (logon Run key) and --updated=<ver> (relaunch after a silent
    # self-update) start to the tray without popping the main window. A manual
    # launch (Start Menu / double-click) has no flag and shows the window.
    updated_ver = next((a.split("=", 1)[1] for a in sys.argv
                        if a.startswith("--updated=")), None)
    silent_start = updated_ver is not None or "--autostart" in sys.argv
    if not silent_start:
        chat_window.open()
    if updated_ver:
        toasts.notify("Net Split-Tunneler",
                      f"Updated to v{updated_ver} — tap to see what's new.", "update")
        notes = changelog.get(updated_ver)
        if notes:
            QTimer.singleShot(800, lambda: chat_window.post_update_notes(updated_ver, notes))

    updates.start()

    sys.exit(app.exec())

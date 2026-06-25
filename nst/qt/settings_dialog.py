"""The dedicated, categorised Settings window (update.md "Settings Module").

A lightweight left-nav + stacked-pages dialog covering General, Notifications,
Storage, Network, Privacy & Users, File Transfer and About. Edits are *staged*
in the controls and only written to :mod:`nst.config` when **Save** is pressed;
**Cancel** discards them. Explicit actions (Clear history, Unblock, Test sound)
run immediately, since they aren't settings.
"""

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QMessageBox, QPushButton, QScrollArea,
                             QSlider, QSpinBox, QStackedWidget, QVBoxLayout,
                             QWidget)

from .. import __version__, antivirus, chatlock, config
from ..constants import CHAT_PRESENCE_PORT, CHAT_TCP_PORT, FILE_TCP_PORT
from ..netinfo import get_all_local_ips, get_local_ip, list_local_ipv4
from . import sound
from .theme import theme
from .widgets import hline

_RETENTION_LABELS = [("7 Days", 7), ("30 Days", 30), ("90 Days", 90),
                     ("180 Days", 180), ("Forever", 0)]


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


class SettingsDialog(QDialog):
    def __init__(self, chat_window, parent=None) -> None:
        super().__init__(parent)
        self.cw = chat_window
        self.chat = chat_window.chat
        self.setWindowTitle("Settings — LAN Chat")
        self.resize(720, 580)
        self.setMinimumSize(620, 480)
        # staged download folder (committed on Save; "" = unchanged)
        self._pending_dir = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # left nav
        nav = QWidget()
        nav.setObjectName("card")
        nav.setFixedWidth(190)
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(10, 14, 10, 14)
        nv.setSpacing(4)
        title = QLabel("SETTINGS")
        title.setObjectName("section")
        nv.addWidget(title)
        self._nav = QListWidget()
        self._nav.setFrameShape(QFrame.Shape.NoFrame)
        self._nav.addItems(["General", "Notifications", "Storage", "Network",
                            "Privacy & Users", "File Transfer", "Remote Screen",
                            "About"])
        self._nav.currentRowChanged.connect(self._on_nav)
        nv.addWidget(self._nav, 1)
        root.addWidget(nav)

        self._pages = QStackedWidget()
        for builder in (self._page_general, self._page_notifications,
                        self._page_storage, self._page_network,
                        self._page_privacy, self._page_filetransfer,
                        self._page_remote, self._page_about):
            self._pages.addWidget(self._scroll(builder()))
        root.addWidget(self._pages, 1)
        outer.addWidget(body, 1)
        outer.addWidget(hline())

        # bottom Save / Cancel bar
        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 16, 12)
        bar.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        savebtn = QPushButton("Save")
        savebtn.setProperty("variant", "accent")
        savebtn.setDefault(True)
        savebtn.clicked.connect(self._save)
        bar.addWidget(cancel)
        bar.addWidget(savebtn)
        outer.addLayout(bar)

        # keep the duplicated "max file size" spin-boxes in sync as the user types
        self._maxmb.valueChanged.connect(
            lambda v: self._ft_maxmb.value() != v and self._ft_maxmb.setValue(v))
        self._ft_maxmb.valueChanged.connect(
            lambda v: self._maxmb.value() != v and self._maxmb.setValue(v))

        self._nav.setCurrentRow(0)

    # ── layout helpers ─────────────────────────────────────────────────────────
    def _scroll(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(inner)
        return sa

    def _page(self, heading: str) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(12)
        h = QLabel(heading)
        h.setObjectName("title")
        v.addWidget(h)
        v.addWidget(hline())
        return w, v

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size:11px; color:%s;" % theme.color("text_sec"))
        return lbl

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    def _on_nav(self, row: int) -> None:
        if row < 0:
            return
        self._pages.setCurrentIndex(row)
        if row == 2:
            self._refresh_storage_stats()
        elif row == 3:
            self._refresh_network()
        elif row == 4:
            self._refresh_blocked()

    # ── Save / Cancel ──────────────────────────────────────────────────────────
    def _save(self) -> None:
        """Commit every staged control value to config, then close."""
        # General
        name = self._name_edit.text().strip()[:32]
        if name and name != self.chat.my_name:
            self.cw.apply_display_name(name)
        want_invisible = self._cb_invisible.isChecked()
        if want_invisible != (self.chat.my_status == "invisible"):
            self.cw.set_status("invisible" if want_invisible else "online")
        if self._cb_autostart.isChecked() != config.is_autostart_enabled():
            ok, msg = config.set_autostart(self._cb_autostart.isChecked())
            if not ok:
                QMessageBox.warning(self, "Autostart", msg)
        config.save_minimize_to_tray(self._cb_tray.isChecked())
        config.save_restore_session(self._cb_restore.isChecked())

        # Notifications
        config.save_notifications_enabled(self._cb_notif_all.isChecked())
        config.save_mute_all(self._cb_mute.isChecked())
        config.save_do_not_disturb(self._cb_dnd.isChecked())
        config.save_sound_volume(self._vol.value())
        prefs = config.load_notify_prefs()
        for (scope, ch), cb in self._notif_boxes.items():
            prefs.setdefault(scope, {})[ch] = cb.isChecked()
        config.save_notify_prefs(prefs)

        # Storage + File transfer
        config.save_retention_days(self._retention.currentData())
        config.save_max_file_mb(self._maxmb.value())
        config.save_file_expiry_min(self._ft_expiry.value())
        config.save_av_mode(self._av_mode.currentData())
        if self._pending_dir:
            config.save_download_dir(self._pending_dir)

        # Remote screen
        remote_on = self._cb_remote.isChecked()
        was_on = config.load_remote_enabled()
        config.save_remote_enabled(remote_on)
        config.save_remote_unattended(self._cb_unattended.isChecked())
        config.save_remote_secret(self._remote_secret.text())
        config.save_remote_quality(self._remote_quality.value())
        config.save_remote_max_edge(self._remote_res.currentData())
        config.save_remote_lossless(self._cb_lossless.isChecked())
        config.save_remote_fps(self._remote_fps.value())
        config.save_remote_timeout(self._remote_timeout.value())
        if remote_on != was_on:
            self.cw.apply_remote_enabled(remote_on)

        self.cw.on_settings_changed()
        self.accept()

    # ── General ─────────────────────────────────────────────────────────────────
    def _page_general(self) -> QWidget:
        w, v = self._page("General")

        v.addWidget(QLabel("Display name"))
        self._name_edit = QLineEdit(self.chat.my_name)
        self._name_edit.setMaxLength(32)
        v.addWidget(self._name_edit)
        v.addWidget(self._hint(
            f"Shown to peers as “{self.chat.my_name} | {self.chat.my_device} | {self.chat.my_ip}”."))

        self._cb_invisible = QCheckBox("Invisible mode (appear offline but still receive messages)")
        self._cb_invisible.setChecked(self.chat.my_status == "invisible")
        v.addWidget(self._cb_invisible)

        self._cb_autostart = QCheckBox("Start application with Windows")
        self._cb_autostart.setChecked(config.is_autostart_enabled())
        v.addWidget(self._cb_autostart)

        self._cb_tray = QCheckBox("Minimise to system tray when closed")
        self._cb_tray.setChecked(config.load_minimize_to_tray())
        v.addWidget(self._cb_tray)

        self._cb_restore = QCheckBox("Restore previous conversation on startup")
        self._cb_restore.setChecked(config.load_restore_session())
        v.addWidget(self._cb_restore)

        v.addStretch(1)
        return w

    # ── Notifications ─────────────────────────────────────────────────────────
    def _page_notifications(self) -> QWidget:
        w, v = self._page("Notifications")

        v.addWidget(self._section_label("GLOBAL"))
        self._cb_notif_all = QCheckBox("Enable all notifications")
        self._cb_notif_all.setChecked(config.load_notifications_enabled())
        v.addWidget(self._cb_notif_all)

        self._cb_mute = QCheckBox("Mute all notification sounds")
        self._cb_mute.setChecked(config.load_mute_all())
        v.addWidget(self._cb_mute)

        self._cb_dnd = QCheckBox("Do Not Disturb (suppress popups, flashing & sound)")
        self._cb_dnd.setChecked(config.load_do_not_disturb())
        v.addWidget(self._cb_dnd)

        volrow = QHBoxLayout()
        volrow.addWidget(QLabel("Sound volume"))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setRange(0, 100)
        self._vol.setValue(config.load_sound_volume())
        self._vol_lbl = QLabel(f"{self._vol.value()}%")
        self._vol.valueChanged.connect(lambda val: self._vol_lbl.setText(f"{val}%"))
        test = QPushButton("Test")
        test.clicked.connect(lambda: sound.play_sound(self._vol.value()))
        volrow.addWidget(self._vol, 1)
        volrow.addWidget(self._vol_lbl)
        volrow.addWidget(test)
        v.addLayout(volrow)

        v.addWidget(hline())
        v.addWidget(self._section_label("PER CONVERSATION TYPE"))
        v.addWidget(self._hint(
            "Set each alert type for private chats, groups and broadcast channels. "
            "“Show window” on = bring the chat window to the front on a new "
            "message; off = show a bottom-right popup instead."))

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        channels = [("Sound", "sound"), ("Show window", "popup"),
                    ("Taskbar flash", "taskbar")]
        for col, (label, _key) in enumerate(channels):
            h = QLabel(label)
            h.setStyleSheet("font-size:11px; font-weight:700;")
            grid.addWidget(h, 0, col + 1)
        prefs = config.load_notify_prefs()
        self._notif_boxes: dict[tuple, QCheckBox] = {}
        scopes = [("Private", "private"), ("Group", "group"), ("Broadcast", "broadcast")]
        for r, (slabel, scope) in enumerate(scopes):
            rl = QLabel(slabel)
            rl.setStyleSheet("font-weight:700;")
            grid.addWidget(rl, r + 1, 0)
            for c, (_label, ch) in enumerate(channels):
                cb = QCheckBox()
                cb.setChecked(bool(prefs.get(scope, {}).get(ch, True)))
                self._notif_boxes[(scope, ch)] = cb
                grid.addWidget(cb, r + 1, c + 1, alignment=Qt.AlignmentFlag.AlignCenter)
        gw = QWidget()
        gw.setLayout(grid)
        v.addWidget(gw)
        v.addStretch(1)
        return w

    # ── Storage ─────────────────────────────────────────────────────────────────
    def _page_storage(self) -> QWidget:
        w, v = self._page("Storage & Retention")

        v.addWidget(QLabel("Keep message history for"))
        self._retention = QComboBox()
        cur = config.load_retention_days()
        for i, (label, days) in enumerate(_RETENTION_LABELS):
            self._retention.addItem(label, days)
            if days == cur:
                self._retention.setCurrentIndex(i)
        v.addWidget(self._retention)
        v.addWidget(self._hint(
            "Older messages are pruned from local history on the next launch. "
            "This never affects other users' copies."))

        v.addWidget(QLabel("Download / save folder"))
        drow = QHBoxLayout()
        self._dl_edit = QLineEdit(config.load_download_dir())
        self._dl_edit.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_download)
        drow.addWidget(self._dl_edit, 1)
        drow.addWidget(browse)
        v.addLayout(drow)

        szrow = QHBoxLayout()
        szrow.addWidget(QLabel("Maximum file transfer size (MB, 0 = unlimited)"))
        self._maxmb = QSpinBox()
        self._maxmb.setRange(0, 1024 * 50)
        self._maxmb.setValue(config.load_max_file_mb())
        szrow.addStretch(1)
        szrow.addWidget(self._maxmb)
        v.addLayout(szrow)

        v.addWidget(hline())
        v.addWidget(self._section_label("STORAGE USAGE"))
        self._stats_lbl = QLabel("…")
        self._stats_lbl.setStyleSheet("font-family:'Consolas',monospace;")
        v.addWidget(self._stats_lbl)

        brow = QHBoxLayout()
        clear = QPushButton("Clear all local chat history")
        clear.setProperty("variant", "danger")
        clear.clicked.connect(self._clear_history)
        brow.addWidget(clear)
        brow.addStretch(1)
        v.addLayout(brow)
        v.addStretch(1)
        return w

    def _browse_download(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose download folder", config.load_download_dir())
        if folder:
            self._pending_dir = folder           # committed on Save
            self._dl_edit.setText(folder)
            if hasattr(self, "_ft_dl"):
                self._ft_dl.setText(folder)

    def _refresh_storage_stats(self) -> None:
        hist = config.get_peer_chat_dir()
        dl = config.load_download_dir()
        convos = len([f for f in os.listdir(hist) if f.endswith(".json")]) \
            if os.path.isdir(hist) else 0
        self._stats_lbl.setText(
            f"Conversations on disk : {convos}\n"
            f"Chat history size     : {_fmt_bytes(_dir_size(hist))}\n"
            f"Downloads folder size : {_fmt_bytes(_dir_size(dl))}")

    def _clear_history(self) -> None:
        if QMessageBox.question(
                self, "Clear history",
                "Delete ALL local chat history on this PC?\n"
                "Other users keep their own copies.") \
                == QMessageBox.StandardButton.Yes:
            n = self.cw.clear_all_history()
            self._refresh_storage_stats()
            QMessageBox.information(self, "Cleared",
                                    f"Cleared history for {n} conversation(s).")

    # ── Network ─────────────────────────────────────────────────────────────────
    def _page_network(self) -> QWidget:
        w, v = self._page("Network")
        self._net_lbl = QLabel("…")
        self._net_lbl.setStyleSheet("font-family:'Consolas',monospace; font-size:12px;")
        self._net_lbl.setWordWrap(True)
        v.addWidget(self._net_lbl)
        refresh = QPushButton("⟳ Refresh network info")
        refresh.clicked.connect(self._refresh_network)
        v.addWidget(refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        v.addStretch(1)
        return w

    def _refresh_network(self) -> None:
        lines = ["Detected interfaces:"]
        for iface, ip, mask in list_local_ipv4():
            lines.append(f"  • {iface}: {ip}  (mask {mask})")
        if len(lines) == 1:
            lines.append("  (none detected)")
        primary = get_local_ip() or "—"
        online = sum(1 for p in self.chat.peers() if self.chat.is_peer_online(p.ip))
        lines += [
            "",
            f"Primary LAN IP   : {primary}",
            f"All local IPs    : {', '.join(get_all_local_ips()) or '—'}",
            f"Presence port    : {CHAT_PRESENCE_PORT} (UDP)",
            f"Messaging port   : {CHAT_TCP_PORT} (TCP)",
            f"File transfer    : {FILE_TCP_PORT} (TCP)",
            f"Peers online now : {online}",
            f"Queued messages  : {self.chat.pending_count()}",
        ]
        self._net_lbl.setText("\n".join(lines))

    # ── Privacy & Users ──────────────────────────────────────────────────────
    def _page_privacy(self) -> QWidget:
        w, v = self._page("Privacy & User Management")

        v.addWidget(self._section_label("CHAT LOCK"))
        v.addWidget(self._hint(
            "Protect your chat history with a password. Locked conversations are "
            "encrypted on disk, so they can't be read without the password — not "
            "even by opening the files directly. A lost password can only be "
            "reset via your security questions, which deletes the locked chats."))
        self._lock_status = QLabel("")
        v.addWidget(self._lock_status)
        lrow = QHBoxLayout()
        self._lock_set_btn = QPushButton("Set password…")
        self._lock_set_btn.clicked.connect(self._lock_setup)
        self._lock_remove_btn = QPushButton("Remove password")
        self._lock_remove_btn.setProperty("variant", "danger")
        self._lock_remove_btn.clicked.connect(self._lock_remove)
        lrow.addWidget(self._lock_set_btn)
        lrow.addWidget(self._lock_remove_btn)
        lrow.addStretch(1)
        v.addLayout(lrow)
        self._refresh_lock_status()
        v.addWidget(hline())

        v.addWidget(self._section_label("BLOCKED USERS"))
        v.addWidget(self._hint(
            "Blocked users cannot send you messages or files, and are excluded "
            "from new groups you create."))
        self._blocked_list = QVBoxLayout()
        self._blocked_list.setSpacing(6)
        holder = QWidget()
        holder.setLayout(self._blocked_list)
        v.addWidget(holder)

        v.addWidget(hline())
        self._pending_lbl = QLabel("")
        self._pending_lbl.setObjectName("muted")
        v.addWidget(self._pending_lbl)
        v.addStretch(1)
        return w

    def _refresh_blocked(self) -> None:
        while self._blocked_list.count():
            item = self._blocked_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        names = {u["ip"]: u.get("name", u["ip"]) for u in config.load_blocked_users()}
        ips = sorted(set(self.chat.blocked_ips()) | set(names))
        if not ips:
            self._blocked_list.addWidget(self._hint("No blocked users."))
        for ip in ips:
            row = QHBoxLayout()
            label = QLabel(f"{names.get(ip, ip)}  ·  {ip}")
            unblock = QPushButton("Unblock")
            unblock.clicked.connect(lambda _=False, x=ip: self._unblock(x))
            row.addWidget(label, 1)
            row.addWidget(unblock)
            rw = QWidget()
            rw.setLayout(row)
            self._blocked_list.addWidget(rw)
        pend = self.chat.pending_request_ips()
        self._pending_lbl.setText(
            f"Pending connection requests: {len(pend)}"
            + (("  (" + ", ".join(pend) + ")") if pend else ""))

    def _unblock(self, ip: str) -> None:
        self.cw.unblock_user(ip)
        self._refresh_blocked()

    def _refresh_lock_status(self) -> None:
        if chatlock.is_set():
            scope = ("the whole chat" if chatlock.scope() == "global"
                     else f"{len(chatlock.locked_keys())} selected chat(s)")
            self._lock_status.setText(f"🔒 Lock is ON — protecting {scope}.")
            self._lock_set_btn.setText("Change password…")
            self._lock_remove_btn.setEnabled(True)
        else:
            self._lock_status.setText("🔓 No password set.")
            self._lock_set_btn.setText("Set password…")
            self._lock_remove_btn.setEnabled(False)

    def _lock_setup(self) -> None:
        self.cw.setup_lock()
        self._refresh_lock_status()

    def _lock_remove(self) -> None:
        if QMessageBox.question(
                self, "Remove password",
                "Remove the chat-lock password? Locked chats will be stored "
                "unencrypted again.") == QMessageBox.StandardButton.Yes:
            self.cw.remove_lock()
            self._refresh_lock_status()

    # ── File Transfer ─────────────────────────────────────────────────────────
    def _page_filetransfer(self) -> QWidget:
        w, v = self._page("File Transfer")

        v.addWidget(QLabel("Default download folder"))
        drow = QHBoxLayout()
        self._ft_dl = QLineEdit(config.load_download_dir())
        self._ft_dl.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_download)
        drow.addWidget(self._ft_dl, 1)
        drow.addWidget(browse)
        v.addLayout(drow)

        szrow = QHBoxLayout()
        szrow.addWidget(QLabel("Maximum file size (MB, 0 = unlimited)"))
        self._ft_maxmb = QSpinBox()
        self._ft_maxmb.setRange(0, 1024 * 50)
        self._ft_maxmb.setValue(config.load_max_file_mb())
        szrow.addStretch(1)
        szrow.addWidget(self._ft_maxmb)
        v.addLayout(szrow)

        exrow = QHBoxLayout()
        exrow.addWidget(QLabel("Unanswered offer expires after (minutes)"))
        self._ft_expiry = QSpinBox()
        self._ft_expiry.setRange(1, 1440)
        self._ft_expiry.setValue(config.load_file_expiry_min())
        exrow.addStretch(1)
        exrow.addWidget(self._ft_expiry)
        v.addLayout(exrow)

        v.addWidget(self._hint(
            "Transfers are peer-to-peer: the sender hosts the file while online "
            "and there is no central storage. Offline file transfer isn't supported."))

        v.addWidget(hline())
        v.addWidget(self._section_label("VIRUS SCANNING"))
        avrow = QHBoxLayout()
        avrow.addWidget(QLabel("Scan shared files"))
        self._av_mode = QComboBox()
        for label, val in [("Block flagged files", "block"),
                           ("Warn, allow override", "warn"),
                           ("Off", "off")]:
            self._av_mode.addItem(label, val)
        idx = self._av_mode.findData(config.load_av_mode())
        self._av_mode.setCurrentIndex(idx if idx >= 0 else 0)
        avrow.addStretch(1)
        avrow.addWidget(self._av_mode)
        v.addLayout(avrow)
        v.addWidget(self._hint(
            "Files are scanned with " + antivirus.engine_label() + " both before "
            "sending and after receiving — using whatever antivirus is active on "
            "this PC. “Block” refuses flagged files; “Warn” lets you decide."))
        v.addStretch(1)
        return w

    # ── Remote Screen ──────────────────────────────────────────────────────────
    def _page_remote(self) -> QWidget:
        w, v = self._page("Remote Screen")

        self._cb_remote = QCheckBox("Allow others to view and control this PC")
        self._cb_remote.setChecked(config.load_remote_enabled())
        v.addWidget(self._cb_remote)
        v.addWidget(self._hint(
            "When on, a peer can request your screen from the 🖥 button in chat. "
            "You'll be asked to approve each connection unless unattended access "
            "is enabled below."))

        v.addWidget(hline())
        v.addWidget(self._section_label("UNATTENDED ACCESS"))
        self._cb_unattended = QCheckBox("Connect without asking if the secret matches")
        self._cb_unattended.setChecked(config.load_remote_unattended())
        v.addWidget(self._cb_unattended)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Secret"))
        self._remote_secret = QLineEdit(config.load_remote_secret())
        self._remote_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._remote_secret.setPlaceholderText("Required for unattended access")
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self._remote_secret.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        srow.addWidget(self._remote_secret, 1)
        srow.addWidget(show)
        v.addLayout(srow)
        v.addWidget(self._hint(
            "⚠ Anyone with this secret can control this PC without your approval. "
            "Use a long, unique secret and share it only with trusted devices."))

        v.addWidget(hline())
        v.addWidget(self._section_label("PERFORMANCE"))

        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("Resolution"))
        self._remote_res = QComboBox()
        for label, px in [("Match host — sharpest", 0), ("Up to 1920px", 1920),
                          ("Up to 1440px", 1440), ("Up to 1080px — fastest", 1080)]:
            self._remote_res.addItem(label, px)
        ridx = self._remote_res.findData(config.load_remote_max_edge())
        self._remote_res.setCurrentIndex(ridx if ridx >= 0 else 1)
        rrow.addStretch(1)
        rrow.addWidget(self._remote_res)
        v.addLayout(rrow)
        v.addWidget(self._hint(
            "Controls how much the host's screen is shrunk before sending. "
            "“Match host” keeps text crispest; lower settings are faster on slow links."))

        self._cb_lossless = QCheckBox("Sharp text mode (lossless PNG)")
        self._cb_lossless.setChecked(config.load_remote_lossless())
        v.addWidget(self._cb_lossless)
        v.addWidget(self._hint(
            "Sends perfectly crisp PNG frames instead of JPEG — best for reading "
            "text and icon labels. Frames are larger, so lower the frame rate if "
            "it feels heavy. (Image quality below applies to JPEG only.)"))

        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("Image quality"))
        self._remote_quality = QSlider(Qt.Orientation.Horizontal)
        self._remote_quality.setRange(20, 95)
        self._remote_quality.setValue(config.load_remote_quality())
        self._rq_lbl = QLabel(str(self._remote_quality.value()))
        self._remote_quality.valueChanged.connect(lambda x: self._rq_lbl.setText(str(x)))
        qrow.addWidget(self._remote_quality, 1)
        qrow.addWidget(self._rq_lbl)
        v.addLayout(qrow)
        v.addWidget(self._hint(
            "Higher quality keeps small text and icon labels readable; lower uses "
            "less bandwidth. 80+ is recommended for reading the remote screen."))

        # Quality only affects JPEG, so grey it out while Sharp text mode is on.
        def _sync_quality_enabled(on: bool) -> None:
            self._remote_quality.setEnabled(not on)
            self._rq_lbl.setEnabled(not on)
        self._cb_lossless.toggled.connect(_sync_quality_enabled)
        _sync_quality_enabled(self._cb_lossless.isChecked())

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Frame rate (frames per second)"))
        self._remote_fps = QSpinBox()
        self._remote_fps.setRange(1, 30)
        self._remote_fps.setValue(config.load_remote_fps())
        frow.addStretch(1)
        frow.addWidget(self._remote_fps)
        v.addLayout(frow)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Approval timeout (seconds)"))
        self._remote_timeout = QSpinBox()
        self._remote_timeout.setRange(10, 600)
        self._remote_timeout.setValue(config.load_remote_timeout())
        trow.addStretch(1)
        trow.addWidget(self._remote_timeout)
        v.addLayout(trow)
        v.addWidget(self._hint(
            "How long an incoming request waits for you to click Allow before it "
            "is automatically declined."))

        v.addStretch(1)
        return w

    # ── About ─────────────────────────────────────────────────────────────────
    def _page_about(self) -> QWidget:
        w, v = self._page("About")
        info = QLabel(
            f"<b>Net Split-Tunneler — LAN Chat</b><br>"
            f"Version {__version__}<br><br>"
            "A lightweight, serverless LAN chat with peer-to-peer file transfer, "
            "groups, broadcast channels and offline message queuing.<br><br>"
            "Developed by Pramod Verma.")
        info.setWordWrap(True)
        v.addWidget(info)
        v.addWidget(hline())
        v.addWidget(self._section_label("DIAGNOSTICS"))
        diag = QLabel(
            f"Display name : {self.chat.my_name}\n"
            f"Device name  : {self.chat.my_device}\n"
            f"Internal ID  : {self.chat.my_uid}\n"
            f"Primary IP   : {self.chat.my_ip}\n"
            f"Python ports : presence {CHAT_PRESENCE_PORT} · chat {CHAT_TCP_PORT} · file {FILE_TCP_PORT}")
        diag.setStyleSheet("font-family:'Consolas',monospace; font-size:12px;")
        diag.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(diag)
        v.addWidget(self._hint(
            "Network troubleshooting: if peers don't appear, confirm all PCs are "
            "on the same subnet and that the above ports aren't blocked by a "
            "firewall. Use “Connect by IP” to reach other subnets manually."))
        v.addStretch(1)
        return w

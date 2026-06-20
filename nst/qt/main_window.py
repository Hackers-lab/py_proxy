"""The main proxy window (PyQt6): Host / Client split-tunnel controls, a live
traffic monitor, an event log and the system-tray integration.  The LAN chat
opens as its own window (wired up in :mod:`nst.qt.app`)."""

import threading
import time

import psutil
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMenu, QMessageBox,
                             QPlainTextEdit, QPushButton, QStackedWidget,
                             QVBoxLayout, QWidget)

from .. import __version__, config
from ..beacon import ClientScanner, HostBeacon
from ..constants import PROXY_PORT
from ..netinfo import (calculate_gateway, check_host_reachable,
                       check_internet_connection, check_internet_via_proxy,
                       format_speed, get_intranet_ip, is_valid_ipv4)
from ..proxy_registry import clear_proxy, read_current_proxy, set_proxy
from ..proxy_server import ProxyServer
from ..dual_access import (check_secondary_ip, check_internet_route,
                           check_intranet_route, check_nrpt,
                           detect_internet_ip,
                           enable_dual_access, disable_dual_access)
from ..routing import (add_intranet_route, check_route_exists,
                       delete_intranet_route, _network_from_ip)
from ..win_utils import is_admin
from .signals import MainSignals
from .theme import theme
from .widgets import hline

_MONO = "font-family:'Consolas','Cascadia Mono',monospace; font-size:12px;"


def _derive_internet_gw(ip: str) -> str:
    parts = ip.split(".") if ip else []
    if len(parts) == 4:
        parts[-1] = "1"
        return ".".join(parts)
    return ""


class MainWindow(QMainWindow):
    def __init__(self, open_chat, run_demo, on_quit) -> None:
        super().__init__()
        self._open_chat = open_chat
        self._run_demo = run_demo
        self._on_quit = on_quit
        self._tray = None
        self._overlay = None
        self._tray_notified = False
        self._update_mgr = None
        self.sig = MainSignals()

        self.setWindowTitle("Net Split-Tunneler & Proxy Sharing Tool")
        self.setMinimumSize(560, 600)
        self.resize(580, 640)

        # services
        self._host_has_internet = False
        self._proxy = ProxyServer()
        self._beacon = HostBeacon(lambda: self._host_has_internet)
        self._scanner = ClientScanner(
            lambda ip, hi: self.sig.beacon.emit(ip, hi))
        self._route_active = check_route_exists()
        proxy_on, proxy_server = read_current_proxy()
        self._client_connected = proxy_on
        self._client_host = proxy_server.split(":")[0] if proxy_server else ""
        self._detected_ip = None
        self._detected_gw = None
        self._route_network: str = "10.0.0.0"  # updated when route is added
        self._dual_active: bool = False
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()

        self._build_menu()
        self._build_ui()
        self._connect_signals()

        self._update_route_btn()
        self._update_proxy_btn()
        self._update_client_btn()
        if self._client_connected and self._client_host:
            self._lbl_cstatus.setText(f"Status  :  CONNECTED  →  {self._client_host}:{PROXY_PORT}")
            self._lbl_cstatus.setStyleSheet(_MONO + f"color:{theme.color('success')};")

        self._scanner.start()
        threading.Thread(target=self._internet_loop, daemon=True).start()

        self._traffic_timer = QTimer(self)
        self._traffic_timer.timeout.connect(self._update_traffic)
        self._traffic_timer.start(1000)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_status)
        self._status_timer.start(3000)
        QTimer.singleShot(50, self._poll_status)

        theme.changed.connect(self._on_theme)
        mode = "administrator" if is_admin() else "standard user"
        self.log(f"Application started — v{__version__} ({mode}).")
        if self._route_active:
            self.log("Existing 10.0.0.0 route detected — marked ACTIVE.")
        if self._client_connected:
            self.log(f"Existing proxy detected: {proxy_server} — marked CONNECTED.")

    # ── injection from app.py ─────────────────────────────────────────────────
    def set_tray(self, tray) -> None:
        self._tray = tray

    def set_overlay(self, overlay) -> None:
        self._overlay = overlay

    # ── menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        mb = self.menuBar()
        filem = mb.addMenu("File")
        a = QAction("Exit", self); a.triggered.connect(self._on_quit); filem.addAction(a)

        setm = mb.addMenu("Settings")
        self._act_autostart = QAction("Start with Windows", self, checkable=True)
        self._act_autostart.setChecked(config.is_autostart_enabled())
        self._act_autostart.triggered.connect(self._toggle_autostart)
        setm.addAction(self._act_autostart)
        self._act_speed = QAction("Show Speed in Taskbar", self, checkable=True)
        self._act_speed.setChecked(config.load_show_speed_in_taskbar())
        self._act_speed.triggered.connect(self._toggle_speed)
        setm.addAction(self._act_speed)
        self._act_autoupdate = QAction("Update automatically", self, checkable=True)
        self._act_autoupdate.setChecked(config.load_auto_update_enabled())
        self._act_autoupdate.triggered.connect(self._toggle_autoupdate)
        setm.addAction(self._act_autoupdate)
        setm.addSeparator()
        self._act_light = QAction("Light theme", self, checkable=True)
        self._act_light.setChecked(not theme.is_dark())
        self._act_light.triggered.connect(self._toggle_theme)
        setm.addAction(self._act_light)

        chatm = mb.addMenu("Chat")
        a = QAction("Open LAN Chat", self); a.triggered.connect(lambda: self._open_chat()); chatm.addAction(a)
        a = QAction("Run Chat Demo", self); a.triggered.connect(lambda: self._run_demo()); chatm.addAction(a)

        aboutm = mb.addMenu("About")
        a = QAction("Check for updates", self); a.triggered.connect(self._check_updates); aboutm.addAction(a)
        a = QAction("About Net Split-Tunneler", self); a.triggered.connect(self._about); aboutm.addAction(a)

    # ── ui ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        # header
        hdr = QHBoxLayout()
        title = QLabel("NetSplitter")
        title.setObjectName("h1")
        title.setStyleSheet(f"color:{theme.color('accent')}; font-size:20px; font-weight:800;")
        hdr.addWidget(title)
        hdr.addStretch(1)
        self._btn_chat = QPushButton("💬  LAN Chat")
        self._btn_chat.setProperty("variant", "chat")
        self._btn_chat.clicked.connect(lambda: self._open_chat())
        hdr.addWidget(self._btn_chat)
        self._theme_btn = QPushButton("☀" if theme.is_dark() else "🌙")
        self._theme_btn.setProperty("variant", "ghost")
        self._theme_btn.setFixedWidth(40)
        self._theme_btn.clicked.connect(self._toggle_theme)
        hdr.addWidget(self._theme_btn)
        root.addLayout(hdr)

        # tabs
        tabs = QHBoxLayout()
        self._tab_host = QPushButton("Host Mode")
        self._tab_host.setObjectName("navItem")
        self._tab_host.clicked.connect(lambda: self._show_tab(0))
        self._tab_client = QPushButton("Client Mode")
        self._tab_client.setObjectName("navItem")
        self._tab_client.clicked.connect(lambda: self._show_tab(1))
        self._tab_dual = QPushButton("Dual Access")
        self._tab_dual.setObjectName("navItem")
        self._tab_dual.clicked.connect(lambda: self._show_tab(2))
        tabs.addWidget(self._tab_host)
        tabs.addWidget(self._tab_client)
        tabs.addWidget(self._tab_dual)
        tabs.addStretch(1)
        root.addLayout(tabs)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_host())
        self._stack.addWidget(self._build_client())
        self._stack.addWidget(self._build_dual())
        root.addWidget(self._stack)

        root.addWidget(self._build_traffic())
        root.addWidget(self._build_log(), 1)

        foot = QHBoxLayout()
        v = QLabel(f"v{__version__}"); v.setObjectName("muted")
        c = QLabel("Copyright © Pramod Verma"); c.setObjectName("muted")
        foot.addWidget(v); foot.addStretch(1); foot.addWidget(c)
        root.addLayout(foot)

        self._show_tab(0)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(); card.setObjectName("card")
        v = QVBoxLayout(card); v.setContentsMargins(14, 12, 14, 12); v.setSpacing(6)
        t = QLabel(title); t.setObjectName("section")
        v.addWidget(t)
        return card, v

    def _build_host(self) -> QWidget:
        card, v = self._card("HOST MODE")
        self._lbl_ip = QLabel("Intranet IP   :  —")
        self._lbl_gw = QLabel("Gateway       :  —")
        self._lbl_proxy = QLabel("Proxy         :  STOPPED")
        self._lbl_route = QLabel("LAN+NET Route :  INACTIVE")
        self._lbl_inet = QLabel("Internet      :  CHECKING…")
        for w in (self._lbl_ip, self._lbl_gw, self._lbl_proxy, self._lbl_route, self._lbl_inet):
            w.setStyleSheet(_MONO)
            v.addWidget(w)
        v.addWidget(hline())
        brow = QHBoxLayout()
        self._btn_route = QPushButton("▶  Enable LAN+NET")
        self._btn_route.setProperty("variant", "success")
        self._btn_route.clicked.connect(self._toggle_route)
        self._btn_proxy = QPushButton("▶  Start Proxy Server")
        self._btn_proxy.setProperty("variant", "proxy")
        self._btn_proxy.clicked.connect(self._toggle_proxy)
        brow.addWidget(self._btn_route); brow.addWidget(self._btn_proxy); brow.addStretch(1)
        v.addLayout(brow)
        return card

    def _build_client(self) -> QWidget:
        card, v = self._card("CLIENT MODE")
        iprow = QHBoxLayout()
        iprow.addWidget(QLabel("Host IP:"))
        self._host_edit = QLineEdit(self._client_host)
        self._host_edit.setStyleSheet(_MONO)
        iprow.addWidget(self._host_edit, 1)
        self._lbl_scan = QLabel("⟳ scanning…"); self._lbl_scan.setObjectName("muted")
        iprow.addWidget(self._lbl_scan)
        v.addLayout(iprow)
        self._lbl_cstatus = QLabel("Status  :  DISCONNECTED")
        self._lbl_cstatus.setStyleSheet(_MONO)
        v.addWidget(self._lbl_cstatus)
        self._chk_disable = QCheckBox("Disable proxy if host has no internet / unreachable")
        v.addWidget(self._chk_disable)
        brow = QHBoxLayout()
        self._btn_client = QPushButton("⬡  Connect to Host Proxy")
        self._btn_client.setProperty("variant", "accent")
        self._btn_client.clicked.connect(self._toggle_client)
        brow.addWidget(self._btn_client); brow.addStretch(1)
        v.addLayout(brow)
        return card

    def _build_dual(self) -> QWidget:
        card, v = self._card("DUAL ACCESS  —  LAN + Internet simultaneously")

        # Two-column input row
        cols = QHBoxLayout()

        left = QVBoxLayout()
        left.setSpacing(3)
        left.addWidget(QLabel("Internet IP"))
        ip_row = QHBoxLayout()
        self._dual_ip_edit = QLineEdit(config.load_dual_internet_ip())
        self._dual_ip_edit.setStyleSheet(_MONO)
        self._dual_ip_edit.setPlaceholderText("e.g. 192.168.1.50")
        self._dual_ip_edit.textChanged.connect(
            lambda t: config.save_dual_internet_ip(t.strip()))
        ip_row.addWidget(self._dual_ip_edit, 1)
        self._btn_dual_detect = QPushButton("Auto-detect")
        self._btn_dual_detect.setFixedWidth(90)
        self._btn_dual_detect.clicked.connect(self._detect_internet_ip)
        ip_row.addWidget(self._btn_dual_detect)
        left.addLayout(ip_row)

        right = QVBoxLayout()
        right.setSpacing(3)
        right.addWidget(QLabel("NRPT Domains  (comma-separated)"))
        self._dual_dom_edit = QLineEdit(",".join(config.load_dual_domains()))
        self._dual_dom_edit.setStyleSheet(_MONO)
        self._dual_dom_edit.setPlaceholderText("e.g. corp.local,company.in")
        self._dual_dom_edit.textChanged.connect(
            lambda t: config.save_dual_domains(
                [s.strip() for s in t.split(",") if s.strip()]))
        right.addWidget(self._dual_dom_edit)

        cols.addLayout(left, 1)
        cols.addSpacing(10)
        cols.addLayout(right, 1)
        v.addLayout(cols)

        hint = QLabel("Intranet DNS is auto-read from your adapter — no extra config needed.")
        hint.setObjectName("muted")
        v.addWidget(hint)

        v.addWidget(hline())

        # Status — two per row
        srow1 = QHBoxLayout()
        self._lbl_da_intranet = QLabel("Intranet Route  :  —")
        self._lbl_da_secip    = QLabel("Secondary IP    :  —")
        self._lbl_da_intranet.setStyleSheet(_MONO)
        self._lbl_da_secip.setStyleSheet(_MONO)
        srow1.addWidget(self._lbl_da_intranet, 1)
        srow1.addWidget(self._lbl_da_secip, 1)
        v.addLayout(srow1)

        srow2 = QHBoxLayout()
        self._lbl_da_inet = QLabel("Internet Route  :  —")
        self._lbl_da_dns  = QLabel("Split DNS/NRPT  :  —")
        self._lbl_da_inet.setStyleSheet(_MONO)
        self._lbl_da_dns.setStyleSheet(_MONO)
        srow2.addWidget(self._lbl_da_inet, 1)
        srow2.addWidget(self._lbl_da_dns, 1)
        v.addLayout(srow2)

        v.addWidget(hline())

        brow = QHBoxLayout()
        self._btn_dual = QPushButton("▶  Enable Dual Access")
        self._btn_dual.setProperty("variant", "success")
        self._btn_dual.clicked.connect(self._toggle_dual)
        brow.addWidget(self._btn_dual)
        brow.addStretch(1)
        v.addLayout(brow)
        return card

    def _update_dual_status(self) -> None:
        internet_ip = self._dual_ip_edit.text().strip()
        internet_gw = _derive_internet_gw(internet_ip)
        domains     = config.load_dual_domains()

        def _mark(lbl, active, text):
            lbl.setText(text)
            lbl.setStyleSheet(_MONO + (
                f"color:{theme.color('success')};" if active
                else f"color:{theme.color('danger')};"))

        intranet_ok = check_intranet_route()
        sec_ip_ok   = check_secondary_ip(internet_ip) if internet_ip else False
        inet_ok     = check_internet_route(internet_gw) if internet_gw else False
        nrpt_ok     = any(check_nrpt(d) for d in domains) if domains else False

        _mark(self._lbl_da_intranet, intranet_ok,
              f"Intranet Route  :  {'ACTIVE' if intranet_ok else 'INACTIVE'}")
        _mark(self._lbl_da_secip, sec_ip_ok,
              f"Secondary IP    :  {'ACTIVE  (' + internet_ip + ')' if sec_ip_ok else 'INACTIVE'}")
        _mark(self._lbl_da_inet, inet_ok,
              f"Internet Route  :  {'ACTIVE' if inet_ok else 'INACTIVE'}")
        _mark(self._lbl_da_dns, nrpt_ok,
              f"Split DNS/NRPT  :  {'ACTIVE' if nrpt_ok else 'INACTIVE'}")

        self._dual_active = sec_ip_ok and inet_ok and nrpt_ok
        if self._dual_active:
            self._set_btn(self._btn_dual, "■  Disable Dual Access", "danger")
        else:
            self._set_btn(self._btn_dual, "▶  Enable Dual Access", "success")

    def _detect_internet_ip(self) -> None:
        if not self._detected_ip:
            self.log("No intranet IP detected — connect to the intranet first.")
            return
        self._btn_dual_detect.setEnabled(False)
        self._btn_dual_detect.setText("…")
        ip, msg = detect_internet_ip(self._detected_ip)
        self._btn_dual_detect.setEnabled(True)
        self._btn_dual_detect.setText("Auto-detect")
        if ip:
            self._dual_ip_edit.setText(ip)
        self.log(msg)

    def _toggle_dual(self) -> None:
        internet_ip = self._dual_ip_edit.text().strip()
        if not internet_ip or not is_valid_ipv4(internet_ip):
            QMessageBox.critical(self, "Missing Internet IP",
                                 "Enter a valid internet IP address (e.g. 192.168.1.50).")
            return
        if not self._detected_ip:
            QMessageBox.critical(self, "No Intranet IP",
                                 "No intranet IP detected. Connect to the intranet first.")
            return

        domains = config.load_dual_domains()

        if self._dual_active:
            ok, msg = disable_dual_access(self._detected_ip, internet_ip, domains)
        else:
            ok, msg = enable_dual_access(self._detected_ip, internet_ip, domains)

        self.log(msg)
        self._update_dual_status()

    def _build_traffic(self) -> QWidget:
        card, v = self._card("NETWORK TRAFFIC MONITOR")
        row = QHBoxLayout()
        self._lbl_down = QLabel("Download  :  0.0 KB/s")
        self._lbl_down.setStyleSheet(_MONO + f"color:{theme.color('success')};")
        self._lbl_up = QLabel("Upload    :  0.0 KB/s")
        self._lbl_up.setStyleSheet(_MONO + f"color:{theme.color('warning')};")
        row.addWidget(self._lbl_down, 1)
        row.addWidget(self._lbl_up, 1)
        self._unit_combo = QComboBox()
        self._unit_combo.addItems(
            ["Auto", "bps", "B/s", "KiB/s", "KB/s", "MiB/s", "MB/s", "Kbps", "Mbps"])
        self._unit_combo.setFixedWidth(80)
        self._unit_combo.setToolTip("Display unit for network speed")
        row.addWidget(self._unit_combo)
        v.addLayout(row)
        return card

    def _fmt_traffic(self, bps: float) -> str:
        unit = self._unit_combo.currentText()
        if unit == "bps":
            return f"{bps * 8:.0f} bps"
        if unit == "B/s":
            return f"{bps:.1f} B/s"
        if unit == "KiB/s":
            return f"{bps / 1024:.2f} KiB/s"
        if unit == "KB/s":
            return f"{bps / 1000:.2f} KB/s"
        if unit == "MiB/s":
            return f"{bps / 1048576:.3f} MiB/s"
        if unit == "MB/s":
            return f"{bps / 1_000_000:.3f} MB/s"
        if unit == "Kbps":
            return f"{bps * 8 / 1000:.1f} Kbps"
        if unit == "Mbps":
            return f"{bps * 8 / 1_000_000:.3f} Mbps"
        return format_speed(bps)  # Auto

    def _build_log(self) -> QWidget:
        card, v = self._card("EVENT LOG")
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(_MONO)
        v.addWidget(self._log)
        return card

    # ── tabs ──────────────────────────────────────────────────────────────────
    def _show_tab(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._tab_host.setProperty("active", "true" if idx == 0 else "false")
        self._tab_client.setProperty("active", "true" if idx == 1 else "false")
        self._tab_dual.setProperty("active", "true" if idx == 2 else "false")
        for b in (self._tab_host, self._tab_client, self._tab_dual):
            b.style().unpolish(b); b.style().polish(b)

    # ── signal wiring ─────────────────────────────────────────────────────────
    def _connect_signals(self) -> None:
        self.sig.beacon.connect(self._on_beacon)
        self.sig.internet.connect(self._on_internet)
        self.sig.client_auto_off.connect(self._on_client_auto_off)

    # ── logging ───────────────────────────────────────────────────────────────
    def log(self, msg: str) -> None:
        self._log.appendPlainText(f"[{time.strftime('%H:%M:%S')}]  {msg}")

    # ── button state ──────────────────────────────────────────────────────────
    def _set_btn(self, btn, text, variant) -> None:
        btn.setText(text)
        btn.setProperty("variant", variant)
        btn.style().unpolish(btn); btn.style().polish(btn)

    def _update_route_btn(self) -> None:
        if self._route_active:
            self._set_btn(self._btn_route, "■  Disable LAN+NET", "danger")
            self._lbl_route.setText("LAN+NET Route :  ACTIVE")
            self._lbl_route.setStyleSheet(_MONO + f"color:{theme.color('success')};")
        else:
            self._set_btn(self._btn_route, "▶  Enable LAN+NET", "success")
            self._lbl_route.setText("LAN+NET Route :  INACTIVE")
            self._lbl_route.setStyleSheet(_MONO)

    def _update_proxy_btn(self) -> None:
        if self._proxy.running:
            self._set_btn(self._btn_proxy, "■  Stop Proxy Server", "danger")
            self._lbl_proxy.setText(f"Proxy         :  RUNNING  (:{PROXY_PORT})")
            self._lbl_proxy.setStyleSheet(_MONO + f"color:{theme.color('success')};")
        else:
            self._set_btn(self._btn_proxy, "▶  Start Proxy Server", "proxy")
            self._lbl_proxy.setText("Proxy         :  STOPPED")
            self._lbl_proxy.setStyleSheet(_MONO + f"color:{theme.color('danger')};")

    def _update_client_btn(self) -> None:
        if self._client_connected:
            self._set_btn(self._btn_client, "✕  Disconnect from Proxy", "danger")
        else:
            self._set_btn(self._btn_client, "⬡  Connect to Host Proxy", "accent")

    # ── host toggles ──────────────────────────────────────────────────────────
    def _toggle_route(self) -> None:
        if self._route_active:
            ok, msg = delete_intranet_route(self._route_network)
            if ok:
                self._route_active = False
        else:
            if not self._detected_ip or not self._detected_gw:
                QMessageBox.critical(self, "No Intranet IP",
                                     "Cannot detect an intranet IP address on this machine.")
                return
            network = _network_from_ip(self._detected_ip)
            ok, msg = add_intranet_route(self._detected_gw, network)
            if ok:
                self._route_active = True
                self._route_network = network
        self.log(msg)
        self._update_route_btn()

    def _toggle_proxy(self) -> None:
        if self._proxy.running:
            ok, msg = self._proxy.stop()
            if ok:
                self._beacon.stop()
        else:
            ok, msg = self._proxy.start()
            if ok and self._detected_ip:
                self._beacon.start(self._detected_ip)
        self.log(msg)
        self._update_proxy_btn()

    def _toggle_client(self) -> None:
        if self._client_connected:
            ok, msg = clear_proxy()
            if ok:
                self._client_connected = False
                self._lbl_cstatus.setText("Status  :  DISCONNECTED")
                self._lbl_cstatus.setStyleSheet(_MONO)
        else:
            host = self._host_edit.text().strip()
            if not host:
                QMessageBox.critical(self, "Missing IP", "Enter the Host IP address or wait for auto-detect.")
                return
            if not is_valid_ipv4(host):
                QMessageBox.critical(self, "Invalid IP", f"'{host}' is not a valid IPv4 address.")
                return
            ok, msg = set_proxy(host, PROXY_PORT)
            if ok:
                self._client_connected = True
                self._client_host = host
                self._lbl_cstatus.setText(f"Status  :  CONNECTED  →  {host}:{PROXY_PORT}")
                self._lbl_cstatus.setStyleSheet(_MONO + f"color:{theme.color('success')};")
        self.log(msg)
        self._update_client_btn()

    def _toggle_autostart(self) -> None:
        ok, msg = config.set_autostart(self._act_autostart.isChecked())
        self.log(msg)
        if not ok:
            QMessageBox.critical(self, "Registry Error", msg)

    def _toggle_speed(self) -> None:
        enabled = self._act_speed.isChecked()
        if config.save_show_speed_in_taskbar(enabled):
            self.log(f"Show Speed in Taskbar {'enabled' if enabled else 'disabled'}.")
            if not enabled and self._overlay:
                self._overlay.hide()
        else:
            QMessageBox.critical(self, "Registry Error", "Failed to save settings.")

    # ── updates ───────────────────────────────────────────────────────────────
    def set_update_manager(self, mgr) -> None:
        self._update_mgr = mgr

    def _toggle_autoupdate(self) -> None:
        enabled = self._act_autoupdate.isChecked()
        config.save_auto_update_enabled(enabled)
        self.log(f"Automatic updates {'enabled' if enabled else 'disabled'}.")

    def _check_updates(self) -> None:
        if self._update_mgr is None:
            return
        self.log("Checking for updates…")
        self._update_mgr.check(manual=True)

    # ── theme ─────────────────────────────────────────────────────────────────
    def _toggle_theme(self) -> None:
        theme.toggle()
        self.log(f"Theme switched to {theme.name}.")

    def _on_theme(self) -> None:
        self._theme_btn.setText("☀" if theme.is_dark() else "🌙")
        self._act_light.setChecked(not theme.is_dark())
        self._update_route_btn(); self._update_proxy_btn(); self._update_client_btn()

    # ── polling ───────────────────────────────────────────────────────────────
    def _poll_status(self) -> None:
        ip = get_intranet_ip()
        if ip:
            gw = calculate_gateway(ip)
            self._lbl_ip.setText(f"Intranet IP   :  {ip}")
            self._lbl_ip.setStyleSheet(_MONO + f"color:{theme.color('success')};")
            self._lbl_gw.setText(f"Gateway       :  {gw}")
            self._detected_ip, self._detected_gw = ip, gw
            self._route_network = _network_from_ip(ip)
            if self._beacon.running:
                self._beacon.ip = ip
            # Auto-fill internet IP once from DHCP cache if field is empty
            if not self._dual_ip_edit.text().strip():
                suggested, _ = detect_internet_ip(ip)
                if suggested:
                    self._dual_ip_edit.setText(suggested)
        else:
            self._lbl_ip.setText("Intranet IP   :  Not detected")
            self._lbl_ip.setStyleSheet(_MONO + f"color:{theme.color('warning')};")
            self._lbl_gw.setText("Gateway       :  —")
            self._detected_ip = self._detected_gw = None
        self._update_proxy_btn(); self._update_route_btn()
        self._update_dual_status()
        if self._client_connected and self._chk_disable.isChecked():
            threading.Thread(target=self._client_health, daemon=True).start()

    def _update_traffic(self) -> None:
        try:
            cur = psutil.net_io_counters()
            t = time.time()
            dt = t - self._last_net_t
            if dt > 0:
                up = (cur.bytes_sent - self._last_net.bytes_sent) / dt
                down = (cur.bytes_recv - self._last_net.bytes_recv) / dt
                up_s, down_s = self._fmt_traffic(up), self._fmt_traffic(down)
                self._lbl_down.setText(f"Download  :  {down_s}")
                self._lbl_up.setText(f"Upload    :  {up_s}")
                if self._act_speed.isChecked():
                    # Show the speed as a readout beside the clock, and keep the
                    # tray icon clean rather than cramming the numbers into 32px.
                    if self._overlay:
                        self._overlay.show_speed(up_s, down_s)
                    if self._tray:
                        self._tray.set_idle()
                else:
                    if self._tray:
                        self._tray.set_idle()
                    if self._overlay:
                        self._overlay.hide()
            self._last_net, self._last_net_t = cur, t
        except Exception:
            pass

    # ── background loops ──────────────────────────────────────────────────────
    def _internet_loop(self) -> None:
        while True:
            res = check_internet_connection()
            self.sig.internet.emit(res)
            time.sleep(3)

    def _on_internet(self, res: bool) -> None:
        self._host_has_internet = res
        if res:
            self._lbl_inet.setText("Internet      :  CONNECTED")
            self._lbl_inet.setStyleSheet(_MONO + f"color:{theme.color('success')};")
        else:
            self._lbl_inet.setText("Internet      :  NO CONNECTION")
            self._lbl_inet.setStyleSheet(_MONO + f"color:{theme.color('danger')};")

    def _client_health(self) -> None:
        if not self._client_connected or not self._chk_disable.isChecked():
            return
        host = self._client_host
        if not host:
            return
        host_ok = check_host_reachable(host, PROXY_PORT)
        inet_ok = check_internet_via_proxy(host, PROXY_PORT) if host_ok else False
        if not host_ok or not inet_ok:
            reason = "Host unreachable" if not host_ok else "No Internet access through proxy"
            self.sig.client_auto_off.emit(reason)

    def _on_client_auto_off(self, reason: str) -> None:
        if self._client_connected:
            ok, _ = clear_proxy()
            if ok:
                self._client_connected = False
                self._lbl_cstatus.setText(f"Status  :  DISCONNECTED ({reason})")
                self._lbl_cstatus.setStyleSheet(_MONO + f"color:{theme.color('warning')};")
                self._update_client_btn()
                self.log(f"Client proxy disabled automatically: {reason}.")

    def _on_beacon(self, ip: str, has_internet: bool) -> None:
        status = "Internet OK" if has_internet else "No Internet"
        self._lbl_scan.setText(f"✓ host: {ip} ({status})")
        self._lbl_scan.setStyleSheet(
            f"color:{theme.color('success') if has_internet else theme.color('danger')};")
        if self._host_edit.text().strip() != ip:
            self._host_edit.setText(ip)
            self.log(f"Host beacon detected: {ip} — IP auto-filled.")
        if self._client_connected and self._client_host == ip and not has_internet \
                and self._chk_disable.isChecked():
            self._on_client_auto_off("Host lost internet")

    # ── about / close ─────────────────────────────────────────────────────────
    def _about(self) -> None:
        QMessageBox.about(
            self, "About Net Split-Tunneler",
            f"<b>Net Split-Tunneler v{__version__}</b><br>"
            "Proxy Sharing Tool + LAN Chat<br><br>"
            "A lightweight Windows utility to split-tunnel local traffic, share a "
            "proxy connection, and chat across the LAN.<br><br>"
            "Developed by Pramod Verma")

    def closeEvent(self, e) -> None:
        # Minimise to tray instead of quitting — unless the user disabled it.
        if not config.load_minimize_to_tray():
            e.accept()
            self._on_quit()
            return
        e.ignore()
        self.hide()
        if self._tray:
            self._tray.show()
            if not self._tray_notified:
                self._tray_notified = True
                self._tray.notify("Still running",
                                  "Net Split-Tunneler is running in the background.\n"
                                  "Use the tray icons to open Proxy or Chat.")

    def shutdown(self) -> None:
        try:
            if self._proxy.running:
                self._proxy.stop()
            self._beacon.stop()
            self._scanner.stop()
        except Exception:
            pass

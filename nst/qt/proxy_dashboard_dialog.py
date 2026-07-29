"""Proxy Server Host Control Dashboard, IP Access Control, Domain Blocker, and Live Link Inspector."""

import time
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget
)

from .. import config
from .theme import theme
from ..proxy_server import clear_link_logs, get_active_clients, get_link_logs

_MONO = "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"


class ProxyDashboardDialog(QDialog):
    """Host Control Dashboard for Proxy Server."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Proxy Server Host Control — Clients, Domain Blocker & Link Inspector")
        self.setMinimumWidth(720)
        self.setMinimumHeight(560)

        self._build_ui()

        # Timer to auto-refresh live views every 2 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_all)
        self._timer.start(2000)

    def _build_ui(self):
        v = QVBoxLayout(self)

        self.tabs = QTabWidget()

        # Tab 1: Connected Clients & IP ACL
        self.tab_clients = self._build_clients_tab()
        self.tabs.addTab(self.tab_clients, "👥 Connected Clients & IP ACL")

        # Tab 2: Website Blocker
        self.tab_blocker = self._build_blocker_tab()
        self.tabs.addTab(self.tab_blocker, "🛡 Website & Domain Blocker")

        # Tab 3: Link Inspector
        self.tab_inspector = self._build_inspector_tab()
        self.tabs.addTab(self.tab_inspector, "🔍 Live Link Inspector & Log")

        v.addWidget(self.tabs)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)

        self._refresh_all()

    def _build_clients_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        lbl = QLabel("CONNECTED CLIENTS (LIVE)")
        lbl.setStyleSheet("font-weight: bold;")
        v.addWidget(lbl)

        self.table_clients = QTableWidget(0, 5)
        self.table_clients.setHorizontalHeaderLabels(["Client IP", "First Seen", "Last Seen", "Active Conns", "Bytes Transferred"])
        self.table_clients.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table_clients)

        row_btn = QHBoxLayout()
        btn_block_sel = QPushButton("🚫 Block Selected Client IP")
        btn_block_sel.clicked.connect(self._block_selected_client)
        row_btn.addWidget(btn_block_sel)
        row_btn.addStretch(1)
        v.addLayout(row_btn)

        # ACL Configuration
        lbl_acl = QLabel("IP ACCESS CONTROL CONFIGURATION (CSV Lists)")
        lbl_acl.setStyleSheet("font-weight: bold; margin-top: 10px;")
        v.addWidget(lbl_acl)

        lbl_allow = QLabel("Allowed Client IPs (Whitelist - leave empty to allow all LAN IPs):")
        v.addWidget(lbl_allow)
        self.edit_allowed_ips = QLineEdit()
        self.edit_allowed_ips.setStyleSheet(_MONO)
        self.edit_allowed_ips.setPlaceholderText("e.g. 10.251.33.45, 10.251.33.46")
        v.addWidget(self.edit_allowed_ips)

        lbl_block = QLabel("Blocked Client IPs (Blacklist):")
        v.addWidget(lbl_block)
        self.edit_blocked_ips = QLineEdit()
        self.edit_blocked_ips.setStyleSheet(_MONO)
        self.edit_blocked_ips.setPlaceholderText("e.g. 10.251.33.99")
        v.addWidget(self.edit_blocked_ips)

        btn_save_ip = QPushButton("💾 Save IP ACL Settings")
        btn_save_ip.clicked.connect(self._save_ip_acl)
        v.addWidget(btn_save_ip)

        # Populate ACL fields
        self.edit_allowed_ips.setText(", ".join(config.load_proxy_allowed_ips()))
        self.edit_blocked_ips.setText(", ".join(config.load_proxy_blocked_ips()))

        return w

    def _build_blocker_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        lbl = QLabel("QUICK WEBSITE BLOCKING TOGGLES")
        lbl.setStyleSheet("font-weight: bold;")
        v.addWidget(lbl)

        # Quick preset checkboxes
        self.chk_yt = QCheckBox("Block YouTube (youtube.com, googlevideo.com)")
        self.chk_wa = QCheckBox("Block WhatsApp Web (whatsapp.com, whatsapp.net)")
        self.chk_ig = QCheckBox("Block Instagram (instagram.com, fbcdn.net)")

        for chk in (self.chk_yt, self.chk_wa, self.chk_ig):
            v.addWidget(chk)
            chk.stateChanged.connect(self._sync_domain_toggles)

        lbl_cust = QLabel("BLOCKED DOMAINS LIST (CSV Format):")
        lbl_cust.setStyleSheet("font-weight: bold; margin-top: 10px;")
        v.addWidget(lbl_cust)

        self.edit_domains = QTextEdit()
        self.edit_domains.setStyleSheet(_MONO)
        self.edit_domains.setPlaceholderText("Enter target domains to block, separated by commas or lines.\ne.g. youtube.com, whatsapp.com, instagram.com, tiktok.com")
        v.addWidget(self.edit_domains)

        btn_save_dom = QPushButton("💾 Save Domain Blocker Rules")
        btn_save_dom.clicked.connect(self._save_domain_rules)
        v.addWidget(btn_save_dom)

        # Load existing blocked domains
        blocked = config.load_proxy_blocked_domains()
        self.edit_domains.setPlainText(", ".join(blocked))
        self._update_toggles_from_list(blocked)

        return w

    def _build_inspector_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        row_top = QHBoxLayout()
        self.chk_tracking = QCheckBox("Enable Live Link Tracking & Inspection")
        self.chk_tracking.setChecked(config.load_proxy_link_tracking())
        self.chk_tracking.stateChanged.connect(self._toggle_link_tracking)
        row_top.addWidget(self.chk_tracking)
        row_top.addStretch(1)

        btn_clear = QPushButton("🧹 Clear Inspection Logs")
        btn_clear.clicked.connect(self._clear_logs)
        row_top.addWidget(btn_clear)
        v.addLayout(row_top)

        self.table_logs = QTableWidget(0, 6)
        self.table_logs.setHorizontalHeaderLabels(["Time", "Client IP", "Method", "Target Host", "Path", "Status"])
        self.table_logs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table_logs)

        return w

    def _refresh_all(self):
        # Refresh Active Clients
        clients = get_active_clients()
        self.table_clients.setRowCount(len(clients))
        for row, c in enumerate(clients):
            self.table_clients.setItem(row, 0, QTableWidgetItem(c["ip"]))
            self.table_clients.setItem(row, 1, QTableWidgetItem(time.strftime("%H:%M:%S", time.localtime(c["first_seen"]))))
            self.table_clients.setItem(row, 2, QTableWidgetItem(time.strftime("%H:%M:%S", time.localtime(c["last_seen"]))))
            self.table_clients.setItem(row, 3, QTableWidgetItem(str(c["active_conns"])))
            self.table_clients.setItem(row, 4, QTableWidgetItem(f"{c['bytes'] / 1024:.1f} KB"))

        # Refresh Link Logs
        logs = get_link_logs()
        self.table_logs.setRowCount(len(logs))
        for row, l in enumerate(logs[:200]):  # display top 200 recent
            t_str = time.strftime("%H:%M:%S", time.localtime(l["timestamp"]))
            self.table_logs.setItem(row, 0, QTableWidgetItem(t_str))
            self.table_logs.setItem(row, 1, QTableWidgetItem(l["client_ip"]))
            self.table_logs.setItem(row, 2, QTableWidgetItem(l["method"]))
            self.table_logs.setItem(row, 3, QTableWidgetItem(l["host"]))
            self.table_logs.setItem(row, 4, QTableWidgetItem(l["path"]))

            item_status = QTableWidgetItem("🚫 BLOCKED" if l["blocked"] else "✅ Allowed")
            if l["blocked"]:
                item_status.setStyleSheet("color: red; font-weight: bold;")
            else:
                item_status.setStyleSheet("color: green;")
            self.table_logs.setItem(row, 5, item_status)

    def _block_selected_client(self):
        row = self.table_clients.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select Client", "Select a client row in the table first.")
            return
        ip = self.table_clients.item(row, 0).text()
        blocked = config.load_proxy_blocked_ips()
        if ip not in blocked:
            blocked.append(ip)
            config.save_proxy_blocked_ips(blocked)
            self.edit_blocked_ips.setText(", ".join(blocked))
            QMessageBox.information(self, "Client Blocked", f"Client IP {ip} has been added to blocked list.")

    def _save_ip_acl(self):
        allowed = [x.strip() for x in self.edit_allowed_ips.text().split(",") if x.strip()]
        blocked = [x.strip() for x in self.edit_blocked_ips.text().split(",") if x.strip()]
        config.save_proxy_allowed_ips(allowed)
        config.save_proxy_blocked_ips(blocked)
        QMessageBox.information(self, "Saved", "IP Access Control settings saved.")

    def _sync_domain_toggles(self):
        domains = set(x.strip() for x in self.edit_domains.toPlainText().replace("\n", ",").split(",") if x.strip())
        yt = {"youtube.com", "googlevideo.com"}
        wa = {"whatsapp.com", "whatsapp.net"}
        ig = {"instagram.com", "fbcdn.net"}

        if self.chk_yt.isChecked(): domains.update(yt)
        else: domains.difference_update(yt)

        if self.chk_wa.isChecked(): domains.update(wa)
        else: domains.difference_update(wa)

        if self.chk_ig.isChecked(): domains.update(ig)
        else: domains.difference_update(ig)

        self.edit_domains.blockSignals(True)
        self.edit_domains.setPlainText(", ".join(sorted(domains)))
        self.edit_domains.blockSignals(False)

    def _update_toggles_from_list(self, domains: list[str]):
        d_set = set(domains)
        self.chk_yt.setChecked("youtube.com" in d_set)
        self.chk_wa.setChecked("whatsapp.com" in d_set)
        self.chk_ig.setChecked("instagram.com" in d_set)

    def _save_domain_rules(self):
        domains = [x.strip() for x in self.edit_domains.toPlainText().replace("\n", ",").split(",") if x.strip()]
        config.save_proxy_blocked_domains(domains)
        QMessageBox.information(self, "Saved", f"Saved {len(domains)} blocked domains.")

    def _toggle_link_tracking(self, state):
        config.save_proxy_link_tracking(bool(state))

    def _clear_logs(self):
        clear_link_logs()
        self._refresh_all()

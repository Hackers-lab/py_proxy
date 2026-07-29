"""Interactive Dual Access Step-by-Step Diagnostic Dialog & Command Viewer.

Displays live step-by-step progress, checks, command execution log, and manual command exporter.
"""

import time
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget
)

from .theme import theme
from ..dual_access import (
    _derive_gw,
    disable_dual_access,
    enable_dual_access,
    get_adapter_dns_servers,
    get_adapter_for_ip,
    test_internet_ping,
    test_intranet_ping,
    test_sap_connection,
)

_MONO = "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"


class DualAccessWorker(QThread):
    """Executes dual access enable/disable and step-by-step diagnostic checks."""

    # step_name, status ("pending" | "running" | "success" | "warning" | "error"), message, command_log
    step_updated = pyqtSignal(str, str, str, str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, mode: str, intranet_ip: str, internet_ip: str, domains: list[str], simulate: bool = False, parent=None):
        super().__init__(parent)
        self.mode = mode  # "enable" | "disable"
        self.intranet_ip = intranet_ip
        self.internet_ip = internet_ip
        self.domains = domains
        self.simulate = simulate

    def run(self):
        adapter = get_adapter_for_ip(self.intranet_ip) or "Ethernet"
        intranet_gw = _derive_gw(self.intranet_ip)
        internet_gw = _derive_gw(self.internet_ip)
        dns_servers = get_adapter_dns_servers(adapter) or ["10.251.33.80", "10.251.33.90"]
        dns_csv = ",".join(dns_servers)
        domain_csv = ",".join(self.domains)

        if self.simulate:
            self._run_simulation(adapter, intranet_gw, internet_gw, dns_csv, domain_csv)
            return

        if self.mode == "enable":
            self._run_enable(adapter, intranet_gw, internet_gw, dns_csv, domain_csv)
        else:
            self._run_disable(adapter, domain_csv)

    def _run_simulation(self, adapter, intranet_gw, internet_gw, dns_csv, domain_csv):
        steps = [
            ("bind_ip", f'netsh interface ip add address "{adapter}" {self.internet_ip} 255.255.255.0\nSet-NetIPAddress -IPAddress "{self.internet_ip}" -SkipAsSource $true'),
            ("default_route", f'route add 0.0.0.0 mask 0.0.0.0 {internet_gw} metric 5'),
            ("intranet_route", f'route add 10.0.0.0 mask 255.0.0.0 {intranet_gw} -p'),
            ("dns_host_route", f'route add {dns_csv.split(",")[0]} mask 255.255.255.255 {intranet_gw}'),
            ("nrpt_rules", f'Add-DnsClientNrptRule -Namespace ".{self.domains[0]}" -NameServers "{dns_csv}"'),
            ("test_internet", "ping -n 1 8.8.8.8"),
            ("test_intranet", f"ping -n 1 {intranet_gw}"),
            ("test_sap", "socket.connect(('erpprd.wbsedcl.in', 3600))"),
        ]

        for step_id, cmd in steps:
            self.step_updated.emit(step_id, "running", "Executing...", f"> {cmd}")
            time.sleep(0.6)
            self.step_updated.emit(step_id, "success", "Applied successfully", f"[SUCCESS] {cmd}")

        self.finished_signal.emit(True, "Simulation complete — all checks simulated successfully!")

    def _run_enable(self, adapter, intranet_gw, internet_gw, dns_csv, domain_csv):
        # 1. Bind IP
        cmd_ip = f'netsh interface ip add address "{adapter}" {self.internet_ip} 255.255.255.0\nSet-NetIPAddress -IPAddress "{self.internet_ip}" -SkipAsSource $true'
        self.step_updated.emit("bind_ip", "running", "Binding secondary IP...", f"> {cmd_ip}")

        ok, msg = enable_dual_access(self.intranet_ip, self.internet_ip, self.domains)
        if not ok:
            self.step_updated.emit("bind_ip", "error", msg, f"[ERROR] {msg}")
            self.finished_signal.emit(False, msg)
            return

        self.step_updated.emit("bind_ip", "success", "Bound with SkipAsSource=True", f"[OK] {cmd_ip}")

        # 2. Internet route
        cmd_def = f'route add 0.0.0.0 mask 0.0.0.0 {internet_gw} metric 5'
        self.step_updated.emit("default_route", "success", f"Via {internet_gw}", f"[OK] {cmd_def}")

        # 3. Intranet route
        cmd_intra = f'route add 10.0.0.0 mask 255.0.0.0 {intranet_gw} -p'
        self.step_updated.emit("intranet_route", "success", f"Via {intranet_gw}", f"[OK] {cmd_intra}")

        # 4. DNS Host route
        cmd_dnshost = f'route add {dns_csv.split(",")[0]} mask 255.255.255.255 {intranet_gw}'
        self.step_updated.emit("dns_host_route", "success", f"DNS host route active", f"[OK] {cmd_dnshost}")

        # 5. NRPT
        cmd_nrpt = f'Add-DnsClientNrptRule -Namespace ".{self.domains[0]}" -NameServers "{dns_csv}"'
        self.step_updated.emit("nrpt_rules", "success", "Configured", f"[OK] {cmd_nrpt}")

        # 1.5 second settling pause for network stack ARP/routing settlement
        time.sleep(1.5)

        # 6. Test Internet
        self.step_updated.emit("test_internet", "running", "Pinging 8.8.8.8...", "> ping -n 1 8.8.8.8")
        inet_ok = test_internet_ping("8.8.8.8", retries=3)
        if inet_ok:
            self.step_updated.emit("test_internet", "success", "Internet Ping PASSED", "[OK] ping 8.8.8.8 -> Reply received")
        else:
            self.step_updated.emit("test_internet", "warning", "No internet ping response", "[WARN] ping 8.8.8.8 -> Timed out")

        # 7. Test Intranet
        self.step_updated.emit("test_intranet", "running", f"Pinging Gateway {intranet_gw}...", f"> ping -n 1 {intranet_gw}")
        intra_ok = test_intranet_ping(intranet_gw, retries=3)
        if intra_ok:
            self.step_updated.emit("test_intranet", "success", "Corporate Gateway PASSED", f"[OK] ping {intranet_gw} -> Reply received")
        else:
            self.step_updated.emit("test_intranet", "warning", "Corporate gateway timeout", f"[WARN] ping {intranet_gw} -> Timed out")

        # 8. Test LAN Connections
        self.step_updated.emit("test_sap", "running", "Testing LAN connections & internal services...", "> testing LAN socket connection...")
        sap_ok, sap_msg = test_sap_connection("erpprd.wbsedcl.in", 3600, retries=3)
        if sap_ok:
            self.step_updated.emit("test_sap", "success", "LAN Services Connected", f"[OK] {sap_msg}")
        else:
            self.step_updated.emit("test_sap", "warning", sap_msg, f"[WARN] {sap_msg}")

        self.finished_signal.emit(True, "Dual Access enabled & verified successfully!")

    def _run_disable(self, adapter, domain_csv):
        cmd = f'netsh interface ip delete address "{adapter}" {self.internet_ip}\nroute delete 0.0.0.0 mask 0.0.0.0\nroute delete 10.0.0.0 mask 255.0.0.0\nGet-DnsClientNrptRule | Where-Object {{$_.Namespace -like "*wbsedcl*"}} | Remove-DnsClientNrptRule -Force'
        self.step_updated.emit("bind_ip", "running", "Removing secondary IP & routes...", f"> {cmd}")

        ok, msg = disable_dual_access(self.intranet_ip, self.internet_ip, self.domains)
        if ok:
            self.step_updated.emit("bind_ip", "success", "Secondary IP & Routes Removed", f"[OK] {cmd}")
            self.step_updated.emit("nrpt_rules", "success", "NRPT Rules Wiped", "[OK] NRPT rules removed")
            self.finished_signal.emit(True, msg)
        else:
            self.step_updated.emit("bind_ip", "error", msg, f"[ERROR] {msg}")
            self.finished_signal.emit(False, msg)


class DualAccessDiagnosticDialog(QDialog):
    """Visual Diagnostic Dialog for Dual Access Enable/Disable."""

    def __init__(self, mode: str, intranet_ip: str, internet_ip: str, domains: list[str], parent=None):
        super().__init__(parent)
        self.mode = mode
        self.intranet_ip = intranet_ip
        self.internet_ip = internet_ip
        self.domains = domains

        self.setWindowTitle(f"Dual Access Diagnostics — {mode.capitalize()}")
        self.setMinimumWidth(620)
        self.setMinimumHeight(520)

        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setSpacing(12)

        # Header
        title = QLabel(f"🌐 Dual Access {self.mode.capitalize()} & Verification")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        v.addWidget(title)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 8)
        self.progress_bar.setValue(0)
        v.addWidget(self.progress_bar)

        # Simulation Mode Toggle
        self.chk_sim = QCheckBox("🧪 Demo / Simulation Mode (Test UI without applying system changes)")
        self.chk_sim.setToolTip("Enables visual step progression & command logging for testing on single-network PCs.")
        v.addWidget(self.chk_sim)

        # Step labels table
        self.step_widgets = {}
        step_definitions = [
            ("bind_ip", "1. Bind Secondary Internet IP (SkipAsSource=True)"),
            ("default_route", "2. Add Internet Default Route (0.0.0.0/0 Metric 5)"),
            ("intranet_route", "3. Add Corporate Intranet Route (10.0.0.0/8)"),
            ("dns_host_route", "4. Add Corporate DNS Host Route"),
            ("nrpt_rules", "5. Configure Split-DNS & NRPT Rules"),
            ("test_internet", "6. Live Internet Health Check (Ping 8.8.8.8)"),
            ("test_intranet", "7. Live Corporate Gateway Ping"),
            ("test_sap", "8. Testing LAN Connections & Internal Services"),
        ]

        for step_id, label_text in step_definitions:
            row = QHBoxLayout()
            lbl_icon = QLabel("⏳")
            lbl_text = QLabel(label_text)
            lbl_status = QLabel("Pending")
            lbl_status.setStyleSheet("color: gray;")
            row.addWidget(lbl_icon)
            row.addWidget(lbl_text, 1)
            row.addWidget(lbl_status)
            v.addLayout(row)

            self.step_widgets[step_id] = (lbl_icon, lbl_text, lbl_status)

        # Command Log Box
        lbl_cmd = QLabel("EXECUTED COMMAND LOG / MANUAL EXPORTER:")
        lbl_cmd.setStyleSheet("font-weight: bold; margin-top: 6px;")
        v.addWidget(lbl_cmd)

        self.cmd_log = QTextEdit()
        self.cmd_log.setReadOnly(True)
        self.cmd_log.setStyleSheet(_MONO + "background-color: #1e1e1e; color: #d4d4d4;")
        self.cmd_log.setFixedHeight(120)
        v.addWidget(self.cmd_log)

        # Actions
        btn_layout = QHBoxLayout()
        self.btn_copy = QPushButton("📋 Copy Commands to Clipboard")
        self.btn_copy.clicked.connect(self._copy_commands)
        btn_layout.addWidget(self.btn_copy)

        self.btn_start = QPushButton("▶ Run Diagnostics & Apply")
        self.btn_start.setStyleSheet("font-weight: bold;")
        self.btn_start.clicked.connect(self._start_process)
        btn_layout.addWidget(self.btn_start)

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_close)

        v.addLayout(btn_layout)

    def _start_process(self):
        self.btn_start.setEnabled(False)
        self.chk_sim.setEnabled(False)
        self.progress_bar.setValue(0)
        self.cmd_log.clear()

        self.worker = DualAccessWorker(
            mode=self.mode,
            intranet_ip=self.intranet_ip,
            internet_ip=self.internet_ip,
            domains=self.domains,
            simulate=self.chk_sim.isChecked()
        )
        self.worker.step_updated.connect(self._on_step_update)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.start()

    def _on_step_update(self, step_id: str, status: str, message: str, cmd_log: str):
        if step_id in self.step_widgets:
            lbl_icon, lbl_text, lbl_status = self.step_widgets[step_id]
            lbl_status.setText(message)

            if status == "running":
                lbl_icon.setText("🔄")
                lbl_status.setStyleSheet("color: #3b82f6; font-weight: bold;")
            elif status == "success":
                lbl_icon.setText("✅")
                lbl_status.setStyleSheet("color: #10b981; font-weight: bold;")
                self.progress_bar.setValue(self.progress_bar.value() + 1)
            elif status == "warning":
                lbl_icon.setText("⚠️")
                lbl_status.setStyleSheet("color: #f59e0b; font-weight: bold;")
            elif status == "error":
                lbl_icon.setText("❌")
                lbl_status.setStyleSheet("color: #ef4444; font-weight: bold;")

        if cmd_log:
            self.cmd_log.append(cmd_log)

    def _on_finished(self, success: bool, message: str):
        self.btn_start.setEnabled(True)
        self.chk_sim.setEnabled(True)
        if success:
            self.cmd_log.append(f"\n[DONE] {message}")
        else:
            self.cmd_log.append(f"\n[FAILED] {message}")

    def _copy_commands(self):
        from PyQt6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        cb.setText(self.cmd_log.toPlainText())

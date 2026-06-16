"""The main application window.

Hosts the Host / Client / LAN Chat tabs, the traffic monitor, the event log and
the system-tray integration, wiring together the service layer
(:mod:`nst.proxy_server`, :mod:`nst.beacon`, :mod:`nst.chat`, ...).
"""

import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import psutil

from .. import config
from ..beacon import ClientScanner, HostBeacon
from ..constants import MONO_FONT, PROXY_PORT, TITLE_FONT
from ..chat import ChatService
from ..netinfo import (
    calculate_gateway,
    check_host_reachable,
    check_internet_connection,
    check_internet_via_proxy,
    format_speed,
    get_intranet_ip,
    is_valid_ipv4,
)
from ..proxy_registry import clear_proxy, read_current_proxy, set_proxy
from ..proxy_server import ProxyServer
from ..routing import add_intranet_route, check_route_exists, delete_intranet_route
from ..theme import theme
from ..win_utils import get_resource_path, set_app_user_model_id
from .chat_view import ChatWindow
from .toast import ToastManager
from .tray import SpeedOverlay, get_tiny_font, make_chat_tray_icon, make_tray_icon
from .widgets import themed_button, themed_label

try:
    import pystray
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False


class App(tk.Tk):
    def __init__(self) -> None:
        set_app_user_model_id()
        super().__init__()

        theme.set_theme(config.load_theme())

        self.title("Net Split-Tunneler & Proxy Sharing Tool")
        self.resizable(True, True)
        self.minsize(560, 620)
        self.configure(bg=theme.color("bg"))
        theme.register(self, bg="bg")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        icon_ico = get_resource_path("icon.ico")
        if os.path.exists(icon_ico):
            try:
                self.iconbitmap(icon_ico)
            except Exception:
                pass

        # ── services ──────────────────────────────────────────────────────────
        self._host_has_internet = False
        self._proxy = ProxyServer()
        self._beacon = HostBeacon(lambda: self._host_has_internet)
        self._scanner = ClientScanner(self._on_beacon_received)
        self._chat = ChatService(
            config.load_display_name(),
            on_roster_change=self._on_roster_change,
            on_message=self._on_chat_message,
            on_file_offer=self._on_file_offer,
            on_file_accept=self._on_file_accept,
            on_file_reject=self._on_file_reject,
            on_chat_request=self._on_chat_request_received,
        )
        self._chat.ip_chat_enabled = config.load_ip_chat_enabled()

        # Persisted state on startup.
        self._route_active = check_route_exists()
        proxy_on, proxy_server = read_current_proxy()
        self._client_connected = proxy_on
        self._client_proxy_host = proxy_server.split(":")[0] if proxy_server else ""

        self._detected_ip: str | None = None
        self._detected_gw: str | None = None
        self._tray = None
        self._chat_tray = None
        self._current_tab = "host"
        self._overlay = SpeedOverlay()
        self._tiny_font = get_tiny_font(9)
        self._tray_notified = False   # balloon shown once on minimize

        self._last_net_bytes = psutil.net_io_counters()
        self._last_net_time = time.time()

        self._build_menu()
        self._build_ui()
        self._toasts = ToastManager(self, on_click=self._open_chat_window)

        # Restyle dynamic widgets whenever the theme flips.
        theme.on_change(self._on_theme_changed)
        self._apply_ttk_style()

        # Recover button labels from persisted state.
        self._update_route_btn()
        self._update_proxy_btn()
        self._update_client_btn()
        if self._client_connected and self._client_proxy_host:
            self._lbl_client_status.config(
                text=f"Status  :  CONNECTED  →  {self._client_proxy_host}:{PROXY_PORT}",
                fg=theme.color("success"))

        self._scanner.start()
        self._chat.start()
        threading.Thread(target=self._internet_check_loop, daemon=True).start()

        if self._show_speed_in_taskbar_var.get():
            self._start_tray()

        self._update_traffic_speed()
        self.after(50, self._poll_status)
        self._log_msg("Application started.  Administrator ✓")
        if self._route_active:
            self._log_msg("Existing 10.0.0.0 route detected — marked ACTIVE.")
        if self._client_connected:
            self._log_msg(f"Existing proxy detected: {proxy_server} — marked CONNECTED.")

    # ── MENU ──────────────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        self.config(menu=menu_bar)

        file_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Exit", command=self._quit_app)

        settings_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Settings", menu=settings_menu)

        self._autostart_var = tk.BooleanVar(value=config.is_autostart_enabled())
        settings_menu.add_checkbutton(
            label="Start with Windows", variable=self._autostart_var,
            command=self._toggle_autostart)

        self._show_speed_in_taskbar_var = tk.BooleanVar(
            value=config.load_show_speed_in_taskbar())
        settings_menu.add_checkbutton(
            label="Show Speed in Taskbar", variable=self._show_speed_in_taskbar_var,
            command=self._toggle_show_speed_in_taskbar)

        settings_menu.add_separator()
        self._theme_light_var = tk.BooleanVar(value=not theme.is_dark())
        settings_menu.add_checkbutton(
            label="Light theme", variable=self._theme_light_var,
            command=self._toggle_theme)

        chat_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Chat", menu=chat_menu)
        chat_menu.add_command(label="Open LAN Chat", command=self._open_chat_window)
        chat_menu.add_command(label="Run Chat Demo", command=self._run_demo)

        about_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="About", menu=about_menu)
        about_menu.add_command(label="About Net Split-Tunneler",
                               command=self._show_about_dialog)

    # ── UI BUILD ──────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self, bg=theme.color("bg"))
        theme.register(hdr, bg="bg")
        hdr.pack(fill="x", padx=20, pady=10)
        themed_label(hdr, "⬡  NET SPLIT-TUNNELER", color_role="accent",
                     font=("Consolas", 13, "bold"), bg_role="bg").pack(side="left")
        themed_label(hdr, "& Proxy Sharing Tool  v4.2", color_role="text_sec",
                     font=("Segoe UI", 9), bg_role="bg").pack(side="left", padx=8, pady=4)

        # Quick theme toggle button (☀/🌙)
        self._theme_btn = tk.Button(
            hdr, text=("☀" if theme.is_dark() else "🌙"), command=self._toggle_theme,
            font=("Segoe UI", 11), relief="flat", cursor="hand2", bd=0,
            bg=theme.color("bg"), fg=theme.color("text_sec"),
            activebackground=theme.color("bg"), activeforeground=theme.color("accent"))
        theme.register(self._theme_btn, bg="bg", activebackground="bg",
                       fg="text_sec", activeforeground="accent")
        self._theme_btn.pack(side="right")

        # Tab bar
        tab_bar = tk.Frame(self, bg=theme.color("bg"))
        theme.register(tab_bar, bg="bg")
        tab_bar.pack(fill="x", padx=20, pady=(5, 10))

        self._btn_tab_host = self._make_tab_button(tab_bar, "Host Mode",
                                                   self._show_host_tab)
        self._btn_tab_host.pack(side="left", padx=(0, 6))
        self._btn_tab_client = self._make_tab_button(tab_bar, "Client Mode",
                                                     self._show_client_tab)
        self._btn_tab_client.pack(side="left", padx=(0, 6))

        # LAN Chat opens in its own standalone window (a launcher, not a tab).
        self._btn_open_chat = themed_button(tab_bar, "💬  LAN Chat",
                                            self._open_chat_window,
                                            color_role="accent", width=14)
        self._btn_open_chat.pack(side="right")

        # Footer is pinned to the bottom first so it can never be clipped.
        self._build_footer()

        # Host/Client share a fixed-height container; both panels below are
        # always visible (the window no longer changes size between tabs).
        self._tab_container = tk.Frame(self, bg=theme.color("bg"), height=215)
        theme.register(self._tab_container, bg="bg")
        self._tab_container.pack(fill="x", padx=20, pady=(0, 10))
        self._tab_container.pack_propagate(False)

        self._build_host_tab()
        self._build_client_tab()
        self._build_traffic_monitor()
        self._build_log()
        self._traffic_frame.pack(fill="x", padx=20, pady=(0, 10))
        self._lf_log.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # The chat lives in a standalone, resizable window created up front
        # (hidden) so chat callbacks always have a target and history persists.
        self._chat_window = ChatWindow(self, self._chat, log_fn=self._log_msg)
        self._chat_view = self._chat_window.view

        self._show_host_tab()

    def _make_tab_button(self, parent, text, command) -> tk.Button:
        btn = tk.Button(
            parent, text=text, command=command,
            font=("Consolas", 9, "bold"), relief="flat", cursor="hand2",
            bd=0, padx=16, pady=6)
        btn.bind("<Enter>", lambda e: self._tab_hover(btn, True))
        btn.bind("<Leave>", lambda e: self._tab_hover(btn, False))
        return btn

    def _tab_hover(self, btn, entering) -> None:
        # Only brighten inactive tabs (active tab uses the panel background).
        if btn["bg"] != theme.color("panel"):
            btn.config(fg=theme.color("text_pri") if entering else theme.color("text_sec"))

    def _build_host_tab(self) -> None:
        self._hf = tk.LabelFrame(
            self._tab_container, text="  HOST MODE  —  Internet Provider  ",
            bg=theme.color("panel"), fg=theme.color("accent"), font=TITLE_FONT,
            bd=1, relief="solid", labelanchor="nw")
        theme.register(self._hf, bg="panel", fg="accent")

        sb = tk.Frame(self._hf, bg=theme.color("panel"))
        theme.register(sb, bg="panel")
        sb.pack(fill="x", padx=12, pady=(8, 4))
        self._lbl_ip = themed_label(sb, "Intranet IP   :  —", color_role="text_sec", font=MONO_FONT)
        self._lbl_gw = themed_label(sb, "Gateway       :  —", color_role="text_sec", font=MONO_FONT)
        self._lbl_proxy = themed_label(sb, "Proxy         :  STOPPED", color_role="danger", font=MONO_FONT)
        self._lbl_route = themed_label(sb, "LAN+NET Route :  INACTIVE", color_role="text_sec", font=MONO_FONT)
        self._lbl_internet = themed_label(sb, "Internet      :  CHECKING...", color_role="warning", font=MONO_FONT)
        for w in (self._lbl_ip, self._lbl_gw, self._lbl_proxy, self._lbl_route, self._lbl_internet):
            w.pack(anchor="w")

        divider = tk.Frame(self._hf, bg=theme.color("border"), height=1)
        theme.register(divider, bg="border")
        divider.pack(fill="x", padx=12, pady=6)

        btn_row = tk.Frame(self._hf, bg=theme.color("panel"))
        theme.register(btn_row, bg="panel")
        btn_row.pack(padx=12, pady=(4, 12))
        self._btn_route = themed_button(btn_row, "▶  Enable LAN+NET", self._toggle_route,
                                        color_role="success_btn")
        self._btn_route.pack(side="left", padx=6)
        self._btn_proxy = themed_button(btn_row, "▶  Start Proxy Server", self._toggle_proxy,
                                        color_role="proxy_btn")
        self._btn_proxy.pack(side="left", padx=6)

    def _build_client_tab(self) -> None:
        self._cf = tk.LabelFrame(
            self._tab_container, text="  CLIENT MODE  —  Internet Consumer  ",
            bg=theme.color("panel"), fg=theme.color("accent2_text"), font=TITLE_FONT,
            bd=1, relief="solid", labelanchor="nw")
        theme.register(self._cf, bg="panel", fg="accent2_text")

        ip_row = tk.Frame(self._cf, bg=theme.color("panel"))
        theme.register(ip_row, bg="panel")
        ip_row.pack(fill="x", padx=12, pady=(12, 4))
        themed_label(ip_row, "Host IP:").pack(side="left")
        self._host_ip_var = tk.StringVar(value=self._client_proxy_host)
        self._host_entry = tk.Entry(
            ip_row, textvariable=self._host_ip_var, font=MONO_FONT, width=18,
            bg=theme.color("entry_bg"), fg=theme.color("text_pri"),
            insertbackground=theme.color("text_pri"), relief="flat", bd=4)
        theme.register(self._host_entry, bg="entry_bg", fg="text_pri",
                       insertbackground="text_pri")
        self._host_entry.pack(side="left", padx=8)
        self._lbl_scan = themed_label(ip_row, "⟳ scanning…", color_role="text_sec",
                                      font=("Consolas", 8))
        self._lbl_scan.pack(side="left", padx=4)

        self._lbl_client_status = themed_label(self._cf, "Status  :  DISCONNECTED",
                                               color_role="text_sec", font=MONO_FONT)
        self._lbl_client_status.pack(anchor="w", padx=12, pady=(2, 4))

        self._disable_if_no_internet_var = tk.BooleanVar(value=False)
        self._chk_disable = tk.Checkbutton(
            self._cf, text="Disable proxy if host has no internet / unreachable",
            variable=self._disable_if_no_internet_var,
            bg=theme.color("panel"), fg=theme.color("text_sec"),
            activebackground=theme.color("panel"), activeforeground=theme.color("text_pri"),
            selectcolor=theme.color("bg"), font=("Segoe UI", 9), bd=0, highlightthickness=0)
        theme.register(self._chk_disable, bg="panel", fg="text_sec",
                       activebackground="panel", activeforeground="text_pri",
                       selectcolor="bg")
        self._chk_disable.pack(anchor="w", padx=12, pady=(0, 4))

        c_btn_row = tk.Frame(self._cf, bg=theme.color("panel"))
        theme.register(c_btn_row, bg="panel")
        c_btn_row.pack(padx=12, pady=(4, 12))
        self._btn_client = themed_button(c_btn_row, "⬡  Connect to Host Proxy",
                                         self._toggle_client, color_role="accent")
        self._btn_client.pack()

    def _build_traffic_monitor(self) -> None:
        self._traffic_frame = tk.LabelFrame(
            self, text="  NETWORK TRAFFIC MONITOR  ",
            bg=theme.color("panel"), fg=theme.color("accent"), font=TITLE_FONT,
            bd=1, relief="solid", labelanchor="nw")
        theme.register(self._traffic_frame, bg="panel", fg="accent")
        # Packing is managed in _build_ui().

        tf_row = tk.Frame(self._traffic_frame, bg=theme.color("panel"))
        theme.register(tf_row, bg="panel")
        tf_row.pack(fill="x", padx=12, pady=6)
        self._lbl_down_speed = themed_label(tf_row, "Download  :  0.0 KB/s",
                                            color_role="success", font=MONO_FONT)
        self._lbl_down_speed.pack(side="left", expand=True, fill="x")
        self._lbl_up_speed = themed_label(tf_row, "Upload    :  0.0 KB/s",
                                          color_role="warning", font=MONO_FONT)
        self._lbl_up_speed.pack(side="left", expand=True, fill="x")

    def _build_log(self) -> None:
        self._lf_log = tk.LabelFrame(self, text="  EVENT LOG  ",
                                     bg=theme.color("panel"), fg=theme.color("text_sec"),
                                     font=TITLE_FONT, bd=1, relief="solid")
        theme.register(self._lf_log, bg="panel", fg="text_sec")
        # Packing is managed in _build_ui().

        self._log = tk.Text(self._lf_log, height=5, bg=theme.color("log_bg"),
                            fg=theme.color("text_pri"), font=MONO_FONT, relief="flat",
                            state="disabled", wrap="word", bd=6)
        theme.register(self._log, bg="log_bg", fg="text_pri")
        self._log_scroll = ttk.Scrollbar(self._lf_log, command=self._log.yview)
        self._log.configure(yscrollcommand=self._log_scroll.set)
        self._log_scroll.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True)

    def _build_footer(self) -> None:
        self._footer = tk.Frame(self, bg=theme.color("bg"))
        theme.register(self._footer, bg="bg")
        self._footer.pack(fill="x", side="bottom", padx=20, pady=(2, 6))
        themed_label(self._footer, "Copyright © Pramod Verma", color_role="text_sec",
                     font=("Segoe UI", 8), bg_role="bg").pack(side="right")
        themed_label(self._footer, "v4.1", color_role="text_sec",
                     font=("Segoe UI", 8), bg_role="bg").pack(side="left")

    # ── THEME ─────────────────────────────────────────────────────────────────
    def _apply_ttk_style(self) -> None:
        try:
            style = ttk.Style(self)
            style.theme_use("default")
            style.configure("Vertical.TScrollbar",
                            background=theme.color("border"),
                            troughcolor=theme.color("panel"),
                            bordercolor=theme.color("panel"),
                            arrowcolor=theme.color("text_sec"))
        except Exception:
            pass

    def _on_chat_request_received(self, ip: str, name: str, msg: dict) -> None:
        def _apply():
            self._chat_view.on_chat_request_received(ip, name, msg)
            self._toasts.notify(name, "Wants to chat — tap to respond", ip)
            self._open_chat_window(ip)
        try:
            self.after(0, _apply)
        except (RuntimeError, tk.TclError):
            pass

    def _toggle_theme(self) -> None:
        name = theme.toggle()
        config.save_theme(name)
        self._theme_light_var.set(name == "light")
        self._theme_btn.config(text=("☀" if theme.is_dark() else "🌙"))
        self._log_msg(f"Theme switched to {name}.")

    def _on_theme_changed(self) -> None:
        """Re-apply colors that depend on state (not a fixed role)."""
        self._apply_ttk_style()
        self._update_route_btn()
        self._update_proxy_btn()
        self._update_client_btn()
        self._update_tabs()

    def _update_tabs(self) -> None:
        active = theme.color("panel")
        inactive = theme.color("bg")
        mapping = {
            "host": (self._btn_tab_host, "accent"),
            "client": (self._btn_tab_client, "accent2_text"),
        }
        for name, (btn, active_role) in mapping.items():
            if name == self._current_tab:
                btn.config(bg=active, fg=theme.color(active_role),
                           activebackground=active, activeforeground=theme.color(active_role))
            else:
                btn.config(bg=inactive, fg=theme.color("text_sec"),
                           activebackground=inactive, activeforeground=theme.color("text_sec"))

    # ── ABOUT ─────────────────────────────────────────────────────────────────
    def _show_about_dialog(self) -> None:
        win = tk.Toplevel(self)
        win.title("About Net Split-Tunneler")
        win.resizable(False, False)
        win.configure(bg=theme.color("bg"))
        win.transient(self)
        win.grab_set()
        win.update_idletasks()
        w, h = 400, 350
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")

        frame = tk.Frame(win, bg=theme.color("panel"), bd=1, relief="solid")
        frame.pack(fill="both", expand=True, padx=15, pady=15)
        tk.Label(frame, text="⬡", bg=theme.color("panel"), fg=theme.color("accent"),
                 font=("Segoe UI", 32)).pack(pady=(10, 2))
        tk.Label(frame, text="Net Split-Tunneler v4.1", bg=theme.color("panel"),
                 fg=theme.color("text_pri"), font=("Segoe UI", 12, "bold")).pack()
        tk.Label(frame, text="Proxy Sharing Tool + LAN Chat", bg=theme.color("panel"),
                 fg=theme.color("text_sec"), font=("Segoe UI", 9, "italic")).pack(pady=(0, 10))
        desc = ("A lightweight Windows utility to split-tunnel local traffic, "
                "share a proxy connection, and chat across the LAN.\n\n"
                "Developed by Pramod Verma")
        tk.Label(frame, text=desc, bg=theme.color("panel"), fg=theme.color("text_pri"),
                 font=("Segoe UI", 9), justify="center", wraplength=300).pack(padx=10)
        tk.Button(frame, text="Close", command=win.destroy,
                  font=("Consolas", 9, "bold"), relief="flat", cursor="hand2",
                  bg=theme.color("accent"), fg=theme.color("text_pri"), bd=0,
                  padx=20, pady=4).pack(side="bottom", pady=15)

    # ── TRAFFIC ───────────────────────────────────────────────────────────────
    def _update_traffic_speed(self) -> None:
        try:
            curr_bytes = psutil.net_io_counters()
            curr_time = time.time()
            dt = curr_time - self._last_net_time
            if dt > 0:
                up = (curr_bytes.bytes_sent - self._last_net_bytes.bytes_sent) / dt
                down = (curr_bytes.bytes_recv - self._last_net_bytes.bytes_recv) / dt
                up_fmt, down_fmt = format_speed(up), format_speed(down)
                self._lbl_down_speed.config(text=f"Download  :  {down_fmt}")
                self._lbl_up_speed.config(text=f"Upload    :  {up_fmt}")

                if self._show_speed_in_taskbar_var.get():
                    self._overlay.show(up_fmt, down_fmt)
                    self._start_tray()
                    if self._tray:
                        self._tray.icon = make_tray_icon()
                        self._tray.title = f"Net Split-Tunneler  Proxy\nUp: {up_fmt}\nDown: {down_fmt}"
                else:
                    self._overlay.hide()
                    self._sync_idle_tray()
            self._last_net_bytes = curr_bytes
            self._last_net_time = curr_time
        except Exception:
            pass
        self.after(1000, self._update_traffic_speed)

    def _sync_idle_tray(self) -> None:
        if self.winfo_viewable() and self._tray:
            self._tray.stop()
            self._tray = None
        elif not self.winfo_viewable() and self._tray:
            self._tray.icon = make_tray_icon()
            self._tray.title = "Net Split-Tunneler  Proxy\n(running in background)"

    # ── LOGGING ───────────────────────────────────────────────────────────────
    def _log_msg(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log.configure(state="normal")
        self._log.insert("end", f"[{ts}]  {msg}\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    # ── BUTTON STATE UPDATERS ─────────────────────────────────────────────────
    def _update_route_btn(self) -> None:
        if self._route_active:
            self._btn_route.config(text="■  Disable LAN+NET", bg=theme.color("danger"),
                                   activebackground=theme.color("danger"))
            self._lbl_route.config(text="LAN+NET Route :  ACTIVE", fg=theme.color("success"))
        else:
            self._btn_route.config(text="▶  Enable LAN+NET", bg=theme.color("success_btn"),
                                   activebackground=theme.color("success_btn"))
            self._lbl_route.config(text="LAN+NET Route :  INACTIVE", fg=theme.color("text_sec"))

    def _update_proxy_btn(self) -> None:
        if self._proxy.running:
            self._btn_proxy.config(text="■  Stop Proxy Server", bg=theme.color("danger"),
                                   activebackground=theme.color("danger"))
            self._lbl_proxy.config(text=f"Proxy         :  RUNNING  (:{PROXY_PORT})",
                                   fg=theme.color("success"))
        else:
            self._btn_proxy.config(text="▶  Start Proxy Server", bg=theme.color("proxy_btn"),
                                   activebackground=theme.color("proxy_btn"))
            self._lbl_proxy.config(text="Proxy         :  STOPPED", fg=theme.color("danger"))

    def _update_client_btn(self) -> None:
        if self._client_connected:
            self._btn_client.config(text="✕  Disconnect from Proxy",
                                    bg=theme.color("danger"),
                                    activebackground=theme.color("danger"))
        else:
            self._btn_client.config(text="⬡  Connect to Host Proxy",
                                    bg=theme.color("accent"),
                                    activebackground=theme.color("accent"))

    # ── POLL LOOP ─────────────────────────────────────────────────────────────
    def _poll_status(self) -> None:
        ip = get_intranet_ip()
        if ip:
            gw = calculate_gateway(ip)
            self._lbl_ip.config(text=f"Intranet IP   :  {ip}", fg=theme.color("success"))
            self._lbl_gw.config(text=f"Gateway       :  {gw}", fg=theme.color("text_pri"))
            self._detected_ip = ip
            self._detected_gw = gw
            if self._beacon.running:
                self._beacon.ip = ip
        else:
            self._lbl_ip.config(text="Intranet IP   :  Not detected", fg=theme.color("warning"))
            self._lbl_gw.config(text="Gateway       :  —", fg=theme.color("text_sec"))
            self._detected_ip = None
            self._detected_gw = None

        self._update_proxy_btn()
        self._update_route_btn()

        if self._client_connected and self._disable_if_no_internet_var.get():
            threading.Thread(target=self._client_health_check, daemon=True).start()

        self.after(3000, self._poll_status)

    # ── HOST TOGGLES ──────────────────────────────────────────────────────────
    def _toggle_route(self) -> None:
        if self._route_active:
            ok, msg = delete_intranet_route()
            if ok:
                self._route_active = False
        else:
            if not self._detected_gw:
                messagebox.showerror("No Intranet IP",
                                     "Cannot detect a 10.x.x.x address on this machine.")
                return
            ok, msg = add_intranet_route(self._detected_gw)
            if ok:
                self._route_active = True
        self._log_msg(msg)
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
        self._log_msg(msg)
        self._update_proxy_btn()

    # ── CLIENT TOGGLE ─────────────────────────────────────────────────────────
    def _toggle_client(self) -> None:
        if self._client_connected:
            ok, msg = clear_proxy()
            if ok:
                self._client_connected = False
                self._lbl_client_status.config(text="Status  :  DISCONNECTED",
                                               fg=theme.color("text_sec"))
        else:
            host = self._host_ip_var.get().strip()
            if not host:
                messagebox.showerror("Missing IP",
                                     "Enter the Host IP address or wait for auto-detect.")
                return
            if not is_valid_ipv4(host):
                messagebox.showerror("Invalid IP", f"'{host}' is not a valid IPv4 address.")
                return
            ok, msg = set_proxy(host, PROXY_PORT)
            if ok:
                self._client_connected = True
                self._client_proxy_host = host
                self._lbl_client_status.config(
                    text=f"Status  :  CONNECTED  →  {host}:{PROXY_PORT}",
                    fg=theme.color("success"))
        self._log_msg(msg)
        self._update_client_btn()

    def _toggle_autostart(self) -> None:
        ok, msg = config.set_autostart(self._autostart_var.get())
        self._log_msg(msg)
        if not ok:
            messagebox.showerror("Registry Error", msg)

    def _toggle_show_speed_in_taskbar(self) -> None:
        enabled = self._show_speed_in_taskbar_var.get()
        if config.save_show_speed_in_taskbar(enabled):
            self._log_msg(f"Show Speed in Taskbar {'enabled' if enabled else 'disabled'}.")
            if enabled:
                self._start_tray()
            else:
                self._overlay.hide()
                self._sync_idle_tray()
        else:
            self._log_msg("Failed to save Show Speed in Taskbar setting.")
            messagebox.showerror("Registry Error", "Failed to save settings to registry.")

    # ── TAB SWITCHING ─────────────────────────────────────────────────────────
    def _show_host_tab(self) -> None:
        self._current_tab = "host"
        self._cf.pack_forget()
        self._hf.pack(fill="both", expand=True)
        self._update_tabs()

    def _show_client_tab(self) -> None:
        self._current_tab = "client"
        self._hf.pack_forget()
        self._cf.pack(fill="both", expand=True)
        self._update_tabs()

    # ── CLIENT HEALTH / INTERNET ──────────────────────────────────────────────
    def _client_health_check(self) -> None:
        if not self._client_connected or not self._disable_if_no_internet_var.get():
            return
        host = self._client_proxy_host
        if not host:
            return
        host_ok = check_host_reachable(host, PROXY_PORT)
        internet_ok = check_internet_via_proxy(host, PROXY_PORT) if host_ok else False
        if not host_ok or not internet_ok:
            def _disable():
                if self._client_connected:
                    ok, _msg = clear_proxy()
                    if ok:
                        self._client_connected = False
                        reason = "Host unreachable" if not host_ok else "No Internet access through proxy"
                        self._lbl_client_status.config(
                            text=f"Status  :  DISCONNECTED ({reason})",
                            fg=theme.color("warning"))
                        self._update_client_btn()
                        self._log_msg(f"Client proxy disabled automatically: {reason}.")
            try:
                self.after(0, _disable)
            except Exception:
                pass

    def _internet_check_loop(self) -> None:
        while True:
            res = check_internet_connection()
            def _update():
                self._host_has_internet = res
                if res:
                    self._lbl_internet.config(text="Internet      :  CONNECTED",
                                              fg=theme.color("success"))
                else:
                    self._lbl_internet.config(text="Internet      :  NO CONNECTION",
                                              fg=theme.color("danger"))
            try:
                self.after(0, _update)
            except Exception:
                pass
            time.sleep(3)

    # ── HOST BEACON (client auto-detect) ──────────────────────────────────────
    def _on_beacon_received(self, ip: str, has_internet: bool) -> None:
        def _apply():
            current = self._host_ip_var.get().strip()
            status = "Internet OK" if has_internet else "No Internet"
            color = theme.color("success") if has_internet else theme.color("danger")
            self._lbl_scan.config(text=f"✓ host: {ip} ({status})", fg=color)
            if current != ip:
                self._host_ip_var.set(ip)
                self._log_msg(f"Host beacon detected: {ip} — IP auto-filled.")
            if self._client_connected and self._client_proxy_host == ip:
                if not has_internet and self._disable_if_no_internet_var.get():
                    ok, _msg = clear_proxy()
                    if ok:
                        self._client_connected = False
                        self._lbl_client_status.config(
                            text="Status  :  DISCONNECTED (Host lost internet)",
                            fg=theme.color("warning"))
                        self._update_client_btn()
                        self._log_msg("Client proxy disabled automatically: "
                                      "Host has no internet connection.")
        self.after(0, _apply)

    # ── CHAT CALLBACKS (from service threads → main thread) ───────────────────
    def _on_roster_change(self, peers) -> None:
        try:
            self.after(0, lambda: self._chat_view.update_roster(peers))
        except (RuntimeError, tk.TclError):
            pass  # window tearing down

    def _on_chat_message(self, ip: str, name: str, text: str, ts: float) -> None:
        def _apply():
            shown = self._chat_view.receive_message(ip, name, text, ts)
            if not shown:
                preview = text if len(text) <= 120 else text[:117] + "…"
                self._toasts.notify(name, preview, ip)
                # Auto-open the chat window so the user never misses a message.
                self._open_chat_window(ip)
        try:
            self.after(0, _apply)
        except (RuntimeError, tk.TclError):
            pass

    def _on_file_offer(self, ip: str, name: str, msg: dict) -> None:
        def _apply():
            shown = self._chat_view.on_file_offer_received(ip, name, msg)
            if not shown:
                filename = msg.get("filename", "file")
                self._toasts.notify(name, f"📎 Wants to send: {filename}", ip)
                self._open_chat_window(ip)
        try:
            self.after(0, _apply)
        except (RuntimeError, tk.TclError):
            pass

    def _on_file_accept(self, ip: str, name: str, msg: dict) -> None:
        try:
            self.after(0, lambda: self._chat_view.on_file_accepted(ip, name, msg))
        except (RuntimeError, tk.TclError):
            pass

    def _on_file_reject(self, ip: str, name: str, msg: dict) -> None:
        try:
            self.after(0, lambda: self._chat_view.on_file_rejected(ip, name, msg))
        except (RuntimeError, tk.TclError):
            pass

    def _open_chat_window(self, select_ip: str | None = None) -> None:
        """Open (or focus) the standalone chat window, optionally on a peer."""
        self._chat_window.open(select_ip)

    def _run_demo(self) -> None:
        self._chat_window.open()
        self._chat_view._start_demo()

    # ── SYSTEM TRAY ───────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        if not HAS_TRAY:
            self._quit_app()
            return
        self.withdraw()
        self._start_tray()
        # Notify the user once that the app is still running.
        if not self._tray_notified and self._tray:
            self._tray_notified = True
            try:
                self._tray.notify(
                    "Still running",
                    "Net Split-Tunneler is running in the background.\n"
                    "Use the tray icons to open Proxy or Chat.",
                )
            except Exception:
                pass

    def _start_tray(self) -> None:
        # ── Proxy tray icon (blue ring / app icon) ────────────────────────────
        if self._tray is None and HAS_TRAY:
            proxy_menu = pystray.Menu(
                pystray.MenuItem("Open Proxy", self._open_proxy_from_tray, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit_app),
            )
            self._tray = pystray.Icon("NetSplitTunnel_Proxy", make_tray_icon(),
                                      "Net Split-Tunneler  Proxy", proxy_menu)
            threading.Thread(target=self._tray.run, daemon=True).start()

        # ── Chat tray icon (green speech bubble) ──────────────────────────────
        if self._chat_tray is None and HAS_TRAY:
            chat_menu = pystray.Menu(
                pystray.MenuItem("Open Chat", self._open_chat_from_tray, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit_app),
            )
            self._chat_tray = pystray.Icon("NetSplitTunnel_Chat",
                                           make_chat_tray_icon(),
                                           "LAN Chat", chat_menu)
            threading.Thread(target=self._chat_tray.run, daemon=True).start()

    def _open_proxy_from_tray(self, icon=None, item=None) -> None:
        """Restore the main proxy window from the tray."""
        if self._tray and not self._show_speed_in_taskbar_var.get():
            self._tray.stop()
            self._tray = None
        if self._chat_tray and not self._show_speed_in_taskbar_var.get():
            self._chat_tray.stop()
            self._chat_tray = None
        self.after(0, self.deiconify)

    def _open_chat_from_tray(self, icon=None, item=None) -> None:
        """Open the chat window directly from the tray."""
        self.after(0, lambda: self._open_chat_window())

    def _restore_from_tray(self, icon=None, item=None) -> None:
        if self._tray and not self._show_speed_in_taskbar_var.get():
            self._tray.stop()
            self._tray = None
        if self._chat_tray and not self._show_speed_in_taskbar_var.get():
            self._chat_tray.stop()
            self._chat_tray = None
        self.after(0, self.deiconify)

    def _quit_app(self, icon=None, item=None) -> None:
        if self._proxy.running:
            self._proxy.stop()
        self._beacon.stop()
        self._scanner.stop()
        self._chat.stop()
        self._overlay.destroy()
        self._toasts.destroy_all()
        if self._tray:
            self._tray.stop()
        if self._chat_tray:
            self._chat_tray.stop()
        self.after(0, self.destroy)

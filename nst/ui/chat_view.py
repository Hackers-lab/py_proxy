"""The LAN Chat tab.

Left:  your identity + a roster of online peers (avatars, unread badges).
Right: a scrolling message-bubble conversation + a composer.

Conversations are kept in memory, one list per peer IP, so several chats run at
once. Incoming messages for an inactive chat bump an unread badge; the app layer
decides whether to also raise a bottom-right toast.
"""

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from .. import config
from ..chat import DemoBot
from ..constants import CHAT_TCP_PORT, FILE_SAVE_DIR, LABEL_FONT, TITLE_FONT
from ..filetransfer import FileTransferService
from ..netinfo import check_host_reachable, is_valid_ipv4
from ..theme import theme
from ..win_utils import get_resource_path
from .widgets import (
    ScrollFrame,
    make_avatar,
    themed_button,
    themed_label,
)


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def _fmt_speed(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


def _fmt_eta(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"

_PLACEHOLDER = "Type a message..."
_MAX_HISTORY_PER_PEER = 200


class _ToggleSwitch(tk.Canvas):
    """Small on/off toggle switch drawn on a Canvas."""
    _W, _H = 36, 18

    def __init__(self, parent, initial: bool = True, command=None, bg_role: str = "panel"):
        self._bg_role = bg_role
        super().__init__(parent, width=self._W, height=self._H,
                         bg=theme.color(bg_role), highlightthickness=0, cursor="hand2")
        theme.register(self, bg=bg_role)
        self._on = initial
        self._cmd = command
        self.bind("<ButtonRelease-1>", self._click)

    def _draw(self) -> None:
        self.delete("all")
        track = "#4CAF50" if self._on else "#9E9E9E"
        r = self._H // 2
        # Rounded track via smooth polygon
        pts = [r, 0, self._W - r, 0, self._W, 0, self._W, r,
               self._W, self._H - r, self._W, self._H,
               self._W - r, self._H, r, self._H,
               0, self._H, 0, self._H - r, 0, r, 0, 0]
        self.create_polygon(pts, smooth=True, fill=track, outline="")
        # Thumb
        pad = 2
        x = self._W - self._H + pad if self._on else pad
        self.create_oval(x, pad, x + self._H - pad * 2, self._H - pad,
                         fill="white", outline="")

    def _click(self, _e=None) -> None:
        self._on = not self._on
        self._draw()
        if self._cmd:
            self._cmd(self._on)

    def set(self, value: bool) -> None:
        self._on = value
        self._draw()

    def pack(self, **kw):
        super().pack(**kw)
        self._draw()   # draw after widget is placed so canvas size is finalised

    def grid(self, **kw):
        super().grid(**kw)
        self._draw()


def _status_dot(parent, online: bool, bg_role: str) -> tk.Canvas:
    """Return a small filled circle (green = online, red = offline)."""
    color = "#4CAF50" if online else "#E53935"
    c = tk.Canvas(parent, width=8, height=8,
                  bg=theme.color(bg_role), highlightthickness=0)
    theme.register(c, bg=bg_role)
    c.create_oval(1, 1, 7, 7, fill=color, outline="")
    return c


class ChatWindow(tk.Toplevel):
    """A standalone, resizable window that hosts the :class:`ChatView`.

    Created once and hidden; closing the window only withdraws it so open
    conversations survive. Visibility is tracked so the app knows when an
    incoming message should be shown in place versus raised as a toast.
    """

    def __init__(self, master, chat_service, log_fn=lambda m: None) -> None:
        super().__init__(master)
        self.title("LAN Chat  —  Net Split-Tunneler")
        self.configure(bg=theme.color("bg"))
        theme.register(self, bg="bg")
        self.geometry("760x540")
        self.minsize(620, 440)
        icon_ico = get_resource_path("icon.ico")
        if os.path.exists(icon_ico):
            try:
                self.iconbitmap(icon_ico)
            except Exception:
                pass

        self.view = ChatView(self, chat_service, log_fn=log_fn)
        self.view.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.bind("<FocusIn>", lambda e: self.view.set_visible(True))
        self.bind("<FocusOut>", lambda e: self.view.set_visible(False))
        self.bind("<Unmap>", lambda e: self.view.set_visible(False))
        self.withdraw()
        self._placed = False

    def open(self, select_ip: str | None = None) -> None:
        if not self._placed:
            self._center_on_master()
            self._placed = True
        self.deiconify()
        self.lift()
        self.focus_force()
        self.view.set_visible(True)
        if select_ip:
            self.view.select_peer(select_ip)

    def hide(self) -> None:
        self.view.set_visible(False)
        self.withdraw()

    def _center_on_master(self) -> None:
        try:
            self.update_idletasks()
            m = self.master
            w, h = 760, 540
            x = m.winfo_x() + (m.winfo_width() - w) // 2
            y = m.winfo_y() + (m.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass


class ChatView(tk.Frame):
    def __init__(self, parent, chat_service, log_fn=lambda m: None,
                 on_name_change=lambda n: None) -> None:
        super().__init__(parent, bg=theme.color("panel"))
        theme.register(self, bg="panel")
        self.chat = chat_service
        self._log = log_fn
        self._on_name_change = on_name_change

        # ip -> list[ (kind, ...) ]
        self._conversations: dict[str, list[tuple]] = {}
        self._names: dict[str, str] = {}
        self._unread: dict[str, int] = {}
        self._active_ip: str | None = None
        self._visible = False
        self._placeholder_on = True
        # Last set of online IPs rendered — lets the periodic tick skip rebuilds
        # when nothing changed.
        self._last_online_sig: frozenset = frozenset()

        # File transfer state (keyed by transfer_id)
        self._progress_vars: dict[str, tk.StringVar] = {}
        self._offer_states: dict[str, str] = {}   # "pending"|"accepted"|"rejected"|"expired"
        # tid -> file path when transfer completes ("" = done but no file to open)
        self._transfer_paths: dict[str, str] = {}

        # Chat request state (keyed by ip)
        self._chat_req_states: dict[str, str] = {}  # "pending"|"accepted"|"blocked"

        # Custom aliases for manual peers (takes priority over _names)
        self._aliases: dict[str, str] = {}

        self._ft = FileTransferService(chat_service)
        self._ft.start()

        self._build()
        self._load_history()
        theme.on_change(self._refresh_active)
        self.after(3000, self._roster_tick)

    # ── construction ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        # ── Left column ───────────────────────────────────────────────────────
        left = tk.Frame(self, bg=theme.color("panel"), width=210)
        theme.register(left, bg="panel")
        left.pack(side="left", fill="y", padx=(12, 0), pady=12)
        left.pack_propagate(False)

        themed_label(left, "YOU", color_role="text_sec",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
        id_row = tk.Frame(left, bg=theme.color("panel2"))
        theme.register(id_row, bg="panel2")
        id_row.pack(fill="x", pady=(4, 8))
        self._self_avatar_holder = tk.Frame(id_row, bg=theme.color("panel2"))
        theme.register(self._self_avatar_holder, bg="panel2")
        self._self_avatar_holder.pack(side="left", padx=6, pady=6)
        self._render_self_avatar()
        self._name_var = tk.StringVar(value=self.chat.my_name)
        self._name_entry = tk.Entry(
            id_row, textvariable=self._name_var, font=("Segoe UI", 10, "bold"),
            relief="flat", bd=4, bg=theme.color("panel2"),
            fg=theme.color("text_pri"), insertbackground=theme.color("text_pri"))
        theme.register(self._name_entry, bg="panel2", fg="text_pri",
                       insertbackground="text_pri")
        self._name_entry.pack(side="left", fill="x", expand=True)
        self._name_entry.bind("<Return>", lambda e: self._rename())
        rename = themed_label(id_row, "✓", color_role="success",
                              font=("Segoe UI", 12, "bold"), bg_role="panel2")
        rename.config(cursor="hand2")
        rename.bind("<Button-1>", lambda e: self._rename())
        rename.pack(side="left", padx=6)

        # ── Manual IP connect (cross-subnet chat) ───────────────────────────
        ip_hdr = tk.Frame(left, bg=theme.color("panel"))
        theme.register(ip_hdr, bg="panel")
        ip_hdr.pack(fill="x", pady=(4, 0))
        themed_label(ip_hdr, "CONNECT BY IP", color_role="text_sec",
                     font=("Segoe UI", 8, "bold"), bg_role="panel").pack(side="left")
        # Sliding toggle for external-IP chat, right-aligned in the header row
        self._ip_toggle = _ToggleSwitch(ip_hdr, initial=self.chat.ip_chat_enabled,
                                        command=self._toggle_ip_chat, bg_role="panel")
        self._ip_toggle.pack(side="right", padx=(4, 0), pady=2)

        ip_row = tk.Frame(left, bg=theme.color("panel2"))
        theme.register(ip_row, bg="panel2")
        ip_row.pack(fill="x", pady=(2, 8))
        self._manual_ip_var = tk.StringVar()
        self._manual_ip_entry = tk.Entry(
            ip_row, textvariable=self._manual_ip_var, font=("Consolas", 9),
            relief="flat", bd=4, bg=theme.color("panel2"),
            fg=theme.color("text_pri"), insertbackground=theme.color("text_pri"),
            width=14)
        theme.register(self._manual_ip_entry, bg="panel2", fg="text_pri",
                       insertbackground="text_pri")
        self._manual_ip_entry.insert(0, "10.x.x.x")
        self._manual_ip_entry.config(fg=theme.color("text_sec"))
        self._manual_ip_entry.bind("<FocusIn>", self._clear_ip_hint)
        self._manual_ip_entry.bind("<FocusOut>", self._restore_ip_hint)
        self._manual_ip_entry.bind("<Return>", lambda e: self._connect_manual_ip())
        self._manual_ip_entry.pack(side="left", fill="x", expand=True)
        connect_lbl = themed_label(ip_row, "➤", color_role="accent",
                                   font=("Segoe UI", 10, "bold"), bg_role="panel2")
        connect_lbl.config(cursor="hand2")
        connect_lbl.bind("<Button-1>", lambda e: self._connect_manual_ip())
        connect_lbl.pack(side="left", padx=6)

        themed_label(left, "PEERS", color_role="text_sec",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._roster = ScrollFrame(left, bg_role="log_bg")
        self._roster.pack(fill="both", expand=True, pady=(4, 8))

        self._demo_btn = themed_button(left, "Try Demo Chat", self._start_demo,
                                       color_role="accent", width=18)
        self._demo_btn.pack(fill="x")

        # ── Divider ───────────────────────────────────────────────────────────
        div = tk.Frame(self, bg=theme.color("border"), width=1)
        theme.register(div, bg="border")
        div.pack(side="left", fill="y", padx=10, pady=12)

        # ── Right column ──────────────────────────────────────────────────────
        right = tk.Frame(self, bg=theme.color("panel"))
        theme.register(right, bg="panel")
        right.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=12)

        self._head = tk.Frame(right, bg=theme.color("panel2"))
        theme.register(self._head, bg="panel2")
        self._head.pack(fill="x")
        self._head_avatar_holder = tk.Frame(self._head, bg=theme.color("panel2"))
        theme.register(self._head_avatar_holder, bg="panel2")
        self._head_avatar_holder.pack(side="left", padx=8, pady=6)
        head_text = tk.Frame(self._head, bg=theme.color("panel2"))
        theme.register(head_text, bg="panel2")
        head_text.pack(side="left", pady=6)
        self._head_name = themed_label(head_text, "LAN Chat", color_role="text_pri",
                                       font=("Segoe UI", 11, "bold"), bg_role="panel2")
        self._head_name.pack(anchor="w")
        self._head_sub = themed_label(head_text, "Select a peer on the left",
                                      color_role="text_sec",
                                      font=("Segoe UI", 8), bg_role="panel2")
        self._head_sub.pack(anchor="w")

        # Header action buttons (right side)
        head_btns = tk.Frame(self._head, bg=theme.color("panel2"))
        theme.register(head_btns, bg="panel2")
        head_btns.pack(side="right", padx=8)
        self._clear_btn = themed_button(head_btns, "Clear", self._clear_chat,
                                        color_role="text_sec", width=7)
        self._clear_btn.pack(side="right", pady=6)

        self._messages = ScrollFrame(right, bg_role="log_bg")
        self._messages.pack(fill="both", expand=True, pady=8)

        composer = tk.Frame(right, bg=theme.color("panel2"))
        theme.register(composer, bg="panel2")
        composer.pack(fill="x")
        self._composer = composer
        self._entry = tk.Entry(
            composer, font=("Segoe UI", 10), relief="flat", bd=8,
            bg=theme.color("panel2"), fg=theme.color("text_sec"),
            insertbackground=theme.color("text_pri"))
        theme.register(self._entry, bg="panel2", insertbackground="text_pri")
        self._entry.insert(0, _PLACEHOLDER)
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<FocusIn>", self._clear_placeholder)
        self._entry.bind("<FocusOut>", self._restore_placeholder)
        self._entry.bind("<Return>", lambda e: self._send())
        self._attach_btn = themed_button(composer, "File", self._attach_file,
                                         color_role="text_sec", width=7)
        self._attach_btn.pack(side="left", padx=(4, 0), pady=4)
        self._send_btn = themed_button(composer, "Send", self._send,
                                       color_role="accent", width=7)
        self._send_btn.pack(side="left", padx=(6, 6), pady=4)

        self._show_empty_state()
        self._set_composer_state(False)
        self.update_roster(self.chat.peers())

    def _peer_display_name(self, ip: str) -> str:
        """Return alias > received name > ip, in priority order."""
        return self._aliases.get(ip) or self._names.get(ip, ip)

    # ── self identity ─────────────────────────────────────────────────────────
    def _render_self_avatar(self) -> None:
        for c in self._self_avatar_holder.winfo_children():
            c.destroy()
        make_avatar(self._self_avatar_holder, self.chat.my_name, size=30,
                    bg_role="panel2").pack()

    def _rename(self) -> None:
        new = self._name_var.get().strip()[:32]
        if not new:
            self._name_var.set(self.chat.my_name)
            return
        self.chat.set_name(new)
        self._name_var.set(new)
        self._render_self_avatar()
        self._on_name_change(new)
        self._log(f"Chat display name set to '{new}'.")

    # ── roster ────────────────────────────────────────────────────────────────
    def _is_online(self, ip: str, live_ips: set[str]) -> bool:
        """Resolve online state for any IP (live, demo, manual, or historical)."""
        if ip == DemoBot.IP:
            return True
        # Auto-discovered peers are only in live_ips while broadcasting.
        if ip in live_ips and not self.chat.is_manual_peer(ip):
            return True
        return self.chat.is_peer_online(ip)

    def _online_ips(self, peers) -> list[str]:
        """All IPs to show in the roster: only those currently online.

        Candidates are live broadcasters plus anyone we have history/manual
        registration for (so a known IP peer reappears the moment it comes back
        online), filtered down to those that pass the online check.
        """
        live_ips = {p.ip for p in peers}
        candidates = (live_ips
                      | set(self._conversations)
                      | set(self._names)
                      | set(self._aliases))
        candidates.discard(self.chat.my_ip)
        return [ip for ip in candidates if self._is_online(ip, live_ips)]

    def _roster_tick(self) -> None:
        """Re-check online status on a timer. No presence event fires when a
        peer simply stops broadcasting, so without this an offline peer would
        linger in the list until the next unrelated roster change."""
        try:
            peers = self.chat.peers()
            if frozenset(self._online_ips(peers)) != self._last_online_sig:
                self.update_roster(peers)
        except tk.TclError:
            return  # view torn down
        self.after(3000, self._roster_tick)

    def update_roster(self, peers) -> None:
        for p in peers:
            self._names[p.ip] = p.name
        self._roster.clear()
        body = self._roster.body

        online = self._online_ips(peers)
        self._last_online_sig = frozenset(online)

        if not online:
            hint = tk.Frame(body, bg=theme.color("log_bg"))
            theme.register(hint, bg="log_bg")
            hint.pack(fill="x", pady=20, padx=10)
            themed_label(hint, "...", color_role="text_sec",
                         font=("Segoe UI", 18), bg_role="log_bg", anchor="center").pack()
            themed_label(hint, "Looking for people on\nyour network...",
                         color_role="text_sec", font=("Segoe UI", 8),
                         bg_role="log_bg", anchor="center").pack()
            themed_label(hint, "Open this app on another PC,\nor click Try Demo Chat.",
                         color_role="text_sec", font=("Segoe UI", 8),
                         bg_role="log_bg", anchor="center").pack(pady=(6, 0))
            return

        for ip in sorted(online, key=lambda x: self._peer_display_name(x).lower()):
            self._add_roster_row(body, ip, True)

        # Update active peer subtext if one is selected
        if self._active_ip:
            ip = self._active_ip
            if ip == DemoBot.IP:
                sub_text = "demo peer"
            else:
                is_on = self._is_online(ip, {p.ip for p in peers})
                sub_text = f"{ip}  ·  {'Online' if is_on else 'Offline'}"
            self._head_sub.config(text=sub_text)

    def _add_roster_row(self, body, ip: str, online: bool) -> None:
        display = self._peer_display_name(ip)
        active = (ip == self._active_ip)
        bg_role = "select_bg" if active else "log_bg"
        row = tk.Frame(body, bg=theme.color(bg_role), cursor="hand2")
        theme.register(row, bg=bg_role)
        row.pack(fill="x", pady=1)

        # Pack right-side widgets FIRST so the expanding txt frame leaves them room
        # Delete (forget) button — removes the peer from the list & its history.
        if ip != DemoBot.IP:
            del_btn = tk.Button(
                row, text="✕",
                bg=theme.color(bg_role), fg=theme.color("text_sec"),
                font=("Segoe UI", 9), relief="flat", cursor="hand2", bd=0,
                activebackground=theme.color(bg_role),
                activeforeground=theme.color("danger"))
            del_btn.pack(side="right", padx=(0, 6), pady=3)

            def _on_del_press(e):
                return "break"   # block <Button-1> from reaching row → no select

            def _on_del_release(e, _ip=ip):
                self._delete_peer(_ip)
                return "break"

            del_btn.bind("<Button-1>", _on_del_press)
            del_btn.bind("<ButtonRelease-1>", _on_del_release)

        unread = self._unread.get(ip, 0)
        if unread:
            badge = tk.Label(row, text=str(unread), bg=theme.color("danger"),
                             fg="#ffffff", font=("Segoe UI", 8, "bold"),
                             padx=5, pady=0)
            badge.pack(side="right", padx=6)

        make_avatar(row, display, size=32, bg_role=bg_role).pack(side="left",
                                                                  padx=6, pady=5)
        txt = tk.Frame(row, bg=theme.color(bg_role))
        theme.register(txt, bg=bg_role)
        txt.pack(side="left", fill="x", expand=True)
        themed_label(txt, display, color_role="text_pri",
                     font=("Segoe UI", 9, "bold"), bg_role=bg_role).pack(anchor="w")

        # Status row: coloured dot + IP / label
        sub_row = tk.Frame(txt, bg=theme.color(bg_role))
        theme.register(sub_row, bg=bg_role)
        sub_row.pack(anchor="w")
        sub_label = "demo peer" if ip == DemoBot.IP else ip
        _status_dot(sub_row, online, bg_role).pack(side="left", padx=(0, 3))
        themed_label(sub_row, sub_label, color_role="text_sec",
                     font=("Consolas", 7), bg_role=bg_role).pack(side="left")

        for w in (row, txt, sub_row):
            w.bind("<Button-1>", lambda e, _ip=ip: self.select_peer(_ip))
        for child in txt.winfo_children():
            child.bind("<Button-1>", lambda e, _ip=ip: self.select_peer(_ip))
        for child in sub_row.winfo_children():
            child.bind("<Button-1>", lambda e, _ip=ip: self.select_peer(_ip))
        if not active:
            row.bind("<Enter>", lambda e: self._hover_row(row, txt, True))
            row.bind("<Leave>", lambda e: self._hover_row(row, txt, False))

    def _delete_peer(self, ip: str) -> None:
        """Forget a peer: drop it from the roster, the chat service and disk."""
        name = self._peer_display_name(ip)
        if not messagebox.askyesno(
                "Remove peer",
                f"Remove {name} from the list and delete its chat history?",
                parent=self):
            return
        self.chat.remove_peer(ip)   # stops probing manual peers, drops presence
        self._conversations.pop(ip, None)
        self._unread.pop(ip, None)
        self._names.pop(ip, None)
        self._aliases.pop(ip, None)
        self._chat_req_states.pop(ip, None)
        self._delete_peer_history(ip)

        if self._active_ip == ip:
            self._active_ip = None
            self._head_name.config(text="LAN Chat")
            self._head_sub.config(text="Select a peer on the left")
            for c in self._head_avatar_holder.winfo_children():
                c.destroy()
            self._set_composer_state(False)
            self._show_empty_state()

        self.update_roster(self.chat.peers())
        self._log(f"Removed {name} from the chat list.")

    def _delete_peer_history(self, ip: str) -> None:
        def _rm():
            try:
                safe = ip.replace(".", "_").replace(":", "_")
                path = os.path.join(config.get_peer_chat_dir(), f"{safe}.json")
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        threading.Thread(target=_rm, daemon=True).start()

    def _hover_row(self, row, txt, entering) -> None:
        c = theme.color("hover" if entering else "log_bg")
        try:
            row.config(bg=c)
            txt.config(bg=c)
            for child in txt.winfo_children():
                child.config(bg=c)
        except tk.TclError:
            pass

    # ── IP chat toggle ─────────────────────────────────────────────────────────
    def _toggle_ip_chat(self, enabled: bool) -> None:
        self.chat.ip_chat_enabled = enabled
        config.save_ip_chat_enabled(enabled)
        self._log(f"External IP chat {'enabled' if enabled else 'disabled'}.")

    # ── conversation ──────────────────────────────────────────────────────────
    def select_peer(self, ip: str) -> None:
        self._active_ip = ip
        self._unread[ip] = 0
        name = self._peer_display_name(ip)
        for c in self._head_avatar_holder.winfo_children():
            c.destroy()
        make_avatar(self._head_avatar_holder, name, size=34,
                    bg_role="panel2").pack()
        self._head_name.config(text=name)
        if ip == DemoBot.IP:
            sub_text = "demo peer"
        elif self.chat.is_manual_peer(ip):
            online = self.chat.is_peer_online(ip)
            sub_text = f"{ip}  ·  {'Online' if online else 'Offline'}"
        else:
            sub_text = f"{ip}  ·  Online"
        self._head_sub.config(text=sub_text)
        self._set_composer_state(True)
        self._render(ip)
        self.update_roster(self.chat.peers())
        self._entry.focus_set()

    def _show_empty_state(self) -> None:
        self._messages.clear()
        wrap = tk.Frame(self._messages.body, bg=theme.color("log_bg"))
        theme.register(wrap, bg="log_bg")
        wrap.pack(expand=True, pady=60)
        themed_label(wrap, "[chat]", color_role="text_sec", font=("Segoe UI", 18),
                     bg_role="log_bg", anchor="center").pack()
        themed_label(wrap, "Pick someone from the list to start chatting.",
                     color_role="text_sec", font=("Segoe UI", 9),
                     bg_role="log_bg", anchor="center").pack(pady=(6, 0))

    def _render(self, ip: str) -> None:
        self._messages.clear()
        msgs = self._conversations.get(ip, [])
        if not msgs:
            themed_label(self._messages.body, "Say hi!", color_role="text_sec",
                         font=("Segoe UI", 9), bg_role="log_bg",
                         anchor="center").pack(pady=30)
        else:
            for entry in msgs:
                self._add_bubble(entry)
        self._messages.scroll_to_bottom()

    def _add_bubble(self, entry: tuple) -> None:
        kind = entry[0]
        body = self._messages.body

        # ── file transfer bubbles ─────────────────────────────────────────────
        if kind in ("file_out", "file_in_offer", "file_in"):
            self._add_file_bubble(entry)
            return

        # ── chat request prompt ───────────────────────────────────────────────
        if kind == "chat_req":
            self._add_chat_req_bubble(entry)
            return

        # ── regular chat bubbles ──────────────────────────────────────────────
        _, sender, text, ts = entry
        stamp = time.strftime("%H:%M", time.localtime(ts))

        if kind == "sys":
            themed_label(body, f"-- {text} --", color_role="text_sec",
                         font=("Segoe UI", 8, "italic"), bg_role="log_bg",
                         anchor="center").pack(fill="x", pady=4)
            return

        row = tk.Frame(body, bg=theme.color("log_bg"))
        theme.register(row, bg="log_bg")
        row.pack(fill="x", padx=8, pady=3)

        is_out = (kind == "out")
        bub_role = "bubble_out" if is_out else "bubble_in"
        tx_role = "bubble_out_tx" if is_out else "bubble_in_tx"
        bubble = tk.Frame(row, bg=theme.color(bub_role))
        theme.register(bubble, bg=bub_role)
        bubble.pack(anchor="e" if is_out else "w")

        if not is_out:
            themed_label(bubble, sender, color_role="accent",
                         font=("Segoe UI", 8, "bold"),
                         bg_role=bub_role).pack(anchor="w", padx=10, pady=(5, 0))
        msg = tk.Label(bubble, text=text, bg=theme.color(bub_role),
                       fg=theme.color(tx_role), font=("Segoe UI", 10),
                       justify="left", wraplength=240, anchor="w")
        theme.register(msg, bg=bub_role, fg=tx_role)
        msg.pack(anchor="w", padx=10, pady=(2, 1))
        themed_label(bubble, stamp, color_role=(tx_role if is_out else "text_sec"),
                     font=("Segoe UI", 7), bg_role=bub_role).pack(
                         anchor="e", padx=10, pady=(0, 4))

    def _add_file_bubble(self, entry: tuple) -> None:
        kind, tid, meta, ts = entry
        body = self._messages.body
        stamp = time.strftime("%H:%M", time.localtime(ts))
        is_out = (kind == "file_out")
        bub_role = "bubble_out" if is_out else "bubble_in"
        tx_role  = "bubble_out_tx" if is_out else "bubble_in_tx"

        row = tk.Frame(body, bg=theme.color("log_bg"))
        theme.register(row, bg="log_bg")
        row.pack(fill="x", padx=8, pady=3)

        bubble = tk.Frame(row, bg=theme.color(bub_role))
        theme.register(bubble, bg=bub_role)
        bubble.pack(anchor="e" if is_out else "w")

        # Filename header
        themed_label(bubble, meta['filename'],
                     color_role=tx_role, font=("Segoe UI", 9, "bold"),
                     bg_role=bub_role).pack(anchor="w", padx=10, pady=(8, 0))
        # Size
        size_color = tx_role if is_out else "text_sec"
        themed_label(bubble, _fmt_size(meta["size"]),
                     color_role=size_color, font=("Segoe UI", 8),
                     bg_role=bub_role).pack(anchor="w", padx=10)

        prog_fg = tx_role if is_out else "text_sec"

        if kind == "file_in_offer":
            state = self._offer_states.get(tid, "pending")
            from_ip = meta["from_ip"]
            expired = (time.time() - ts > 60) and state == "pending"

            if expired or state == "expired":
                themed_label(bubble, "Offer expired",
                             color_role=prog_fg, font=("Segoe UI", 8, "italic"),
                             bg_role=bub_role).pack(anchor="w", padx=10, pady=(4, 2))
            elif state == "pending":
                btn_row = tk.Frame(bubble, bg=theme.color(bub_role))
                theme.register(btn_row, bg=bub_role)
                btn_row.pack(anchor="w", padx=10, pady=(6, 2))
                tk.Button(
                    btn_row, text="Accept",
                    bg=theme.color("success"), fg="#ffffff",
                    font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                    command=lambda: self._accept_file(
                        tid, from_ip, meta["filename"], meta["size"])
                ).pack(side="left", padx=(0, 6))
                tk.Button(
                    btn_row, text="Reject",
                    bg=theme.color("danger"), fg="#ffffff",
                    font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                    command=lambda: self._reject_file(tid, from_ip)
                ).pack(side="left")
            else:
                var = self._progress_vars.get(tid)
                if var:
                    prog = tk.Label(bubble, textvariable=var,
                                    bg=theme.color(bub_role), fg=theme.color(prog_fg),
                                    font=("Consolas", 8), justify="left")
                    theme.register(prog, bg=bub_role, fg=prog_fg)
                    prog.pack(anchor="w", padx=10, pady=(4, 2))
                    done_path = self._transfer_paths.get(tid)
                    if done_path is None:
                        # In progress — show cancel button
                        _tid = tid
                        tk.Button(bubble, text="Cancel",
                                  bg=theme.color("danger"), fg="#ffffff",
                                  font=("Segoe UI", 7, "bold"), relief="flat",
                                  cursor="hand2",
                                  command=lambda: self._cancel_file(_tid)
                                  ).pack(anchor="w", padx=10, pady=(0, 2))
                    elif done_path:
                        # Done with a saved file — show open buttons
                        self._add_open_buttons(bubble, done_path, bub_role)
        else:
            # file_out (sender)
            var = self._progress_vars.get(tid)
            if var:
                prog = tk.Label(bubble, textvariable=var,
                                bg=theme.color(bub_role), fg=theme.color(prog_fg),
                                font=("Consolas", 8), justify="left")
                theme.register(prog, bg=bub_role, fg=prog_fg)
                prog.pack(anchor="w", padx=10, pady=(4, 2))
                done_path = self._transfer_paths.get(tid)
                if done_path is None:
                    # In progress or waiting — show cancel button
                    _tid = tid
                    tk.Button(bubble, text="Cancel",
                              bg=theme.color("danger"), fg="#ffffff",
                              font=("Segoe UI", 7, "bold"), relief="flat",
                              cursor="hand2",
                              command=lambda: self._cancel_file(_tid)
                              ).pack(anchor="w", padx=10, pady=(0, 2))
                elif done_path:
                    # Sender has the original file — show open buttons
                    self._add_open_buttons(bubble, done_path, bub_role)

        themed_label(bubble, stamp, color_role=prog_fg,
                     font=("Segoe UI", 7), bg_role=bub_role).pack(
                         anchor="e", padx=10, pady=(2, 6))

    def _add_open_buttons(self, parent: tk.Frame, path: str, bub_role: str) -> None:
        """Add 'Open File' and 'Open Folder' buttons after a completed transfer."""
        btn_row = tk.Frame(parent, bg=theme.color(bub_role))
        theme.register(btn_row, bg=bub_role)
        btn_row.pack(anchor="w", padx=10, pady=(2, 0))
        _p = str(path)
        tk.Button(btn_row, text="Open File",
                  bg=theme.color("panel2"), fg=theme.color("text_pri"),
                  font=("Segoe UI", 7), relief="flat", cursor="hand2",
                  command=lambda: os.startfile(_p)
                  ).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Open Folder",
                  bg=theme.color("panel2"), fg=theme.color("text_pri"),
                  font=("Segoe UI", 7), relief="flat", cursor="hand2",
                  command=lambda: subprocess.Popen(
                      f'explorer /select,"{_p}"', shell=True)
                  ).pack(side="left")

    def _add_chat_req_bubble(self, entry: tuple) -> None:
        _, ip, meta, ts = entry
        body = self._messages.body
        stamp = time.strftime("%H:%M", time.localtime(ts))
        state = self._chat_req_states.get(ip, "pending")

        card = tk.Frame(body, bg=theme.color("panel2"), relief="flat")
        theme.register(card, bg="panel2")
        card.pack(fill="x", padx=8, pady=6)

        themed_label(card, f"{meta['from_name']} ({ip}) wants to chat",
                     color_role="text_pri", font=("Segoe UI", 9, "bold"),
                     bg_role="panel2").pack(anchor="w", padx=10, pady=(8, 2))

        if meta.get("first_msg"):
            themed_label(card, f"\"{meta['first_msg'][:80]}\"",
                         color_role="text_sec", font=("Segoe UI", 8, "italic"),
                         bg_role="panel2").pack(anchor="w", padx=10, pady=(0, 4))

        if state == "pending":
            btns = tk.Frame(card, bg=theme.color("panel2"))
            theme.register(btns, bg="panel2")
            btns.pack(anchor="w", padx=10, pady=(4, 8))
            tk.Button(btns, text="Accept",
                      bg=theme.color("success"), fg="#ffffff",
                      font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                      command=lambda: self._accept_chat(ip)
                      ).pack(side="left", padx=(0, 6))
            tk.Button(btns, text="Block",
                      bg=theme.color("danger"), fg="#ffffff",
                      font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                      command=lambda: self._block_chat(ip)
                      ).pack(side="left")
        elif state == "accepted":
            themed_label(card, "Accepted — messages will now appear normally.",
                         color_role="success", font=("Segoe UI", 8),
                         bg_role="panel2").pack(anchor="w", padx=10, pady=(4, 8))
        else:
            themed_label(card, "Blocked — messages from this IP are discarded.",
                         color_role="danger", font=("Segoe UI", 8),
                         bg_role="panel2").pack(anchor="w", padx=10, pady=(4, 8))

        themed_label(card, stamp, color_role="text_sec",
                     font=("Segoe UI", 7), bg_role="panel2").pack(
                         anchor="e", padx=10, pady=(0, 4))

    def _refresh_active(self) -> None:
        """Re-render after a theme switch so bubbles pick up new colors."""
        if self._active_ip:
            self._render(self._active_ip)

    # ── composer ──────────────────────────────────────────────────────────────
    def _set_composer_state(self, enabled: bool) -> None:
        """Show the composer + Clear button only when a peer is selected;
        hide them entirely otherwise (rather than leaving them greyed out)."""
        if enabled:
            self._composer.pack(fill="x")
            self._clear_btn.pack(side="right", pady=6)
        else:
            self._composer.pack_forget()
            self._clear_btn.pack_forget()

    def _clear_placeholder(self, _e=None) -> None:
        if self._placeholder_on:
            self._entry.delete(0, "end")
            self._entry.config(fg=theme.color("text_pri"))
            self._placeholder_on = False

    def _restore_placeholder(self, _e=None) -> None:
        if not self._entry.get().strip():
            self._entry.delete(0, "end")
            self._entry.insert(0, _PLACEHOLDER)
            self._entry.config(fg=theme.color("text_sec"))
            self._placeholder_on = True

    def _send(self) -> None:
        ip = self._active_ip
        if not ip or self._placeholder_on:
            return
        text = self._entry.get().strip()
        if not text:
            return
        self._entry.delete(0, "end")
        self._conversations.setdefault(ip, []).append(("out", "You", text, time.time()))
        self._trim_history(ip)
        self._save_peer_history(ip)
        self._render(ip)

        def worker():
            ok = self.chat.send(ip, text)
            if not ok:
                def fail():
                    self._conversations.setdefault(ip, []).append(
                        ("sys", "", "not delivered (peer offline?)", time.time()))
                    if ip == self._active_ip:
                        self._render(ip)
                self.after(0, fail)
        threading.Thread(target=worker, daemon=True).start()

    def receive_message(self, ip: str, name: str, text: str, ts: float) -> bool:
        """Store an incoming message. Returns True if shown in the active+visible
        conversation (so no toast is needed)."""
        self._names[ip] = name
        self._conversations.setdefault(ip, []).append(("in", name, text, ts))
        self._trim_history(ip)
        self._save_peer_history(ip)
        shown = (ip == self._active_ip and self._visible)
        if shown:
            self._add_bubble(("in", name, text, ts))
            self._messages.scroll_to_bottom()
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())
        return shown

    # ── demo ──────────────────────────────────────────────────────────────────
    def _start_demo(self) -> None:
        if not self.chat.has_demo():
            self.chat.add_demo_bot()
            self._log("Demo chat started — say hi to the Demo Bot.")
        self.after(150, lambda: self.select_peer(DemoBot.IP))

    # ── visibility (driven by the tab switcher) ───────────────────────────────
    def set_visible(self, visible: bool) -> None:
        self._visible = visible
        if visible and self._active_ip:
            self._unread[self._active_ip] = 0
            self.update_roster(self.chat.peers())

    def is_active_conversation(self, ip: str) -> bool:
        return ip == self._active_ip

    # ── manual IP connect ─────────────────────────────────────────────────────
    def _clear_ip_hint(self, _e=None) -> None:
        if self._manual_ip_var.get() == "10.x.x.x":
            self._manual_ip_entry.delete(0, "end")
            self._manual_ip_entry.config(fg=theme.color("text_pri"))

    def _restore_ip_hint(self, _e=None) -> None:
        if not self._manual_ip_var.get().strip():
            self._manual_ip_entry.delete(0, "end")
            self._manual_ip_entry.insert(0, "10.x.x.x")
            self._manual_ip_entry.config(fg=theme.color("text_sec"))

    def _connect_manual_ip(self) -> None:
        ip = self._manual_ip_var.get().strip()
        if not ip or ip == "10.x.x.x":
            return
        if not is_valid_ipv4(ip) or not ip.startswith("10."):
            self._log(f"Invalid IP: {ip!r} — must be a valid 10.x.x.x address.")
            return
        if ip == self.chat.my_ip:
            self._log("Cannot chat with yourself.")
            return

        # Ask the user to name this PC so the roster shows something friendlier
        # than a bare IP. Cancelling / leaving it blank keeps the IP as the name.
        name = simpledialog.askstring(
            "Name this PC",
            f"Enter a name for {ip}:",
            parent=self)
        if name and name.strip():
            self._aliases[ip] = name.strip()[:32]

        self.chat.add_manual_peer(ip)
        self._names.setdefault(ip, ip)
        self._manual_ip_var.set("")
        self._restore_ip_hint()
        self.select_peer(ip)
        self._save_peer_history(ip)  # persist the alias + manual flag right away

        def _probe():
            ok = check_host_reachable(ip, CHAT_TCP_PORT)
            if not ok:
                def _show_err():
                    self._conversations.setdefault(ip, []).append((
                        "sys", "", "Not reachable — make sure the app is running on that PC.", time.time()
                    ))
                    if self._active_ip == ip:
                        self._render(ip)
                self.after(0, _show_err)

        threading.Thread(target=_probe, daemon=True).start()

    # ── chat history persistence (per-peer files) ─────────────────────────────
    def _load_history(self) -> None:
        try:
            chats_dir = config.get_peer_chat_dir()
            loaded_any = False
            for fname in os.listdir(chats_dir):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(chats_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ip = data.get("ip")
                    if not ip:
                        continue
                    if data.get("name"):
                        self._names[ip] = data["name"]
                    if data.get("alias"):
                        self._aliases[ip] = data["alias"]
                    self._conversations[ip] = [
                        tuple(m) for m in data.get("messages", [])[-_MAX_HISTORY_PER_PEER:]
                    ]
                    # Re-register manually-added (cross-subnet) peers so presence
                    # probing resumes and their online status stays accurate.
                    if data.get("manual"):
                        self.chat.add_manual_peer(ip)
                    loaded_any = True
                except Exception:
                    pass
            # One-time migration from legacy single file
            if not loaded_any:
                self._load_legacy_history()
        except Exception:
            pass

    def _load_legacy_history(self) -> None:
        try:
            path = config.get_chat_history_path()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ip, msgs in data.items():
                if ip == "_names":
                    self._names.update(msgs)
                    continue
                if ip == "_aliases":
                    self._aliases.update(msgs)
                    continue
                self._conversations[ip] = [
                    tuple(m) for m in msgs[-_MAX_HISTORY_PER_PEER:]
                ]
            self._log("Chat history loaded.")
        except Exception:
            pass

    def _save_peer_history(self, ip: str) -> None:
        """Persist a single peer's chat history asynchronously."""
        msgs = list(self._conversations.get(ip, []))
        name = self._names.get(ip, ip)
        alias = self._aliases.get(ip)
        manual = self.chat.is_manual_peer(ip)

        def _write():
            try:
                safe = ip.replace(".", "_").replace(":", "_")
                path = os.path.join(config.get_peer_chat_dir(), f"{safe}.json")
                kept = [m for m in msgs
                        if not m[0].startswith("file_") and not m[0].startswith("chat_req")]
                data: dict = {
                    "ip": ip,
                    "name": name,
                    "messages": [list(m) for m in kept[-_MAX_HISTORY_PER_PEER:]],
                }
                if alias:
                    data["alias"] = alias
                if manual:
                    data["manual"] = True
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass

        threading.Thread(target=_write, daemon=True).start()

    def _trim_history(self, ip: str) -> None:
        msgs = self._conversations.get(ip)
        if msgs and len(msgs) > _MAX_HISTORY_PER_PEER:
            self._conversations[ip] = msgs[-_MAX_HISTORY_PER_PEER:]

    # ── file transfer — outgoing ──────────────────────────────────────────────
    def _attach_file(self) -> None:
        ip = self._active_ip
        if not ip:
            return
        path = filedialog.askopenfilename(parent=self)
        if not path:
            return
        filename = os.path.basename(path)
        size = os.path.getsize(path)
        var = tk.StringVar(value=f"Waiting for {self._names.get(ip, ip)} to accept...")

        def _do_offer():
            tid_holder: list[str | None] = [None]

            def _progress(done, total, speed, elapsed, eta):
                _tid = tid_holder[0]
                if _tid is None:
                    return
                pct = int(done * 100 / total) if total else 0
                self.after(0, lambda: var.set(
                    f"Sending {pct}%  {_fmt_speed(speed)}"
                    f"  elapsed {_fmt_eta(elapsed)}  ETA {_fmt_eta(eta)}"
                ))

            def _done():
                _tid = tid_holder[0]
                def _update():
                    var.set("Sent!")
                    if _tid:
                        self._transfer_paths[_tid] = path  # original file for open buttons
                        if self._active_ip == ip:
                            self._render(self._active_ip)
                self.after(0, _update)

            def _error(msg):
                _tid = tid_holder[0]
                def _update():
                    var.set(f"Failed: {msg}")
                    if _tid:
                        self._transfer_paths[_tid] = ""  # mark done, no file
                        if self._active_ip == ip:
                            self._render(self._active_ip)
                self.after(0, _update)

            try:
                def _expire():
                    self.after(0, lambda: var.set("No response — offer expired"))

                tid = self._ft.offer_file(ip, path,
                                          progress_cb=_progress,
                                          done_cb=_done,
                                          error_cb=_error,
                                          expire_cb=_expire)
                tid_holder[0] = tid

                def _show():
                    self._progress_vars[tid] = var
                    self._conversations.setdefault(ip, []).append(
                        ("file_out", tid, {"filename": filename, "size": size}, time.time())
                    )
                    if self._active_ip == ip:
                        self._render(ip)
                    self._log(f"File offer sent: {filename} ({_fmt_size(size)})")
                self.after(0, _show)
            except Exception as e:
                self.after(0, lambda: self._log(f"Could not send file offer: {e}"))

        threading.Thread(target=_do_offer, daemon=True).start()

    # ── file transfer — incoming offer ────────────────────────────────────────
    def on_file_offer_received(self, ip: str, name: str, msg: dict) -> bool:
        """Called (on main thread) when a file offer arrives. Returns True if shown in-view."""
        tid = msg["transfer_id"]
        filename = msg["filename"]
        size = msg["size"]
        self._names[ip] = name
        self._offer_states[tid] = "pending"
        entry = ("file_in_offer", tid, {"filename": filename, "size": size, "from_ip": ip}, time.time())
        self._conversations.setdefault(ip, []).append(entry)
        shown = (ip == self._active_ip and self._visible)
        if shown:
            self._add_bubble(entry)
            self._messages.scroll_to_bottom()
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())
        return shown

    def _accept_file(self, tid: str, from_ip: str, filename: str, size: int) -> None:
        self._offer_states[tid] = "accepted"
        var = tk.StringVar(value="Connecting...")
        self._progress_vars[tid] = var
        if self._active_ip:
            self._render(self._active_ip)

        def _progress(done, total, speed, elapsed, eta):
            pct = int(done * 100 / total) if total else 0
            self.after(0, lambda: var.set(
                f"Receiving {pct}%  {_fmt_speed(speed)}"
                f"  elapsed {_fmt_eta(elapsed)}  ETA {_fmt_eta(eta)}"
            ))

        def _done(save_path):
            def _update():
                var.set(f"Saved!")
                self._transfer_paths[tid] = save_path
                if self._active_ip:
                    self._render(self._active_ip)
            self.after(0, _update)

        def _error(msg):
            def _update():
                var.set(f"Failed: {msg}")
                self._transfer_paths[tid] = ""  # mark done
                if self._active_ip:
                    self._render(self._active_ip)
            self.after(0, _update)

        def _do_accept():
            # send_accept blocks up to 3 s — run it off the main thread
            self._ft.send_accept(from_ip, tid)
            self._ft.receive_file(tid, from_ip,
                                  progress_cb=_progress, done_cb=_done, error_cb=_error)

        threading.Thread(target=_do_accept, daemon=True).start()

    def _reject_file(self, tid: str, from_ip: str) -> None:
        self._offer_states[tid] = "rejected"
        if self._active_ip:
            self._render(self._active_ip)
        threading.Thread(target=lambda: self._ft.send_reject(from_ip, tid),
                         daemon=True).start()

    def _cancel_file(self, tid: str) -> None:
        """Cancel an outgoing offer or an active transfer (works for both sides)."""
        var = self._progress_vars.get(tid)
        self._ft.cancel_offer(tid)
        self._ft.cancel_transfer(tid)
        self._transfer_paths[tid] = ""  # mark done, no file to open
        if var:
            var.set("Cancelled")
        if self._active_ip:
            self._render(self._active_ip)

    # ── file transfer — sender receives response ──────────────────────────────
    def on_file_accepted(self, ip: str, name: str, msg: dict) -> None:
        tid = msg["transfer_id"]
        var = self._progress_vars.get(tid)
        if var:
            var.set(f"{name} accepted — sending...")

    def on_file_rejected(self, ip: str, name: str, msg: dict) -> None:
        tid = msg["transfer_id"]
        self._ft.cancel_offer(tid)
        self._transfer_paths[tid] = ""  # mark done
        var = self._progress_vars.get(tid)
        if var:
            var.set(f"Rejected by {name}")
        if self._active_ip == ip:
            self._render(self._active_ip)

    # ── clear chat ────────────────────────────────────────────────────────────
    def _clear_chat(self) -> None:
        ip = self._active_ip
        if not ip:
            return
        self._conversations.pop(ip, None)
        self._unread.pop(ip, None)
        self._save_peer_history(ip)
        self._show_empty_state()
        self._log(f"Chat with {self._peer_display_name(ip)} cleared.")

    # ── chat request (incoming external IP) ───────────────────────────────────
    def on_chat_request_received(self, ip: str, name: str, msg: dict) -> None:
        """Show an inline Accept/Block prompt for a first-contact external IP."""
        if ip in self._chat_req_states:
            if self._chat_req_states[ip] == "accepted":
                self.chat.approve_ip(ip)
            return
        self._names[ip] = name
        self._chat_req_states[ip] = "pending"
        text = str(msg.get("text", ""))
        entry = ("chat_req", ip, {"from_name": name, "first_msg": text}, time.time())
        self._conversations.setdefault(ip, []).append(entry)
        shown = (ip == self._active_ip and self._visible)
        if shown:
            self._add_bubble(entry)
            self._messages.scroll_to_bottom()
        else:
            self._unread[ip] = self._unread.get(ip, 0) + 1
            self.update_roster(self.chat.peers())

    def _accept_chat(self, ip: str) -> None:
        self._chat_req_states[ip] = "accepted"
        self.chat.approve_ip(ip)
        if self._active_ip == ip:
            self._render(ip)

    def _block_chat(self, ip: str) -> None:
        self._chat_req_states[ip] = "blocked"
        self.chat.block_ip(ip)
        if self._active_ip == ip:
            self._render(ip)

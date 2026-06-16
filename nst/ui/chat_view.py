"""The LAN Chat tab.

Left:  your identity + a roster of online peers (avatars, unread badges).
Right: a scrolling message-bubble conversation + a composer.

Conversations are kept in memory, one list per peer IP, so several chats run at
once. Incoming messages for an inactive chat bump an unread badge; the app layer
decides whether to also raise a bottom-right toast.
"""

import json
import os
import threading
import time
import tkinter as tk

from .. import config
from ..chat import DemoBot
from ..constants import CHAT_TCP_PORT, LABEL_FONT, TITLE_FONT
from ..netinfo import check_host_reachable, is_valid_ipv4
from ..theme import theme
from ..win_utils import get_resource_path
from .widgets import (
    ScrollFrame,
    make_avatar,
    themed_button,
    themed_label,
)

_PLACEHOLDER = "Type a message…"
_MAX_HISTORY_PER_PEER = 200


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

        # ip -> list[ (kind, sender, text, ts) ]   kind in {"in","out","sys"}
        self._conversations: dict[str, list[tuple]] = {}
        self._names: dict[str, str] = {}
        self._unread: dict[str, int] = {}
        self._active_ip: str | None = None
        self._visible = False
        self._placeholder_on = True

        self._build()
        self._load_history()
        theme.on_change(self._refresh_active)

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
        themed_label(left, "CONNECT BY IP", color_role="text_sec",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(4, 0))
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
                                   font=("Segoe UI", 12, "bold"), bg_role="panel2")
        connect_lbl.config(cursor="hand2")
        connect_lbl.bind("<Button-1>", lambda e: self._connect_manual_ip())
        connect_lbl.pack(side="left", padx=6)

        themed_label(left, "ONLINE PEERS", color_role="text_sec",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._roster = ScrollFrame(left, bg_role="log_bg")
        self._roster.pack(fill="both", expand=True, pady=(4, 8))

        self._demo_btn = themed_button(left, "✨  Try Demo Chat", self._start_demo,
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

        self._messages = ScrollFrame(right, bg_role="log_bg")
        self._messages.pack(fill="both", expand=True, pady=8)

        composer = tk.Frame(right, bg=theme.color("panel2"))
        theme.register(composer, bg="panel2")
        composer.pack(fill="x")
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
        self._send_btn = themed_button(composer, "➤ Send", self._send,
                                       color_role="accent", width=8)
        self._send_btn.pack(side="left", padx=(8, 6), pady=4)

        self._show_empty_state()
        self._set_composer_state(False)
        self.update_roster(self.chat.peers())

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
    def update_roster(self, peers) -> None:
        for p in peers:
            self._names[p.ip] = p.name
        self._roster.clear()
        body = self._roster.body

        if not peers:
            hint = tk.Frame(body, bg=theme.color("log_bg"))
            theme.register(hint, bg="log_bg")
            hint.pack(fill="x", pady=20, padx=10)
            themed_label(hint, "🔍", color_role="text_sec",
                         font=("Segoe UI", 18), bg_role="log_bg", anchor="center").pack()
            themed_label(hint, "Looking for people on\nyour network…",
                         color_role="text_sec", font=("Segoe UI", 8),
                         bg_role="log_bg", anchor="center").pack()
            themed_label(hint, "Open this app on another PC,\nor click Try Demo Chat.",
                         color_role="text_sec", font=("Segoe UI", 8),
                         bg_role="log_bg", anchor="center").pack(pady=(6, 0))
            return

        for p in sorted(peers, key=lambda x: x.name.lower()):
            self._add_roster_row(body, p.ip, p.name)

        # Update active peer subtext if one is selected
        if self._active_ip:
            ip = self._active_ip
            if ip == DemoBot.IP:
                sub_text = "demo peer"
            elif self.chat.is_manual_peer(ip):
                online = self.chat.is_peer_online(ip)
                sub_text = f"{ip}  ·  manual  ({'reachable ✓' if online else 'unreachable ✗'})"
            else:
                sub_text = f"{ip}  ·  online"
            self._head_sub.config(text=sub_text)

    def _add_roster_row(self, body, ip: str, name: str) -> None:
        active = (ip == self._active_ip)
        bg_role = "select_bg" if active else "log_bg"
        row = tk.Frame(body, bg=theme.color(bg_role), cursor="hand2")
        theme.register(row, bg=bg_role)
        row.pack(fill="x", pady=1)

        make_avatar(row, name, size=32, bg_role=bg_role).pack(side="left",
                                                             padx=6, pady=5)
        txt = tk.Frame(row, bg=theme.color(bg_role))
        theme.register(txt, bg=bg_role)
        txt.pack(side="left", fill="x", expand=True)
        themed_label(txt, name, color_role="text_pri",
                     font=("Segoe UI", 9, "bold"), bg_role=bg_role).pack(anchor="w")
        
        if ip == DemoBot.IP:
            sub = "demo peer"
        elif self.chat.is_manual_peer(ip):
            online = self.chat.is_peer_online(ip)
            sub = f"{ip}  ·  {'reachable ✓' if online else 'unreachable ✗'}"
        else:
            sub = ip
            
        themed_label(txt, sub, color_role="text_sec",
                     font=("Consolas", 7), bg_role=bg_role).pack(anchor="w")

        unread = self._unread.get(ip, 0)
        if unread:
            badge = tk.Label(row, text=str(unread), bg=theme.color("danger"),
                             fg="#ffffff", font=("Segoe UI", 8, "bold"),
                             padx=5, pady=0)
            badge.pack(side="right", padx=6)

        for w in (row, txt):
            w.bind("<Button-1>", lambda e, _ip=ip: self.select_peer(_ip))
        for child in txt.winfo_children():
            child.bind("<Button-1>", lambda e, _ip=ip: self.select_peer(_ip))
        # hover (only when not the active row)
        if not active:
            row.bind("<Enter>", lambda e: self._hover_row(row, txt, True))
            row.bind("<Leave>", lambda e: self._hover_row(row, txt, False))

    def _hover_row(self, row, txt, entering) -> None:
        c = theme.color("hover" if entering else "log_bg")
        try:
            row.config(bg=c)
            txt.config(bg=c)
            for child in txt.winfo_children():
                child.config(bg=c)
        except tk.TclError:
            pass

    # ── conversation ──────────────────────────────────────────────────────────
    def select_peer(self, ip: str) -> None:
        self._active_ip = ip
        self._unread[ip] = 0
        name = self._names.get(ip, ip)
        for c in self._head_avatar_holder.winfo_children():
            c.destroy()
        make_avatar(self._head_avatar_holder, name, size=34,
                    bg_role="panel2").pack()
        self._head_name.config(text=name)
        if ip == DemoBot.IP:
            sub_text = "demo peer"
        elif self.chat.is_manual_peer(ip):
            online = self.chat.is_peer_online(ip)
            sub_text = f"{ip}  ·  manual  ({'reachable ✓' if online else 'unreachable ✗'})"
        else:
            sub_text = f"{ip}  ·  online"
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
        themed_label(wrap, "💬", color_role="text_sec", font=("Segoe UI", 34),
                     bg_role="log_bg", anchor="center").pack()
        themed_label(wrap, "Pick someone from the list to start chatting.",
                     color_role="text_sec", font=("Segoe UI", 9),
                     bg_role="log_bg", anchor="center").pack(pady=(6, 0))

    def _render(self, ip: str) -> None:
        self._messages.clear()
        msgs = self._conversations.get(ip, [])
        if not msgs:
            themed_label(self._messages.body, "Say hi 👋", color_role="text_sec",
                         font=("Segoe UI", 9), bg_role="log_bg",
                         anchor="center").pack(pady=30)
        else:
            for entry in msgs:
                self._add_bubble(entry)
        self._messages.scroll_to_bottom()

    def _add_bubble(self, entry: tuple) -> None:
        kind, sender, text, ts = entry
        body = self._messages.body
        stamp = time.strftime("%H:%M", time.localtime(ts))

        if kind == "sys":
            themed_label(body, f"— {text} —", color_role="text_sec",
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

    def _refresh_active(self) -> None:
        """Re-render after a theme switch so bubbles pick up new colors."""
        if self._active_ip:
            self._render(self._active_ip)

    # ── composer ──────────────────────────────────────────────────────────────
    def _set_composer_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._entry.config(state=state)
        self._send_btn.config(state=state)

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
        self._save_history()
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
        self._save_history()
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

        self.chat.add_manual_peer(ip)
        self._names.setdefault(ip, ip)
        self._manual_ip_var.set("")
        self._restore_ip_hint()
        self.select_peer(ip)

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

    # ── chat history persistence ─────────────────────────────────────────────
    def _load_history(self) -> None:
        """Load saved conversations from disk."""
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
                self._conversations[ip] = [
                    tuple(m) for m in msgs[-_MAX_HISTORY_PER_PEER:]
                ]
            self._log("Chat history loaded.")
        except Exception:
            pass

    def _save_history(self) -> None:
        """Persist conversations to disk (debounced, non-blocking)."""
        def _write():
            try:
                data = {}
                for ip, msgs in self._conversations.items():
                    data[ip] = [list(m) for m in msgs[-_MAX_HISTORY_PER_PEER:]]
                data["_names"] = dict(self._names)
                path = config.get_chat_history_path()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()

    def _trim_history(self, ip: str) -> None:
        """Keep at most _MAX_HISTORY_PER_PEER messages per peer."""
        msgs = self._conversations.get(ip)
        if msgs and len(msgs) > _MAX_HISTORY_PER_PEER:
            self._conversations[ip] = msgs[-_MAX_HISTORY_PER_PEER:]

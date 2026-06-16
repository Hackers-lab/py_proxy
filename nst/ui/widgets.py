"""Themed widget factories.

Every widget created here registers itself with the global :data:`theme` so it
recolors automatically on a light/dark switch.
"""

import tkinter as tk
from tkinter import ttk

from ..constants import BTN_FONT, LABEL_FONT
from ..theme import theme

# Stable, theme-independent avatar colors picked by name hash.
AVATAR_COLORS = ["#ef4444", "#f59e0b", "#10b981", "#3b82f6",
                 "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]


def avatar_color(name: str) -> str:
    return AVATAR_COLORS[sum(ord(c) for c in name) % len(AVATAR_COLORS)]


def make_avatar(parent, name: str, size: int = 34, bg_role: str = "panel") -> tk.Canvas:
    """A circular initials avatar (colored by name)."""
    cv = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                   bd=0, bg=theme.color(bg_role))
    theme.register(cv, bg=bg_role)
    color = avatar_color(name or "?")
    pad = 2
    cv.create_oval(pad, pad, size - pad, size - pad, fill=color, outline="")
    initial = next((c for c in name if c.isalnum()), "?").upper()
    cv.create_text(size / 2, size / 2 + 1, text=initial, fill="#ffffff",
                   font=("Segoe UI", int(size * 0.42), "bold"))
    return cv


def themed_button(parent, text, command, color_role="accent", width=24):
    """A flat action button whose base color follows ``color_role``."""
    c = theme.color(color_role)
    btn = tk.Button(
        parent, text=text, command=command,
        font=BTN_FONT, width=width, relief="flat", cursor="hand2",
        bg=c, fg=theme.color("text_pri"), activebackground=c,
        activeforeground=theme.color("text_pri"), bd=0, pady=6,
    )
    theme.register(btn, bg=color_role, activebackground=color_role,
                   fg="text_pri", activeforeground="text_pri")
    return btn


def themed_label(parent, text, color_role="text_pri", font=None, anchor="w",
                 bg_role="panel"):
    lbl = tk.Label(parent, text=text, bg=theme.color(bg_role),
                   fg=theme.color(color_role), font=font or LABEL_FONT, anchor=anchor)
    theme.register(lbl, bg=bg_role, fg=color_role)
    return lbl


class ScrollFrame(tk.Frame):
    """A vertically scrollable container.

    Pack/grid children into ``.body``. The body always matches the canvas width
    so content reflows instead of needing a horizontal scrollbar.  The scrollbar
    auto-hides when all content fits within the visible area.
    """

    def __init__(self, parent, bg_role: str = "panel", **kw) -> None:
        super().__init__(parent, bg=theme.color(bg_role), **kw)
        theme.register(self, bg=bg_role)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                bg=theme.color(bg_role))
        theme.register(self.canvas, bg=bg_role)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self._vsb_visible = False
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg=theme.color(bg_role))
        theme.register(self.body, bg=bg_role)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_body_configure(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._check_scrollbar()

    def _on_canvas_configure(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)
        self._check_scrollbar()

    def _check_scrollbar(self) -> None:
        """Show the scrollbar only when the content overflows the canvas."""
        self.update_idletasks()
        body_h = self.body.winfo_reqheight()
        canvas_h = self.canvas.winfo_height()
        needs_scroll = body_h > canvas_h
        if needs_scroll and not self._vsb_visible:
            self.vsb.pack(side="right", fill="y")
            self._vsb_visible = True
        elif not needs_scroll and self._vsb_visible:
            self.vsb.pack_forget()
            self._vsb_visible = False

    def _on_wheel(self, e) -> None:
        if self._vsb_visible:
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    def scroll_to_bottom(self) -> None:
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

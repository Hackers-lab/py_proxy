"""Bottom-right toast notifications for incoming chat messages.

Toasts stack upward, auto-dismiss after a few seconds, and forward clicks to a
callback (used to focus the app and open the sender's conversation).
"""

import ctypes
import tkinter as tk

from ..constants import LABEL_FONT
from ..theme import theme

_WIDTH = 300
_GAP = 8
_MARGIN = 12
_LIFETIME_MS = 6000
_MAX_VISIBLE = 5


def _work_area() -> tuple[int, int, int, int]:
    """Screen work area (excludes the taskbar): (left, top, right, bottom)."""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1040


class _Toast:
    def __init__(self, manager: "ToastManager", title: str, body: str,
                 peer_ip: str) -> None:
        self.manager = manager
        self.peer_ip = peer_ip
        self.height = 0

        win = tk.Toplevel(manager.root)
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        self.win = win

        panel = theme.color("panel")
        accent = theme.color("accent")
        border = theme.color("border")

        outer = tk.Frame(win, bg=border)
        outer.pack(fill="both", expand=True)
        card = tk.Frame(outer, bg=panel)
        card.pack(fill="both", expand=True, padx=1, pady=1)

        # Accent stripe down the left edge.
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")

        inner = tk.Frame(card, bg=panel)
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=8)

        head = tk.Frame(inner, bg=panel)
        head.pack(fill="x")
        tk.Label(head, text="💬", bg=panel, fg=accent,
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Label(head, text=title, bg=panel, fg=theme.color("text_pri"),
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=(4, 0))
        close = tk.Label(head, text="✕", bg=panel, fg=theme.color("text_sec"),
                         font=("Segoe UI", 9), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: (self.manager.dismiss(self), "break"))

        msg = tk.Label(inner, text=body, bg=panel, fg=theme.color("text_sec"),
                       font=LABEL_FONT, anchor="w", justify="left",
                       wraplength=_WIDTH - 40)
        msg.pack(fill="x", pady=(2, 0))

        # Click anywhere (except the close button) opens the conversation.
        for w in (win, outer, card, inner, head, msg):
            w.bind("<Button-1>", self._on_click)

        win.update_idletasks()
        self.height = max(win.winfo_reqheight(), 48)

    def _on_click(self, _event=None):
        self.manager.on_click(self.peer_ip)
        self.manager.dismiss(self)
        return "break"

    def place(self, x: int, y: int) -> None:
        try:
            self.win.geometry(f"{_WIDTH}x{self.height}+{x}+{y}")
        except Exception:
            pass

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass


class ToastManager:
    def __init__(self, root: tk.Tk, on_click=lambda ip: None) -> None:
        self.root = root
        self.on_click = on_click
        self._toasts: list[_Toast] = []

    def notify(self, title: str, body: str, peer_ip: str) -> None:
        while len(self._toasts) >= _MAX_VISIBLE:
            self.dismiss(self._toasts[0])
        toast = _Toast(self, title, body, peer_ip)
        self._toasts.append(toast)
        self._reflow()
        self.root.after(_LIFETIME_MS, lambda: self.dismiss(toast))

    def dismiss(self, toast: "_Toast") -> None:
        if toast not in self._toasts:
            return
        self._toasts.remove(toast)
        toast.destroy()
        self._reflow()

    def _reflow(self) -> None:
        left, top, right, bottom = _work_area()
        x = right - _WIDTH - _MARGIN
        y = bottom - _MARGIN
        # Newest toast sits closest to the tray; older ones pushed up.
        for toast in reversed(self._toasts):
            y -= toast.height
            if y < top:
                toast.win.withdraw()
                continue
            toast.place(x, y)
            y -= _GAP

    def destroy_all(self) -> None:
        for toast in list(self._toasts):
            toast.destroy()
        self._toasts.clear()

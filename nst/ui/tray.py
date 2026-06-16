"""System-tray icon, taskbar speed icon drawing, and the tray-anchored
speed overlay window."""

import os
import tkinter as tk

from ..win_utils import get_resource_path, get_tray_notify_rect, set_clickthrough

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def get_tiny_font(size: int = 9):
    if not HAS_PIL:
        return None
    for name in ("tahoma.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_tray_icon():
    """Load the app icon, else draw a fallback blue ring in memory."""
    if not HAS_PIL:
        return None
    icon_png = get_resource_path("icon.png")
    if os.path.exists(icon_png):
        try:
            return Image.open(icon_png)
        except Exception:
            pass
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, size - 4, size - 4], fill=(59, 130, 246, 255))
    d.ellipse([18, 18, size - 18, size - 18], fill=(26, 29, 35, 255))
    return img


def draw_speed_icon(up_speed_str: str, down_speed_str: str, font):
    """A 32×32 tray icon showing up/down speed on two lines."""
    if not HAS_PIL:
        return None
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, 31, 31], radius=4, fill=(26, 29, 35, 240))
    draw.text((2, 2), f"▲{up_speed_str}", fill=(245, 158, 11, 255), font=font)
    draw.text((2, 16), f"▼{down_speed_str}", fill=(34, 197, 94, 255), font=font)
    return img


class SpeedOverlay:
    """A small click-through window pinned next to the clock showing live speed."""

    def __init__(self) -> None:
        self._win: tk.Toplevel | None = None
        self._up_lbl: tk.Label | None = None
        self._down_lbl: tk.Label | None = None

    def show(self, up_str: str, down_str: str) -> None:
        if self._win is None:
            self._win = tk.Toplevel()
            self._win.overrideredirect(True)
            self._win.wm_attributes("-topmost", True)
            self._win.config(bg="#010101")
            self._win.attributes("-transparentcolor", "#010101")

            self._up_lbl = tk.Label(
                self._win, text="", fg="#f59e0b", bg="#010101",
                font=("Segoe UI", 8, "bold"), anchor="w")
            self._up_lbl.pack(anchor="w", fill="x")

            self._down_lbl = tk.Label(
                self._win, text="", fg="#22c55e", bg="#010101",
                font=("Segoe UI", 8, "bold"), anchor="w")
            self._down_lbl.pack(anchor="w", fill="x")

            self._win.update_idletasks()
            set_clickthrough(self._win.winfo_id())

        self._up_lbl.config(text=f"U: {up_str}")
        self._down_lbl.config(text=f"D: {down_str}")

        rect = get_tray_notify_rect()
        if rect:
            left, top, right, bottom = rect
            ow, oh = 95, 34
            if (bottom - top) < (right - left):  # horizontal taskbar
                x = left - ow - 8
                y = top + (bottom - top - oh) // 2
            else:
                x = left + (right - left - ow) // 2
                y = top - oh - 8
            self._win.geometry(f"{ow}x{oh}+{x}+{y}")
            self._win.deiconify()
        else:
            self._win.withdraw()

    def hide(self) -> None:
        if self._win is not None:
            self._win.withdraw()

    def destroy(self) -> None:
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None

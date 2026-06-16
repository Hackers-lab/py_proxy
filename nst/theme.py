"""Runtime light/dark theming.

Colors are no longer hard-coded at widget-creation time. Instead each widget is
*registered* with a mapping of ``tk option -> palette role`` (e.g.
``bg="panel"``). When the theme changes, :meth:`Theme.apply` walks the registry
and re-applies every option from the active palette, so the whole UI recolors
live without rebuilding it.

State-driven widgets (buttons that turn green/red) should read colors through
``theme.color("success")`` and re-apply themselves from an :meth:`on_change`
callback rather than registering a fixed role.
"""

import tkinter as tk

# Palette roles (identical key sets for both themes):
#   bg            window background
#   panel         raised card / frame background
#   accent        primary accent (host, links, primary buttons)
#   accent2       secondary accent (client / purple)
#   accent2_text  readable secondary-accent text on a panel
#   proxy_btn     proxy button base color
#   success_btn   "enable" button base color
#   success / danger / warning  status colors
#   text_pri      primary text
#   text_sec      muted / secondary text
#   border        thin divider / frame border
#   entry_bg      text entry background
#   log_bg        log / console background
#   select_bg     listbox selection background

DARK = {
    "bg":           "#1a1d23",
    "panel":        "#22262f",
    "accent":       "#3b82f6",
    "accent2":      "#7c3aed",
    "accent2_text": "#a78bfa",
    "proxy_btn":    "#7c3aed",
    "success_btn":  "#16a34a",
    "success":      "#22c55e",
    "danger":       "#ef4444",
    "warning":      "#f59e0b",
    "text_pri":     "#f1f5f9",
    "text_sec":     "#94a3b8",
    "border":       "#2e3340",
    "entry_bg":     "#2e3340",
    "log_bg":       "#12151b",
    "select_bg":    "#2e3340",
    "panel2":       "#2a2f3a",
    "bubble_in":    "#2a2f3a",
    "bubble_out":   "#2563eb",
    "bubble_in_tx": "#e6edf6",
    "bubble_out_tx": "#ffffff",
    "hover":        "#2a2f3a",
}

LIGHT = {
    "bg":           "#eef1f6",
    "panel":        "#ffffff",
    "accent":       "#2563eb",
    "accent2":      "#7c3aed",
    "accent2_text": "#6d28d9",
    "proxy_btn":    "#7c3aed",
    "success_btn":  "#16a34a",
    "success":      "#16a34a",
    "danger":       "#dc2626",
    "warning":      "#d97706",
    "text_pri":     "#0f172a",
    "text_sec":     "#64748b",
    "border":       "#d6dbe4",
    "entry_bg":     "#f1f5f9",
    "log_bg":       "#f8fafc",
    "select_bg":    "#dbeafe",
    "panel2":       "#eef2f7",
    "bubble_in":    "#eef2f7",
    "bubble_out":   "#2563eb",
    "bubble_in_tx": "#0f172a",
    "bubble_out_tx": "#ffffff",
    "hover":        "#eef2f7",
}

PALETTES = {"dark": DARK, "light": LIGHT}


class Theme:
    def __init__(self, name: str = "dark") -> None:
        self.name = name if name in PALETTES else "dark"
        self.palette = PALETTES[self.name]
        # registry entries: (widget, {tk_option: role_key})
        self._widgets: list[tuple[tk.Misc, dict[str, str]]] = []
        self._callbacks: list = []

    # ── palette access ────────────────────────────────────────────────────────
    def color(self, role: str) -> str:
        return self.palette.get(role, "#ff00ff")

    def is_dark(self) -> bool:
        return self.name == "dark"

    # ── registration ──────────────────────────────────────────────────────────
    def register(self, widget: tk.Misc, **role_map: str) -> tk.Misc:
        """Register ``widget`` so each ``option=role`` recolors on theme change.

        Applies the current palette immediately and returns the widget for
        convenient inline use.
        """
        self._widgets.append((widget, role_map))
        self._apply_one(widget, role_map)
        return widget

    def on_change(self, callback) -> None:
        """Register a callback fired after every theme apply (for ttk styles,
        state-driven buttons, custom drawing, etc.)."""
        self._callbacks.append(callback)

    # ── apply / switch ────────────────────────────────────────────────────────
    def _apply_one(self, widget: tk.Misc, role_map: dict[str, str]) -> bool:
        opts = {opt: self.palette[role] for opt, role in role_map.items()
                if role in self.palette}
        try:
            widget.configure(**opts)
            return True
        except tk.TclError:
            return False  # widget destroyed — drop it

    def apply(self) -> None:
        alive: list[tuple[tk.Misc, dict[str, str]]] = []
        for widget, role_map in self._widgets:
            if self._apply_one(widget, role_map):
                alive.append((widget, role_map))
        self._widgets = alive
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                pass

    def set_theme(self, name: str) -> None:
        if name not in PALETTES:
            return
        self.name = name
        self.palette = PALETTES[name]
        self.apply()

    def toggle(self) -> str:
        self.set_theme("light" if self.name == "dark" else "dark")
        return self.name


# Module-level singleton shared across the UI.
theme = Theme()

"""Qt theming: a single QSS stylesheet generated from a light/dark palette.

Unlike the old Tkinter theme (which recolored each widget by hand), Qt lets us
push one application-wide stylesheet and let property selectors do the work.
Switching themes is therefore just ``app.setStyleSheet(theme.qss())`` again —
every bubble, button and row restyles instantly with no widget rebuild.
"""

from PyQt6.QtCore import QObject, pyqtSignal

from .. import config

DARK = {
    "bg": "#15171c", "panel": "#1e2229", "panel2": "#262b34",
    "accent": "#3b82f6", "accent2": "#7c3aed", "accent2_text": "#a78bfa",
    "proxy_btn": "#7c3aed", "success_btn": "#16a34a", "chat_btn": "#14b8a6",
    "success": "#22c55e", "danger": "#ef4444", "warning": "#f59e0b",
    "text_pri": "#f1f5f9", "text_sec": "#94a3b8", "border": "#2e3340",
    "entry_bg": "#2a2f3a", "log_bg": "#101318", "select_bg": "#2b3champ",
    "hover": "#252b35", "bubble_in": "#262b34", "bubble_out": "#2563eb",
    "bubble_in_tx": "#e6edf6", "bubble_out_tx": "#ffffff", "scroll": "#39414f",
}
# (typo guard — keep select_bg valid)
DARK["select_bg"] = "#2b313d"

LIGHT = {
    "bg": "#eef1f6", "panel": "#ffffff", "panel2": "#f1f5f9",
    "accent": "#2563eb", "accent2": "#7c3aed", "accent2_text": "#6d28d9",
    "proxy_btn": "#7c3aed", "success_btn": "#16a34a", "chat_btn": "#0d9488",
    "success": "#16a34a", "danger": "#dc2626", "warning": "#d97706",
    "text_pri": "#0f172a", "text_sec": "#64748b", "border": "#d6dbe4",
    "entry_bg": "#f1f5f9", "log_bg": "#f8fafc", "select_bg": "#dbeafe",
    "hover": "#eef2f7", "bubble_in": "#eef2f7", "bubble_out": "#2563eb",
    "bubble_in_tx": "#0f172a", "bubble_out_tx": "#ffffff", "scroll": "#c3cbd9",
}

PALETTES = {"dark": DARK, "light": LIGHT}

# Stable avatar colors picked by name hash (theme-independent).
AVATAR_COLORS = ["#ef4444", "#f59e0b", "#10b981", "#3b82f6",
                 "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]


def avatar_color(name: str) -> str:
    return AVATAR_COLORS[sum(ord(c) for c in (name or "?")) % len(AVATAR_COLORS)]


class QtTheme(QObject):
    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        name = config.load_theme()
        self.name = name if name in PALETTES else "dark"
        self.p = PALETTES[self.name]

    def color(self, role: str) -> str:
        return self.p.get(role, "#ff00ff")

    def is_dark(self) -> bool:
        return self.name == "dark"

    def set_theme(self, name: str) -> None:
        if name not in PALETTES:
            return
        self.name = name
        self.p = PALETTES[name]
        config.save_theme(name)
        self.changed.emit()

    def toggle(self) -> str:
        self.set_theme("light" if self.name == "dark" else "dark")
        return self.name

    # ── stylesheet ──────────────────────────────────────────────────────────
    def qss(self) -> str:
        p = self.p
        return f"""
        * {{
            font-family: 'Segoe UI', 'Segoe UI Variable', sans-serif;
            font-size: 13px;
            color: {p['text_pri']};
            outline: 0;
        }}
        QWidget {{ background: {p['bg']}; }}
        QMainWindow, QDialog {{ background: {p['bg']}; }}
        QToolTip {{ background: {p['panel2']}; color: {p['text_pri']};
                    border: 1px solid {p['border']}; padding: 4px; }}

        QFrame#card {{ background: {p['panel']}; border: 1px solid {p['border']};
                       border-radius: 12px; }}
        QFrame#card2 {{ background: {p['panel2']}; border-radius: 10px; }}
        QFrame#divider {{ background: {p['border']}; max-width: 1px; border: none; }}
        QFrame#hdivider {{ background: {p['border']}; max-height: 1px; border: none; }}

        QLabel {{ background: transparent; }}
        QLabel#h1 {{ font-size: 18px; font-weight: 700; }}
        QLabel#title {{ font-size: 15px; font-weight: 700; }}
        QLabel#section {{ color: {p['text_sec']}; font-size: 10px; font-weight: 700;
                          letter-spacing: 1px; }}
        QLabel#muted {{ color: {p['text_sec']}; }}
        QLabel#accent {{ color: {p['accent']}; font-weight: 600; }}

        QLineEdit {{
            background: {p['entry_bg']}; border: 1px solid {p['border']};
            border-radius: 9px; padding: 7px 11px;
            selection-background-color: {p['accent']}; selection-color: #fff;
        }}
        QLineEdit:focus {{ border: 1px solid {p['accent']}; }}
        QPlainTextEdit, QTextEdit {{
            background: {p['log_bg']}; border: 1px solid {p['border']};
            border-radius: 10px; padding: 6px;
            selection-background-color: {p['accent']};
        }}
        QPlainTextEdit#composer {{
            background: {p['entry_bg']}; border: 1px solid {p['border']};
            border-radius: 12px;
        }}
        QPlainTextEdit#composer:focus {{ border: 1px solid {p['accent']}; }}

        QPushButton {{
            background: {p['panel2']}; color: {p['text_pri']};
            border: 1px solid {p['border']}; border-radius: 9px;
            padding: 8px 14px; font-weight: 600;
        }}
        QPushButton:hover {{ background: {p['hover']}; }}
        QPushButton:disabled {{ color: {p['text_sec']}; }}
        QPushButton[variant="accent"] {{ background: {p['accent']}; color: #fff; border: none; }}
        QPushButton[variant="accent"]:hover {{ background: {p['accent']}; }}
        QPushButton[variant="success"] {{ background: {p['success_btn']}; color: #fff; border: none; }}
        QPushButton[variant="danger"] {{ background: {p['danger']}; color: #fff; border: none; }}
        QPushButton[variant="proxy"] {{ background: {p['proxy_btn']}; color: #fff; border: none; }}
        QPushButton[variant="chat"] {{ background: {p['chat_btn']}; color: #fff; border: none; }}
        QPushButton[variant="ghost"] {{ background: transparent; border: none;
                                        color: {p['text_sec']}; }}
        QPushButton[variant="ghost"]:hover {{ color: {p['text_pri']}; background: {p['hover']}; }}

        QPushButton#navItem {{ background: transparent; border: none; border-radius: 10px;
                               text-align: left; padding: 10px 14px; color: {p['text_sec']}; }}
        QPushButton#navItem:hover {{ background: {p['hover']}; color: {p['text_pri']}; }}
        QPushButton#navItem[active="true"] {{ background: {p['panel']}; color: {p['accent']}; }}

        QToolButton {{ background: transparent; border: none; border-radius: 8px;
                       padding: 4px; color: {p['text_sec']}; }}
        QToolButton:hover {{ background: {p['hover']}; color: {p['text_pri']}; }}

        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px;
                                border: 1px solid {p['border']}; background: {p['entry_bg']}; }}
        QCheckBox::indicator:checked {{ background: {p['accent']}; border: 1px solid {p['accent']}; }}

        QMenu {{ background: {p['panel2']}; border: 1px solid {p['border']};
                 border-radius: 8px; padding: 5px; }}
        QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
        QMenu::item:selected {{ background: {p['accent']}; color: #fff; }}
        QMenu::separator {{ height: 1px; background: {p['border']}; margin: 5px 8px; }}
        QMenuBar {{ background: {p['bg']}; }}
        QMenuBar::item {{ background: transparent; padding: 5px 10px; }}
        QMenuBar::item:selected {{ background: {p['hover']}; border-radius: 6px; }}

        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {p['scroll']}; border-radius: 4px; min-height: 30px; }}
        QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QScrollBar:horizontal {{ height: 0; }}

        /* roster rows */
        QFrame#rosterRow {{
            background: transparent;
            border-radius: 8px;
            border: none;
            border-left: 3px solid transparent;
        }}
        QFrame#rosterRow:hover {{
            background: {p['hover']};
        }}
        QFrame#rosterRow[active="true"] {{
            background: {p['select_bg']};
            border-left: 3px solid {p['accent']};
        }}

        QLabel#unread {{
            background: {p['accent']}; color: #fff; font-size: 10px;
            font-weight: 700; border-radius: 9px; padding: 1px 7px;
            min-width: 18px;
        }}

        /* drag-drop overlay */
        QFrame#dropZone {{
            background: rgba(59,130,246,0.13);
            border: 2px dashed {p['accent']};
            border-radius: 14px;
        }}
        QLabel#dropZoneLabel {{
            color: {p['accent']}; font-size: 18px; font-weight: 700;
            background: transparent; border: none;
        }}

        /* chat bubbles */
        QFrame[bubble="out"] {{ background: {p['bubble_out']}; border-radius: 14px; }}
        QFrame[bubble="in"]  {{ background: {p['bubble_in']};  border-radius: 14px; }}
        QFrame#replyBar {{ background: {p['panel2']}; border-radius: 10px; }}
        QFrame#quote {{ background: rgba(127,127,127,0.12); border-radius: 7px; }}

        /* combo / spin / slider / tabs / lists (settings) */
        QComboBox, QSpinBox {{
            background: {p['entry_bg']}; border: 1px solid {p['border']};
            border-radius: 9px; padding: 6px 10px; min-height: 18px;
        }}
        QComboBox:focus, QSpinBox:focus {{ border: 1px solid {p['accent']}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{
            background: {p['panel2']}; border: 1px solid {p['border']};
            border-radius: 8px; selection-background-color: {p['accent']};
            selection-color: #fff; outline: 0;
        }}
        QSlider::groove:horizontal {{ height: 5px; background: {p['border']};
                                      border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {p['accent']}; width: 15px;
                                      margin: -6px 0; border-radius: 7px; }}

        QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 10px;
                            top: -1px; }}
        QTabBar::tab {{ background: transparent; color: {p['text_sec']};
                        padding: 8px 14px; border-radius: 8px; margin: 2px; }}
        QTabBar::tab:selected {{ background: {p['panel']}; color: {p['accent']};
                                 font-weight: 700; }}
        QTabBar::tab:hover {{ background: {p['hover']}; }}

        QListWidget {{ background: {p['entry_bg']}; border: 1px solid {p['border']};
                       border-radius: 10px; padding: 4px; }}
        QListWidget::item {{ border-radius: 6px; padding: 2px; }}
        QListWidget::item:selected {{ background: {p['select_bg']}; color: {p['text_pri']}; }}
        """


theme = QtTheme()

"""Registry-backed persistent settings.

Autostart lives under the standard ``...\\CurrentVersion\\Run`` key; everything
else lives under ``HKCU\\Software\\NetSplitTunnel``.
"""

import os
import socket
import sys
import winreg

from .constants import REG_APP_PATH, REG_RUN_PATH, RUN_VALUE_NAME

# ── Autostart ─────────────────────────────────────────────────────────────────

def set_autostart(enabled: bool) -> tuple[bool, str]:
    try:
        if getattr(sys, "frozen", False):
            path = f'"{sys.executable}"'
        else:
            path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH,
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, path)
            msg = "Autostart enabled in registry."
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
                msg = "Autostart disabled in registry."
            except FileNotFoundError:
                msg = "Autostart was already disabled."
        winreg.CloseKey(key)
        return True, msg
    except Exception as e:
        return False, f"Registry update failed: {e}"


def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH,
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            enabled = True
        except FileNotFoundError:
            enabled = False
        winreg.CloseKey(key)
        return enabled
    except Exception:
        return False

# ── Generic app-key helpers ───────────────────────────────────────────────────

def _read_value(name: str, default):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_APP_PATH, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            val = default
        winreg.CloseKey(key)
        return val
    except Exception:
        return default


def _write_value(name: str, regtype: int, value) -> bool:
    try:
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_APP_PATH)
        except Exception:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_APP_PATH,
                                 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, name, 0, regtype, value)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

# ── Show speed in taskbar ─────────────────────────────────────────────────────

def load_show_speed_in_taskbar() -> bool:
    return bool(_read_value("ShowSpeedInTaskbar", 0))


def save_show_speed_in_taskbar(enabled: bool) -> bool:
    return _write_value("ShowSpeedInTaskbar", winreg.REG_DWORD, 1 if enabled else 0)

# ── Theme preference ──────────────────────────────────────────────────────────

def load_theme() -> str:
    """Return 'dark' or 'light' (default 'light')."""
    val = _read_value("Theme", "light")
    return "light" if str(val).lower() == "light" else "dark"


def save_theme(name: str) -> bool:
    return _write_value("Theme", winreg.REG_SZ, "light" if name == "light" else "dark")

# ── Chat display name ─────────────────────────────────────────────────────────

def load_display_name() -> str:
    """Return the user's chat display name (defaults to the hostname)."""
    default = socket.gethostname() or "PC"
    val = _read_value("DisplayName", default)
    val = str(val).strip()
    return val or default


def save_display_name(name: str) -> bool:
    name = (name or "").strip()[:32]
    if not name:
        return False
    return _write_value("DisplayName", winreg.REG_SZ, name)

# ── IP chat toggle ───────────────────────────────────────────────────────────

def load_ip_chat_enabled() -> bool:
    return bool(_read_value("IpChatEnabled", 1))


def save_ip_chat_enabled(enabled: bool) -> bool:
    return _write_value("IpChatEnabled", winreg.REG_DWORD, 1 if enabled else 0)

# ── Chat history path ────────────────────────────────────────────────────────

def get_chat_history_path() -> str:
    """Legacy single-file history path (kept for migration reads only)."""
    appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    folder = os.path.join(appdata, "NetSplitTunnel")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "chat_history.json")


def get_peer_chat_dir() -> str:
    """Return the directory where per-peer chat JSON files are stored.

    Each peer is saved as ``{safe_ip}.json`` inside this directory.
    Creates the directory if it doesn't already exist.
    """
    appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    folder = os.path.join(appdata, "NetSplitTunnel", "chats")
    os.makedirs(folder, exist_ok=True)
    return folder

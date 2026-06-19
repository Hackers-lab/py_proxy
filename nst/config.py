"""Registry-backed persistent settings.

Autostart lives under the standard ``...\\CurrentVersion\\Run`` key; everything
else lives under ``HKCU\\Software\\NetSplitTunnel``.
"""

import json
import os
import socket
import sys
import uuid
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


def _read_json(name: str, default):
    """Read a JSON-encoded value from the app key, falling back to *default*."""
    raw = _read_value(name, None)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _write_json(name: str, value) -> bool:
    try:
        return _write_value(name, winreg.REG_SZ, json.dumps(value))
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

# ── Device identity (internal unique ID + device name) ─────────────────────────

def load_device_id() -> str:
    """Return this install's stable internal unique ID, generating it once.

    Identity for routing stays IP-based (see update.md #4); this UID is an
    informational stable handle that survives IP changes and restarts.
    """
    val = str(_read_value("DeviceId", "")).strip()
    if not val:
        val = uuid.uuid4().hex
        _write_value("DeviceId", winreg.REG_SZ, val)
    return val


def get_device_name() -> str:
    """The computer/device name shown alongside the display name."""
    return socket.gethostname() or "PC"

# ── IP chat toggle ───────────────────────────────────────────────────────────

def load_ip_chat_enabled() -> bool:
    return bool(_read_value("IpChatEnabled", 1))


def save_ip_chat_enabled(enabled: bool) -> bool:
    return _write_value("IpChatEnabled", winreg.REG_DWORD, 1 if enabled else 0)

# ── Notifications (popup toasts) ──────────────────────────────────────────────

def load_notifications_enabled() -> bool:
    return bool(_read_value("NotificationsEnabled", 1))


def save_notifications_enabled(enabled: bool) -> bool:
    return _write_value("NotificationsEnabled", winreg.REG_DWORD, 1 if enabled else 0)

# ── Presence status (manual: online / away / invisible) ───────────────────────

def load_my_status() -> str:
    """Return the saved presence status: 'online', 'away', or 'invisible'."""
    val = str(_read_value("MyStatus", "online")).strip().lower()
    return val if val in ("online", "away", "invisible") else "online"


def save_my_status(status: str) -> bool:
    status = status.strip().lower()
    if status not in ("online", "away", "invisible"):
        status = "online"
    return _write_value("MyStatus", winreg.REG_SZ, status)


# Keep legacy helpers so existing code doesn't break during migration.
def load_presence_online() -> bool:
    return load_my_status() != "invisible"


def save_presence_online(online: bool) -> bool:
    return save_my_status("online" if online else "invisible")

# ── Hidden (deleted) peers ────────────────────────────────────────────────────

def load_hidden_peers() -> list[str]:
    """IPs the user has deleted; kept out of the roster until they make contact."""
    val = str(_read_value("HiddenPeers", "")).strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def save_hidden_peers(ips: list[str]) -> bool:
    return _write_value("HiddenPeers", winreg.REG_SZ, ",".join(sorted(set(ips))))

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


# ── Storage & retention (update.md #17) ───────────────────────────────────────

# Allowed retention windows in days; 0 means "Forever".
RETENTION_CHOICES = (7, 30, 90, 180, 0)


def load_retention_days() -> int:
    """Days of chat history to keep (0 = forever). Defaults to forever."""
    try:
        val = int(_read_value("RetentionDays", 0))
    except (TypeError, ValueError):
        val = 0
    return val if val in RETENTION_CHOICES else 0


def save_retention_days(days: int) -> bool:
    days = days if days in RETENTION_CHOICES else 0
    return _write_value("RetentionDays", winreg.REG_DWORD, days)


def default_download_dir() -> str:
    """The built-in download/save folder (Documents\\NetSplitter)."""
    return os.path.join(os.path.expanduser("~"), "Documents", "NetSplitter")


def load_download_dir() -> str:
    """Folder received files are saved to (configurable)."""
    val = str(_read_value("DownloadDir", "")).strip()
    folder = val or default_download_dir()
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        folder = default_download_dir()
        os.makedirs(folder, exist_ok=True)
    return folder


def save_download_dir(path: str) -> bool:
    path = (path or "").strip()
    return _write_value("DownloadDir", winreg.REG_SZ, path)


def load_max_file_mb() -> int:
    """Maximum allowed file-transfer size in MB (0 = unlimited)."""
    try:
        return max(0, int(_read_value("MaxFileMB", 0)))
    except (TypeError, ValueError):
        return 0


def save_max_file_mb(mb: int) -> bool:
    return _write_value("MaxFileMB", winreg.REG_DWORD, max(0, int(mb)))


def load_file_expiry_min() -> int:
    """Minutes a sender keeps an unanswered file offer alive (default 1)."""
    try:
        return max(1, int(_read_value("FileExpiryMin", 1)))
    except (TypeError, ValueError):
        return 1


def save_file_expiry_min(minutes: int) -> bool:
    return _write_value("FileExpiryMin", winreg.REG_DWORD, max(1, int(minutes)))

# ── General behaviour ──────────────────────────────────────────────────────────

def load_minimize_to_tray() -> bool:
    """Minimise to the system tray when a window is closed (default on)."""
    return bool(_read_value("MinimizeToTray", 1))


def save_minimize_to_tray(enabled: bool) -> bool:
    return _write_value("MinimizeToTray", winreg.REG_DWORD, 1 if enabled else 0)


def load_restore_session() -> bool:
    """Re-open the last conversation when chat starts (default on)."""
    return bool(_read_value("RestoreSession", 1))


def save_restore_session(enabled: bool) -> bool:
    return _write_value("RestoreSession", winreg.REG_DWORD, 1 if enabled else 0)


def load_last_active_chat() -> str:
    return str(_read_value("LastActiveChat", "")).strip()


def save_last_active_chat(key: str) -> bool:
    return _write_value("LastActiveChat", winreg.REG_SZ, str(key or ""))

# ── Notification preferences (update.md #16, settings module) ──────────────────

# Notification "channels" that can be toggled per conversation scope.
NOTIFY_CHANNELS = ("sound", "popup", "taskbar", "tray")
# Conversation scopes that carry independent notification settings.
NOTIFY_SCOPES = ("private", "group", "broadcast")


def _default_notify_prefs() -> dict:
    return {scope: {ch: True for ch in NOTIFY_CHANNELS} for scope in NOTIFY_SCOPES}


def load_notify_prefs() -> dict:
    """Per-scope notification channel toggles. Missing keys default to on."""
    prefs = _default_notify_prefs()
    saved = _read_json("NotifyPrefs", {})
    if isinstance(saved, dict):
        for scope in NOTIFY_SCOPES:
            sd = saved.get(scope)
            if isinstance(sd, dict):
                for ch in NOTIFY_CHANNELS:
                    if ch in sd:
                        prefs[scope][ch] = bool(sd[ch])
    return prefs


def save_notify_prefs(prefs: dict) -> bool:
    return _write_json("NotifyPrefs", prefs)


def load_sound_volume() -> int:
    """Notification sound volume, 0–100 (default 80)."""
    try:
        return max(0, min(100, int(_read_value("SoundVolume", 80))))
    except (TypeError, ValueError):
        return 80


def save_sound_volume(vol: int) -> bool:
    return _write_value("SoundVolume", winreg.REG_DWORD, max(0, min(100, int(vol))))


def load_mute_all() -> bool:
    return bool(_read_value("MuteAll", 0))


def save_mute_all(enabled: bool) -> bool:
    return _write_value("MuteAll", winreg.REG_DWORD, 1 if enabled else 0)


def load_do_not_disturb() -> bool:
    return bool(_read_value("DoNotDisturb", 0))


def save_do_not_disturb(enabled: bool) -> bool:
    return _write_value("DoNotDisturb", winreg.REG_DWORD, 1 if enabled else 0)


def load_raise_on_message() -> bool:
    """Bring the chat window to the front on a new background message.

    When on, the window is raised and the bottom-right popup is suppressed;
    when off, the popup card shows instead (default off — non-intrusive).
    """
    return bool(_read_value("RaiseOnMessage", 0))


def save_raise_on_message(enabled: bool) -> bool:
    return _write_value("RaiseOnMessage", winreg.REG_DWORD, 1 if enabled else 0)

# ── Blocked users (update.md #12) ──────────────────────────────────────────────

def load_blocked_users() -> list[dict]:
    """Return ``[{"ip": .., "name": ..}, …]`` of permanently blocked users."""
    val = _read_json("BlockedUsers", [])
    out = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict) and item.get("ip"):
                out.append({"ip": str(item["ip"]), "name": str(item.get("name", item["ip"]))})
    return out


def save_blocked_users(users: list[dict]) -> bool:
    clean, seen = [], set()
    for u in users:
        ip = str(u.get("ip", "")).strip()
        if ip and ip not in seen:
            seen.add(ip)
            clean.append({"ip": ip, "name": str(u.get("name", ip))})
    return _write_json("BlockedUsers", clean)



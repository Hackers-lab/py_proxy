"""Registry-backed persistent settings.

Autostart uses the HKCU ``Run`` key. The app runs as a normal user (no UAC
manifest), so the Run key starts it silently at logon — no Task Scheduler
workaround is needed. The installer also writes this value; the in-app toggle
lets users opt out.

Everything else lives under ``HKCU\\Software\\NetSplitTunnel``.
"""

import json
import os
import socket
import subprocess
import sys
import uuid
import winreg

from .constants import REG_APP_PATH, REG_RUN_PATH, RUN_VALUE_NAME

# ── Autostart (HKCU Run key) ──────────────────────────────────────────────────

def _autostart_command() -> str:
    """The command Windows runs at logon.

    The --autostart flag makes the app start to the tray (no main window).
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" --autostart'


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Add/remove the HKCU Run-key value that launches the app at logon."""
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH)
        try:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        return False, f"Autostart registry error: {e}"

    # Best-effort: clean up the old scheduled task left by pre-4.9.3 installs.
    if enabled:
        try:
            subprocess.run(f'schtasks /delete /f /tn "{RUN_VALUE_NAME}"',
                           shell=True, capture_output=True, text=True)
        except Exception:
            pass

    return True, f"Autostart {'enabled' if enabled else 'disabled'}."


def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0,
                             winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
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
    """Minutes a sender keeps an unanswered file offer alive (default 3)."""
    try:
        return max(1, int(_read_value("FileExpiryMin", 3)))
    except (TypeError, ValueError):
        return 3


def save_file_expiry_min(minutes: int) -> bool:
    return _write_value("FileExpiryMin", winreg.REG_DWORD, max(1, int(minutes)))

# ── Remote screen (view + control) ────────────────────────────────────────────

def load_remote_enabled() -> bool:
    """Whether this PC accepts incoming screen-view/control sessions at all."""
    return bool(_read_value("RemoteEnabled", 1))


def save_remote_enabled(enabled: bool) -> bool:
    return _write_value("RemoteEnabled", winreg.REG_DWORD, 1 if enabled else 0)


def load_remote_unattended() -> bool:
    """Whether a peer presenting the correct secret connects without a prompt.

    Off by default — unattended access is effectively a backdoor, so the user
    must opt in explicitly and set a secret.
    """
    return bool(_read_value("RemoteUnattended", 0))


def save_remote_unattended(enabled: bool) -> bool:
    return _write_value("RemoteUnattended", winreg.REG_DWORD, 1 if enabled else 0)


def load_remote_secret() -> str:
    """The shared secret that authorises unattended access (empty = none)."""
    return str(_read_value("RemoteSecret", "")).strip()


def save_remote_secret(secret: str) -> bool:
    return _write_value("RemoteSecret", winreg.REG_SZ, (secret or "").strip())


def load_remote_quality() -> int:
    """JPEG quality (1-100) the host encodes screen frames at."""
    from .constants import SCREEN_QUALITY
    try:
        return min(95, max(20, int(_read_value("RemoteQuality", SCREEN_QUALITY))))
    except (TypeError, ValueError):
        return SCREEN_QUALITY


def save_remote_quality(quality: int) -> bool:
    return _write_value("RemoteQuality", winreg.REG_DWORD,
                        min(95, max(20, int(quality))))


def load_remote_fps() -> int:
    """Frames per second the host streams (higher = smoother, more bandwidth)."""
    from .constants import SCREEN_FPS
    try:
        return min(30, max(1, int(_read_value("RemoteFps", SCREEN_FPS))))
    except (TypeError, ValueError):
        return SCREEN_FPS


def save_remote_fps(fps: int) -> bool:
    return _write_value("RemoteFps", winreg.REG_DWORD, min(30, max(1, int(fps))))


def load_remote_timeout() -> int:
    """Seconds the host waits for the user to accept an incoming request."""
    from .constants import SCREEN_REQUEST_TIMEOUT
    try:
        return min(600, max(10, int(_read_value("RemoteTimeout", SCREEN_REQUEST_TIMEOUT))))
    except (TypeError, ValueError):
        return SCREEN_REQUEST_TIMEOUT


def save_remote_timeout(seconds: int) -> bool:
    return _write_value("RemoteTimeout", winreg.REG_DWORD, min(600, max(10, int(seconds))))

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
# "popup" means "bring the main window to the front"; when it is off, a
# bottom-right toast is shown instead (the two are opposites, auto-decided).
NOTIFY_CHANNELS = ("sound", "popup", "taskbar")
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

# ── Auto-update ────────────────────────────────────────────────────────────────

def load_auto_update_enabled() -> bool:
    """Whether the app silently self-updates from GitHub Releases (default on)."""
    return bool(_read_value("AutoUpdate", 1))


def save_auto_update_enabled(enabled: bool) -> bool:
    return _write_value("AutoUpdate", winreg.REG_DWORD, 1 if enabled else 0)


def load_staged_update() -> tuple[str, str]:
    """A downloaded-but-not-yet-applied installer: ``(version, path)``.

    Set when an update is found mid-session while the chat window is open; the
    installer runs once the chat closes or on the next launch. Empty if none.
    """
    return (str(_read_value("StagedUpdateVersion", "")).strip(),
            str(_read_value("StagedUpdatePath", "")).strip())


def save_staged_update(version: str, path: str) -> bool:
    ok = _write_value("StagedUpdateVersion", winreg.REG_SZ, version or "")
    return _write_value("StagedUpdatePath", winreg.REG_SZ, path or "") and ok


def clear_staged_update() -> None:
    save_staged_update("", "")


# ── Dual Access ────────────────────────────────────────────────────────────────

def load_dual_internet_ip() -> str:
    return str(_read_value("DualInternetIP", "")).strip()

def save_dual_internet_ip(ip: str) -> bool:
    return _write_value("DualInternetIP", winreg.REG_SZ, ip.strip())

def load_dual_dns_servers() -> list[str]:
    val = str(_read_value("DualDnsServers", "10.251.33.80,10.251.33.90")).strip()
    return [s.strip() for s in val.split(",") if s.strip()]

def save_dual_dns_servers(servers: list[str]) -> bool:
    return _write_value("DualDnsServers", winreg.REG_SZ, ",".join(servers))

def load_dual_domains() -> list[str]:
    val = str(_read_value("DualDomains", "wbsedcl.in,wbsedcl.co.in")).strip()
    return [s.strip() for s in val.split(",") if s.strip()]

def save_dual_domains(domains: list[str]) -> bool:
    return _write_value("DualDomains", winreg.REG_SZ, ",".join(domains))

def save_dual_prev_dns(mode: str, servers: list[str]) -> bool:
    """Remember the adapter's DNS setup before dual access changed it."""
    ok = _write_value("DualPrevDnsMode", winreg.REG_SZ, mode or "dhcp")
    return _write_value("DualPrevDnsServers", winreg.REG_SZ,
                        ",".join(servers)) and ok

def load_dual_prev_dns() -> tuple[str, list[str]]:
    mode = str(_read_value("DualPrevDnsMode", "dhcp")).strip() or "dhcp"
    val  = str(_read_value("DualPrevDnsServers", "")).strip()
    return mode, [s.strip() for s in val.split(",") if s.strip()]


# ── IP Switch profiles ─────────────────────────────────────────────────────────

def load_ip_profile(n: int) -> dict:
    """Return profile dict for slot n (1-4). All values are strings."""
    return {
        "name":    str(_read_value(f"IpSwitch{n}Name",    "")).strip(),
        "adapter": str(_read_value(f"IpSwitch{n}Adapter", "")).strip(),
        "mode":    str(_read_value(f"IpSwitch{n}Mode",    "static")).strip(),
        "ip":      str(_read_value(f"IpSwitch{n}IP",      "")).strip(),
        "mask":    str(_read_value(f"IpSwitch{n}Mask",    "255.255.255.0")).strip(),
        "gateway": str(_read_value(f"IpSwitch{n}Gateway", "")).strip(),
        "dns":     str(_read_value(f"IpSwitch{n}DNS",     "")).strip(),
    }

def save_ip_profile(n: int, name: str, adapter: str, mode: str,
                    ip: str, mask: str, gateway: str, dns: str) -> bool:
    ok  = _write_value(f"IpSwitch{n}Name",    winreg.REG_SZ, name)
    ok &= _write_value(f"IpSwitch{n}Adapter", winreg.REG_SZ, adapter)
    ok &= _write_value(f"IpSwitch{n}Mode",    winreg.REG_SZ, mode)
    ok &= _write_value(f"IpSwitch{n}IP",      winreg.REG_SZ, ip)
    ok &= _write_value(f"IpSwitch{n}Mask",    winreg.REG_SZ, mask)
    ok &= _write_value(f"IpSwitch{n}Gateway", winreg.REG_SZ, gateway)
    ok &= _write_value(f"IpSwitch{n}DNS",     winreg.REG_SZ, dns)
    return ok



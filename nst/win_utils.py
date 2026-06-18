"""Windows-specific plumbing: resource paths, admin/elevation, single instance,
console hiding, DPI/click-through helpers and system-tray geometry."""

import ctypes
import os
import sys

# ── Resource resolution (dev vs PyInstaller bundle) ───────────────────────────

def get_resource_path(relative_path: str) -> str:
    """Absolute path to a bundled resource, for dev and PyInstaller alike."""
    try:
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ── Admin & single-instance elevation ─────────────────────────────────────────

_app_mutex = None


def check_single_instance() -> bool:
    """True if this is the only instance; False if another already holds the mutex."""
    global _app_mutex
    try:
        mutex_name = "Local\\NetSplitTunnel_SingleInstance_Mutex_3248"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _app_mutex = kernel32.CreateMutexW(None, True, mutex_name)
        last_error = kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183
        if last_error == ERROR_ALREADY_EXISTS:
            if _app_mutex:
                kernel32.CloseHandle(_app_mutex)
                _app_mutex = None
            return False
        return True
    except Exception:
        return True


def hide_console() -> None:
    """Hide the console window of the current process, if any."""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate() -> None:
    """Relaunch the process elevated via UAC, then exit the current one."""
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    executable = sys.executable
    # Use pythonw.exe so no cmd window flashes while elevating.
    if executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, f'"{script}" {params}', None, 1
    )
    sys.exit(0)


def get_idle_seconds() -> float:
    """Seconds since the last system-wide keyboard/mouse input.

    Used to flip presence to *away* automatically when the user is idle.
    Returns 0.0 if the query fails (treated as active).
    """
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, (tick - info.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def set_app_user_model_id(appid: str = "hackerslab.netsplittunnel.v4") -> None:
    """Set the AppUserModelID so the taskbar uses the bundled icon."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def show_already_running_dialog() -> None:
    ctypes.windll.user32.MessageBoxW(
        None,
        "Another instance of Net Split-Tunneler is already running.",
        "Application Already Running",
        0x10 | 0x0,  # MB_ICONERROR | MB_OK
    )

# ── Tray geometry ─────────────────────────────────────────────────────────────

def get_tray_notify_rect():
    """Return (left, top, right, bottom) of the taskbar notification area, or None."""
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        shell_tray = user32.FindWindowW("Shell_TrayWnd", None)
        if not shell_tray:
            return None
        tray_notify = user32.FindWindowExW(shell_tray, 0, "TrayNotifyWnd", None)
        if not tray_notify:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long),
                        ("top", ctypes.c_long),
                        ("right", ctypes.c_long),
                        ("bottom", ctypes.c_long)]
        rect = RECT()
        if user32.GetWindowRect(tray_notify, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None

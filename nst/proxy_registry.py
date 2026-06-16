"""Toggle the Windows system proxy via the registry (client side)."""

import ctypes
import winreg

from .constants import PROXY_PORT, REG_INTERNET_SETTINGS


def _notify_wininet() -> None:
    try:
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # OPTION_SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # OPTION_REFRESH
    except Exception:
        pass


def set_proxy(host_ip: str, port: int = PROXY_PORT) -> tuple[bool, str]:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_INTERNET_SETTINGS,
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host_ip}:{port}")
        winreg.CloseKey(key)
        _notify_wininet()
        return True, f"System proxy set to {host_ip}:{port}."
    except Exception as exc:
        return False, f"Registry write failed: {exc}"


def clear_proxy() -> tuple[bool, str]:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_INTERNET_SETTINGS,
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, "")
        winreg.CloseKey(key)
        _notify_wininet()
        return True, "System proxy cleared."
    except Exception as exc:
        return False, f"Registry write failed: {exc}"


def read_current_proxy() -> tuple[bool, str]:
    """Return (enabled, server_string) from the registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_INTERNET_SETTINGS,
                             0, winreg.KEY_READ)
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except Exception:
            server = ""
        winreg.CloseKey(key)
        return bool(enabled), server
    except Exception:
        return False, ""

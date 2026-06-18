"""
Network Split-Tunneler & Proxy Sharing Tool  (NST)
Windows 10/11 only — Python 3.10+

Package layout:
    constants       — ports, magic strings, fonts
    theme           — dark/light palettes + runtime theme manager
    config          — registry-backed settings (autostart, display name, theme, ...)
    win_utils       — admin/elevation/console/single-instance + Win32 helpers
    netinfo         — IP detection, command runner, connectivity checks, formatters
    routing         — persistent 10.0.0.0/8 route add/delete
    proxy_registry  — system proxy registry toggle
    proxy_server    — HTTP/HTTPS tunnelling proxy server
    beacon          — host discovery (UDP broadcast) for the proxy feature
    chat            — LAN peer discovery + text messaging
    qt.*            — PyQt6 UI (main window, chat window, widgets, tray, toasts)
"""

__version__ = "4.5"

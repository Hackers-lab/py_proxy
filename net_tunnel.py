"""
Network Split-Tunneler & Proxy Sharing Tool  v3
Windows 10/11 only — Python 3.10+

Improvements over v1:
  • No CMD flash  — CREATE_NO_WINDOW + shell=False everywhere
  • Route state persisted — checks 'route print' on startup
  • Toggle buttons — single button flips label/color by state
  • Host beacon — UDP broadcast so clients auto-detect the host IP
  • Client toggle — single Connect/Disconnect button
  • System tray — closing the window hides to tray (pystray + Pillow)

Third-party deps (pip install before running / bundle with PyInstaller):
    pip install pystray pillow
"""

# ──────────────────────────────────────────────────────────────────────────────
import ctypes, os, sys, socket, threading, subprocess, winreg, time, io
import tkinter as tk
from tkinter import ttk, messagebox
import psutil

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

def get_resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ──────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
PROXY_PORT    = 8080
BEACON_PORT   = 54321          # UDP broadcast port for host discovery
BEACON_MAGIC  = b"NST_HOST_V3" # payload the host sends
BUFFER_SIZE   = 65536
CONN_TIMEOUT  = 30

DARK_BG   = "#1a1d23"
PANEL_BG  = "#22262f"
ACCENT    = "#3b82f6"
SUCCESS   = "#22c55e"
DANGER    = "#ef4444"
WARNING   = "#f59e0b"
TEXT_PRI  = "#f1f5f9"
TEXT_SEC  = "#94a3b8"
BORDER    = "#2e3340"

BTN_FONT   = ("Consolas", 9, "bold")
LABEL_FONT = ("Segoe UI", 9)
MONO_FONT  = ("Consolas", 9)
TITLE_FONT = ("Segoe UI", 10, "bold")

_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# ──────────────────────────────────────────────────────────────────────────────
#  ADMIN & SINGLE-INSTANCE ELEVATION
# ──────────────────────────────────────────────────────────────────────────────

_app_mutex = None

def check_single_instance() -> bool:
    """Check if another instance of the app is already running using a named Mutex."""
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
    """Hide the console window associated with the current process if it exists."""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            # SW_HIDE = 0
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def elevate() -> None:
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    executable = sys.executable
    # Use pythonw.exe instead of python.exe to prevent the cmd shell window when elevating
    if executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, f'"{script}" {params}', None, 1
    )
    sys.exit(0)

# ──────────────────────────────────────────────────────────────────────────────
#  NETWORK HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def get_intranet_ip() -> str | None:
    """Return the first 10.x.x.x address on this host using psutil (very fast, no DNS lookup)."""
    try:
        import psutil
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip.startswith("10."):
                        return ip
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip.startswith("10."):
                return ip
    except Exception:
        pass
    return None

def calculate_gateway(ip: str) -> str:
    parts = ip.split(".")
    parts[-1] = "1"
    return ".".join(parts)

def run_cmd(args: list[str]) -> tuple[int, str, str]:
    """Run silently — no console window, no shell."""
    r = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return r.returncode, r.stdout.decode(errors="replace").strip(), \
           r.stderr.decode(errors="replace").strip()

def check_internet_connection() -> bool:
    """Check if the machine can reach a public DNS server to verify internet access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect(("8.8.8.8", 53))
            return True
    except Exception:
        return False

def check_host_reachable(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=1.5):
            return True
    except Exception:
        return False

def check_internet_via_proxy(proxy_host: str, proxy_port: int) -> bool:
    """Connect to the proxy and request a tunnel to a public IP to verify internet access through the proxy."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((proxy_host, proxy_port))
            # Send CONNECT request to check if proxy can reach public DNS
            s.sendall(b"CONNECT 8.8.8.8:53 HTTP/1.1\r\n\r\n")
            resp = s.recv(1024)
            return b"200" in resp
    except Exception:
        return False

def set_autostart(enabled: bool) -> tuple[bool, str]:
    try:
        if getattr(sys, 'frozen', False):
            path = f'"{sys.executable}"'
        else:
            path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
            
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, "NetSplitTunnel", 0, winreg.REG_SZ, path)
            msg = "Autostart enabled in registry."
        else:
            try:
                winreg.DeleteValue(key, "NetSplitTunnel")
                msg = "Autostart disabled in registry."
            except FileNotFoundError:
                msg = "Autostart was already disabled."
        winreg.CloseKey(key)
        return True, msg
    except Exception as e:
        return False, f"Registry update failed: {e}"

def is_autostart_enabled() -> bool:
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, "NetSplitTunnel")
            enabled = True
        except FileNotFoundError:
            enabled = False
        winreg.CloseKey(key)
        return enabled
    except Exception:
        return False

def load_show_speed_in_taskbar() -> bool:
    try:
        key_path = r"Software\NetSplitTunnel"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, "ShowSpeedInTaskbar")
            enabled = bool(val)
        except FileNotFoundError:
            enabled = False
        winreg.CloseKey(key)
        return enabled
    except Exception:
        return False

def save_show_speed_in_taskbar(enabled: bool) -> bool:
    try:
        key_path = r"Software\NetSplitTunnel"
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        except Exception:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ShowSpeedInTaskbar", 0, winreg.REG_DWORD, 1 if enabled else 0)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
#  ROUTING  — with startup detection
# ──────────────────────────────────────────────────────────────────────────────

def check_route_exists() -> bool:
    """Parse 'route print' to see if 10.0.0.0 mask 255.0.0.0 is present."""
    _, out, _ = run_cmd(["route", "print", "10.0.0.0"])
    # route print filters to that destination; look for the mask
    return "255.0.0.0" in out and "10.0.0.0" in out

def add_intranet_route(gateway: str) -> tuple[bool, str]:
    code, _, err = run_cmd(
        ["route", "add", "10.0.0.0", "mask", "255.0.0.0", gateway, "-p"]
    )
    if code == 0:
        return True, f"Route 10.0.0.0/8 → {gateway} added (persistent)."
    return False, f"route add failed: {err}"

def delete_intranet_route() -> tuple[bool, str]:
    code, _, err = run_cmd(["route", "delete", "10.0.0.0"])
    if code == 0:
        return True, "Route 10.0.0.0/8 removed."
    return False, f"route delete failed: {err}"

# ──────────────────────────────────────────────────────────────────────────────
#  REGISTRY  — proxy toggle
# ──────────────────────────────────────────────────────────────────────────────

def _notify_wininet() -> None:
    try:
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
    except Exception:
        pass

def set_proxy(host_ip: str, port: int = PROXY_PORT) -> tuple[bool, str]:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH,
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
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH,
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, "")
        winreg.CloseKey(key)
        _notify_wininet()
        return True, "System proxy cleared."
    except Exception as exc:
        return False, f"Registry write failed: {exc}"

def read_current_proxy() -> tuple[bool, str]:
    """Return (enabled, server_string) from registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH,
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

# ──────────────────────────────────────────────────────────────────────────────
#  PROXY SERVER  (HTTP + HTTPS tunnel)
# ──────────────────────────────────────────────────────────────────────────────

def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(BUFFER_SIZE):
            dst.sendall(chunk)
    except Exception:
        pass
    for s in (src, dst):
        try: s.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: s.close()
        except Exception: pass

def _handle_client(client: socket.socket) -> None:
    try:
        client.settimeout(CONN_TIMEOUT)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = client.recv(4096)
            if not chunk:
                return
            raw += chunk

        first = raw.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first.split()
        if len(parts) < 3:
            return
        method, url = parts[0].upper(), parts[1]

        if method == "CONNECT":
            hp = url.rsplit(":", 1)
            host, port = hp[0], int(hp[1]) if len(hp) > 1 else 443
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            stripped = url[7:] if url.startswith("http://") else url
            idx = stripped.find("/")
            host_part = stripped[:idx] if idx != -1 else stripped
            path      = stripped[idx:] if idx != -1 else "/"
            hp2 = host_part.rsplit(":", 1)
            host, port = hp2[0], int(hp2[1]) if len(hp2) > 1 else 80
            lines = raw.split(b"\r\n")
            lines[0] = f"{method} {path} HTTP/1.1".encode()
            remote = socket.create_connection((host, port), timeout=CONN_TIMEOUT)
            remote.sendall(b"\r\n".join(lines))

        t1 = threading.Thread(target=_pipe, args=(client, remote), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(remote, client), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
    except Exception:
        pass
    finally:
        try: client.close()
        except Exception: pass

class ProxyServer:
    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self.running = False

    def start(self) -> tuple[bool, str]:
        if self.running:
            return False, "Proxy already running."
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", PROXY_PORT))
            self._sock.listen(256)
            self._sock.settimeout(1.0)
            self.running = True
            threading.Thread(target=self._loop, daemon=True).start()
            return True, f"Proxy listening on 0.0.0.0:{PROXY_PORT}."
        except Exception as exc:
            return False, f"Failed to start proxy: {exc}"

    def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Proxy is not running."
        self.running = False
        try: self._sock.close()
        except Exception: pass
        return True, "Proxy stopped."

    def _loop(self) -> None:
        while self.running:
            try:
                client, _ = self._sock.accept()
                threading.Thread(target=_handle_client, args=(client,),
                                 daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

# ──────────────────────────────────────────────────────────────────────────────
#  HOST BEACON  (UDP broadcast so clients can auto-discover)
# ──────────────────────────────────────────────────────────────────────────────

class HostBeacon:
    """Broadcasts BEACON_MAGIC on UDP port BEACON_PORT every 2 s."""
    def __init__(self, get_internet_status_cb) -> None:
        self.running = False
        self._ip: str = ""
        self._get_internet_status = get_internet_status_cb

    def start(self, ip: str) -> None:
        self._ip = ip
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.0)
            while self.running:
                try:
                    internet_status = "1" if self._get_internet_status() else "0"
                    payload = BEACON_MAGIC + b"|" + self._ip.encode() + b"|" + internet_status.encode()
                    s.sendto(payload, ("<broadcast>", BEACON_PORT))
                except Exception:
                    pass
                time.sleep(2)
            s.close()
        except Exception:
            pass

class ClientScanner:
    """Listens for UDP beacons; calls callback(ip_str, has_internet) when found."""
    def __init__(self, callback) -> None:
        self._cb = callback
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", BEACON_PORT))
            s.settimeout(1.0)
            while self.running:
                try:
                    data, addr = s.recvfrom(256)
                    parts = data.split(b"|")
                    if len(parts) >= 2 and parts[0] == BEACON_MAGIC:
                        ip = parts[1].decode(errors="replace")
                        has_internet = True
                        if len(parts) >= 3:
                            has_internet = (parts[2] == b"1")
                        self._cb(ip, has_internet)
                except socket.timeout:
                    continue
                except Exception:
                    continue
            s.close()
        except Exception:
            pass

# ──────────────────────────────────────────────────────────────────────────────
#  SYSTEM TRAY ICON  (pystray + Pillow)
# ──────────────────────────────────────────────────────────────────────────────

def _make_tray_icon() -> "Image.Image":
    """Load the app icon if it exists, otherwise draw a fallback blue circle in memory."""
    icon_png = get_resource_path("icon.png")
    if os.path.exists(icon_png):
        try:
            return Image.open(icon_png)
        except Exception:
            pass
    # Fallback
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, size - 4, size - 4], fill=(59, 130, 246, 255))
    d.ellipse([18, 18, size - 18, size - 18], fill=(26, 29, 35, 255))
    return img

# ──────────────────────────────────────────────────────────────────────────────
#  GUI HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _btn(parent, text, command, color=ACCENT, width=24):
    return tk.Button(
        parent, text=text, command=command,
        font=BTN_FONT, width=width, relief="flat", cursor="hand2",
        bg=color, fg=TEXT_PRI, activebackground=color, activeforeground=TEXT_PRI,
        bd=0, pady=6,
    )

def _label(parent, text, color=TEXT_PRI, font=None, anchor="w"):
    return tk.Label(parent, text=text, bg=PANEL_BG,
                    fg=color, font=font or LABEL_FONT, anchor=anchor)

def format_speed_short(bps) -> str:
    if bps < 1024:
        return f"{int(bps)}"
    elif bps < 1024 * 1024:
        kb = bps / 1024
        if kb < 10:
            return f"{kb:.1f}K"
        return f"{int(kb)}K"
    else:
        mb = bps / (1024 * 1024)
        if mb < 10:
            return f"{mb:.1f}M"
        return f"{int(mb)}M"

def draw_speed_icon(up_speed_str, down_speed_str, font) -> "Image.Image":
    # 32x32 icon (high DPI system tray icon)
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0)) # transparent background
    draw = ImageDraw.Draw(img)
    # Draw rounded dark card background
    draw.rounded_rectangle([0, 0, 31, 31], radius=4, fill=(26, 29, 35, 240))
    # Draw speeds in two lines
    draw.text((2, 2), f"▲{up_speed_str}", fill=(245, 158, 11, 255), font=font)
    draw.text((2, 16), f"▼{down_speed_str}", fill=(34, 197, 94, 255), font=font)
    return img

def get_tray_notify_rect():
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

def set_clickthrough(hwnd):
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        LWA_ALPHA = 0x00000002
        
        is_64bit = (ctypes.sizeof(ctypes.c_void_p) == 8)
        if is_64bit:
            get_style = user32.GetWindowLongPtrW
            set_style = user32.SetWindowLongPtrW
            get_style.restype = ctypes.c_void_p
            set_style.restype = ctypes.c_void_p
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
            set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        else:
            get_style = user32.GetWindowLongW
            set_style = user32.SetWindowLongW
            get_style.restype = ctypes.c_long
            set_style.restype = ctypes.c_long
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
            set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            
        style = get_style(hwnd, GWL_EXSTYLE)
        new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        set_style(hwnd, GWL_EXSTYLE, new_style)
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ──────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        # Set AppUserModelID so Windows taskbar displays the custom application icon
        try:
            myappid = 'hackerslab.netsplittunnel.v3'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

        super().__init__()
        self.title("Net Split-Tunneler & Proxy Sharing Tool")
        self.resizable(False, False)
        self.configure(bg=DARK_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set window icon
        icon_ico = get_resource_path("icon.ico")
        if os.path.exists(icon_ico):
            try:
                self.iconbitmap(icon_ico)
            except Exception:
                pass

        self._host_has_internet = False
        self._proxy   = ProxyServer()
        self._beacon  = HostBeacon(lambda: self._host_has_internet)
        self._scanner = ClientScanner(self._on_beacon_received)

        # Detect persisted state on startup
        self._route_active      = check_route_exists()
        proxy_on, proxy_server  = read_current_proxy()
        self._client_connected  = proxy_on
        self._client_proxy_host = proxy_server.split(":")[0] if proxy_server else ""

        self._detected_ip: str | None = None
        self._detected_gw: str | None = None
        self._tray: "pystray.Icon | None" = None

        # Cache font for system tray speed monitor
        self._tiny_font = self._get_tiny_font(9)

        # Network speed tracking state
        self._last_net_bytes = psutil.net_io_counters()
        self._last_net_time = time.time()

        self._build_menu()
        self._build_ui()

        # Apply recovered state to button labels
        self._update_route_btn()
        self._update_proxy_btn()
        self._update_client_btn()
        if self._client_connected and self._client_proxy_host:
            self._lbl_client_status.config(
                text=f"Status  :  CONNECTED  →  {self._client_proxy_host}:{PROXY_PORT}",
                fg=SUCCESS,
            )

        # Start scanning for host beacons (client side always listens)
        self._scanner.start()

        # Start background host internet checking thread
        threading.Thread(target=self._internet_check_loop, daemon=True).start()

        # Start tray icon early if speed monitor is enabled
        if self._show_speed_in_taskbar_var.get():
            self._start_tray()

        self._update_traffic_speed()
        self.after(50, self._poll_status)
        self._log_msg("Application started.  Administrator ✓")
        if self._route_active:
            self._log_msg("Existing 10.0.0.0 route detected — marked ACTIVE.")
        if self._client_connected:
            self._log_msg(f"Existing proxy detected: {proxy_server} — marked CONNECTED.")

    # ── UI BUILD ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self, bg=DARK_BG, pady=10)
        hdr.pack(fill="x", padx=20)
        tk.Label(hdr, text="⬡  NET SPLIT-TUNNELER", bg=DARK_BG,
                 fg=ACCENT, font=("Consolas", 13, "bold")).pack(side="left")
        tk.Label(hdr, text="& Proxy Sharing Tool  v3", bg=DARK_BG,
                 fg=TEXT_SEC, font=("Segoe UI", 9)).pack(side="left", padx=8, pady=4)

        # ── TAB BAR ───────────────────────────────────────────────────────────
        tab_bar = tk.Frame(self, bg=DARK_BG)
        tab_bar.pack(fill="x", padx=20, pady=(5, 10))

        self._btn_tab_host = tk.Button(
            tab_bar, text="Host Mode", command=self._show_host_tab,
            font=BTN_FONT, relief="flat", cursor="hand2", bd=0, padx=16, pady=6,
            bg=PANEL_BG, fg=ACCENT, activebackground=PANEL_BG, activeforeground=ACCENT
        )
        self._btn_tab_host.pack(side="left", padx=(0, 6))

        self._btn_tab_client = tk.Button(
            tab_bar, text="Client Mode", command=self._show_client_tab,
            font=BTN_FONT, relief="flat", cursor="hand2", bd=0, padx=16, pady=6,
            bg=DARK_BG, fg=TEXT_SEC, activebackground=DARK_BG, activeforeground=TEXT_SEC
        )
        self._btn_tab_client.pack(side="left")

        # Tab hover micro-interactions
        def on_enter_host(e):
            if self._btn_tab_host["bg"] != PANEL_BG:
                self._btn_tab_host.config(fg=TEXT_PRI)
        def on_leave_host(e):
            if self._btn_tab_host["bg"] != PANEL_BG:
                self._btn_tab_host.config(fg=TEXT_SEC)

        def on_enter_client(e):
            if self._btn_tab_client["bg"] != PANEL_BG:
                self._btn_tab_client.config(fg=TEXT_PRI)
        def on_leave_client(e):
            if self._btn_tab_client["bg"] != PANEL_BG:
                self._btn_tab_client.config(fg=TEXT_SEC)

        self._btn_tab_host.bind("<Enter>", on_enter_host)
        self._btn_tab_host.bind("<Leave>", on_leave_host)
        self._btn_tab_client.bind("<Enter>", on_enter_client)
        self._btn_tab_client.bind("<Leave>", on_leave_client)

        # Tab content container frame
        self._tab_container = tk.Frame(self, bg=DARK_BG, height=215)
        self._tab_container.pack(fill="x", padx=20, pady=(0, 10))
        self._tab_container.pack_propagate(False)

        # ── HOST MODE ─────────────────────────────────────────────────────────
        self._hf = tk.LabelFrame(self._tab_container, text="  HOST MODE  —  Internet Provider  ",
                            bg=PANEL_BG, fg=ACCENT, font=TITLE_FONT,
                            bd=1, relief="solid", labelanchor="nw")

        sb = tk.Frame(self._hf, bg=PANEL_BG, pady=6)
        sb.pack(fill="x", padx=12, pady=(8, 4))
        self._lbl_ip    = _label(sb, "Intranet IP   :  —",        color=TEXT_SEC, font=MONO_FONT)
        self._lbl_gw    = _label(sb, "Gateway       :  —",        color=TEXT_SEC, font=MONO_FONT)
        self._lbl_proxy = _label(sb, "Proxy         :  STOPPED",  color=DANGER,   font=MONO_FONT)
        self._lbl_route = _label(sb, "LAN+NET Route :  INACTIVE", color=TEXT_SEC, font=MONO_FONT)
        self._lbl_internet = _label(sb, "Internet      :  CHECKING...", color=WARNING, font=MONO_FONT)
        for w in (self._lbl_ip, self._lbl_gw, self._lbl_proxy, self._lbl_route, self._lbl_internet):
            w.pack(anchor="w")

        tk.Frame(self._hf, bg=BORDER, height=1).pack(fill="x", padx=12, pady=6)

        btn_row = tk.Frame(self._hf, bg=PANEL_BG)
        btn_row.pack(padx=12, pady=(4, 12))

        self._btn_route = _btn(btn_row, "▶  Enable LAN+NET", self._toggle_route)
        self._btn_route.pack(side="left", padx=6)

        self._btn_proxy = _btn(btn_row, "▶  Start Proxy Server", self._toggle_proxy,
                               color="#7c3aed")
        self._btn_proxy.pack(side="left", padx=6)

        # ── CLIENT MODE ───────────────────────────────────────────────────────
        self._cf = tk.LabelFrame(self._tab_container, text="  CLIENT MODE  —  Internet Consumer  ",
                           bg=PANEL_BG, fg="#a78bfa", font=TITLE_FONT,
                           bd=1, relief="solid", labelanchor="nw")

        ip_row = tk.Frame(self._cf, bg=PANEL_BG)
        ip_row.pack(fill="x", padx=12, pady=(12, 4))

        _label(ip_row, "Host IP:", font=LABEL_FONT).pack(side="left")
        self._host_ip_var = tk.StringVar(value=self._client_proxy_host)
        self._host_entry = tk.Entry(
            ip_row, textvariable=self._host_ip_var,
            font=MONO_FONT, width=18,
            bg="#2e3340", fg=TEXT_PRI, insertbackground=TEXT_PRI,
            relief="flat", bd=4,
        )
        self._host_entry.pack(side="left", padx=8)

        self._lbl_scan = _label(ip_row, "⟳ scanning…", color=TEXT_SEC,
                                font=("Consolas", 8))
        self._lbl_scan.pack(side="left", padx=4)

        self._lbl_client_status = _label(self._cf, "Status  :  DISCONNECTED",
                                         color=TEXT_SEC, font=MONO_FONT)
        self._lbl_client_status.pack(anchor="w", padx=12, pady=(2, 4))

        self._disable_if_no_internet_var = tk.BooleanVar(value=False)
        self._chk_disable_if_no_internet = tk.Checkbutton(
            self._cf, text="Disable proxy if host has no internet / unreachable",
            variable=self._disable_if_no_internet_var,
            bg=PANEL_BG, fg=TEXT_SEC, activebackground=PANEL_BG,
            activeforeground=TEXT_PRI, selectcolor=DARK_BG,
            font=LABEL_FONT, bd=0, highlightthickness=0
        )
        self._chk_disable_if_no_internet.pack(anchor="w", padx=12, pady=(0, 4))

        c_btn_row = tk.Frame(self._cf, bg=PANEL_BG)
        c_btn_row.pack(padx=12, pady=(4, 12))
        self._btn_client = _btn(c_btn_row, "⬡  Connect to Host Proxy",
                                self._toggle_client, color=ACCENT)
        self._btn_client.pack()

        # Show Host Tab by default
        self._show_host_tab()

        # ── TRAFFIC MONITOR ───────────────────────────────────────────────────
        self._traffic_frame = tk.LabelFrame(
            self, text="  NETWORK TRAFFIC MONITOR  ",
            bg=PANEL_BG, fg=ACCENT, font=TITLE_FONT,
            bd=1, relief="solid", labelanchor="nw"
        )
        self._traffic_frame.pack(fill="x", padx=20, pady=(0, 10))

        tf_row = tk.Frame(self._traffic_frame, bg=PANEL_BG, pady=6)
        tf_row.pack(fill="x", padx=12)

        self._lbl_down_speed = _label(tf_row, "Download  :  0.0 KB/s", color=SUCCESS, font=MONO_FONT)
        self._lbl_down_speed.pack(side="left", expand=True, fill="x")

        self._lbl_up_speed = _label(tf_row, "Upload    :  0.0 KB/s", color=WARNING, font=MONO_FONT)
        self._lbl_up_speed.pack(side="left", expand=True, fill="x")

        # ── EVENT LOG ─────────────────────────────────────────────────────────
        self._lf_log = tk.LabelFrame(self, text="  EVENT LOG  ",
                           bg=PANEL_BG, fg=TEXT_SEC, font=TITLE_FONT,
                           bd=1, relief="solid")
        self._lf_log.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        self._log = tk.Text(self._lf_log, height=5, bg="#12151b", fg=TEXT_PRI,
                            font=MONO_FONT, relief="flat", state="disabled",
                            wrap="word", bd=6)
        vsb = ttk.Scrollbar(self._lf_log, command=self._log.yview)
        self._log.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._log.pack(fill="both", expand=True)

        # ── FOOTER ────────────────────────────────────────────────────────────
        self._footer_frame = tk.Frame(self, bg=DARK_BG)
        self._footer_frame.pack(fill="x", side="bottom", padx=20, pady=(0, 6))
        tk.Label(self._footer_frame, text="Copyright © Pramod Verma", bg=DARK_BG,
                 fg=TEXT_SEC, font=("Segoe UI", 8)).pack(side="right")

    # ── MENU & TRAFFIC METHODS ────────────────────────────────────────────────

    def _get_tiny_font(self, size=9):
        for name in ["tahoma.ttf", "arial.ttf", "segoeui.ttf"]:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def _build_menu(self) -> None:
        # Create Menu Bar
        menu_bar = tk.Menu(self)
        self.config(menu=menu_bar)

        # File Menu
        file_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Exit", command=self._quit_app)

        # Settings Menu
        settings_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="Settings", menu=settings_menu)

        # Start with Windows
        self._autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        settings_menu.add_checkbutton(
            label="Start with Windows",
            variable=self._autostart_var,
            command=self._toggle_autostart
        )

        # Show Speed in Taskbar
        self._show_speed_in_taskbar_var = tk.BooleanVar(value=load_show_speed_in_taskbar())
        settings_menu.add_checkbutton(
            label="Show Speed in Taskbar",
            variable=self._show_speed_in_taskbar_var,
            command=self._toggle_show_speed_in_taskbar
        )

        # About Menu
        about_menu = tk.Menu(menu_bar, tearoff=0)
        menu_bar.add_cascade(label="About", menu=about_menu)
        about_menu.add_command(label="About Net Split-Tunneler", command=self._show_about_dialog)

    def _toggle_show_speed_in_taskbar(self) -> None:
        enabled = self._show_speed_in_taskbar_var.get()
        ok = save_show_speed_in_taskbar(enabled)
        if ok:
            status = "enabled" if enabled else "disabled"
            self._log_msg(f"Show Speed in Taskbar {status}.")
            if enabled:
                self._start_tray()
            else:
                self._hide_overlay()
                if self.winfo_viewable() and self._tray:
                    self._tray.stop()
                    self._tray = None
                elif not self.winfo_viewable() and self._tray:
                    self._tray.icon = _make_tray_icon()
                    self._tray.title = "Net Split-Tunneler\n(running in background)"
        else:
            self._log_msg("Failed to save Show Speed in Taskbar setting.")
            messagebox.showerror("Registry Error", "Failed to save settings to registry.")

    def _show_about_dialog(self) -> None:
        about_win = tk.Toplevel(self)
        about_win.title("About Net Split-Tunneler")
        about_win.resizable(False, False)
        about_win.configure(bg=DARK_BG)
        
        # Make modal
        about_win.transient(self)
        about_win.grab_set()
        
        # Center about window relative to parent
        about_win.update_idletasks()
        w, h = 380, 260
        pw = self.winfo_width()
        ph = self.winfo_height()
        px = self.winfo_x()
        py = self.winfo_y()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        about_win.geometry(f"{w}x{h}+{x}+{y}")
        
        # Content
        pad = 15
        main_frame = tk.Frame(about_win, bg=PANEL_BG, bd=1, relief="solid")
        main_frame.pack(fill="both", expand=True, padx=pad, pady=pad)
        
        # App Icon or Emblem
        tk.Label(main_frame, text="⬡", bg=PANEL_BG, fg=ACCENT, font=("Segoe UI", 32)).pack(pady=(10, 2))
        
        tk.Label(main_frame, text="Net Split-Tunneler v3.1", bg=PANEL_BG, fg=TEXT_PRI, font=("Segoe UI", 12, "bold")).pack()
        tk.Label(main_frame, text="Proxy Sharing Tool", bg=PANEL_BG, fg=TEXT_SEC, font=("Segoe UI", 9, "italic")).pack(pady=(0, 10))
        
        desc = (
            "A lightweight Windows utility to split-tunnel local traffic "
            "and share network proxy connections.\n\n"
            "Developed by Pramod Verma"
        )
        tk.Label(main_frame, text=desc, bg=PANEL_BG, fg=TEXT_PRI, font=("Segoe UI", 9), justify="center", wrap=300).pack(padx=10)
        
        # Close Button
        btn = tk.Button(
            main_frame, text="Close", command=about_win.destroy,
            font=BTN_FONT, relief="flat", cursor="hand2", bg=ACCENT, fg=TEXT_PRI,
            activebackground=ACCENT, activeforeground=TEXT_PRI, bd=0, padx=20, pady=4
        )
        btn.pack(side="bottom", pady=15)

    def _update_traffic_speed(self) -> None:
        try:
            curr_bytes = psutil.net_io_counters()
            curr_time = time.time()
            dt = curr_time - self._last_net_time
            if dt > 0:
                bytes_sent = curr_bytes.bytes_sent - self._last_net_bytes.bytes_sent
                bytes_recv = curr_bytes.bytes_recv - self._last_net_bytes.bytes_recv
                
                up_speed = bytes_sent / dt
                down_speed = bytes_recv / dt
                
                # Format speed
                def format_speed(bps):
                    if bps < 1024:
                        return f"{bps:.1f} B/s"
                    elif bps < 1024 * 1024:
                        return f"{bps/1024:.1f} KB/s"
                    else:
                        return f"{bps/(1024*1024):.1f} MB/s"
                
                up_formatted = format_speed(up_speed)
                down_formatted = format_speed(down_speed)
                
                self._lbl_down_speed.config(text=f"Download  :  {down_formatted}")
                self._lbl_up_speed.config(text=f"Upload    :  {up_formatted}")
                
                # Update taskbar text overlay and tray icon if enabled
                if self._show_speed_in_taskbar_var.get():
                    self._show_overlay(up_formatted, down_formatted)
                    self._start_tray()
                    if self._tray:
                        self._tray.icon = _make_tray_icon()
                        self._tray.title = f"Net Split-Tunneler\nUp: {up_formatted}\nDown: {down_formatted}"
                else:
                    self._hide_overlay()
                    if self.winfo_viewable() and self._tray:
                        self._tray.stop()
                        self._tray = None
                    elif not self.winfo_viewable() and self._tray:
                        self._tray.icon = _make_tray_icon()
                        self._tray.title = "Net Split-Tunneler\n(running in background)"
                
            self._last_net_bytes = curr_bytes
            self._last_net_time = curr_time
        except Exception:
            pass
        self.after(1000, self._update_traffic_speed)

    def _show_overlay(self, up_str: str, down_str: str) -> None:
        if not hasattr(self, "_overlay") or self._overlay is None:
            self._overlay = tk.Toplevel()  # standalone toplevel
            self._overlay.overrideredirect(True)
            self._overlay.wm_attributes("-topmost", True)
            self._overlay.config(bg="#010101")
            self._overlay.attributes("-transparentcolor", "#010101")
            
            # Place labels
            self._overlay_up_lbl = tk.Label(
                self._overlay, text="", fg="#f59e0b", bg="#010101",
                font=("Segoe UI", 8, "bold"), anchor="w"
            )
            self._overlay_up_lbl.pack(anchor="w", fill="x", padx=0, pady=0)
            
            self._overlay_down_lbl = tk.Label(
                self._overlay, text="", fg="#22c55e", bg="#010101",
                font=("Segoe UI", 8, "bold"), anchor="w"
            )
            self._overlay_down_lbl.pack(anchor="w", fill="x", padx=0, pady=0)
            
            # Make click-through
            self._overlay.update_idletasks()
            hwnd = self._overlay.winfo_id()
            set_clickthrough(hwnd)
            
        self._overlay_up_lbl.config(text=f"U: {up_str}")
        self._overlay_down_lbl.config(text=f"D: {down_str}")
        
        # Position overlay
        rect = get_tray_notify_rect()
        if rect:
            left, top, right, bottom = rect
            ow, oh = 95, 34
            # If horizontal taskbar
            if (bottom - top) < (right - left):
                x = left - ow - 8
                y = top + (bottom - top - oh) // 2
            else:
                x = left + (right - left - ow) // 2
                y = top - oh - 8
            self._overlay.geometry(f"{ow}x{oh}+{x}+{y}")
            self._overlay.deiconify() # ensure visible
        else:
            self._overlay.withdraw() # hide if tray rect not found

    def _hide_overlay(self) -> None:
        if hasattr(self, "_overlay") and self._overlay is not None:
            self._overlay.withdraw()

    # ── LOGGING ───────────────────────────────────────────────────────────────

    def _log_msg(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log.configure(state="normal")
        self._log.insert("end", f"[{ts}]  {msg}\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    # ── BUTTON STATE UPDATERS ─────────────────────────────────────────────────

    def _update_route_btn(self) -> None:
        if self._route_active:
            self._btn_route.config(text="■  Disable LAN+NET", bg=DANGER)
            self._lbl_route.config(text="LAN+NET Route :  ACTIVE", fg=SUCCESS)
        else:
            self._btn_route.config(text="▶  Enable LAN+NET", bg="#16a34a")
            self._lbl_route.config(text="LAN+NET Route :  INACTIVE", fg=TEXT_SEC)

    def _update_proxy_btn(self) -> None:
        if self._proxy.running:
            self._btn_proxy.config(text="■  Stop Proxy Server", bg=DANGER)
            self._lbl_proxy.config(
                text=f"Proxy         :  RUNNING  (:{PROXY_PORT})", fg=SUCCESS)
        else:
            self._btn_proxy.config(text="▶  Start Proxy Server", bg="#7c3aed")
            self._lbl_proxy.config(text="Proxy         :  STOPPED", fg=DANGER)

    def _update_client_btn(self) -> None:
        if self._client_connected:
            self._btn_client.config(text="✕  Disconnect from Proxy", bg=DANGER)
        else:
            self._btn_client.config(text="⬡  Connect to Host Proxy", bg=ACCENT)

    # ── POLL LOOP ─────────────────────────────────────────────────────────────

    def _poll_status(self) -> None:
        ip = get_intranet_ip()
        if ip:
            gw = calculate_gateway(ip)
            self._lbl_ip.config(text=f"Intranet IP   :  {ip}", fg=SUCCESS)
            self._lbl_gw.config(text=f"Gateway       :  {gw}", fg=TEXT_PRI)
            self._detected_ip = ip
            self._detected_gw = gw
            if self._beacon.running:
                self._beacon._ip = ip   # keep beacon IP fresh
        else:
            self._lbl_ip.config(text="Intranet IP   :  Not detected", fg=WARNING)
            self._lbl_gw.config(text="Gateway       :  —",            fg=TEXT_SEC)
            self._detected_ip = None
            self._detected_gw = None

        self._update_proxy_btn()
        self._update_route_btn()

        # Trigger client health check in a background thread if connected
        if self._client_connected and self._disable_if_no_internet_var.get():
            threading.Thread(target=self._client_health_check, daemon=True).start()

        self.after(3000, self._poll_status)

    # ── HOST TOGGLE ACTIONS ───────────────────────────────────────────────────

    def _toggle_route(self) -> None:
        if self._route_active:
            ok, msg = delete_intranet_route()
            if ok:
                self._route_active = False
        else:
            if not self._detected_gw:
                messagebox.showerror("No Intranet IP",
                                     "Cannot detect a 10.x.x.x address on this machine.")
                return
            ok, msg = add_intranet_route(self._detected_gw)
            if ok:
                self._route_active = True
        self._log_msg(msg)
        self._update_route_btn()

    def _toggle_proxy(self) -> None:
        if self._proxy.running:
            ok, msg = self._proxy.stop()
            if ok:
                self._beacon.stop()
        else:
            ok, msg = self._proxy.start()
            if ok and self._detected_ip:
                self._beacon.start(self._detected_ip)
        self._log_msg(msg)
        self._update_proxy_btn()

    # ── CLIENT TOGGLE ACTION ──────────────────────────────────────────────────

    def _toggle_client(self) -> None:
        if self._client_connected:
            ok, msg = clear_proxy()
            if ok:
                self._client_connected = False
                self._lbl_client_status.config(text="Status  :  DISCONNECTED",
                                               fg=TEXT_SEC)
        else:
            host = self._host_ip_var.get().strip()
            if not host:
                messagebox.showerror("Missing IP",
                                     "Enter the Host IP address or wait for auto-detect.")
                return
            parts = host.split(".")
            if len(parts) != 4 or not all(
                p.isdigit() and 0 <= int(p) <= 255 for p in parts
            ):
                messagebox.showerror("Invalid IP",
                                     f"'{host}' is not a valid IPv4 address.")
                return
            ok, msg = set_proxy(host, PROXY_PORT)
            if ok:
                self._client_connected = True
                self._client_proxy_host = host
                self._lbl_client_status.config(
                    text=f"Status  :  CONNECTED  →  {host}:{PROXY_PORT}", fg=SUCCESS
                )
        self._log_msg(msg)
        self._update_client_btn()

    def _toggle_autostart(self) -> None:
        enabled = self._autostart_var.get()
        ok, msg = set_autostart(enabled)
        self._log_msg(msg)
        if not ok:
            messagebox.showerror("Registry Error", msg)

    def _show_host_tab(self) -> None:
        self._btn_tab_host.config(bg=PANEL_BG, fg=ACCENT)
        self._btn_tab_client.config(bg=DARK_BG, fg=TEXT_SEC)
        self._cf.pack_forget()
        self._hf.pack(fill="both", expand=True)

    def _show_client_tab(self) -> None:
        self._btn_tab_host.config(bg=DARK_BG, fg=TEXT_SEC)
        self._btn_tab_client.config(bg=PANEL_BG, fg="#a78bfa")
        self._hf.pack_forget()
        self._cf.pack(fill="both", expand=True)

    def _client_health_check(self) -> None:
        """Runs in a background thread to check client connection health."""
        if not self._client_connected or not self._disable_if_no_internet_var.get():
            return
        
        host = self._client_proxy_host
        if not host:
            return
            
        host_ok = check_host_reachable(host, PROXY_PORT)
        internet_ok = check_internet_via_proxy(host, PROXY_PORT) if host_ok else False
        
        if not host_ok or not internet_ok:
            def _disable():
                if self._client_connected:
                    ok, msg = clear_proxy()
                    if ok:
                        self._client_connected = False
                        reason = "Host unreachable" if not host_ok else "No Internet access through proxy"
                        self._lbl_client_status.config(
                            text=f"Status  :  DISCONNECTED ({reason})", fg=WARNING
                        )
                        self._update_client_btn()
                        self._log_msg(f"Client proxy disabled automatically: {reason}.")
            try:
                self.after(0, _disable)
            except Exception:
                pass

    def _internet_check_loop(self) -> None:
        while True:
            res = check_internet_connection()
            def _update():
                self._host_has_internet = res
                if res:
                    self._lbl_internet.config(text="Internet      :  CONNECTED", fg=SUCCESS)
                else:
                    self._lbl_internet.config(text="Internet      :  NO CONNECTION", fg=DANGER)
            try:
                self.after(0, _update)
            except Exception:
                pass
            time.sleep(3)

    # ── BEACON RECEIVED (client auto-detect) ─────────────────────────────────

    def _on_beacon_received(self, ip: str, has_internet: bool) -> None:
        """Called from scanner thread — marshal to main thread via after()."""
        def _apply():
            current = self._host_ip_var.get().strip()
            internet_status = "Internet OK" if has_internet else "No Internet"
            color = SUCCESS if has_internet else DANGER
            self._lbl_scan.config(text=f"✓ host: {ip} ({internet_status})", fg=color)

            if current != ip:
                self._host_ip_var.set(ip)
                self._log_msg(f"Host beacon detected: {ip} — IP auto-filled.")

            # If client is connected to this host, and checkbox is checked, and host has no internet
            if self._client_connected and self._client_proxy_host == ip:
                if not has_internet and self._disable_if_no_internet_var.get():
                    ok, msg = clear_proxy()
                    if ok:
                        self._client_connected = False
                        self._lbl_client_status.config(
                            text="Status  :  DISCONNECTED (Host lost internet)", fg=WARNING
                        )
                        self._update_client_btn()
                        self._log_msg("Client proxy disabled automatically: Host has no internet connection.")
        self.after(0, _apply)

    # ── SYSTEM TRAY ───────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if not HAS_TRAY:
            self._quit_app()
            return
        self.withdraw()            # hide window, keep process alive
        self._start_tray()

    def _start_tray(self) -> None:
        if self._tray is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Show Window", self._restore_from_tray, default=True),
            pystray.MenuItem("Quit",        self._quit_app),
        )
        icon_img = _make_tray_icon()
        self._tray = pystray.Icon(
            "NetSplitTunnel", icon_img,
            "Net Split-Tunneler",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _restore_from_tray(self, icon=None, item=None) -> None:
        if self._tray and not self._show_speed_in_taskbar_var.get():
            self._tray.stop()
            self._tray = None
        self.after(0, self.deiconify)

    def _quit_app(self, icon=None, item=None) -> None:
        # Clean up
        if self._proxy.running:
            self._proxy.stop()
        self._beacon.stop()
        self._scanner.stop()
        if hasattr(self, "_overlay") and self._overlay:
            try:
                self._overlay.destroy()
            except Exception:
                pass
        if self._tray:
            self._tray.stop()
        self.after(0, self.destroy)


# ──────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    hide_console()
    if not is_admin():
        elevate()

    # Elevated now, check for single instance
    if not check_single_instance():
        ctypes.windll.user32.MessageBoxW(
            None,
            "Another instance of Net Split-Tunneler is already running.",
            "Application Already Running",
            0x10 | 0x0  # MB_ICONERROR | MB_OK
        )
        sys.exit(0)

    if not HAS_TRAY:
        import warnings
        warnings.warn(
            "pystray / Pillow not installed — system tray disabled. "
            "Run: pip install pystray pillow",
            stacklevel=1,
        )

    app = App()
    w, h = 480, 575
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    app.mainloop()


# ──────────────────────────────────────────────────────────────────────────────
#  BUILD (run once, in the project directory):
#
#  pip install pystray pillow pyinstaller
#
#  pyinstaller --onefile --windowed --uac-admin \
#              --name "NetSplitTunnel_v3" \
#              --hidden-import "pystray._win32" \
#              --collect-all pystray \
#              net_tunnel.py
#
#  Output: dist\NetSplitTunnel_v3.exe
#  The --uac-admin manifest means Windows prompts for elevation on every launch.
# ──────────────────────────────────────────────────────────────────────────────

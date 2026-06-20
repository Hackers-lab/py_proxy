"""Zero-dependency screen capture and input injection for the remote-screen
feature.

Capture uses the Win32 GDI ``BitBlt`` path via :mod:`ctypes` so it runs in a
plain worker thread (unlike ``QScreen.grabWindow``, which is GUI-thread only).
The raw BGRA pixels are wrapped in a :class:`QImage` — which *is* safe to use
off the GUI thread — and encoded to JPEG. No ``mss``/``Pillow``/``numpy``; the
only dependency is the Qt that the app already bundles.

Input injection (applying a viewer's mouse/keyboard on the host) uses the same
``user32`` calls the OS exposes to any process: ``SetCursorPos``,
``mouse_event`` and ``SendInput``.
"""

import ctypes
from ctypes import wintypes

from PyQt6.QtCore import QBuffer, QByteArray, Qt
from PyQt6.QtGui import QImage

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# Handles are pointer-sized; without explicit arg/res types ctypes assumes c_int
# and silently truncates 64-bit handles, which makes BitBlt/GetDIBits fail with
# "int too long to convert" (intermittently — only when a handle exceeds 2^31).
_C = ctypes
_VOID = _C.c_void_p
_INT = _C.c_int
_UINT = _C.c_uint
_DWORD = wintypes.DWORD

_user32.GetDC.restype = _VOID
_user32.GetDC.argtypes = [_VOID]
_user32.ReleaseDC.restype = _INT
_user32.ReleaseDC.argtypes = [_VOID, _VOID]
_user32.GetCursorInfo.argtypes = [_VOID]
_user32.DrawIconEx.argtypes = [_VOID, _INT, _INT, _VOID, _INT, _INT, _UINT, _VOID, _UINT]
_user32.SetCursorPos.argtypes = [_INT, _INT]

_gdi32.CreateCompatibleDC.restype = _VOID
_gdi32.CreateCompatibleDC.argtypes = [_VOID]
_gdi32.CreateCompatibleBitmap.restype = _VOID
_gdi32.CreateCompatibleBitmap.argtypes = [_VOID, _INT, _INT]
_gdi32.SelectObject.restype = _VOID
_gdi32.SelectObject.argtypes = [_VOID, _VOID]
_gdi32.BitBlt.restype = _C.c_bool
_gdi32.BitBlt.argtypes = [_VOID, _INT, _INT, _INT, _INT, _VOID, _INT, _INT, _DWORD]
_gdi32.GetDIBits.restype = _INT
_gdi32.GetDIBits.argtypes = [_VOID, _VOID, _UINT, _UINT, _VOID, _VOID, _UINT]
_gdi32.DeleteObject.argtypes = [_VOID]
_gdi32.DeleteDC.argtypes = [_VOID]

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_DIB_RGB_COLORS = 0
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

_DPI_AWARE_SET = False


def _ensure_dpi_aware() -> None:
    """Report true pixel dimensions (not DPI-virtualised ones) for capture."""
    global _DPI_AWARE_SET
    if _DPI_AWARE_SET:
        return
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass
    _DPI_AWARE_SET = True


def screen_size() -> tuple[int, int]:
    """Primary-monitor size in physical pixels."""
    _ensure_dpi_aware()
    return (_user32.GetSystemMetrics(_SM_CXSCREEN),
            _user32.GetSystemMetrics(_SM_CYSCREEN))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class ScreenGrabber:
    """Reusable GDI capture surface for the primary monitor.

    Allocating the memory DC/bitmap once per session (rather than per frame)
    keeps the capture loop cheap. Call :meth:`grab_jpeg` each frame and
    :meth:`close` when the session ends.
    """

    def __init__(self) -> None:
        _ensure_dpi_aware()
        self._w = 0
        self._h = 0
        self._hdc_screen = None
        self._hdc_mem = None
        self._hbmp = None
        self._bmi = None
        self._buf = None

    def _ensure_surface(self) -> bool:
        w = _user32.GetSystemMetrics(_SM_CXSCREEN)
        h = _user32.GetSystemMetrics(_SM_CYSCREEN)
        if w <= 0 or h <= 0:
            return False
        if w == self._w and h == self._h and self._hbmp:
            return True
        self._free_surface()
        self._hdc_screen = _user32.GetDC(0)
        self._hdc_mem = _gdi32.CreateCompatibleDC(self._hdc_screen)
        self._hbmp = _gdi32.CreateCompatibleBitmap(self._hdc_screen, w, h)
        _gdi32.SelectObject(self._hdc_mem, self._hbmp)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # negative => top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        self._bmi = bmi
        self._buf = (ctypes.c_char * (w * h * 4))()
        self._w, self._h = w, h
        return True

    def _draw_cursor(self) -> None:
        """Blit the current mouse cursor into the captured bitmap (BitBlt omits it)."""
        try:
            ci = _CURSORINFO()
            ci.cbSize = ctypes.sizeof(_CURSORINFO)
            if not _user32.GetCursorInfo(ctypes.byref(ci)):
                return
            CURSOR_SHOWING = 0x00000001
            if ci.flags != CURSOR_SHOWING or not ci.hCursor:
                return
            _user32.DrawIconEx(self._hdc_mem, ci.ptScreenPos.x, ci.ptScreenPos.y,
                               ci.hCursor, 0, 0, 0, None, 0x0003)  # DI_NORMAL
        except Exception:
            pass

    def grab_qimage(self) -> QImage | None:
        if not self._ensure_surface():
            return None
        ok = _gdi32.BitBlt(self._hdc_mem, 0, 0, self._w, self._h,
                           self._hdc_screen, 0, 0, _SRCCOPY | _CAPTUREBLT)
        if not ok:
            return None
        self._draw_cursor()
        _gdi32.GetDIBits(self._hdc_mem, self._hbmp, 0, self._h, self._buf,
                         ctypes.byref(self._bmi), _DIB_RGB_COLORS)
        # Format_RGB32 is 0xffRRGGBB, stored BGRA in memory on little-endian —
        # exactly what GetDIBits produced. copy() detaches from our reused buffer.
        img = QImage(self._buf, self._w, self._h, QImage.Format.Format_RGB32)
        return img.copy()

    def grab_jpeg(self, max_edge: int, quality: int) -> tuple[bytes, int, int] | None:
        """Capture, optionally downscale, and encode. Returns (bytes, w, h)."""
        img = self.grab_qimage()
        if img is None or img.isNull():
            return None
        longest = max(img.width(), img.height())
        if max_edge and longest > max_edge:
            if img.width() >= img.height():
                img = img.scaledToWidth(max_edge, Qt.TransformationMode.SmoothTransformation)
            else:
                img = img.scaledToHeight(max_edge, Qt.TransformationMode.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        # JPEG needs the qjpeg image-format plugin; fall back to (lossless, larger)
        # PNG if it isn't present so the feature still works.
        if not img.save(buf, "JPEG", quality):
            ba.clear()
            buf.seek(0)
            img.save(buf, "PNG")
        buf.close()
        return bytes(ba), img.width(), img.height()

    def _free_surface(self) -> None:
        try:
            if self._hbmp:
                _gdi32.DeleteObject(self._hbmp)
            if self._hdc_mem:
                _gdi32.DeleteDC(self._hdc_mem)
            if self._hdc_screen:
                _user32.ReleaseDC(0, self._hdc_screen)
        except Exception:
            pass
        self._hbmp = self._hdc_mem = self._hdc_screen = None

    def close(self) -> None:
        self._free_surface()
        self._buf = None
        self._w = self._h = 0


# ── Input injection ───────────────────────────────────────────────────────────

_MOUSEEVENTF = {
    ("l", True): 0x0002, ("l", False): 0x0004,
    ("r", True): 0x0008, ("r", False): 0x0010,
    ("m", True): 0x0020, ("m", False): 0x0040,
}
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


_user32.SendInput.argtypes = [_UINT, _C.c_void_p, _INT]
_user32.SendInput.restype = _UINT


def _send_unicode(ch: str) -> None:
    code = ord(ch)
    arr = (_INPUT * 2)()
    for i, up in enumerate((False, True)):
        arr[i].type = _INPUT_KEYBOARD
        arr[i].u.ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0), 0, None)
    _user32.SendInput(2, arr, ctypes.sizeof(_INPUT))


def apply_input(ev: dict, screen_w: int, screen_h: int) -> None:
    """Replay one viewer input event on this machine.

    Coordinates arrive normalised (0..1) so the viewer's display size and the
    host's resolution don't have to match.
    """
    try:
        kind = ev.get("k")
        if kind == "move":
            _user32.SetCursorPos(int(ev["x"] * screen_w), int(ev["y"] * screen_h))
        elif kind == "button":
            _user32.SetCursorPos(int(ev["x"] * screen_w), int(ev["y"] * screen_h))
            flag = _MOUSEEVENTF.get((ev.get("btn", "l"), bool(ev.get("down"))))
            if flag:
                _user32.mouse_event(flag, 0, 0, 0, 0)
        elif kind == "wheel":
            _user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(ev.get("delta", 0)), 0)
        elif kind == "key":
            vk = int(ev.get("vk", 0))
            if vk:
                _user32.keybd_event(vk, 0, 0 if ev.get("down") else _KEYEVENTF_KEYUP, 0)
        elif kind == "text":
            for ch in str(ev.get("ch", "")):
                _send_unicode(ch)
    except Exception:
        pass

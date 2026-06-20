"""Remote screen viewer & control — a lightweight VNC/AnyDesk-style session.

Design mirrors :mod:`nst.filetransfer`: a small TCP service that the host runs,
plus a viewer side that opens a connection to a peer. Everything for one session
travels over a single TCP socket using a length-prefixed frame protocol (see
:mod:`nst.constants` ``SF_*``):

  * the **viewer** connects to the host's ``SCREEN_TCP_PORT`` and sends a HELLO;
  * the **host** either auto-accepts (unattended secret matches) or asks the
    user, then streams JPEG frames while replaying the viewer's input;
  * the viewer paints frames and forwards mouse/keyboard/clipboard events.

The actual capture and input injection live in :mod:`nst.screencap` (pure Win32
via ctypes, so no new third-party dependency). Callbacks run on background
threads; the Qt layer marshals them to the GUI thread via signals.
"""

import json
import socket
import struct
import threading
import time
import uuid

from . import config
from .constants import (
    SCREEN_MAX_EDGE,
    SCREEN_TCP_PORT,
    SF_ACCEPT,
    SF_BYE,
    SF_CLIP,
    SF_FRAME,
    SF_HELLO,
    SF_INPUT,
    SF_PING,
    SF_REJECT,
)
from .screencap import ScreenGrabber, apply_input, screen_size

_HDR = struct.Struct(">BI")   # frame type (1 byte) + payload length (4 bytes)
_MAX_PAYLOAD = 32 * 1024 * 1024


# ── framing helpers ───────────────────────────────────────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _send_frame(sock: socket.socket, lock: threading.Lock, ftype: int,
                payload: bytes = b"") -> None:
    with lock:
        sock.sendall(_HDR.pack(ftype, len(payload)) + payload)


def _recv_frame(sock: socket.socket) -> tuple[int, bytes] | tuple[None, None]:
    hdr = _recv_exact(sock, _HDR.size)
    if not hdr:
        return None, None
    ftype, length = _HDR.unpack(hdr)
    if length > _MAX_PAYLOAD:
        return None, None
    payload = _recv_exact(sock, length) if length else b""
    if payload is None:
        return None, None
    return ftype, payload


def _send_json(sock, lock, ftype, obj) -> None:
    _send_frame(sock, lock, ftype, json.dumps(obj).encode("utf-8"))


# ── viewer side ───────────────────────────────────────────────────────────────

class ViewerSession:
    """A live connection the local user opened to view/control a remote host."""

    def __init__(self, ip: str, secret: str, my_name: str, my_ip: str,
                 on_frame=None, on_accept=None, on_reject=None,
                 on_clipboard=None, on_closed=None) -> None:
        self.ip = ip
        self._secret = secret
        self._my_name = my_name
        self._my_ip = my_ip
        self._on_frame = on_frame
        self._on_accept = on_accept
        self._on_reject = on_reject
        self._on_clipboard = on_clipboard
        self._on_closed = on_closed
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._running = False
        self.remote_w = 0
        self.remote_h = 0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        reason = ""
        try:
            self._sock = socket.create_connection((self.ip, SCREEN_TCP_PORT), timeout=6.0)
            self._sock.settimeout(None)
            _send_json(self._sock, self._send_lock, SF_HELLO, {
                "name": self._my_name, "ip": self._my_ip, "secret": self._secret,
            })
            self._running = True
            while self._running:
                ftype, payload = _recv_frame(self._sock)
                if ftype is None:
                    break
                if ftype == SF_FRAME:
                    if self._on_frame:
                        self._on_frame(payload)
                elif ftype == SF_ACCEPT:
                    info = json.loads(payload or b"{}")
                    self.remote_w = int(info.get("w", 0))
                    self.remote_h = int(info.get("h", 0))
                    if self._on_accept:
                        self._on_accept(info.get("name", self.ip), self.remote_w, self.remote_h)
                elif ftype == SF_REJECT:
                    info = json.loads(payload or b"{}")
                    reason = info.get("reason", "Declined")
                    if self._on_reject:
                        self._on_reject(reason)
                    self._running = False
                    break
                elif ftype == SF_CLIP:
                    info = json.loads(payload or b"{}")
                    if self._on_clipboard:
                        self._on_clipboard(info.get("text", ""))
                elif ftype == SF_BYE:
                    break
        except Exception as e:
            reason = reason or str(e)
        finally:
            self._running = False
            self._close_sock()
            if self._on_closed:
                self._on_closed(reason)

    # ── outgoing (viewer -> host) ────────────────────────────────────────────
    def send_input(self, ev: dict) -> None:
        if not self._running or not self._sock:
            return
        try:
            _send_json(self._sock, self._send_lock, SF_INPUT, ev)
        except Exception:
            self.close()

    def send_clipboard(self, text: str) -> None:
        if not self._running or not self._sock:
            return
        try:
            _send_json(self._sock, self._send_lock, SF_CLIP, {"text": text})
        except Exception:
            pass

    def close(self) -> None:
        if not self._running and not self._sock:
            return
        self._running = False
        try:
            if self._sock:
                _send_frame(self._sock, self._send_lock, SF_BYE)
        except Exception:
            pass
        self._close_sock()

    def _close_sock(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None


# ── host side ─────────────────────────────────────────────────────────────────

class HostSession:
    """One peer currently viewing/controlling this machine."""

    def __init__(self, service: "RemoteScreenService", conn: socket.socket,
                 viewer_name: str, viewer_ip: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.viewer_name = viewer_name
        self.viewer_ip = viewer_ip
        self._service = service
        self._sock = conn
        self._send_lock = threading.Lock()
        self._running = False
        self._grabber: ScreenGrabber | None = None

    def begin(self) -> None:
        """Accepted: tell the viewer, then stream frames and replay its input."""
        self._running = True
        w, h = screen_size()
        _send_json(self._sock, self._send_lock, SF_ACCEPT,
                   {"name": self._service.my_name, "w": w, "h": h})
        threading.Thread(target=self._input_loop, daemon=True).start()
        self._capture_loop()

    def reject(self, reason: str) -> None:
        try:
            _send_json(self._sock, self._send_lock, SF_REJECT, {"reason": reason})
        except Exception:
            pass
        self._close_sock()

    def _capture_loop(self) -> None:
        self._grabber = ScreenGrabber()
        try:
            while self._running and self._service.running:
                t0 = time.time()
                quality = config.load_remote_quality()
                shot = self._grabber.grab_jpeg(SCREEN_MAX_EDGE, quality)
                if shot is not None:
                    data, _w, _h = shot
                    try:
                        _send_frame(self._sock, self._send_lock, SF_FRAME, data)
                    except Exception:
                        break
                interval = 1.0 / max(1, config.load_remote_fps())
                time.sleep(max(0.0, interval - (time.time() - t0)))
        finally:
            self.stop()

    def _input_loop(self) -> None:
        sw, sh = screen_size()
        try:
            while self._running:
                ftype, payload = _recv_frame(self._sock)
                if ftype is None or ftype == SF_BYE:
                    break
                if ftype == SF_INPUT:
                    try:
                        apply_input(json.loads(payload or b"{}"), sw, sh)
                    except Exception:
                        pass
                elif ftype == SF_CLIP:
                    try:
                        info = json.loads(payload or b"{}")
                        self._service._emit_clipboard_from_viewer(info.get("text", ""))
                    except Exception:
                        pass
                elif ftype == SF_PING:
                    pass
        finally:
            self.stop()

    def send_clipboard(self, text: str) -> None:
        try:
            _send_json(self._sock, self._send_lock, SF_CLIP, {"text": text})
        except Exception:
            pass

    def stop(self) -> None:
        if not self._running:
            self._close_sock()
            return
        self._running = False
        try:
            _send_frame(self._sock, self._send_lock, SF_BYE)
        except Exception:
            pass
        if self._grabber:
            self._grabber.close()
        self._close_sock()
        self._service._on_session_ended(self)

    def _close_sock(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class RemoteScreenService:
    """Accepts incoming screen sessions and opens outgoing viewer sessions.

    Host policy is read live from :mod:`nst.config` so toggling settings takes
    effect without a restart.
    """

    def __init__(self, chat_service,
                 on_request=None,
                 on_share_started=None,
                 on_share_stopped=None,
                 on_clipboard_from_viewer=None) -> None:
        """
        on_request(name, ip, respond)        -- a peer wants in; call respond(bool).
        on_share_started(session)            -- a viewer session became active.
        on_share_stopped(session)            -- an active viewer session ended.
        on_clipboard_from_viewer(text)       -- a viewer pushed us clipboard text.
        All callbacks fire on background threads; marshal to the GUI thread.
        """
        self._chat = chat_service
        self._on_request = on_request
        self._on_share_started = on_share_started
        self._on_share_stopped = on_share_stopped
        self._on_clipboard_from_viewer = on_clipboard_from_viewer
        self._sessions: dict[str, HostSession] = {}
        self._lock = threading.Lock()
        self.running = False

    @property
    def my_name(self) -> str:
        return getattr(self._chat, "my_name", "PC")

    @property
    def my_ip(self) -> str:
        return getattr(self._chat, "my_ip", "127.0.0.1")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._server_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.stop()

    def active_sessions(self) -> list[HostSession]:
        with self._lock:
            return list(self._sessions.values())

    # ── host: incoming sessions ──────────────────────────────────────────────
    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # No SO_REUSEADDR: on Windows it lets another process co-bind and
            # hijack connections (see nst.chat). Single-instance is enforced
            # elsewhere, so an exclusive bind surfaces real conflicts instead.
            srv.bind(("0.0.0.0", SCREEN_TCP_PORT))
            srv.listen(4)
            srv.settimeout(1.0)
            while self.running:
                try:
                    conn, addr = srv.accept()
                    threading.Thread(target=self._handle_session,
                                     args=(conn, addr), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break
            srv.close()
        except Exception:
            pass

    def _handle_session(self, conn: socket.socket, addr) -> None:
        try:
            conn.settimeout(8.0)
            ftype, payload = _recv_frame(conn)
            if ftype != SF_HELLO:
                conn.close()
                return
            hello = json.loads(payload or b"{}")
            name = str(hello.get("name") or addr[0])
            ip = str(hello.get("ip") or addr[0])
            secret = str(hello.get("secret") or "")
            conn.settimeout(None)
            session = HostSession(self, conn, name, ip)

            if not config.load_remote_enabled():
                session.reject("Remote control is disabled on this PC")
                return

            if self._secret_ok(secret):
                self._run_accepted(session)
                return

            # Attended: ask the user, blocking this thread until they answer.
            if not self._on_request:
                session.reject("No one is available to accept")
                return
            decision = threading.Event()
            result = {"ok": False}

            def respond(ok: bool) -> None:
                result["ok"] = bool(ok)
                decision.set()

            self._on_request(name, ip, respond)
            if not decision.wait(config.load_remote_timeout()) or not result["ok"]:
                session.reject("Declined" if decision.is_set() else "No response")
                return
            self._run_accepted(session)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def _secret_ok(self, secret: str) -> bool:
        if not config.load_remote_unattended():
            return False
        want = config.load_remote_secret()
        return bool(want) and secret == want

    def _run_accepted(self, session: HostSession) -> None:
        with self._lock:
            self._sessions[session.id] = session
        if self._on_share_started:
            self._on_share_started(session)
        session.begin()   # blocks until the session ends

    def _on_session_ended(self, session: HostSession) -> None:
        with self._lock:
            self._sessions.pop(session.id, None)
        if self._on_share_stopped:
            self._on_share_stopped(session)

    def stop_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.stop()

    def _emit_clipboard_from_viewer(self, text: str) -> None:
        if text and self._on_clipboard_from_viewer:
            self._on_clipboard_from_viewer(text)

    # ── viewer: outgoing sessions ────────────────────────────────────────────
    def connect(self, ip: str, secret: str = "", on_frame=None, on_accept=None,
                on_reject=None, on_clipboard=None, on_closed=None) -> ViewerSession:
        """Open a session to view/control the host at *ip*. Returns immediately;
        callbacks fire on a background thread as the session progresses."""
        vs = ViewerSession(ip, secret, self.my_name, self.my_ip,
                           on_frame=on_frame, on_accept=on_accept,
                           on_reject=on_reject, on_clipboard=on_clipboard,
                           on_closed=on_closed)
        vs.start()
        return vs

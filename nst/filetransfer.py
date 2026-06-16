"""File transfer service.

Sender side:
  - ``offer_file``  sends a JSON offer via the chat TCP port (54323)
  - A file-data server on FILE_TCP_PORT (54324) serves the raw bytes once the
    receiver connects after accepting

Receiver side:
  - ``send_accept`` / ``send_reject``  reply via the chat TCP port
  - ``receive_file``  connects to the sender's FILE_TCP_PORT and pulls the data

Progress callbacks signature: (done_bytes, total_bytes, speed_bps, elapsed_secs, eta_secs)
"""

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from .constants import CHAT_TCP_PORT, FILE_SAVE_DIR, FILE_TCP_PORT

CHUNK = 65536


class FileTransferService:
    def __init__(self, chat_service) -> None:
        self._chat = chat_service
        # transfer_id -> (path, size, progress_cb, done_cb, error_cb)
        self._pending_sends: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self.running = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        save_dir = Path.home() / "Documents" / FILE_SAVE_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._server_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False

    # ── sender ────────────────────────────────────────────────────────────────
    def offer_file(self, ip: str, path: str,
                   progress_cb=None, done_cb=None, error_cb=None,
                   expire_cb=None, expire_after: int = 60) -> str:
        """Register a pending send, notify peer. Returns transfer_id. Raises on network error."""
        tid = uuid.uuid4().hex[:12]
        size = os.path.getsize(path)
        filename = os.path.basename(path)

        def _on_expire():
            with self._lock:
                if tid in self._pending_sends:
                    del self._pending_sends[tid]
            if expire_cb:
                expire_cb()

        timer = threading.Timer(expire_after, _on_expire)
        timer.daemon = True

        with self._lock:
            self._pending_sends[tid] = (path, size, progress_cb, done_cb, error_cb, timer)
        timer.start()

        payload = json.dumps({
            "type": "file_offer",
            "transfer_id": tid,
            "filename": filename,
            "size": size,
            "from_name": self._chat.my_name,
            "from_ip": self._chat.my_ip,
        }).encode() + b"\n"
        try:
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=3.0) as s:
                s.sendall(payload)
        except Exception:
            timer.cancel()
            with self._lock:
                self._pending_sends.pop(tid, None)
            raise
        return tid

    def cancel_offer(self, tid: str) -> None:
        """Cancel a pending outgoing offer (e.g. receiver rejected it)."""
        with self._lock:
            entry = self._pending_sends.pop(tid, None)
        if entry and len(entry) > 5 and entry[5]:
            entry[5].cancel()

    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", FILE_TCP_PORT))
            srv.listen(8)
            srv.settimeout(1.0)
            while self.running:
                try:
                    conn, addr = srv.accept()
                    threading.Thread(target=self._serve_one,
                                     args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception:
                    break
            srv.close()
        except Exception:
            pass

    def _serve_one(self, conn: socket.socket) -> None:
        tid = None
        try:
            conn.settimeout(5.0)
            buf = b""
            while b"\n" not in buf and len(buf) < 256:
                chunk = conn.recv(256)
                if not chunk:
                    return
                buf += chunk
            tid = buf.split(b"\n", 1)[0].decode().strip()
            with self._lock:
                entry = self._pending_sends.get(tid)
            if not entry or not os.path.exists(entry[0]):
                return
            path, size, progress_cb, done_cb, error_cb = entry[0], entry[1], entry[2], entry[3], entry[4]
            timer = entry[5] if len(entry) > 5 else None
            if timer:
                timer.cancel()
            filename = os.path.basename(path)
            header = json.dumps({"filename": filename, "size": size}).encode() + b"\n"
            conn.settimeout(None)
            conn.sendall(header)
            sent = 0
            start = time.time()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    sent += len(chunk)
                    if progress_cb:
                        elapsed = max(time.time() - start, 0.001)
                        speed = sent / elapsed
                        eta = (size - sent) / speed if speed > 0 else 0
                        progress_cb(sent, size, speed, elapsed, eta)
            with self._lock:
                self._pending_sends.pop(tid, None)
            if done_cb:
                done_cb()
        except Exception as e:
            if tid:
                with self._lock:
                    entry = self._pending_sends.pop(tid, None)
                if entry:
                    if len(entry) > 5 and entry[5]:
                        entry[5].cancel()
                    if entry[4]:
                        entry[4](str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── receiver ──────────────────────────────────────────────────────────────
    def send_accept(self, sender_ip: str, transfer_id: str) -> None:
        self._send_ctrl(sender_ip, {
            "type": "file_accept",
            "transfer_id": transfer_id,
            "from_name": self._chat.my_name,
            "from_ip": self._chat.my_ip,
        })

    def send_reject(self, sender_ip: str, transfer_id: str) -> None:
        self._send_ctrl(sender_ip, {
            "type": "file_reject",
            "transfer_id": transfer_id,
            "from_name": self._chat.my_name,
            "from_ip": self._chat.my_ip,
        })

    def receive_file(self, transfer_id: str, sender_ip: str,
                     progress_cb=None, done_cb=None, error_cb=None) -> None:
        threading.Thread(
            target=self._receive_one,
            args=(transfer_id, sender_ip, progress_cb, done_cb, error_cb),
            daemon=True,
        ).start()

    def _receive_one(self, tid: str, sender_ip: str,
                     progress_cb, done_cb, error_cb) -> None:
        try:
            time.sleep(0.3)
            with socket.create_connection((sender_ip, FILE_TCP_PORT), timeout=6.0) as s:
                s.sendall((tid + "\n").encode())
                buf = b""
                while b"\n" not in buf and len(buf) < 4096:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise ConnectionError("No header from sender")
                    buf += chunk
                line, rest = buf.split(b"\n", 1)
                info = json.loads(line)
                filename = info["filename"]
                total = info["size"]
                save_path = self._unique_path(filename)
                received = len(rest)
                start = time.time()
                with open(save_path, "wb") as f:
                    if rest:
                        f.write(rest)
                    while received < total:
                        chunk = s.recv(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        if progress_cb:
                            elapsed = max(time.time() - start, 0.001)
                            speed = received / elapsed
                            eta = (total - received) / speed if speed > 0 else 0
                            progress_cb(received, total, speed, elapsed, eta)
            if done_cb:
                done_cb(str(save_path))
        except Exception as e:
            if error_cb:
                error_cb(str(e))

    def _unique_path(self, filename: str) -> Path:
        base = Path.home() / "Documents" / FILE_SAVE_DIR
        path = base / filename
        if not path.exists():
            return path
        stem, suffix = Path(filename).stem, Path(filename).suffix
        i = 1
        while True:
            path = base / f"{stem} ({i}){suffix}"
            if not path.exists():
                return path
            i += 1

    def _send_ctrl(self, ip: str, payload: dict) -> None:
        try:
            data = json.dumps(payload).encode() + b"\n"
            with socket.create_connection((ip, CHAT_TCP_PORT), timeout=3.0) as s:
                s.sendall(data)
        except Exception:
            pass

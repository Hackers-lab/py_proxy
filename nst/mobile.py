"""Mobile web/PWA bridge.

Every desktop runs a lightweight HTTP + WebSocket server so phones on the
same Wi-Fi can join the LAN chat by scanning a QR code — no app install
required.

Architecture
============
* An ``aiohttp`` app runs inside a dedicated asyncio event-loop thread.
* ``GET /``   → serves the single-page PWA (HTML + CSS + JS, all inline).
* ``GET /ws`` → WebSocket; each connection becomes a ``MobileSession``.

Session lifecycle
-----------------
1. Phone opens the page, enters a display name, and connects the WebSocket.
2. Phone sends ``{"type": "hello", "name": "...", "device": "..."}``.
3. Desktop receives ``on_join`` callback → shows approval card.
4. Desktop calls ``approve(sid)`` / ``reject(sid)`` / ``block(sid)``.
5. Approved phone sends ``{"type": "chat", "text": "..."}``.
6. Desktop calls ``send_to(sid, ...)`` to send messages to the phone.

Thread safety
-------------
All public methods (``approve``, ``send_to``, etc.) are safe to call from
any thread.  They schedule work on the asyncio loop with
``asyncio.run_coroutine_threadsafe``.  The callbacks (``on_join``,
``on_leave``, ``on_message``) fire from the asyncio thread — the Qt layer
wires them through ``pyqtSignal`` which delivers them safely on the main
thread.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

try:
    from aiohttp import web as _aio_web
    from aiohttp import WSMsgType as _WSMsgType
    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False

import subprocess

from .netinfo import get_all_local_ips


# ── Firewall helper ───────────────────────────────────────────────────────────

def _add_firewall_rule(port: int) -> None:
    """Attempt to add a Windows Firewall inbound rule for *port* (requires admin).

    Runs silently; failure is swallowed so missing admin rights don't break startup.
    """
    name = f"NetSplitTunnel_Mobile_{port}"
    try:
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             f"name={name}", "dir=in", "action=allow",
             "protocol=TCP", f"localport={port}", "profile=any"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


# ── Mobile PWA (served from memory, no files on disk) ────────────────────────
_INDEX_HTML = """\
<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1'>
<meta name='apple-mobile-web-app-capable' content='yes'>
<title>LAN Chat</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f1117;color:#e2e8f0;height:100dvh;display:flex;
     flex-direction:column;overflow:hidden}
#setup{flex:1;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:14px;padding:32px}
#chat{flex:1;display:none;flex-direction:column}
#hdr{padding:12px 16px;background:#1a1b23;border-bottom:1px solid #2d3748;
     font-weight:700;font-size:15px;display:flex;align-items:center;gap:8px}
#msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:5px}
.b{max-width:78%;padding:8px 12px;border-radius:12px;font-size:14px;line-height:1.45;
   word-break:break-word}
.o{align-self:flex-end;background:#3b82f6;color:#fff;border-bottom-right-radius:3px}
.i{align-self:flex-start;background:#1e2433;border-bottom-left-radius:3px}
.bn{font-size:11px;font-weight:700;margin-bottom:3px;color:#60a5fa}
.sy{align-self:center;font-size:11px;color:#6b7280;padding:3px 0}
#comp{display:flex;gap:8px;padding:8px;border-top:1px solid #2d3748}
#mi{flex:1;background:#1e2433;border:1px solid #374151;color:#e2e8f0;
    border-radius:22px;padding:10px 16px;font-size:15px;outline:none}
#sb{background:#3b82f6;color:#fff;border:none;border-radius:22px;
    padding:10px 18px;font-size:15px;cursor:pointer;font-weight:600}
#setup input{width:100%;max-width:300px;background:#1e2433;border:1px solid #374151;
             color:#e2e8f0;border-radius:10px;padding:12px 16px;font-size:16px;outline:none}
#setup button{width:100%;max-width:300px;background:#3b82f6;color:#fff;border:none;
              border-radius:10px;padding:13px;font-size:16px;font-weight:700;cursor:pointer}
#setup h2{font-size:22px;font-weight:700}
#setup p{font-size:13px;color:#6b7280;text-align:center}
#st{font-size:13px;color:#6b7280;text-align:center;min-height:20px}
</style>
</head>
<body>
<div id='setup'>
  <h2>📡 LAN Chat</h2>
  <p>Enter your name to request access from the desktop</p>
  <input id='ni' placeholder='Your name' maxlength='32' autocomplete='name'>
  <button id='jb' onclick='join()'>Request Access</button>
  <div id='st'></div>
</div>
<div id='chat'>
  <div id='hdr'><span>📡</span><span id='hn'>LAN Chat</span></div>
  <div id='msgs'></div>
  <div id='comp'>
    <button id='ab' onclick='document.getElementById("fi").click()' style='background:none;border:none;color:#94a3b8;font-size:20px;cursor:pointer;padding:0 8px'>📎</button>
    <input type="file" id="fi" style="display:none" onchange="uploadFile()">
    <input id='mi' placeholder='Message…' autocomplete='off'>
    <button id='sb' onclick='send()'>&#10148;</button>
  </div>
</div>
<script>
var ws=null,myName='',ok=false;
var clientId=localStorage.getItem('nst_client_id');
if(!clientId){
  clientId='mob_'+Math.random().toString(36).substring(2,15)+Math.random().toString(36).substring(2,15);
  localStorage.setItem('nst_client_id',clientId);
}
var savedName=localStorage.getItem('nst_name')||'';
if(savedName){
  document.getElementById('ni').value=savedName;
  join();
}
function st(t){document.getElementById('st').textContent=t;}
function join(){
  var n=document.getElementById('ni').value.trim();
  if(!n)return;
  myName=n;
  localStorage.setItem('nst_name',myName);
  document.getElementById('jb').disabled=true;
  st('Connecting…');
  ws=new WebSocket('ws://'+location.host+'/ws');
  ws.onopen=function(){
    ws.send(JSON.stringify({type:'hello',name:myName,device:navigator.userAgent.slice(0,80),client_id:clientId}));
    st('Waiting for approval from the desktop…');
  };
  ws.onmessage=function(e){
    var m=JSON.parse(e.data);
    if(m.type==='approved'){
      ok=true;
      document.getElementById('setup').style.display='none';
      var c=document.getElementById('chat');c.style.display='flex';
      document.getElementById('hn').textContent='LAN Chat — '+myName;
      var mi=document.getElementById('mi');
      mi.focus();
      mi.onkeydown=function(ev){if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();send();}};
    }else if(m.type==='rejected'){
      st('Your request was rejected.');
      document.getElementById('jb').disabled=false;
    }else if(m.type==='blocked'){
      st('You have been blocked from this chat.');
    }else if(m.type==='chat'){
      addMsg(m.name,m.text,m.name===myName);
    }else if(m.type==='history'){
      (m.messages||[]).forEach(function(x){addMsg(x.name,x.text,x.name!=='You');});
      sc();
    }else if(m.type==='sys'){
      addSys(m.text);
    }else if(m.type==='file_offer'){
      addFileOffer(m.transfer_id, m.filename, m.size);
    }else if(m.type==='file_cancel'){
      var el = document.getElementById('offer_'+m.transfer_id);
      if(el) {
          el.innerHTML = '📎 <strong>' + el.getAttribute('data-filename') + '</strong><br><br><span style="color:#ef4444">Cancelled by desktop</span>';
      }
    }else if(m.type==='file_accept'){
      var file = (window.pendingUploads||{})[m.transfer_id];
      if(file) doUpload(file, m.transfer_id);
    }else if(m.type==='file_reject'){
      addSys('Desktop rejected ' + ((window.pendingUploads||{})[m.transfer_id]||{}).name);
    }
  };
  ws.onclose=function(){
    if(ok){addSys('Disconnected from server.');}
    else{st('Connection closed.');document.getElementById('jb').disabled=false;}
  };
  ws.onerror=function(){st('Could not connect to server.');document.getElementById('jb').disabled=false;};
}
function addMsg(name,text,isOut){
  var d=document.createElement('div');d.className='b '+(isOut?'o':'i');
  if(!isOut){var n=document.createElement('div');n.className='bn';n.textContent=name;d.appendChild(n);}
  var t=document.createElement('div');t.textContent=text;d.appendChild(t);
  document.getElementById('msgs').appendChild(d);sc();
}
function addSys(t){
  var d=document.createElement('div');d.className='sy';d.textContent=t;
  document.getElementById('msgs').appendChild(d);sc();
  return d;
}
function sc(){var m=document.getElementById('msgs');m.scrollTop=m.scrollHeight;}
function send(){
  var i=document.getElementById('mi'),t=i.value.trim();
  if(!t||!ws||ws.readyState!==1)return;
  ws.send(JSON.stringify({type:'chat',text:t}));
  addMsg(myName,t,true);
  i.value='';
}
function formatSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / (1024 * 1024)).toFixed(1) + ' MB';
}
function addFileOffer(tid, filename, size) {
  var d = document.createElement('div');
  d.className = 'b i';
  var n = document.createElement('div');
  n.className = 'bn';
  n.textContent = 'File Offer';
  d.appendChild(n);
  var t = document.createElement('div');
  t.id = 'offer_' + tid;
  t.setAttribute('data-filename', filename);
  t.innerHTML = '📎 <strong>' + filename + '</strong> (' + formatSize(size) + ')<br><br>' +
    '<a href="/download?tid=' + tid + '&sid=' + clientId + '" target="_blank" style="display:inline-block;background:#3b82f6;color:#fff;text-decoration:none;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:700">Download</a>';
  d.appendChild(t);
  document.getElementById('msgs').appendChild(d);
  sc();
}
function uploadFile() {
  var fi = document.getElementById('fi');
  if (!fi.files || fi.files.length === 0) return;
  var file = fi.files[0];
  var tid = 'tid_' + Math.random().toString(36).substring(2,15);
  window.pendingUploads = window.pendingUploads || {};
  window.pendingUploads[tid] = file;
  if(ws && ws.readyState===1) {
    ws.send(JSON.stringify({type:'file_offer', transfer_id:tid, filename:file.name, size:file.size}));
    addSys('Offered ' + file.name + ' to desktop...');
  }
}
function doUpload(file, tid) {
  var fd = new FormData();
  fd.append('file', file);
  var statusMsg = addSys('Uploading ' + file.name + ' (0%)');
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload?sid=' + clientId + '&tid=' + tid, true);
  xhr.upload.onprogress = function(e) {
    if (e.lengthComputable) {
      var pct = Math.round((e.loaded / e.total) * 100);
      statusMsg.textContent = 'Uploading ' + file.name + ' (' + pct + '%)';
    }
  };
  xhr.onload = function() {
    if (xhr.status === 200) {
      statusMsg.textContent = '✓ Uploaded ' + file.name;
    } else {
      statusMsg.textContent = '✗ Upload failed: ' + (xhr.statusText || 'Error code ' + xhr.status);
    }
  };
  xhr.onerror = function() {
    statusMsg.textContent = '✗ Upload failed.';
  };
  xhr.send(fd);
}
</script>
</body>
</html>
"""


# ── Session ───────────────────────────────────────────────────────────────────

@dataclass
class MobileSession:
    sid: str
    name: str
    device: str
    ip: str
    connected_at: float = field(default_factory=time.time)
    state: str = "pending"   # pending | approved | blocked | rejected
    _ws: object = field(default=None, repr=False, compare=False)


# ── Server ────────────────────────────────────────────────────────────────────

class MobileServer:
    """Embedded HTTP + WebSocket bridge for mobile clients.

    Parameters
    ----------
    port        TCP port (default MOBILE_HTTP_PORT).
    on_join     Called when a phone submits its name; session.state == "pending".
    on_leave    Called when a WebSocket closes.
    on_message  Called when an approved phone sends a chat message.
    """

    def __init__(self, port: int = 8765,
                 on_join=None, on_leave=None, on_message=None,
                 on_file_offer=None, on_file_progress=None,
                 on_file=None, on_file_downloaded=None) -> None:
        self._port = port
        self._on_join = on_join
        self._on_leave = on_leave
        self._on_message = on_message
        self._on_file_offer = on_file_offer
        self._on_file_progress = on_file_progress
        self._on_file = on_file
        self._on_file_downloaded = on_file_downloaded

        self._sessions: dict[str, MobileSession] = {}
        self._blocked_ips: set[str] = set()
        self._pending_files: dict[str, str] = {}  # tid -> local_file_path
        self._lock = threading.Lock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner = None
        self._thread: threading.Thread | None = None
        self.running = False
        self.is_serving = False   # True only after server is successfully listening
        self.start_error = ""     # populated if startup fails

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _AIOHTTP_OK:
            return
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, name="mobile-server",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_server())
            self.is_serving = True
            self._loop.run_forever()
        except Exception as exc:
            self.start_error = str(exc)
            self.running = False

    async def _start_server(self) -> None:
        _add_firewall_rule(self._port)   # attempt; no-op if not admin
        app = _aio_web.Application()
        app.router.add_get("/", self._http_index)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/download", self._http_download)
        app.router.add_post("/upload", self._http_upload)
        self._runner = _aio_web.AppRunner(app)
        await self._runner.setup()
        site = _aio_web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()

    # ── HTTP ───────────────────────────────────────────────────────────────────

    async def _http_index(self, request):
        return _aio_web.Response(text=_INDEX_HTML, content_type="text/html",
                                 charset="utf-8")

    async def _http_download(self, request):
        tid = request.query.get("tid")
        sid = request.query.get("sid")
        with self._lock:
            session = self._sessions.get(sid)
        if not session or session.state != "approved":
            return _aio_web.Response(status=403, text="Unauthorized")
        path = None
        with self._lock:
            path = self._pending_files.get(tid)
        if not path or not os.path.exists(path):
            return _aio_web.Response(status=404, text="File not found")
        filename = os.path.basename(path)
        if self._on_file_downloaded:
            try:
                self._on_file_downloaded(sid, tid)
            except Exception:
                pass
        return _aio_web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        })

    async def _http_upload(self, request):
        sid = request.query.get("sid")
        tid = request.query.get("tid", "")
        with self._lock:
            session = self._sessions.get(sid)
        if not session or session.state != "approved":
            return _aio_web.Response(status=403, text="Unauthorized")
        reader = await request.multipart()
        field = await reader.next()
        if not field or field.name != "file":
            return _aio_web.Response(status=400, text="No file field")
        filename = field.filename
        from .filetransfer import FILE_SAVE_DIR
        base = Path.home() / "Documents" / FILE_SAVE_DIR
        base.mkdir(parents=True, exist_ok=True)
        path = base / filename
        if path.exists():
            stem, suffix = Path(filename).stem, Path(filename).suffix
            i = 1
            while True:
                path = base / f"{stem} ({i}){suffix}"
                if not path.exists():
                    break
                i += 1
        size = 0
        with open(path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
                if self._on_file_progress:
                    try:
                        self._on_file_progress(session, tid, size)
                    except Exception:
                        pass
        if self._on_file:
            try:
                self._on_file(session, tid, filename, str(path), size)
            except Exception:
                pass
        return _aio_web.Response(text="OK")

    # ── WebSocket ──────────────────────────────────────────────────────────────

    async def _handle_ws(self, request):
        peer_ip = request.remote or "0.0.0.0"
        # Strip IPv6-mapped prefix (::ffff:192.168.x.x)
        if peer_ip.startswith("::ffff:"):
            peer_ip = peer_ip[7:]

        if peer_ip in self._blocked_ips:
            return _aio_web.Response(status=403, text="Blocked")

        ws = _aio_web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        sid = uuid.uuid4().hex[:12]
        session = MobileSession(sid=sid, name="", device="", ip=peer_ip)
        session._ws = ws

        with self._lock:
            self._sessions[sid] = session

        try:
            async for raw in ws:
                if raw.type == _WSMsgType.TEXT:
                    try:
                        await self._dispatch(session, json.loads(raw.data))
                    except Exception:
                        pass
                elif raw.type in (_WSMsgType.ERROR, _WSMsgType.CLOSE):
                    break
        finally:
            with self._lock:
                if self._sessions.get(session.sid) is session:
                    self._sessions.pop(session.sid, None)
            if self._on_leave:
                try:
                    self._on_leave(session)
                except Exception:
                    pass

        return ws

    async def _dispatch(self, session: MobileSession, msg: dict) -> None:
        mtype = msg.get("type", "")
        if mtype == "hello" and session.state == "pending":
            client_id = msg.get("client_id")
            if client_id:
                old_sid = session.sid
                session.sid = client_id
                with self._lock:
                    self._sessions.pop(old_sid, None)
                    self._sessions[client_id] = session
            session.name = str(msg.get("name", "")).strip()[:32] or "Mobile"
            session.device = str(msg.get("device", "")).strip()[:80]
            
            # Check if this device is already approved
            from .config import load_approved_mobile_devices
            approved_list = load_approved_mobile_devices()
            if client_id and client_id in approved_list:
                session.state = "approved"
                self._send_nowait(session, {"type": "approved"})
                
            if self._on_join:
                self._on_join(session)
        elif mtype == "chat" and session.state == "approved":
            text = str(msg.get("text", "")).strip()
            if text and self._on_message:
                self._on_message(session, text)
        elif mtype == "file_offer" and session.state == "approved":
            tid = msg.get("transfer_id")
            filename = msg.get("filename")
            size = msg.get("size")
            if tid and filename and self._on_file_offer:
                self._on_file_offer(session, tid, filename, size)

    # ── control (safe to call from any thread) ────────────────────────────────

    def approve(self, sid: str, history: list[dict] | None = None) -> None:
        """Approve a pending session; optionally send recent history."""
        with self._lock:
            session = self._sessions.get(sid)
        if not session:
            return
        session.state = "approved"
        self._send_nowait(session, {"type": "approved"})
        if history:
            msgs = [{"name": e.get("sender", ""), "text": e.get("text", "")}
                    for e in history[-50:]
                    if e.get("text") and e.get("kind") in ("in", "out")]
            if msgs:
                self._send_nowait(session, {"type": "history", "messages": msgs})

    def reject(self, sid: str) -> None:
        with self._lock:
            session = self._sessions.get(sid)
        if not session:
            return
        session.state = "rejected"
        self._send_nowait(session, {"type": "rejected"})

    def block(self, sid: str) -> None:
        with self._lock:
            session = self._sessions.get(sid)
        if not session:
            return
        session.state = "blocked"
        self._blocked_ips.add(session.ip)
        self._send_nowait(session, {"type": "blocked"})

    def accept_file(self, sid: str, tid: str) -> None:
        with self._lock:
            session = self._sessions.get(sid)
        if session and session.state == "approved":
            self._send_nowait(session, {"type": "file_accept", "transfer_id": tid})

    def reject_file(self, sid: str, tid: str) -> None:
        with self._lock:
            session = self._sessions.get(sid)
        if session and session.state == "approved":
            self._send_nowait(session, {"type": "file_reject", "transfer_id": tid})

    def cancel_file_offer(self, sid: str, tid: str) -> None:
        with self._lock:
            self._pending_files.pop(tid, None)
            session = self._sessions.get(sid)
        if session and session.state == "approved":
            self._send_nowait(session, {"type": "file_cancel", "transfer_id": tid})

    def unblock_ip(self, ip: str) -> None:
        self._blocked_ips.discard(ip)

    def send_to(self, sid: str, sender_name: str, text: str) -> None:
        """Send a message to one specific mobile session."""
        with self._lock:
            session = self._sessions.get(sid)
        if session and session.state == "approved":
            self._send_nowait(session, {"type": "chat", "name": sender_name,
                                        "text": text})

    def broadcast_chat(self, sender_name: str, text: str,
                       exclude_sid: str = "") -> None:
        """Relay a message to every approved mobile session except *exclude_sid*."""
        payload = {"type": "chat", "name": sender_name, "text": text}
        with self._lock:
            targets = [s for s in self._sessions.values()
                       if s.state == "approved" and s.sid != exclude_sid]
        for s in targets:
            self._send_nowait(s, payload)

    def send_sys(self, sid: str, text: str) -> None:
        """Send a system notice to one session."""
        with self._lock:
            session = self._sessions.get(sid)
        if session:
            self._send_nowait(session, {"type": "sys", "text": text})

    def disconnect(self, sid: str) -> None:
        """Forcibly close a WebSocket connection."""
        with self._lock:
            session = self._sessions.get(sid)
        if session and session._ws and self._loop:
            asyncio.run_coroutine_threadsafe(session._ws.close(), self._loop)

    def sessions(self) -> list[MobileSession]:
        with self._lock:
            return list(self._sessions.values())

    def approved_sessions(self) -> list[MobileSession]:
        with self._lock:
            return [s for s in self._sessions.values() if s.state == "approved"]

    def blocked_ips(self) -> list[str]:
        return list(self._blocked_ips)

    # ── URL / QR helpers ──────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        return self._port

    def get_access_urls(self) -> list[str]:
        return [f"http://{ip}:{self._port}" for ip in get_all_local_ips()]

    def get_access_urls_labeled(self) -> list[tuple[str, str]]:
        """Return [(human_label, url), ...] for every local IP."""
        result = []
        for ip in get_all_local_ips():
            if ip.startswith("192.168."):
                label = "Wi-Fi / Hotspot"
            elif ip.startswith("10."):
                label = "LAN (10.x)"
            elif ip.startswith("172."):
                label = "LAN (172.x)"
            else:
                label = "Network"
            result.append((label, f"http://{ip}:{self._port}"))
        return result

    def get_qr_url(self) -> str | None:
        """Best URL for QR: prefer 192.168.x (Wi-Fi/hotspot), then any private IP."""
        ips = get_all_local_ips()
        for ip in ips:
            if ip.startswith("192.168."):
                return f"http://{ip}:{self._port}"
        return f"http://{ips[0]}:{self._port}" if ips else None

    def register_pending_file(self, tid: str, path: str) -> None:
        """Register a file to be served for download via HTTP /download."""
        with self._lock:
            self._pending_files[tid] = path

    def send_file_offer(self, sid: str, tid: str, filename: str, size: int) -> None:
        """Send a file offer via WebSocket to the approved mobile client."""
        with self._lock:
            session = self._sessions.get(sid)
        if session and session.state == "approved":
            self._send_nowait(session, {
                "type": "file_offer",
                "transfer_id": tid,
                "filename": filename,
                "size": size
            })

    # ── internal ─────────────────────────────────────────────────────────────

    def _send_nowait(self, session: MobileSession, payload: dict) -> None:
        if not self._loop or not session._ws:
            return
        data = json.dumps(payload, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(
            session._ws.send_str(data), self._loop)


# ── QR code generation ────────────────────────────────────────────────────────

def make_qr_png(url: str) -> bytes | None:
    """Return PNG bytes for a QR code of *url*, or None if qrcode/Pillow absent."""
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(box_size=5, border=4,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

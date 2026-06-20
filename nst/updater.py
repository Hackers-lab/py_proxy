"""Silent self-update from GitHub Releases.

The app checks the project's *latest* release on startup and every 24h. When a
newer version is found it downloads the per-user Inno Setup installer and runs
it ``/VERYSILENT``; the installer closes the running app, swaps the files and
relaunches it. Updates are applied **only while the chat window is closed** so
an active conversation is never interrupted — if the chat is open, the
installer is *staged* and applied the moment the chat closes (or on next launch).

Stdlib-only (urllib + json); all network/IO is best-effort and never blocks
startup or raises into the UI.
"""

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import __version__, config

GITHUB_LATEST = "https://api.github.com/repos/Hackers-lab/py_proxy/releases/latest"
_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000  # 24 hours
# /VERYSILENT  — no UI; /NORESTART — never reboot the machine.
# The installer's own [Run] entry relaunches the app, so we don't ask Restart
# Manager to do it (which would double-launch).
_SILENT_FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


# ── version helpers ───────────────────────────────────────────────────────────

def _parse(v: str) -> tuple:
    """'v4.9.2' / '4.9.2' → (4, 9, 2). Non-numeric parts are dropped."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) or (0,)


def _is_newer(remote: str, local: str) -> bool:
    return _parse(remote) > _parse(local)


# ── GitHub query + download ───────────────────────────────────────────────────

def _fetch_latest() -> tuple[str, str] | None:
    """Return ``(version, setup_download_url)`` for the newest installer asset.

    A release can accidentally carry more than one ``*Setup*.exe`` (e.g. a stale
    file from a previous build). Pick the asset with the HIGHEST version in its
    filename and report THAT version — not the release tag — so the "is it
    newer?" check matches the file we'd actually install. Comparing against the
    tag while installing an older asset causes an endless update loop.
    Returns None on any error or if no versioned installer asset exists.
    """
    req = urllib.request.Request(
        GITHUB_LATEST,
        headers={"User-Agent": "NetSplitTunnel-Updater",
                 "Accept": "application/vnd.github+json"},
    )
    def _open(ctx=None):
        return urllib.request.urlopen(req, timeout=15, context=ctx)
    try:
        resp_ctx = _open()
    except urllib.error.URLError as exc:
        if "certificate" in str(exc).lower() or "ssl" in str(exc).lower():
            _ctx = ssl.create_default_context()
            _ctx.check_hostname = False
            _ctx.verify_mode = ssl.CERT_NONE
            resp_ctx = _open(_ctx)
        else:
            raise
    with resp_ctx as resp:
        data = json.loads(resp.read().decode("utf-8"))
    best = None  # (version_tuple, version_str, url)
    for asset in data.get("assets", []):
        name = str(asset.get("name", ""))
        url = asset.get("browser_download_url")
        if not url or "setup" not in name.lower() or not name.lower().endswith(".exe"):
            continue
        m = re.search(r"(\d+(?:\.\d+)+)", name)
        if not m:
            continue
        key = _parse(m.group(1))
        if best is None or key > best[0]:
            best = (key, m.group(1), url)
    if best is None:
        return None
    return best[1], best[2]


def _fmt_size(n: int) -> str:
    """Bytes → a short human size, e.g. '12.4 MB'."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _download(url: str, version: str, progress=None) -> str:
    """Download *url* to %TEMP% and return the local path.

    *progress* (optional) is called with human-readable strings as the download
    starts, advances (~every 25%) and completes — wired to the event log so the
    user can see the installer's size and download progress.
    """
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", version) or "latest"
    dest = os.path.join(tempfile.gettempdir(),
                        f"NetSplitTunnel_Setup_{safe}.exe")
    req = urllib.request.Request(
        url, headers={"User-Agent": "NetSplitTunnel-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        if progress:
            progress(f"Downloading update v{version}"
                     + (f" ({_fmt_size(total)})…" if total else "…"))
        got = 0
        last_pct = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                pct = got * 100 // total
                if pct >= last_pct + 25 and pct < 100:
                    last_pct = pct
                    progress(f"Update v{version}: {pct}% "
                             f"({_fmt_size(got)} / {_fmt_size(total)})")
        if progress:
            progress(f"Update v{version} downloaded ({_fmt_size(got)}).")
    return dest


def _check_for_update(progress=None) -> tuple[str, str] | None:
    """Find + download a newer installer. Returns ``(version, path)`` or None.

    No-ops when running from source (not frozen). *progress* is forwarded to
    :func:`_download` for event-log reporting.
    """
    if not getattr(sys, "frozen", False):
        return None
    latest = _fetch_latest()
    if not latest:
        return None
    version, url = latest
    if not _is_newer(version, __version__):
        return None
    if progress:
        progress(f"Update v{version} found (current v{__version__}).")
    return version, _download(url, version, progress)


def _launch_installer(path: str) -> bool:
    """Start the staged installer silently. Returns True if it launched."""
    try:
        subprocess.Popen([path, *_SILENT_FLAGS], close_fds=True)
        return True
    except Exception:
        return False


def apply_staged_on_launch() -> None:
    """If a newer installer was staged earlier, run it before the UI opens.

    Called at the very start of ``nst.qt.app.run``; exits the process so the
    installer can replace the files and relaunch the new version.
    """
    version, path = config.load_staged_update()
    if (version and path and os.path.exists(path)
            and _is_newer(version, __version__)):
        if _launch_installer(path):
            config.clear_staged_update()
            sys.exit(0)
    elif version or path:
        # Stale entry (already applied or file gone) — clean it up.
        config.clear_staged_update()


# ── Qt-side manager ───────────────────────────────────────────────────────────

class UpdateManager(QObject):
    """Drives the startup + 24h checks and applies updates when chat is closed.

    *is_chat_open* is a callable returning whether the chat window is currently
    visible; *quit_app* shuts the app down after the installer launches.
    """

    _ready = pyqtSignal(str, str)   # version, installer_path  (worker → UI thread)
    status = pyqtSignal(str)        # human message, for manual "Check for updates"
    log = pyqtSignal(str)           # detailed progress (update found, size, %) → event log

    def __init__(self, is_chat_open, quit_app, parent=None) -> None:
        super().__init__(parent)
        self._is_chat_open = is_chat_open
        self._quit_app = quit_app
        self._ready.connect(self._on_ready)
        self._timer = QTimer(self)
        self._timer.setInterval(_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(lambda: self.check(manual=False))

    def start(self) -> None:
        """Begin periodic checks and run the first check now."""
        self.check(manual=False)
        self._timer.start()

    def check(self, manual: bool = False) -> None:
        if not manual and not config.load_auto_update_enabled():
            return
        threading.Thread(target=self._worker, args=(manual,), daemon=True).start()

    def _worker(self, manual: bool) -> None:
        try:
            # self.log.emit is thread-safe (Qt queues it to the GUI thread), so
            # the download can report size/progress straight into the event log.
            result = _check_for_update(progress=self.log.emit)
        except Exception as e:
            if manual:
                self.status.emit(f"Update check failed: {e}")
            return
        if result:
            self._ready.emit(result[0], result[1])
        elif manual:
            self.status.emit(f"You're on the latest version ({__version__}).")

    def _on_ready(self, version: str, path: str) -> None:
        """On the UI thread: apply now if chat is closed, else stage it."""
        if self._is_chat_open():
            config.save_staged_update(version, path)
            self.status.emit(
                f"Update {version} downloaded — it will install when you "
                f"close the chat window.")
        else:
            self._apply(version, path)

    def apply_staged_if_any(self) -> None:
        """Apply a staged update now (called when the chat window closes)."""
        version, path = config.load_staged_update()
        if (version and path and os.path.exists(path)
                and _is_newer(version, __version__)):
            self._apply(version, path)

    def _apply(self, version: str, path: str) -> None:
        self.status.emit(
            f"Update {version} ready — restarting in 5 seconds…")
        QTimer.singleShot(5000, lambda: self._launch_and_quit(version, path))

    def _launch_and_quit(self, version: str, path: str) -> None:
        if not os.path.exists(path):
            self.status.emit(
                f"Update {version}: installer not found (may have been removed "
                f"by antivirus). Restart the app to re-download.")
            return
        if _launch_installer(path):
            config.clear_staged_update()
            self._quit_app()
        else:
            self.status.emit(
                f"Update {version}: installer failed to launch. "
                f"Restart the app to retry, or reinstall from GitHub.")

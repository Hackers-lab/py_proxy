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
    """Return ``(version_tag, setup_download_url)`` of the latest release.

    Picks the release asset whose name contains 'Setup' and ends in '.exe'.
    Returns None on any error or if no suitable asset exists.
    """
    req = urllib.request.Request(
        GITHUB_LATEST,
        headers={"User-Agent": "NetSplitTunnel-Updater",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name", "")).strip()
    for asset in data.get("assets", []):
        name = str(asset.get("name", ""))
        if "setup" in name.lower() and name.lower().endswith(".exe"):
            url = asset.get("browser_download_url")
            if tag and url:
                return tag, url
    return None


def _download(url: str, version: str) -> str:
    """Download *url* to %TEMP% and return the local path."""
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", version) or "latest"
    dest = os.path.join(tempfile.gettempdir(),
                        f"NetSplitTunnel_Setup_{safe}.exe")
    req = urllib.request.Request(
        url, headers={"User-Agent": "NetSplitTunnel-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    return dest


def _check_for_update() -> tuple[str, str] | None:
    """Find + download a newer installer. Returns ``(version, path)`` or None.

    No-ops when running from source (not frozen).
    """
    if not getattr(sys, "frozen", False):
        return None
    latest = _fetch_latest()
    if not latest:
        return None
    version, url = latest
    if not _is_newer(version, __version__):
        return None
    return version, _download(url, version)


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
            result = _check_for_update()
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
        if _launch_installer(path):
            config.clear_staged_update()
            self._quit_app()

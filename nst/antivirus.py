"""Outgoing/incoming file malware scan using whatever antivirus is active.

A hand-written "Python antivirus" isn't real protection — real protection is a
signature database kept continuously current. So instead of pretending to be an
engine, this module drives the antivirus the machine *already* runs:

1. **Cheap offline heuristics** — the EICAR test signature and the classic
   double-extension disguise (``invoice.pdf.exe``). Instant, no engine needed.
2. **The active resident antivirus, via an on-access "tripwire"** — we copy the
   file to a temp location; every real-time AV (Windows Defender *or* a
   third-party product like Kaspersky/Sophos/McAfee) inspects that write and
   quarantines it if it's malware. If the copy is removed or locked, the active
   AV flagged it. This is engine-agnostic: it uses whatever is protecting the PC.
3. **Windows Defender on-demand** (``MpCmdRun.exe``) when its engine is the active
   one, purely to attach a named verdict to a detection.

Used on the **sender** before a file offer goes out *and* on the **receiver**
before a downloaded file is kept — trusting the other side's scan alone is unsafe.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

# The official EICAR anti-malware test string. Any real engine flags it; we also
# detect it directly so the feature is demonstrable even with no engine present.
# Split so this very source file isn't itself flagged.
_EICAR = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
          b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")

# Executable/script extensions that, hidden behind a second "safe-looking"
# extension, are a textbook malware disguise. A *plain* single-extension file
# (e.g. legitimately sharing setup.exe) is NOT blocked by heuristics.
_EXEC_EXTS = {
    ".exe", ".scr", ".com", ".pif", ".bat", ".cmd", ".vbs", ".vbe", ".js",
    ".jse", ".ws", ".wsf", ".wsh", ".ps1", ".psm1", ".msi", ".msp", ".hta",
    ".cpl", ".jar", ".lnk", ".reg", ".scf", ".inf",
}
_LURE_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".jpg",
    ".jpeg", ".png", ".gif", ".mp4", ".mp3", ".zip", ".rar",
}

# Skip the tripwire copy for very large files (the resident AV still scans them
# on access when they're actually opened); scanning multi-GB copies is wasteful.
_TRIPWIRE_MAX = 512 * 1024 * 1024   # 512 MB

CREATE_NO_WINDOW = 0x08000000   # don't flash a console window for child processes

_OFF_HINTS = ("failed with hr", "product/feature disabled", "scan engine is disabled")


@dataclass
class ScanResult:
    ok: bool          # True = safe to send/keep
    threat: str = ""  # human-readable reason when not ok
    engine: str = ""  # which AV/heuristic produced the verdict
    scanned: bool = True


# ── active-AV detection (Windows Security Center) ─────────────────────────────
_av_names: list[str] | None = None
_av_lock = threading.Lock()
_av_started = False


def active_av_names() -> list[str]:
    """Display names of registered antivirus products (cached for the process).

    Queried from the Security Center; this is what's actually protecting the PC.
    The query (a PowerShell call) is run **off the GUI thread** the first time
    and returns ``[]`` until it completes, so callers (e.g. the settings page)
    never block. Prime it early with :func:`prime` so the name is usually ready.
    """
    global _av_started
    if _av_names is not None:
        return _av_names
    with _av_lock:
        if not _av_started:
            _av_started = True
            threading.Thread(target=_load_av_names_bg, daemon=True).start()
    return []


def _load_av_names_bg() -> None:
    global _av_names
    _av_names = _query_av_names()


def prime() -> None:
    """Kick off the background antivirus-name query (call once at startup)."""
    active_av_names()


def _query_av_names() -> list[str]:
    ps = ("Get-CimInstance -Namespace root/SecurityCenter2 "
          "-ClassName AntiVirusProduct | Select-Object -ExpandProperty displayName")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW)
        names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return names
    except (OSError, subprocess.TimeoutExpired):
        return []


def engine_label() -> str:
    """A short description of the scanning that will happen, for the settings UI.

    Non-blocking: uses the cached AV names if ready, else a generic phrase.
    """
    names = active_av_names()
    if names:
        return ", ".join(names) + " (real-time) + heuristics"
    return "your active antivirus (real-time) + heuristics"


# ── offline heuristics ────────────────────────────────────────────────────────
def _heuristic_scan(path: str) -> ScanResult | None:
    """Offline pre-checks. Returns a *blocking* ScanResult, or None if clean."""
    name = os.path.basename(path).lower()
    stem, ext = os.path.splitext(name)
    if ext in _EXEC_EXTS:
        _stem2, ext2 = os.path.splitext(stem)
        if ext2 in _LURE_EXTS:
            return ScanResult(False, f"disguised executable ({ext2}{ext})", "heuristics")
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if _EICAR in head:
            return ScanResult(False, "EICAR test signature", "heuristics")
    except OSError:
        pass
    return None


# ── active resident AV via on-access tripwire ─────────────────────────────────
def _readable(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _active_av_tripwire(path: str) -> ScanResult | None:
    """Copy *path* to temp so the resident real-time AV inspects the write.

    If the active AV removes or locks the fresh copy, the content is malware →
    we return a blocking result. If the copy survives, we conclude nothing (an
    AV allowing it and *no AV at all* look the same), so we return None and let
    the caller fall through. Conservative on purpose: a failed copy is treated
    as "couldn't test", never as a detection, so clean files are never blocked.
    """
    try:
        if os.path.getsize(path) > _TRIPWIRE_MAX:
            return None
    except OSError:
        return None
    ext = os.path.splitext(path)[1]
    tmp = os.path.join(tempfile.gettempdir(), f"nst_scan_{uuid.uuid4().hex[:12]}{ext}")
    try:
        shutil.copyfile(path, tmp)
    except OSError:
        # The write itself was refused — could be the AV, could be an IO error.
        # Don't risk a false positive; report inconclusive.
        return None
    try:
        # A clean copy stays readable. A flagged one is deleted/locked by the AV,
        # usually within a moment of the write completing — retry briefly so a
        # benign in-progress scan-lock isn't mistaken for a block.
        blocked = not _readable(tmp)
        for _ in range(5):
            if not blocked:
                break
            time.sleep(0.1)
            blocked = (not os.path.exists(tmp)) or (not _readable(tmp))
        if blocked:
            who = ", ".join(active_av_names()) or "resident antivirus"
            return ScanResult(False, "blocked by resident antivirus", who)
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── Windows Defender on-demand (named verdicts) ───────────────────────────────
def _find_mpcmdrun() -> str | None:
    candidates = []
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    platform_dir = os.path.join(progdata, "Microsoft", "Windows Defender", "Platform")
    try:
        if os.path.isdir(platform_dir):
            for v in sorted(os.listdir(platform_dir), reverse=True):
                exe = os.path.join(platform_dir, v, "MpCmdRun.exe")
                if os.path.isfile(exe):
                    candidates.append(exe)
                    break
    except OSError:
        pass
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", r"C:\Program Files")):
        exe = os.path.join(base, "Windows Defender", "MpCmdRun.exe")
        if os.path.isfile(exe):
            candidates.append(exe)
    return candidates[0] if candidates else None


_engine_active: bool | None = None


def engine_active() -> bool:
    """True if Windows Defender's on-demand engine can actually scan (cached)."""
    global _engine_active
    if _engine_active is None:
        _engine_active = _probe_engine()
    return _engine_active


def _probe_engine() -> bool:
    exe = _find_mpcmdrun()
    if not exe:
        return False
    probe = os.path.join(tempfile.gettempdir(), "nst_av_probe.txt")
    try:
        with open(probe, "w") as f:
            f.write("nst antivirus engine probe")
        proc = subprocess.run(
            [exe, "-Scan", "-ScanType", "3", "-File", probe, "-DisableRemediation"],
            capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        low = (proc.stdout or "").lower()
        if any(h in low for h in _OFF_HINTS):
            return False
        return proc.returncode in (0, 2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _defender_scan(path: str) -> ScanResult | None:
    """Run Defender on one file for a NAMED verdict. None if it couldn't run."""
    exe = _find_mpcmdrun()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
            capture_output=True, text=True, timeout=180, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    low = (proc.stdout or "").lower()
    if any(h in low for h in _OFF_HINTS):
        return None   # engine off — generic failure, NOT a detection
    if proc.returncode == 2:
        return ScanResult(False, _threat_name(proc.stdout) or "malware detected",
                          "Windows Defender")
    if proc.returncode == 0:
        return ScanResult(True, "", "Windows Defender")
    return None


def _threat_name(output: str) -> str:
    for line in (output or "").splitlines():
        s = line.strip()
        if s.lower().startswith("threat") and ":" in s:
            return s.split(":", 1)[1].strip()
    return ""


# ── public entry point ────────────────────────────────────────────────────────
def scan(path: str) -> ScanResult:
    """Scan *path* with heuristics + the machine's active antivirus.

    ``ok=False`` means the file should not be sent/kept. ``scanned=False`` means
    no engine could be consulted (only heuristics ran); callers in "block" mode
    still let such files through, since a clean heuristic pass with no engine
    isn't grounds to block.
    """
    if not path or not os.path.isfile(path):
        return ScanResult(True, "", "heuristics", scanned=False)

    h = _heuristic_scan(path)
    if h is not None:
        return h

    # Whatever AV is actually running on this PC, via real-time on-access scan.
    t = _active_av_tripwire(path)
    if t is not None:
        return t

    # If Defender's on-demand engine is the active one, get a named clean/threat.
    if engine_active():
        d = _defender_scan(path)
        if d is not None:
            return d

    label = ", ".join(active_av_names()) or "heuristics"
    return ScanResult(True, "", label, scanned=bool(active_av_names()))

"""Notification sound + a small notification-policy gate.

Sound is synthesised in-memory (a soft two-note chime scaled by the volume
slider) and played asynchronously via :mod:`winsound`, so there are no audio
dependencies and the volume control has a real effect.

:func:`should_notify` is the single place that decides whether a given
notification *channel* (sound / popup / taskbar flash / tray badge) is allowed
for a conversation *scope* (private / group / broadcast). It folds together the
global master switch, Do-Not-Disturb, mute-all and the per-scope toggles from
:mod:`nst.config`, so callers never have to re-derive that logic.
"""

import io
import math
import struct
import threading
import wave

try:
    import winsound
except Exception:                       # non-Windows dev box / import safety
    winsound = None

from .. import config

_SAMPLE_RATE = 44100
_chime_cache: dict[int, bytes] = {}
_lock = threading.Lock()


def _build_chime(volume: int) -> bytes:
    """A short, soft rising two-note chime as a 16-bit mono WAV, scaled by volume."""
    amp = max(0.0, min(1.0, volume / 100.0)) * 0.55   # headroom, never clip
    tones = [(880.0, 0.09), (1174.7, 0.15)]           # A5 → D6
    frames = bytearray()
    for freq, dur in tones:
        n = int(_SAMPLE_RATE * dur)
        for i in range(n):
            # short attack / longer decay envelope to avoid clicks
            env = min(1.0, i / 240.0, (n - i) / 1600.0)
            val = int(32767 * amp * env * math.sin(2 * math.pi * freq * i / _SAMPLE_RATE))
            frames += struct.pack("<h", val)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def play_sound(volume: int | None = None) -> None:
    """Play the notification chime asynchronously (no-op below audible volume)."""
    if winsound is None:
        return
    vol = config.load_sound_volume() if volume is None else volume
    if vol <= 0:
        return
    bucket = max(0, min(100, (vol + 9) // 10 * 10))   # cache at 10% steps
    with _lock:
        data = _chime_cache.get(bucket)
        if data is None:
            data = _build_chime(bucket)
            _chime_cache[bucket] = data
    try:
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        pass


def should_notify(scope: str, channel: str) -> bool:
    """True if *channel* may fire for a *scope* conversation right now.

    scope:   'private' | 'group' | 'broadcast'
    channel: 'sound' | 'popup' | 'taskbar' | 'tray'
    """
    prefs = config.load_notify_prefs()
    scope_prefs = prefs.get(scope, {})
    if not scope_prefs.get(channel, True):
        return False
    # Master switch: "Enable/Disable all notifications".
    if not config.load_notifications_enabled():
        return False
    if channel == "sound":
        if config.load_mute_all() or config.load_do_not_disturb():
            return False
        if config.load_sound_volume() <= 0:
            return False
    # Do-Not-Disturb silences active interruptions but leaves the passive
    # tray badge alone so unread counts still update.
    if channel in ("popup", "taskbar") and config.load_do_not_disturb():
        return False
    return True


def scope_of(key: str) -> str:
    """Map a conversation key to its notification scope."""
    if not key:
        return "private"
    if key.startswith("channel:"):
        return "broadcast"
    if key.startswith("group:"):
        return "group"
    return "private"

"""Static configuration: ports, magic payloads, buffer sizes and fonts.

Colors live in :mod:`nst.theme` because they change at runtime with the
light/dark toggle; everything here is fixed for the lifetime of the process.
"""

# ── Proxy / host-discovery ────────────────────────────────────────────────────
PROXY_PORT    = 8080
BEACON_PORT   = 54321            # UDP broadcast port for host discovery
BEACON_MAGIC  = b"NST_HOST_V3"   # payload the host sends
BUFFER_SIZE   = 65536
CONN_TIMEOUT  = 30

# ── LAN chat ──────────────────────────────────────────────────────────────────
CHAT_PRESENCE_PORT = 54322       # UDP broadcast port for chat peer presence
CHAT_TCP_PORT      = 54323       # TCP port each peer listens on for messages
FILE_TCP_PORT      = 54324       # TCP port for file transfer data streams
# V2 presence: payload is ``CHAT_MAGIC|<json>`` carrying uid/device/ips/status.
CHAT_MAGIC         = b"NST_CHAT_V2"
CHAT_PRESENCE_EVERY = 3          # seconds between presence broadcasts
CHAT_PEER_TIMEOUT   = 10         # seconds of silence before a peer is dropped
CHAT_AWAY_AFTER     = 300        # seconds of input idle before we report "away"
FILE_SAVE_DIR      = "NetSplitter"   # subfolder under Documents

# ── Remote screen (view + control) ────────────────────────────────────────────
SCREEN_TCP_PORT = 54325          # TCP port the host listens on for screen sessions

# Length-prefixed frame protocol on the session socket: a 1-byte type followed
# by a 4-byte big-endian payload length (see nst.remotescreen). Control frames
# carry JSON; SF_FRAME carries raw JPEG/PNG image bytes.
SF_HELLO  = 0x01   # viewer -> host: {name, ip, secret}
SF_ACCEPT = 0x02   # host -> viewer: {name, w, h}
SF_REJECT = 0x03   # host -> viewer: {reason}
SF_FRAME  = 0x10   # host -> viewer: image bytes
SF_INPUT  = 0x20   # viewer -> host: {k: move|button|wheel|key|text, ...}
SF_CLIP   = 0x30   # either way:     {text}
SF_PING   = 0x40   # either way:     keepalive (empty)
SF_BYE    = 0x50   # either way:     graceful close

SCREEN_FPS      = 8      # default host capture cadence (frames/sec); lower suits
                         # the default lossless-PNG frames (heavier than JPEG)
SCREEN_QUALITY  = 80     # default JPEG quality (1-100); higher keeps text crisp
SCREEN_MAX_EDGE = 1920   # default cap on the longest captured edge (px); 0 = native.
                         # 1920 leaves a 1080p host un-scaled, so text stays sharp.
SCREEN_REQUEST_TIMEOUT = 60   # seconds the host waits for the user to accept

# ── Fonts ─────────────────────────────────────────────────────────────────────
BTN_FONT   = ("Consolas", 9, "bold")
LABEL_FONT = ("Segoe UI", 9)
MONO_FONT  = ("Consolas", 9)
TITLE_FONT = ("Segoe UI", 10, "bold")

# ── Windows registry paths ────────────────────────────────────────────────────
REG_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
REG_RUN_PATH          = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_APP_PATH          = r"Software\NetSplitTunnel"
RUN_VALUE_NAME        = "NetSplitTunnel"

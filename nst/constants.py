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
MOBILE_HTTP_PORT   = 8765        # HTTP+WebSocket port for mobile PWA bridge
# V2 presence: payload is ``CHAT_MAGIC|<json>`` carrying uid/device/ips/status.
CHAT_MAGIC         = b"NST_CHAT_V2"
CHAT_PRESENCE_EVERY = 3          # seconds between presence broadcasts
CHAT_PEER_TIMEOUT   = 10         # seconds of silence before a peer is dropped
CHAT_AWAY_AFTER     = 300        # seconds of input idle before we report "away"
FILE_SAVE_DIR      = "NetSplitter"   # subfolder under Documents

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

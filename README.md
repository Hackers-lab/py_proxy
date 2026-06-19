# Net Split-Tunneler  v4.8

Share one PC's internet (VPN, hotspot, or restricted Wi-Fi) with other PCs on the same
LAN — **without** losing access to local shares, printers, and intranet sites. Includes a
built-in, serverless **LAN Chat** with file transfer.

Windows 10/11 · Python 3.10+ · PyQt6

---

## How it works

- **Host** — a PC with internet runs a proxy other PCs connect to.
- **Client** — a PC with no internet uses the Host's proxy.
- **Split routing** — local traffic (`10.x`, `172.16–31.x`, `192.168.x`) stays on the LAN
  while internet traffic goes through the Host. So you keep both at once.

> [!NOTE]
> Run as **Administrator** — the app edits the Windows routing table for split-tunneling.
> Accept the UAC prompt on launch.

---

## Quick start

**Host (shares internet)** — open the app → **Host Mode** →
1. **▶ Enable LAN+NET** (sets up split routing)
2. **▶ Start Proxy Server**
3. Note the **Intranet IP** (e.g. `10.x.x.x`) — give it to the client.

**Client (uses internet)** — open the app → **Client Mode** →
1. The Host is found automatically and its IP is filled into **Host IP**
   (or type the `10.x.x.x` manually).
2. Optionally tick **Disable proxy if host has no internet / unreachable**.
3. **⬡ Connect to Host Proxy** — your browser and apps now have internet.

---

## LAN Chat

Click **💬 LAN Chat**. Every PC running the app discovers the others on the same subnet
(UDP presence broadcast — only this app's instances appear, no ping scanning).

- Set your display name (defaults to the PC name) and pick a peer to chat.
- **Groups** with full admin controls — any user can create one and becomes its admin;
  admins add/remove members, promote/demote admins and rename the group. Ownership
  transfers automatically if the last admin leaves, and a removed member loses the group.
- **Broadcast channels** (＋ New → channel) — admins post, members read only.
- Replies, reactions, forwarding, typing indicators, read receipts, and group seen-counts.
- **Emoji & multi-line** input: **Enter** sends, **Shift+Enter** adds a new line.
- **Offline delivery** — messages to an offline peer are held locally and delivered
  automatically when they come back online (lost only if you quit first).
- **Search** (🔍 in the chat header) across all message text and file names.
- **Notifications** — bottom-right popups, a notification sound, taskbar flashing and a
  system-tray unread badge, each configurable per chat type in Settings.
- Chat history is saved and restored across restarts.

**Settings** (⚙ next to *YOU*, or the gear) — a categorised window for General
(display name, invisible mode, start-with-Windows, tray/session behaviour), Notifications
(global + per-type sound/popup/flash/badge, volume, DND, mute), Storage & retention
(7/30/90/180 days or forever, clear history, download folder, max file size, usage),
Network (interfaces, IPs, ports, peers online), Privacy (blocked-user management) and
File Transfer (download folder, size limit, offer expiry).

**Connect by IP (cross-subnet)** — discovery only spans one subnet. To reach a different
`10.x.x.x` subnet, enter the peer's IP in **Connect by IP** and press ➤. The app probes
port `54323`; the peer turns reachable the moment their app is up.

**File transfer** — attach a file (📎) to send it. The recipient accepts/rejects; both
sides show live progress (size · speed · elapsed · ETA). Images preview as a thumbnail.
Completed and cancelled transfers stay in the chat history. Cancelling deletes the
partial file on the receiving side.

**Delete a peer** — the ✕ on a peer removes it and its history. It stays gone until that
peer contacts you again (or you re-add it by IP).

> [!TIP]
> No second PC? Click **✨ Try Demo Chat** (or **Chat → Run Chat Demo**) to chat with a
> local Demo Bot.

---

## Other features

- **Network traffic monitor** — live download (green) / upload (amber) speeds.
- **Show Speed in Taskbar** (**Settings**) — a speed pill pinned next to the clock that
  stays active even when the window is hidden.
- **Start with Windows** (**Settings**) — launch automatically at sign-in.
- **Light / Dark theme** — toggle with **☀ / 🌙** (top-right) or **Settings → Light theme**.
  Remembered between launches.
- **Tray behaviour** — the **X** hides to the tray (keeps the connection alive).
  Restore from the tray; **File → Exit** (or tray → **Quit**) shuts down fully.

---

## Build

```bat
pip install psutil PyQt6 pyinstaller
pyinstaller NetSplitTunnel.spec
```

Output: `dist\NetSplitTunnel_v4.8.exe` (single file, prompts for admin via an embedded
manifest). To regenerate the app icon: `pip install Pillow` then `python make_icon.py`.

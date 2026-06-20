# Net Split-Tunneler  v4.10

A Windows desktop tool for the messy reality of office networks: **share internet without losing the LAN**, **reach the intranet and the internet at the same time**, **flip between saved IP setups in one click**, and run a **full-featured LAN chat** between every PC on the network — all without any server, cloud account, or router config.

Windows 10/11 · Python 3.10+ · PyQt6

> [!NOTE]
> Most features just work as a normal user. The actions that change Windows
> networking (routes, secondary IP, switching adapter settings) ask for a
> one-time **UAC / administrator** approval when you click them.

---

## The main window — four tabs

Everything in the main window is organised into four tabs, plus the LAN Chat that opens in its own window.

| Tab | What it's for |
|---|---|
| 🖥️ **Host Mode** | This PC shares its internet to other PCs and keeps LAN access local |
| 🔗 **Client Mode** | This PC borrows another PC's internet over the LAN |
| 🌐 **Dual Access** | Use the corporate intranet **and** the internet at the same time, on one cable |
| 🔀 **IP Switch** | Save up to 4 network setups and switch between them with one click |

## Features at a glance

| | Feature |
|---|---|
| 🔀 | **Split-Tunneler** — internet via host, LAN stays local |
| 🌐 | **Proxy Sharing** — one PC shares internet to all others |
| 🌐 | **Dual Access** — intranet + internet together, with split DNS |
| 🔀 | **IP Switch** — one-click static/DHCP network profiles |
| 💬 | **LAN Chat** — private messages, groups, broadcast channels |
| 📎 | **File Transfer** — send files with live progress |
| 🖥️ | **Remote Screen** — view and control a peer's PC: mouse, keyboard, clipboard |
| 🔔 | **Notifications** — sound, toast, window-raise or taskbar flash |
| 🔍 | **Search** — full-text search across all messages and files |
| 📶 | **Speed Monitor** — live network speeds, optionally pinned to the taskbar |

---

## Split-Tunneler

The core problem this solves: you're on a VPN or restricted hotspot on one PC, and you want other PCs on the same desk (or same room) to share that internet — but you still need them to reach printers, NAS drives, and intranet sites on the local network.

Standard internet-sharing breaks LAN access. Net Split-Tunneler doesn't.

**How it works:**

- The **Host PC** runs a proxy and also sets up a split-tunnel route on itself — internet traffic goes out through its VPN/hotspot, local subnet traffic (`10.x`, `172.16–31.x`, `192.168.x`) stays on the LAN.
- **Client PCs** point their system proxy at the Host. Their browsers and apps get internet, and local network access is unaffected because their own routes are untouched.
- Discovery is automatic — clients find the Host by UDP broadcast on the same subnet. No IP typing required.

**Host setup:**
1. Open the app → **Host Mode**
2. Click **▶ Enable LAN+NET** — installs the split route
3. Click **▶ Start Proxy Server** — starts the HTTP/HTTPS proxy
4. Note your **Intranet IP** (shown in the app) and share it with clients if needed

**Client setup:**
1. Open the app → **Client Mode**
2. The Host is found automatically; its IP appears in **Host IP**
   (or type it manually if cross-subnet)
3. Optionally enable **Disable proxy if host unreachable** — your system falls back to direct when the Host goes offline
4. Click **⬡ Connect to Host Proxy** — done

---

## Proxy Sharing

The proxy server is a standard HTTP/HTTPS tunnel (CONNECT method), compatible with every browser and most apps without extra configuration. It listens on your LAN IP so only peers on the same network can connect — no open internet exposure.

- Automatically discovered by Client PCs on the same subnet
- Works with VPN, mobile hotspot, or any internet source on the Host
- The **Network traffic monitor** shows live download/upload speeds for the proxy connection
- **Show Speed in Taskbar** pins a live speed pill next to the clock, visible even when the main window is hidden

---

## Dual Access — intranet *and* internet at once

The everyday office problem: your desk cable gives you the **corporate intranet** (a `10.x` address, internal sites, SAP/ERP, file servers) but **no internet** — or a Wi-Fi/hotspot gives you internet but cuts you off from the intranet. Normally you switch back and forth all day.

**Dual Access gives you both at the same time, over the same network adapter.**

In one click it:
- Adds a second **internet IP** to your adapter (alongside your intranet IP)
- Sends internet traffic out the internet gateway, and keeps intranet traffic (`10.0.0.0/8`) on the corporate network — adding the intranet route automatically if it isn't there
- Sets up **split DNS** so internal hostnames (e.g. `*.yourcompany.in`) resolve through the corporate DNS servers, while everything else uses public DNS

**How to use it:**
1. Open **Dual Access**
2. The **Internet IP** is auto-filled from your adapter's saved DHCP address — or click **Auto** to detect it, or type it in
3. (Optional) Adjust the **NRPT Domains** — the internal domains that should resolve via corporate DNS. Your intranet DNS servers are read automatically from the adapter.
4. Click **▶ Enable Dual Access** and approve the UAC prompt

The four status rows (Intranet Route, Secondary IP, Internet Route, Split DNS) turn green as each piece comes up. **Disable** reverses everything and restores your adapter to its original state.

---

## IP Switch — saved network profiles, one click

If you regularly move a laptop between networks — office static IP, home DHCP, a lab subnet, a client site — IP Switch saves each setup and lets you apply it instantly instead of digging through Windows adapter settings.

- **Four profile buttons.** Each shows your chosen name and a status icon: **green ■** when that profile is currently active, **▶** when it's ready to apply. Click a button to switch.
- **Configure once.** Click **⚙ Configure Profiles** to open a tabbed editor (Profile 1–4). For each profile set:
  - **Name** — what the button shows (e.g. "Office Intranet", "Home Wi-Fi")
  - **Network Adapter** — picked from the adapters on this PC
  - **Address Mode** — a **Static / Auto (DHCP)** toggle. Choose **Auto** and Windows obtains everything automatically (the IP fields disappear); choose **Static** and fill in IP, subnet mask, gateway and DNS.
- Applying a profile asks for a one-time UAC approval, then reconfigures the adapter in seconds.

---

## LAN Chat

Click **💬 LAN Chat** to open the chat window. Every PC running the app on the same subnet is discovered automatically via UDP presence broadcast — no accounts, no server, no config.

### Conversations

**Private chat** — click any peer in the list to open a 1-on-1 conversation. Messages are stored locally and restored on restart.

**Groups** (＋ New → Group):
- Any user can create a group and becomes its first admin
- Admins can add or remove members, promote or demote other admins, and rename the group
- Removing the last admin automatically promotes the next member — a group can never be admin-less
- Removed members lose the group from their list immediately
- The creator can delete the group entirely; ownership transfers to another member first

**Broadcast channels** (＋ New → Channel):
- Only channel admins can post; members read only
- Useful for announcements or one-way updates to the whole team

### Messaging

- **Enter** sends · **Shift+Enter** inserts a new line
- **Reply** to any message (↩ Reply) — the quoted snippet appears inside the bubble
- **Reactions** — right-click any message to react with an emoji (👍 ❤️ 😂 😮 😢 🙏)
- **Forward** a message to another peer or group
- **Delete for everyone** — within 3 minutes of sending; the bubble becomes a tombstone on both sides
- **Delete for me** — removes a message from your local view only
- **Typing indicators** — shows who is typing in real time
- **Read receipts** — grey single tick (sent), grey double tick (delivered), **green** double tick (read); group messages show a "X/Y Seen" count

### File Transfer

- Click **📎** to attach a file to any 1-on-1 chat
- The recipient sees an accept/reject prompt; both sides show a live progress bar with percentage, speed, and ETA
- Images show as a clickable thumbnail
- Cancelling a transfer removes the partial file on the receiver's side
- Completed and cancelled transfers stay in the chat history

### Offline Delivery

If a peer is offline when you send, the message is held in a local queue and delivered automatically when they come back online. Queued messages show a **🕓** clock tick. Messages are best-effort — the queue is cleared if you quit before they reconnect.

### Search

Click **🔍** in the chat header to search across all your message text and file names in one place.

### Blocking

Right-click any peer in the roster to **Block** them. Blocked peers cannot send you messages or file offers — the drop happens silently at the network level, on LAN and cross-subnet alike. Unblock anytime from **Settings → Privacy & Users**.

### Notifications

Each chat type (Private, Group, Broadcast) has its own set of toggles:

| Toggle | What it does |
|---|---|
| **Sound** | Plays a soft two-note chime |
| **Show window** | Raises the chat window when it's hidden or minimised (badge in roster tells you who sent; no chat-switch) |
| **Taskbar flash** | Flashes the taskbar button |

When **Show window** is off (or the window is already visible), a **bottom-right toast** appears for background messages instead. Clicking the toast jumps to that conversation.

Global controls: **master on/off**, **Do Not Disturb**, **Mute all sounds**, and a **volume slider**.

### Connect by IP (cross-subnet)

Automatic discovery only spans one subnet. To reach a peer on a different network segment, enter their IP in **Connect by IP** and press ➤. The app probes port `54323`; the peer becomes available the moment their app is running.

---

## Remote Screen — view & control a PC

A lightweight, built-in remote desktop — no AnyDesk, VNC or TeamViewer needed, and no extra download (it reuses the same Qt the app already ships, so it adds almost nothing to the size).

**Start a session:** open a 1:1 chat and click the **🖥** button in the conversation header. A window opens showing the peer's screen. By default the peer is **asked to approve** the connection; once they click **Allow**, you can:

- **Move and click** the mouse (left / right / middle, scroll wheel)
- **Type** with your keyboard, including shortcuts like Ctrl+C / Ctrl+V
- **Sync the clipboard** — push your clipboard text to the remote PC with one button
- **Send Ctrl+Alt+End** (the remote equivalent of Ctrl+Alt+Del)
- Toggle **Control: on/off** to switch between controlling and view-only

You can also connect to a manual IP. The host listens on TCP `54325`.

**While you're being viewed**, a red "*X is viewing your screen*" banner stays on top of your screen with a **Stop** button so a session can never run silently.

**Unattended access (optional):** in **Settings → Remote Screen** you can set a **secret** and enable *connect without asking*. A peer who presents that secret is let in automatically, with no approval prompt — handy for your own machines.

> [!WARNING]
> Unattended access is effectively a backdoor: anyone with the secret can control the PC without approval. It is **off by default**. Only enable it with a long, unique secret shared only with devices you trust.

**Readable text:** by default the host sends **lossless PNG** frames at its **native resolution**, so small text and icon labels stay crisp — no JPEG smearing or downscaling. This is the *Sharp text mode* toggle in **Settings → Remote Screen**; on a slow link you can turn it off to fall back to (lighter) JPEG and pick a lower **Resolution**.

Performance is tunable in Settings (sharp-text mode, resolution, image quality, frame rate). On a LAN it's smooth enough for clicking, typing and copy-paste; over the internet, lower the resolution or turn off sharp-text mode and expect more lag.

---

## Settings

Open Settings from the **⚙** gear next to *YOU* in the chat sidebar. Changes take effect on **Save**; **Cancel** discards everything.

| Page | What you configure |
|---|---|
| **General** | Display name, invisible mode (appear offline), start with Windows, minimise to tray, restore last session on open |
| **Notifications** | Master switch, DND, mute all, volume slider; per-type (Private / Group / Broadcast) toggles for Sound, Show window, Taskbar flash |
| **Storage** | Message retention (7 / 30 / 90 / 180 days or forever), download folder, max file transfer size, usage stats, clear all history |
| **Network** | Active interfaces, IPs, ports in use, peers currently online, queued offline messages |
| **Privacy & Users** | Blocked peer list with instant Unblock |
| **File Transfer** | Download folder, max file size, offer expiry time |
| **Remote Screen** | Allow incoming sessions, unattended secret, sharp-text (lossless) mode, resolution, image quality, frame rate, approval timeout |
| **About** | Version, diagnostics |

---

## Other features

- **Light / Dark theme** — toggle with ☀ / 🌙 (top-right) or in Settings. Remembered across restarts.
- **Tray behaviour** — closing the window hides to the system tray (keeps everything running). Use **File → Exit** or tray → **Quit** to fully shut down.
- **Demo Bot** — no second PC? Click **✨ Try Demo Chat** to chat with a built-in Demo Bot and see all chat features working.
- **Status** — set yourself as Online, Away, or Invisible from the ⚙ gear menu.
- **Automatic updates** — the app silently self-updates from GitHub Releases, and only while the chat window is closed so a conversation is never interrupted. The main window's **event log** reports each step: when a newer version is **found**, the installer's **size**, **download progress**, and completion. Toggle auto-updates or run a manual check from the **About** menu.

---

## Build from source

```bat
pip install psutil PyQt6 pyinstaller
pyinstaller NetSplitTunnel.spec --noconfirm
```

Output: `dist\NetSplitTunnel_v4.9.exe` — a single-file executable that prompts for admin via an embedded UAC manifest.

To regenerate the app icon: `pip install Pillow` then `python make_icon.py`.

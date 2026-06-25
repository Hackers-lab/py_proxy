"""Per-version what's-new bullets shown in the in-app Updates chat on launch."""

NOTES: dict[str, list[str]] = {
    "4.13.1": [
        "🔓 Fixed: unlocking a locked chat no longer crashes the app — clicking the unlock banner caused a TypeError that closed the window immediately.",
        "🔄 Fixed: silent self-updates that download but never install. The installer is now fully detached from the parent process so it survives the app closing, and retries automatically if it fails.",
        "🌐 Fixed: dual-homed peers (e.g. Dual Access with 10.x + 192.x) no longer create phantom conversations — messages are routed to the correct thread via the sender's UID.",
    ],
    "4.13.0": [
        "✏ Edit sent messages for up to 2 minutes — right-click your message → Edit. Recipients see the update with an 'edited' tag.",
        "📷 Paste images straight into the composer (Ctrl+V) — like WhatsApp. Copied screenshots/web images appear inline in the chat; add a caption and Send.",
        "🔒 Chat lock — protect your history with a password. Locked conversations are encrypted on disk, so they can't be read without it. Lock the whole chat or just selected conversations (Settings → Privacy & Users).",
        "🔑 Forgot the password? Reset via your security questions — this deletes the locked chats (they're unrecoverable by design).",
        "🛡 Antivirus scan for shared files — every file is checked before sending and after receiving, using whatever antivirus is active on the PC (Windows Defender or third-party) plus built-in heuristics. Flagged files are blocked (configurable in Settings → File Transfer).",
        "🛡 File bubbles now show a “Scanned by <antivirus>” mark once a file passes the check, so you can see at a glance that it was scanned.",
    ],
    "4.12.4": [
        "🔒 Fixed: a kicked member can no longer message the group — remaining members now reject posts from anyone who isn't on the member list.",
        "🛡 Fixed: a removed user can't sneak back in. Membership and admin changes are now accepted only from a current admin, so a stray message can't rewrite everyone's roster or re-add the person who left.",
        "📢 Channels are strictly admin-post: a post from a non-admin is dropped on arrival.",
        "📨 Kicks are now queued and retried, so an offline user still loses the group when they come back online.",
    ],
    "4.12.3": [
        "🧹 Fixed: a person no longer appears twice with two IPs (e.g. 10.x and 192.168.x). Multi-homed machines (Dual Access, VPN, Wi-Fi + Ethernet) now collapse to a single contact, always keyed by the LAN IP.",
        "🛡 New: inbound rate limit — at most 5 messages per second from any one sender. Extra messages are dropped so no one can flood your chat.",
    ],
    "4.12.1": [
        "💬 New: 'What's New' chat — release notes appear as messages in a virtual peer after every update. Click any update toast to open it.",
        "🔔 Bell pause timer — pause window pop-up for 15 min / 1 hr / 2 hr / 6 hr / 24 hr. Toast and sound keep working while paused. Click 🔕 to resume instantly.",
        "🌙 Dark/light mode toggle (☀️ / 🌙) added to the chat header.",
        "📏 Active peer selection bar is now a clean straight vertical line.",
        "🔊 Sound diagnostic: event log now shows why sound was skipped (muted, DND, volume 0, per-scope setting).",
    ],
    "4.12.0": [
        "🚀 Chat is now the primary window — app opens straight into LAN Chat.",
        "📋 New header menu (⚙) — reach Network Tools, Settings, Updates, About and Quit without leaving chat.",
        "🔔 Bell icon shows window pop-up pause status — click to choose 15 min / 1 hr / 2 hr / 6 hr / 24 hr pause. Toast and sound keep working while paused.",
        "🌙 Dark/light mode toggle added to the chat header (☀️ / 🌙).",
        "👤 Status chip shows your presence dot — click to switch Online / Away / Invisible.",
        "🔕 Notifications: mute all or Do Not Disturb are still in Settings → Notifications for full silence.",
        "🔍 Connect-by-IP placeholder simplified to 10.x.x.x.",
        "🛠 Suppressed benign Qt console warnings (EDID monitor interface, font point-size).",
    ],
    "4.11.1": [
        "📋 The event log now shows update progress live: version found, installer size, download %, and completion — a silent self-update is no longer invisible.",
    ],
    "4.11.0": [
        "🛑 Fixed: the Stop button on the 'X is viewing your screen' banner now actually ends the session.",
        "🖥 Clearer remote screen: frames no longer shrunk to 1600 px, default quality higher, viewer scales smoothly.",
        "✨ New: Sharp text mode (lossless PNG) — pixel-perfect frames, ideal for reading text. On by default.",
        "📐 New: Resolution control in Settings → Remote Screen (Match host down to 1080 p).",
        "🎞 Default frame rate lowered to 8 fps — better fit for lossless frames.",
        "⚠️ Host now warns if the screen-share listener can't start (e.g. port in use).",
    ],
    "4.10.0": [
        "🖥 New: Remote Screen — built-in lightweight remote desktop over LAN.",
        "🖱 Full mouse and keyboard control: move, click (left/right/middle), scroll, Ctrl+C/V and shortcuts.",
        "📋 One-click clipboard sync between viewer and host.",
        "🔒 The person being viewed approves each connection and sees an always-on-top Stop banner.",
        "🔑 Optional unattended access: set a secret in Settings → Remote Screen (off by default).",
    ],
}


def get(version: str) -> list[str]:
    """Return what's-new bullets for *version*, or empty list if unknown."""
    return NOTES.get(version, [])

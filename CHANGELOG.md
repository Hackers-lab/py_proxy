# Changelog

Per-version record of what changed. Add a new section at the top for every
release (see [RELEASING.md](RELEASING.md)). Newest first.

<!-- Template for a new release — copy this above the latest entry:
## vX.Y.Z — YYYY-MM-DD
- What changed, in plain language (one bullet per user-visible change).
-->

## v4.9.13 — 2026-06-20
- Chat read receipts now show **green** double-ticks (read) vs grey double-ticks
  (delivered), so the two are easy to tell apart.
- Reworked the message UI: cleaner quoted-reply cards, an always-visible reply
  arrow beside each message, consecutive same-sender messages grouped, sender
  avatars in group chats, system notices shown as pills, and an animated typing
  indicator.
- Composer polish: emoji button moved inside the input, file + send buttons now
  match the composer height, a simple painted paperclip attach icon, and a
  circular Send button that lights up only when there's text.
- Added a confirmation prompt before clearing a conversation.
- Demo Bot now simulates delivered/read receipts so the ticks can be tried out.
- Dual Access: restores the adapter's original DNS on disable (instead of forcing
  DHCP), and only removes the internet default route it added (no longer wipes
  the real gateway); the internet route now wins via an explicit metric.
- Update check survives corporate SSL-inspection (retries on certificate errors).

## v4.9.12 — 2026-06-20
- New **IP Switch** tab: save up to 4 network profiles (static/auto) and apply
  one with a click, with a modern segmented configure dialog.
- Fixed profile/route/dual switching failing with "exit 2" when run from source.
- **Dual Access** tab: auto-detects the internet IP from the DHCP cache, reads
  intranet DNS automatically, with background status checks (no UI freeze).
- Traffic monitor uses bps as the lowest unit in Auto mode; larger main window.

## v4.9.11 — 2026-06-20
- New **Dual Access** tab: use the corporate intranet and the internet at the
  same time over one adapter (secondary IP + split routes + split DNS).

## v4.9.10 — 2026-06-20
- Fixed an endless self-update loop: a stale `Setup_v4.9.6.exe` had been
  committed and was being re-published with every release, so the updater kept
  installing the old version. Untracked it and ignored `Output/`.
- Updater now picks the highest-version installer asset and compares against
  that asset's version, so a stray asset can never cause a loop again.
- CI wipes `Output/` before building and publishes only the exact version file.

## v4.9.9 — 2026-06-20
- Fixed update-toast spam (single notification, no countdown loop).

## v4.9.8 — 2026-06-20
- Dynamic intranet routing (route network derived from the detected IP).
- Show the app version in the LAN chat.
- Update countdown before applying.

## v4.9.7 — 2026-06-20
- Switched the installed build to **one-folder** (onedir): fixes the
  "Failed to load python3xx.dll from _MEI…" error during self-update and speeds
  up restart.
- Update toasts: "Updating to vX — restart in 5s" before applying; "Updated to
  version X" after relaunch.
- Auto-launch (logon / post-update) starts to the tray only; manual / Start Menu
  launch shows the window.
- Fixed a stale startup log line that claimed "Administrator".

## v4.9.6 — 2026-06-20
- Made the intranet route configurable; embedded Windows version info in the exe.

## v4.9.5 — 2026-06-20
- Relaunch the app automatically after a silent self-update.

## v4.9.3 — 2026-06-20
- App now runs as a normal user — **no UAC prompt on launch**. Admin is requested
  on demand only for the split-tunnel route.
- Autostart switched to the silent HKCU Run key (reliable on every PC).
- Added the per-user Inno Setup installer (no admin to install or update).
- Added silent self-update from GitHub Releases (applies only while the chat
  window is closed).
- Trimmed the build size (dropped the ~20MB software-OpenGL fallback and unused
  Qt modules).
- CI fix: untracked `build/`/`dist/` and added `--clean` so releases build
  reproducibly.

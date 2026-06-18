# LAN Chat vNext - Detailed Enhancement Requirements

## Existing System

The application is currently a Windows LAN chat application where desktop clients communicate directly over LAN using IP addresses. The application currently assumes 10.x.x.x network ranges and needs to be generalized.


---

# 1. Network Architecture Improvements

Remove all assumptions that the network starts with 10.x.x.x.

The application must automatically detect available network interfaces and work on:

* 10.x.x.x
* 172.16.x.x – 172.31.x.x
* 192.168.x.x
* Any custom corporate subnet

User discovery must be subnet-aware and interface-aware rather than hardcoded to specific IP prefixes.

Users should also be able to manually communicate with devices on other reachable subnets by entering an IP address.

---

# 2. Mobile Access Architecture

A native mobile application is NOT required.

Instead, every desktop client should be capable of hosting a lightweight web/PWA interface.

Example:

Desktop:

* LAN IP: 10.18.5.6
* Wi-Fi IP: 192.168.1.20

Mobile:

* Connected to Wi-Fi
* IP: 192.168.1.50

The desktop application should host a local web server.

The desktop generates a QR code containing:

[http://192.168.1.20:PORT](http://192.168.1.20:PORT)

When scanned, the mobile browser opens the chat interface.

The desktop acts as a bridge between:

* Mobile clients on Wi-Fi
* Desktop users on LAN

This allows mobile users to communicate with LAN users even if they are not directly connected to the LAN subnet.

The desktop hosting the mobile session acts as a gateway only for those mobile users.

If the hosting desktop is shut down:

* All connected mobile sessions are terminated.
* Mobile users disappear from the network.
* Reconnection and re-approval are required.

Multiple mobile users may connect to the same desktop simultaneously.

No artificial mobile-user limit should exist.

---

# 3. Mobile Approval Workflow

Desktop users join automatically.

Mobile users require approval every session.

No trusted devices.

No persistent login.

Every connection request must display:

* Display Name
* Device Name
* IP Address

Actions:

* Approve
* Reject
* Permanently Block

After approval the mobile user joins the network.

If the browser session ends, approval is required again.

---

# 4. User Identity

Each user must have:

* Display Name
* Computer/Device Name
* IP Address
* Internal Unique ID

Example:

Pramod | KUSHIDA-PC-01 | 10.18.5.6

Identity is IP-based.

If the IP changes, the system treats the device as a new user.

---

# 5. Presence System

Support:

* Online
* Away/Idle (automatic)
* Offline
* Invisible

Invisible users should not appear in online lists but should continue receiving messages.

User list should show:

* Online users
* Offline users
* Last Seen timestamps

---

# 6. Private Messaging

Private chats are automatically created when the first message is sent.

Support:

* Sent status
* Delivered status
* Read status

Indicators:

✓ Sent

✓✓ Delivered

✓✓ Read

---

# 7. Group System

No global administrator exists.

Any user can create a group.

Group creator becomes first admin.

Admins may:

* Add members
* Remove members
* Promote admins
* Demote admins
* Rename group
* Change group information

If creator leaves:

* Ownership transfers automatically.

Groups must never exist without an admin.

Users may only be added manually.

No:

* Invite links
* Join codes

If a user is removed:

* Group disappears immediately.
* No access to previous history.

---

# 8. Broadcast Channels

Support channels where:

Admins:

* Post messages
* Post files

Members:

* Read only

---

# 9. Message Features

Support:

* Plain text
* Emojis
* Emoji-only messages
* Reactions
* Reply to message
* Forward messages
* Forward files

Do NOT support:

* Rich text
* Bold
* Italics
* Voice messages
* Message editing

Message input behavior:

Enter = Send

Shift + Enter = New Line

---

# 10. Message Deletion

Support:

Delete for Self

Delete for Everyone

Delete-for-everyone allowed only within 3 minutes.

Applies to:

* Messages
* Images
* Documents
* Files

After 3 minutes:

Only Delete for Self.

---

# 11. Group Read Tracking

Support:

Seen count:

12/18 Seen

Seen-by list.

Not-seen list.

Typing indicators:

* User is typing...
* 2 users are typing...

---

# 12. Blocking System

Blocked users cannot:

* Send private messages
* Send files
* Be added to newly-created groups by blocker

Existing groups remain unaffected until removed by admins.

---

# 13. File Transfer System

File transfer must be peer-to-peer.

No central file storage.

Sender temporarily hosts file.

Recipients download directly from sender.

File remains available:

* While sender is online
* Until expiry period

After download:

Recipient receives local copy.

History stores metadata only.

One file per message.

Support:

* Drag and drop
* Attachment button

Display:

* Filename
* File size
* Progress %
* Transfer speed
* Cancel button
* Success/failure status

Previews:

Images:

* Thumbnail

PDF:

* Thumbnail or first-page preview

Other files:

* File icon

Opening files should use external applications.

No built-in viewer required.

---

# 14. Offline Messaging

No central server exists.

Implement sender-retained queues.

Example:

User A sends message.

User B is offline.

User A stores pending messages locally.

When User B reconnects:

Pending messages are delivered.

If User A shuts down before delivery:

Messages are lost.

This applies to:

* Private chats
* Group chats

No offline file transfer support.

---

# 15. Search

Add:

* Message search
* File search

Existing:

* User search
* Group search

---

# 16. Notifications

Existing:

* Desktop popups
* Taskbar flashing

Add:

* Sound notifications
* System tray unread count

---

# 17. Storage & Retention

User-configurable retention:

* 7 Days
* 30 Days
* 90 Days
* 180 Days
* Forever

Support local chat clearing without affecting other users.

---

# 18. Performance Goals

The application must remain:

* Extremely lightweight
* Fast startup
* Instant chat refresh
* Low LAN traffic
* Low memory consumption
* Responsive with many users and groups

The chat experience should feel modern, similar to WhatsApp/Teams, while preserving a lightweight LAN-first decentralized architecture.

## Settings Module Requirements

Add a dedicated **Settings** icon/button in the main chat interface. The settings window should be lightweight, modern, and organized into categories.

### General Settings

* Change Display Name.
* Enable/Disable Invisible Mode.
* Start application with Windows.
* Minimize to system tray when closed.
* Restore previous session on startup.

### Notification Settings

#### Global Controls

* Enable/Disable all notifications.
* Mute all notifications.
* Do Not Disturb mode.
* Configure notification sound volume.

#### Private Chat Notifications

Users can independently enable or disable:

* Sound notifications.
* Desktop popup notifications.
* Windows toast notifications.
* Taskbar flashing.
* System tray unread badge/count.

#### Group Chat Notifications

Users can independently enable or disable:

* Sound notifications.
* Desktop popup notifications.
* Windows toast notifications.
* Taskbar flashing.
* System tray unread badge/count.

#### Broadcast Channel Notifications

Users can independently enable or disable:

* Sound notifications.
* Desktop popup notifications.
* Windows toast notifications.
* Taskbar flashing.
* System tray unread badge/count.

### Storage Settings

* Configure message retention period:

  * 7 Days
  * 30 Days
  * 90 Days
  * 180 Days
  * Forever
* Clear local chat history.
* Configure file download location.
* Configure maximum allowed file transfer size.
* Display storage usage statistics.

### Network Settings

* Display all detected network interfaces.
* Display LAN IP addresses.
* Display Wi-Fi IP addresses.
* Display listening port.
* Show network connection status.
* Generate and display QR code for mobile access.
* Refresh network information without restarting application.

### Mobile Access Settings

* Enable/Disable mobile web access.
* Show all connected mobile sessions.
* Display:

  * Display Name
  * Device Name
  * IP Address
  * Connection Time
* Disconnect selected mobile sessions.
* View pending approval requests.
* View blocked mobile devices.

### Privacy & User Management

* View blocked users.
* Unblock users.
* Manage pending mobile approval requests.
* View recent connection attempts.
* Session management controls.

### File Transfer Settings

* Configure default download folder.
* Configure maximum file size allowed for transfer.
* Configure temporary file expiry duration.
* Configure transfer limits and behavior.

### About Section

* Application version.
* Build information.
* Update information.
* Diagnostics information.
* Network troubleshooting details.

### Requirements

* All settings must be stored locally.
* Settings must persist across restarts.
* Settings changes should take effect immediately where possible.
* The settings UI should remain lightweight, responsive, and consistent with the overall LAN Chat design.

# Net Split-Tunneler & Proxy Sharing Tool  v4

Net Split-Tunneler is a lightweight, easy-to-use Windows utility designed to share your internet connection (including VPNs or proxy connections) with other computers on your local home or office network (intranet), while making sure you can still access your local network folders and devices.

---

## Why Use It?

* **Host Mode (Internet Provider)**: If your computer has internet access (e.g., through a VPN, a mobile hotspot, or a restricted Wi-Fi connection) and you want to share it with another PC on the same local network.
* **Client Mode (Internet Consumer)**: If you are on a computer that has no internet connection, but is connected to the same local network as the Host computer.
* **Split Routing (Tunneler)**: Normally, connecting to another computer's internet proxy blocks you from accessing local network shares, printers, or internal sites. Net Split-Tunneler automatically splits your network traffic so that local traffic stays local, and internet traffic goes through the shared connection.

---

## Getting Started

> [!NOTE]
> Net Split-Tunneler must be run with **Administrator** permissions because it makes temporary updates to the Windows network routing tables to allow split-tunneling. When launched, Windows will prompt you with a User Account Control (UAC) dialog. Click **Yes** to continue.

### 1. Operating as the Host (Internet Provider)
If your computer has the internet connection you want to share:
1. Open the application.
2. Select the **Host Mode** tab.
3. Click the **Enable LAN+NET** button. This configures Windows to route shared internet packets.
4. Click the **Start Proxy Server** button. This starts the sharing server.
5. Note the **Intranet IP** address displayed (e.g., `10.x.x.x`). You will need this for the client PC.

### 2. Operating as the Client (Internet Consumer)
If your computer needs internet access from the Host PC:
1. Open the application.
2. Select the **Client Mode** tab.
3. The application will scan the local network and attempt to find the Host PC automatically. Once found, the Host PC's IP address will be auto-filled in the **Host IP** box.
   * *If it is not detected automatically, simply type the Host PC's Intranet IP address into the **Host IP** box.*
4. Check **Disable proxy if host has no internet / unreachable** if you want your PC to automatically stop using the proxy if the Host PC goes offline or loses internet.
5. Click **Connect to Host Proxy**. Your web browser and applications will now have internet access.

---

## LAN Chat

A built-in **LAN Chat** tab lets every PC running the app talk to the others on the same
local network — no server, no internet required.

1. Open the application and select the **LAN Chat** tab.
2. Each PC automatically announces itself, so the **Online Peers** list fills in on its own
   within a few seconds. *(Only computers running this app appear — discovery uses a small
   UDP presence broadcast, not a raw ping scan.)*
3. Set your own name in the **You:** box and click **✓**. That name is what other PCs
   see next to your messages. It defaults to your computer name.
4. Click a peer in the list, type a message, and press **Enter** (or **Send**). You can keep
   several conversations going at once — just click between peers.
5. When a message arrives for a conversation you are not currently looking at, a
   **notification pops up in the bottom-right corner** showing the sender's name and the
   message text. Click it to jump straight to that conversation. A small unread badge also
   appears next to the sender in the peer list.

Chat history is saved to disk and restored when you reopen the app.

### Connect by IP (Cross-Subnet Chat)

Automatic discovery only works when both PCs are on the **same subnet**. To chat with someone
on a different `10.x.x.x` subnet:

1. Ask the other person for their `10.x.x.x` IP address.
2. Type it into the **Connect by IP** box in the LAN Chat tab and press ➤ (or Enter).
3. The app immediately probes port 54323 on that IP to verify the other instance is running.
   * If **reachable** — the chat opens and the peer appears in the roster as `reachable ✓`.
   * If **not reachable** — a system message appears in the chat explaining the peer could not be reached. The probe retries every 5 seconds and the roster updates to `reachable ✓` the moment the other PC comes online.
4. Once connected, messages work exactly like a local peer.

> [!TIP]
> Want to see how it works without a second PC? Click **✨ Try Demo Chat** in the LAN Chat
> tab (or **Chat → Run Chat Demo**). A friendly **Demo Bot** appears in the peer list, greets
> you with a pop-up notification, and replies to your messages so you can try the full
> experience on one machine.

## Light & Dark Theme

The app ships in a dark theme, with a one-click **light theme** toggle:

* Click the **☀ / 🌙** button in the top-right of the header, **or**
* Use **Settings → Light theme**.

Your choice is remembered between launches.

## Extra Features

### Real-Time Network Traffic Monitor
Directly inside the application window, you will see a panel named **Network Traffic Monitor**. This shows your current network speeds:
* **Download Speed** (Green text)
* **Upload Speed** (Amber text)

### Settings Menu
At the top of the window, you will find the **Settings** menu:
* **Start with Windows**: Check this option to make Net Split-Tunneler launch automatically whenever you turn on your PC.
* **Show Speed in Taskbar**: Check this option to show the Upload and Download speeds continuously in the system tray (right side of your taskbar next to the clock) in two lines.
  * When checked, you can minimize or close the window, and the speed monitor card will stay active next to the clock.

### Closing and Minimizing
* Clicking the **X** close button in the top-right corner of the window does not close the application. Instead, it hides the window to the background to keep your internet connection active.
* To restore the window, double-click the tray icon or right-click it and select **Show Window**.
* To fully shut down the application, go to **File** -> **Exit** in the menu bar, or right-click the tray icon and select **Quit**.

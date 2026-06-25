# Dual Access — troubleshooting & manual guide

Dual Access runs the **corporate intranet** and the **internet** at the same
time over one network cable. It does this by stacking a second IP on your
adapter and juggling routes + DNS.

This doc covers: recovering a PC that got stuck, *why* it gets stuck, and how to
do the whole thing by hand so you can verify each piece.

> Code: [`nst/dual_access.py`](nst/dual_access.py). Config keys live in
> [`nst/config.py`](nst/config.py) under `HKCU\Software\NetSplitTunnel`.

---

## 1. Recover a stuck PC (do this first)

**Symptom:** Dual Access was enabled (or enabled then disabled) and now you
**can't reach any intranet server**, even though your office IP came back.

**Cause:** Disable does **not** remove the intranet route it added, and that
route is marked *persistent* (`-p`) so it survives Disable *and* a reboot. If it
was pointed at the wrong gateway, it blackholes the whole `10.x` network. (See
section 2.)

Open **Command Prompt as Administrator** and inspect first:

```cmd
ipconfig /all
route print -4
netsh interface ipv4 show dnsservers
```

Look for:
- a **second IP** still stacked on your Ethernet adapter (e.g. `192.168.x.x`
  next to your `10.x` office IP);
- a `10.0.0.0 / 255.0.0.0` line in the table **and** under **"Persistent
  Routes"** at the bottom — this is the culprit;
- DNS stuck on `8.8.8.8` instead of your corporate DNS.

Then clean up (replace `"Ethernet"` with your real adapter name, `<stale-ip>`
with the extra address if present):

```cmd
route delete 10.0.0.0 mask 255.0.0.0
netsh interface ip delete address "Ethernet" <stale-ip>
netsh interface ip set dns "Ethernet" dhcp
ipconfig /flushdns
ipconfig /release
ipconfig /renew
```

The **first line is the important one.** Re-check `route print` and confirm the
`10.0.0.0` line is gone from both the main table and the Persistent Routes
section. If it persists, force it:

```cmd
route -p delete 10.0.0.0 mask 255.0.0.0
```

If your office adapter normally uses **static** DNS, set that instead of the
`set dns ... dhcp` line, then reboot once to be sure nothing stale remains.

---

## 2. What actually happens (the two bugs / assumptions)

**A. It assumes every gateway is `x.x.x.1`.** Gateways are derived by replacing
the last octet with `1`:

```python
def _derive_gw(ip):
    parts = ip.split("."); parts[-1] = "1"; return ".".join(parts)
```

So office IP `10.251.33.45` → assumed gateway `10.251.33.1`, and the internet IP
→ `.1` too. **If your real gateway is anything else** (`.254`, `.175`, a
different subnet…), both the intranet route and the internet default route point
at a gateway that doesn't exist → **neither side works**.

**B. Disable doesn't remove the intranet route.** Enable adds it persistently:

```python
run_cmd(["route", "add", "10.0.0.0", "mask", "255.0.0.0", intranet_gw, "-p"])
```

`_do_disable` removes the secondary IP, the internet default route, and the
NRPT/DNS settings — but **never deletes the `10.0.0.0/8` route**. If it was
pointed at a wrong gateway, it keeps blackholing all intranet traffic after
Disable, and survives reboot because it's `-p`.

**Two smaller gotchas while enabled:**
- Servers **outside `10.0.0.0/8`** (e.g. `172.16.x`, `192.168.x` internal
  servers) aren't covered by the intranet route, so they get sent out the
  internet gateway and become unreachable.
- The adapter DNS is switched to `8.8.8.8`, so internal hostnames only resolve
  if their domain is in your **NRPT Domains** list. Anything not listed fails by
  name (but still works by IP).

---

## 3. Manual Dual Access (do it by hand, verifiably)

Run everything in an **elevated Command Prompt**. Gather your real values first
with `ipconfig /all` and `route print 0.0.0.0`:

| Value | Example | How to find it |
|---|---|---|
| Adapter name | `Ethernet` | `ipconfig` heading |
| Intranet IP | `10.251.33.45` | your current office IP |
| **Intranet gateway** | `10.251.33.1` | "Default Gateway" in `ipconfig` — **verify, don't assume `.1`** |
| Internet IP | `192.168.1.50` | a free address on the internet subnet |
| **Internet gateway** | `192.168.1.1` | the router that actually has internet |
| Corp DNS | `10.251.33.80`, `.90` | your current DNS servers |
| Internal domains | `wbsedcl.in`, `wbsedcl.co.in` | domains you reach by name |

### Enable

```cmd
:: 1. add the second (internet) IP alongside your office IP
netsh interface ip add address "Ethernet" 192.168.1.50 255.255.255.0

:: 2. internet default route, low metric so it wins for general traffic
route add 0.0.0.0 mask 0.0.0.0 192.168.1.1 metric 5

:: 3. keep intranet traffic on the corporate gateway (use the REAL gateway).
::    drop the -p so it's easy to remove later
route add 10.0.0.0 mask 255.0.0.0 10.251.33.1
```

Split DNS — elevated **PowerShell**, one per internal domain:

```powershell
Add-DnsClientNrptRule -Namespace ".wbsedcl.in"    -NameServers "10.251.33.80","10.251.33.90"
Add-DnsClientNrptRule -Namespace ".wbsedcl.co.in" -NameServers "10.251.33.80","10.251.33.90"
```

Adapter DNS — public first, corporate as fallback (back in cmd):

```cmd
netsh interface ip set dns "Ethernet" static 8.8.8.8 primary
netsh interface ip add dns "Ethernet" 10.251.33.80 index=2
netsh interface ip add dns "Ethernet" 10.251.33.90 index=3
```

### Verify each piece (this tells you *why* it fails)

```cmd
ipconfig                          :: both IPs should be listed
ping 8.8.8.8                      :: internet routing works?
nslookup google.com               :: public DNS works?
ping 10.251.x.x                   :: intranet by IP (a server you know)
nslookup server.wbsedcl.in        :: intranet by NAME -> resolves via 10.251.33.80
```

- `ping 8.8.8.8` fails → wrong **internet gateway** (fix step 2).
- ping a server by IP fails → wrong **intranet gateway** (fix step 3), or that
  server isn't in `10.0.0.0/8` (add an explicit route, e.g.
  `route add 172.16.0.0 mask 255.255.0.0 10.251.33.1`).
- ping by IP works but `nslookup name` fails → that server's domain isn't in
  your NRPT list (add it).

### Disable / full undo

```cmd
netsh interface ip delete address "Ethernet" 192.168.1.50
route delete 0.0.0.0 mask 0.0.0.0 192.168.1.1
route delete 10.0.0.0 mask 255.0.0.0
netsh interface ip set dns "Ethernet" dhcp
ipconfig /flushdns
```
```powershell
Remove-DnsClientNrptRule -Namespace ".wbsedcl.in"    -Force
Remove-DnsClientNrptRule -Namespace ".wbsedcl.co.in" -Force
```

The `route delete 10.0.0.0 mask 255.0.0.0` is the step the app currently skips —
always run it when undoing by hand.

---

## 4. Prerequisites the tool can't fix

1. **The single cable must actually carry an internet-capable path.** If your
   desk port only routes the intranet (no DHCP scope/gateway with internet on
   that link), there is no internet gateway to point at — Dual Access can't
   create internet from nothing. You need a port/VLAN that genuinely offers both.
2. **Use your real gateways.** The `.1` assumption is the most common reason it
   silently fails. Doing it manually with the verified gateway is the reliable
   path until the code is fixed to ask for the gateway and to clean up the
   intranet route on disable.

# Changelog

## [Unreleased]

### Changed
- **Alert emails no longer repeat every hour.** A policy now emails **once when it starts alerting** and **once when it clears** — the hourly *"(still active)"* reminder that kept arriving while an alert stayed raised is gone. This applies to **every policy** (threshold/standard monitors, script policies, backup health, and UniFi alerts), since they all share the same alert state machine.
- **Wake-on-LAN is now a monitor + remediation, not one opaque policy.** Wake-on-LAN used to be a single policy that only configured NICs. The wake itself is now a proper **remediation** you can attach to any monitor — most usefully the **Device offline** monitor, which then **auto-wakes** a machine when it drops (it sends a magic packet, so it works while the device is offline, via a relay node where needed). Choose *Wake-on-LAN (send magic packet)* in a policy's **Remediation** field, or use the **Remediate** button to wake a chosen device on demand. The original policy remains as **"Wake-on-LAN readiness"** — the device-side NIC enable + Fast Startup off that makes a machine wakeable in the first place. So: monitor = *Device offline*, remediation = *Wake-on-LAN*.
- **Smaller agent log files.** The agent's rotating log (`agent.log`) now caps each file at **50 KB** instead of 1 MB (still 9 rotated backups), keeping the active log small and quick to open. Requires the updated agent (v2.2.40+).
- **Devices no longer flip to "offline" during a server restart.** A device now stays shown as **online** for a short grace period (120s, `RMM_ONLINE_GRACE`) after its connection drops, as long as it recently heartbeated. Previously the dashboard's online state was purely "is a live socket connected this instant", so **every server redeploy** — which briefly restarts the process and closes all agent sockets with WebSocket code `1012` — flipped the **entire fleet to offline** for the few seconds each agent took to reconnect. Anyone opening the dashboard just after a deploy saw everything offline until it recovered. Control actions (remote control, terminal, reboot/shutdown, agent update, Wake-on-LAN) still require a genuinely live connection. Note: this only smooths over brief blips — a device that is actually down still shows offline once the grace period lapses.
- **Device Overview: Services moved below Applied policies.** The Services list (with "Show services") now sits under the *Applied policies* section instead of above the history charts, grouping the policy/services information together.

### Fixed
- **Devices no longer get stuck "offline" after an agent update or reconnect.** When an agent reconnected — most often right after a **self-update** — the server's cleanup of its *previous* connection could evict the *new* one from the online registry, leaving the device shown as **offline even though it was connected and heartbeating** (its socket was never closed, so the agent's own log looked healthy). Because several devices auto-update around the same time, this typically hit **multiple machines at once**. The connection registry now only removes a device's entry if it's still the *current* connection, so a stale handler can't evict a live reconnection.

### Added
- **Wiki & Debug shortcuts in the top bar.** The top bar now has a **Wiki / documentation** link (opens the project wiki in a new tab) and a **Debug** button that opens a **pre-filled GitHub issue** — with the RMM version, current page, browser and timestamp already filled in — so reporting a problem takes one click.
- **"Updates available" policy + "Notify the signed-in user" remediation.** A new **Updates available** monitor alerts when a device has OS updates ready to install — **Windows Update**, or **Linux** apt/dnf — with the threshold as the minimum number of pending updates (the agent checks in the background, at most every ~6h, so it never delays a heartbeat). Pair it with the new **Notify the signed-in user** remediation: a **desktop toast shown on the device** (via the tray, which runs in the user's session) using the **policy's name as the message** — so the user gets a heads-up like *"Er staan updates klaar om te installeren"* the moment updates pile up. Both the notification and Wake-on-LAN are built-in remediations you can pick on any policy's **Remediation** field or fire from the **Remediate** button. Requires agent **v2.2.41+**.
- **Remediation on every policy — automatic and on-demand.** Every monitoring policy can now run a remediation script (any script from your library — standard or custom), not just script policies. Template/standard rules (Low disk space, Service not running, Firewall, Backup health, …) gained an optional **Remediation script** field: when the policy alerts on a device, that script now runs **automatically** on it. And every policy has an always-available **Remediate** button to run a script on a chosen target device **on demand** — pick the device and script (defaulting to the policy's configured remediation) and run it there and then.
- **Backup health is now a policy you control (and off by default).** Synology Active Backup monitoring used to run **automatically** on every device that reported backup data and email `backup failed` / `backup stale` alerts with no way to turn it off, scope it, or retune it — the source of unexpected NAS alert spam. It's now a proper **"Backup health"** template in **Policies → New policy**: **off by default** (so the automatic NAS emails stop), and you opt in per NAS/org/device with a configurable stale window (hours), severity, notification and target. Add it from the template gallery when you want backup alerting back.
- **Device-type tags & filter on Policies.** Each policy in the **Policies** tab now shows **colour-coded device-type tags** — *Windows*, *Windows Server*, *Linux*, *NAS*, or *All devices* — so you can see at a glance what it supports, and a matching **device-type filter** in the header narrows the long list to one type. The same tags also appear on every tile in the **template gallery** when you add a policy, which now has its **own device-type filter** too — so you can narrow the template list to just Windows, Linux, NAS, etc. before choosing one. **NAS is a first-class type**: the *Backup health* policy shows under **NAS** only, Windows-only monitors under Windows, and cross-platform ones under all. The header shows "*N of M policies*" while filtered; script policies (which apply by target, not OS) aren't tagged and appear under every filter.
- **One-click shareable installer link (Windows).** An organisation's **Downloads → Shareable one-click installer link** now produces a link you can send to anyone **without an RMM login**. The recipient runs a small self-elevating installer that downloads the agent and installs it silently with the **server address and a fresh enrolment key already filled in** — nothing to type — after which the new device lands in your **approval queue** to accept. The link is multi-use (each install mints its own one-time enrolment key), time-limited and revocable. Previously the "shareable" option handed out a raw MSI that still prompted for the server URL and key on the target machine, and the plain Download button required an admin session.

## [1.5.75] - 2026-07-12

### Added
- **Server reachability self-check (Settings → Logs, `health` tag).** The server now periodically verifies that its own **public URL still resolves** in DNS and reads its **TLS certificate's expiry**, logging the result under a new **`health`** tag (filterable and searchable in Settings → Logs). It warns when the advertised hostname stops resolving — the server-side signal behind a browser's *"server not found"* — or when the certificate is within 14 days of expiry. This fills a blind spot: a name-resolution failure otherwise leaves **no trace** in the logs, because the request never reaches the server.
- **Clearer agent DNS diagnostics.** When an agent can't **resolve the server's hostname** (dead or wrong DNS, or a removed DNS record), it now logs a distinct **`DNS resolution failed for <host>`** line instead of a generic "connection lost", so a fleet-wide name-resolution outage is obvious at a glance. Requires the updated agent (v2.2.39+).
- **Device activity audit trail (device → History tab).** Each device's **History** tab now shows an **Activity** section recording operator actions — **who did what, and when**: remote-control sessions, terminal sessions, commands and scripts run, power (reboot/shutdown) and Wake-on-LAN, and file uploads/downloads/deletes. Kept for 90 days.
- **Richer, filterable server logs (Settings → Logs).** Every log line now carries a **tag** (area — `remote`, `http`, `ws`, `unifi`, `update`, `app`, …) you can filter by, plus a free-text search box, and a **Download CSV** button that exports the current (filtered) log with timestamp / level / tag / logger / message columns. The in-memory buffer was doubled (2000 lines). Remote-desktop sessions now log detailed open/close diagnostics — **close code** (who ended it: 1000/1001 browser, 1006 proxy/network, 1011 server), session duration, frames and throughput relayed, and any per-frame send failure — to pin down why a session drops.

### Fixed
- **Remote control no longer drops unattended sessions.** A remote session to a machine nobody was actively touching would end itself every ~15–50 seconds with *"The remote session was ended at the device"* — even though no one was there. Cause: the agent's internal capture channel had a 15-second read timeout, and when the operator went a few seconds without moving the mouse or typing, that idle timeout was misread as a disconnect. Fixed in the agent (requires v2.2.38+); sessions now stay up when idle. Also adds detailed agent-side session logging (a streaming heartbeat with live fps/throughput, and the precise reason a session ends) to `screen.log`.
- **Notification bell now works.** The bell (top bar, every page) fetched a `/api/global` endpoint that didn't exist — it silently 404'd, so it always showed "No new notifications" and never surfaced open alerts. Added the endpoint; the bell now lists the monitor/rule alerts currently raised across your organisations, with the red dot when there are any.
- **Remote control stays connected.** Raised the server's WebSocket ping-timeout so a busy remote-desktop stream is no longer false-positive disconnected: uvicorn's default 20s timeout could kill a healthy-but-saturated screen connection (its keepalive ping queued behind buffered video frames), which dropped the viewer roughly once a minute even though the device never went offline. Combined with the auto-reconnect below, remote sessions are now stable.
- **Remote control auto-reconnects.** If the browser's connection to the server is briefly severed mid-session (a proxy/CDN or network blip — the device stays online the whole time), the viewer now silently re-establishes the session with backoff instead of stranding you on "Disconnected". Reconnects are seamless; only a deliberate Disconnect ends the session.

### Changed
- **Sharper remote control.** The remote-desktop viewer now streams at much higher quality: full-resolution, higher-frame-rate presets — **Balanced** (default) is crisper and smoother, **Sharp** is near-lossless at native resolution, and **Smooth** favours frame rate on slower links. One-shot screenshots are crisper too. (A further efficiency step — H.264 delta encoding — is in progress.)

## [1.5.63] - 2026-07-08

### Added
- **Modernised dashboard & UI.** The dashboard and its sub-pages moved to a refreshed design system. The device list is now a **card grid** with CPU / memory / disk **ring gauges** (replacing the table); the organisation switcher is a dropdown with per-org device counts; and **Remote control** is a full-page surface with a session bar, connection pill, live metrics, a control toolbar and a Session / Display / Activity side panel around the real screen stream. The Windows tray settings dialog and the Synology NAS status page were restyled to match, and the tray gained an optional **server-fingerprint** field (pins the server's TLS certificate, MITM-proof even on a self-signed setup).
- **Extended standard-policy monitors.** Nine new built-in monitor templates in the Policies template gallery: **Disk health (SMART)** (Windows / Linux / Synology), **Reboot required**, **Excessive uptime**, **Antivirus health** (Defender real-time protection / definition age / active threats), **Firewall disabled**, **BitLocker off**, **Failed logon attempts** (brute-force), **Process not running**, and **Windows Event log error** (filter by log, level and event ID). Backed by a new best-effort, throttled collector in the agent. Requires the updated agent.
- **Synology Active Backup visibility.** A monitored Synology NAS now shows a **Backups** section on its device page: **Active Backup for Business** tasks (computers, servers, VMs) with each task's **last successful backup time**, version count, protected-device count, type and running/manual state; plus **Active Backup for Microsoft 365** (and Google Workspace) tasks. The agent collects this on the NAS through Synology's own internal API (`synowebapi`, as root) — no database access, no extra packages — and last-backup recency comes from each task's backup versions (an objective signal: a silent backup shows up as stale). Failure / stale-backup **alerting** is a planned follow-up. Requires the updated Synology agent.
- **Synology NAS monitoring via Package Center.** Monitor a Synology NAS like any other device — no manual setup. Each organisation's **Downloads** tab now shows a **Package Center source URL**; paste it into DSM under **Package Center → Settings → Package Sources → Add**, then install **Leuffen RMM** from the new source. The NAS appears in the device list (with a NAS icon) reporting CPU, memory, storage, uptime, system temperature, model and DSM version, plus volume/disk health — and supports remote shell, scripts, the file browser and reboot/shutdown. The `.spk` is **assembled by the server on demand** from a new pure-stdlib agent (no psutil/websockets — it speaks WebSocket itself and reads `/proc` + DSM CLIs, so one `noarch` package works on every model with any installed DSM Python 3 package — official Python3.9 or a SynoCommunity Python3.10+), with the enrolment config baked in so install needs zero typing. The agent source is **fetched live from the agent repo** (the same way the Windows MSI is proxied from its release) and cached, so a change pushed to the agent repo reaches NASes **without redeploying the server**; it falls back to the copy bundled in the image if GitHub is unreachable, and `RMM_SYNOLOGY_AGENT_REF` (default `main`) can pin a release tag or force the bundled copy. Version bumps surface automatically in Package Center as an available upgrade. A global **Settings → Agents → Synology NAS** toggle (`RMM_SYNOLOGY_SOURCE`, on by default) lets an admin enable or disable the source. Requires the updated agent build (v2.2.22+).
- **SNMP monitoring (v1 / v2c).** A new **SNMP** tab lets you poll network devices — switches, printers, UPSes, anything SNMP — from a network node. Add a target (host, port, community, v1 or v2c, poll interval) and the OIDs to read (type them as `OID = Label`, or load a built-in **preset**: System info, Printer page-count/toner, UPS battery). The assigned node polls each target on its interval over UDP/161 and the dashboard shows the latest value per OID (with friendly formatting, e.g. uptime), per-target status (OK / error / never polled), and a **Poll now** button. Built on a dependency-free SNMP client in the agent — no extra libraries or drivers. SNMPv3 is coming in a later release. Requires the updated agent (v2.2.21+) on the polling node.
- **Windows CPU temperature is now opt-in (advanced).** Reading the CPU die temperature on Windows needs LibreHardwareMonitor's **WinRing0** kernel driver, which is on Microsoft's vulnerable-driver blocklist — Defender detects and removes it (`VulnerableDriver:WinNT/Winring0`), and Memory Integrity (HVCI) blocks it from loading. The agent **no longer loads it by default**, so it stays clean; Windows CPU temperature simply reads N/A. A new **CPU temperature (advanced)** control in each org's **Downloads** tab (Default / On / Off, with a global default — same pattern as auto-update) enables it per organisation, on the machines where you accept that tradeoff. GPU temperature (nvidia-smi) and Linux CPU temperature are unaffected. Requires the updated agent (v2.2.20+).
- **Better node network scan** — Node discovery is more reliable and now names device vendors accurately. It uses **nmap** automatically when it's installed on the node (clean host discovery + vendor data); otherwise the built-in sweep now adds a **TCP-connect probe** alongside ICMP, so hosts that drop ping (common on Windows firewalls) are still found, and vendor names come from a **bundled IEEE OUI database** (~52k prefixes) instead of a tiny hand-maintained list. No configuration and no extra drivers. Requires the updated agent (v2.2.18+).
- **Hyper-V virtualisation visibility** — A Windows host running Hyper-V now reports its **virtual machines** on each heartbeat. The device list shows a **Hyper-V** badge with the VM count on host machines, and the device **Overview** has a collapsible **Hyper-V** section: the header shows a host summary (total VMs, how many are running) and folds out — like the "Show GPU & temperatures" history fold-out — to reveal the per-VM list: name, state, live **CPU %**, **memory** (demand / assigned), **uptime** and vCPU count. The role is detected automatically (no config) and only appears on hosts that have it; hosts with the role but no VMs are shown too. Requires the updated agent (v2.2.16+).
- **GPU & temperature monitoring** — Devices now report **GPU usage**, **GPU temperature** and **VRAM use** (NVIDIA via `nvidia-smi` on any OS; other GPUs report usage via Windows GPU performance counters or Linux sysfs) plus **CPU temperature** (Linux hardware sensors; on Windows the true CPU die sensor via a bundled LibreHardwareMonitor — same source as HWMonitor). The device **Overview** shows GPU usage, VRAM and temperatures as ring cards alongside CPU/Memory/Disk; the **History** charts add GPU-usage, CPU-temp and GPU-temp graphs behind a collapsible "Show GPU & temperatures" fold-out (kept tidy by default); and three new alert templates — **High GPU usage**, **High CPU temperature** and **High GPU temperature** — let you alert/email when they cross a threshold. Values show as N/A where the hardware can't report them. Requires the updated agent (v2.2.15+; accurate Windows CPU temperature needs v2.2.17+); data accumulates going forward.
- **Device History tab** — Each device drawer now has a **History** tab listing policy issues: **current issues** (monitor-rule alerts raised right now, with when they started) and **resolved** issues (past alerts, with when they cleared and how long they lasted). Resolved issues are recorded going forward whenever a policy alert clears (kept for 90 days); a healthy device shows a clear "no active issues" state.
- **Automatic agent updates** — A new policy keeps agents on the latest build without clicking "Update all". Set a **global default** in Settings → Agents → Updates, and **override it per organisation** under each org's Downloads tab (Default / On / Off). When effectively on, an agent that connects on an older version is upgraded in place immediately, and a periodic server sweep (every ~6h) catches always-on devices after a new version ships. (Also fixes the "Update all online agents" button, which compared the agent build against the *server* version and so could mis-judge which agents were outdated.)
- **Device screenshot** — A "Screenshot" button (next to Remote control on a device's Actions tab) grabs a single still of the device's current screen so you can quickly check whether someone's using it, without starting a full remote session. It reuses the existing secure screen channel and shows a timestamp with a Refresh button. The capture is **silent** — no consent banner is shown for a one-shot screenshot (the persistent banner still appears for full remote-control sessions). Silent capture requires the updated agent (v2.2.14+); older agents still flash the remote-session banner.
- **History chart tooltips** — Hovering the CPU / Memory / Disk history charts on a device now shows the exact percentage and timestamp at that point, with a guide line and marker dot.
- **Invite delivery choice** — When inviting a user you now pick how the invite is sent: **email + link**, **email only**, or **link only**. The invite dialog shows the shareable link with a copy button (and falls back to it automatically if email delivery isn't configured or fails).
- **Email verification on signup** — Invitees confirm their email with a 6-digit code when setting up their account. If the server has no mail delivery configured the step is skipped gracefully and the account is flagged unverified. Unverified accounts are marked in Settings → Users & roles.
- **Edit user accounts** — Admins can edit an existing account (display name, email, role, and password reset) from the pencil button on each user row, not just delete it.
- **Secure agent connection** — End-to-end hardening of the agent WebSocket:
  - **TLS certificate pinning.** The server now exposes its certificate's SHA-256 fingerprint (Settings → Security, `GET /api/server-fingerprint`, and the startup log). Pin it on agents via `RMM_SERVER_FINGERPRINT` or the `server_fingerprint` config key so a self-signed deployment is still safe against man-in-the-middle.
  - **Per-device secret (trust-on-first-use).** The server issues each agent a secret on first connect (stored hashed) and requires it on later reconnects, so a stolen `device_id` can't impersonate a device.
  - **Device-secret enforcement is now a Settings toggle** (Security → Device identity), applied live without a restart. **On by default for new installs** (detected by having no enrolled devices); **off for existing installs** so a not-yet-updated fleet isn't locked out. The `RMM_REQUIRE_DEVICE_SECRET` env var still works and overrides the toggle.

### Fixed
- **Notifications bell.** On the dashboard the bell opened an empty, invisible box when there were no alerts — it now shows **"No new notifications"** and only lights the red dot when there really are open alerts. On the Settings and Account pages the bell was a dead button (no menu, and a permanently-lit red dot); it's now a working dropdown fed by the same open-alerts feed.
- **Settings / Account pages could render blank.** The shared header script (`chrome.js`) declared a top-level helper that collided with an identically-named one in the page scripts; because plain page scripts share one scope, that threw a redeclaration error which aborted the page before it rendered. The helper is now function-scoped.
- **Windows Defender quarantined the agent (false positive).** Microsoft Defender flagged the installed agent as `Trojan:Win32/Bearfoos.B!ml` — a machine-learning *false positive* common to PyInstaller-built apps — and removed the agent executable after install, leaving the device unmanaged. Two mitigations ship together: the agent is now built with PyInstaller **onedir** instead of onefile (a self-extracting onefile exe is the single biggest trigger for this detection), and the installer adds a **Microsoft Defender exclusion** for the install folder and agent/tray processes (best-effort — it still installs cleanly where Tamper Protection blocks the change). Requires the updated agent (v2.2.19+). Devices whose agent was already quarantined need a one-time manual allow/reinstall, since a removed agent can't self-update. *(Not a guarantee on every endpoint; reporting the file to Microsoft's false-positive portal and/or code-signing remain stronger long-term options.)*
- **Hyper-V section showed no VMs when expanded.** The device detail API reused the device-list decorator, which replaced the full per-VM Hyper-V data with the compact summary, so the fold-out had nothing to show. The detail endpoint now keeps the full per-VM list. (Also: the fold-out trigger is now the same pill button style as "Show GPU & temperatures".)
- **Windows CPU temperature was wrong or missing.** It read the motherboard's ACPI thermal zone (`MSAcpi_ThermalZoneTemperature`), which is a different, often-bogus sensor — it reported implausible values (e.g. 18 °C while the CPU was at 52 °C) and didn't exist on many boards. The agent now reads the real CPU die sensor through a bundled **LibreHardwareMonitor** (the same source HWMonitor/Core Temp use), so values match those tools on both Intel and AMD. The reading is cached (~60 s) because it loads a kernel driver, and still shows N/A where the driver can't load (e.g. machines with Memory Integrity/HVCI blocking it). Requires the updated agent (v2.2.17+).
- **Processor showed a raw CPU ID instead of the model name.** Inventory reported the bare architecture string (e.g. "AMD64 Family 26 Model 68 Stepping 0, AuthenticAMD") because it used `platform.processor()`. It now reads the marketing name from WMI on Windows (`Win32_Processor.Name`, e.g. "AMD Ryzen 7 9700X 8-Core Processor") and `/proc/cpuinfo` on Linux. Also hardens the Windows hardware query so a blank serial number can no longer shift the manufacturer/model parsing. Requires the updated agent (v2.2.15+).
- **Monitor rule dialog looked cramped.** The "Add/Edit rule" dialog reused the input/label styles that were scoped only to the policy modal, so its labels sat inline with tiny native-width inputs. The fields now stack cleanly with full-width inputs, the paired Threshold/Sustained and Severity/Email rows are laid out vertically, and the dropdowns get a proper chevron.
- **Software page always showed "0 programs".** The agent collected installed software with `ConvertTo-Json -AsArray`, a parameter that only exists in PowerShell 7+. On Windows 10/11 (which ship Windows PowerShell 5.1 as `powershell.exe`) the command errored and the agent reported an empty list. The scan now reads the uninstall registry directly via `winreg` — no PowerShell, faster, and it also recovers per-user (HKU) installs that the old script missed. Requires the updated agent (v2.2.13+).
- **Web remote "Send Ctrl+Alt+Del" did nothing (e.g. the Windows Server login screen).** The agent's `SendSAS` call from the SYSTEM service is silently ignored unless the `SoftwareSASGeneration` policy permits services, which Windows doesn't enable by default. The agent now enables that policy on demand (no reboot) before sending the SAS, so the button works at the login/lock screen. Requires the updated agent (v2.2.12+).
- **Agent MSI download pointed at the wrong repo.** `RMM_MSI_URL` / `RMM_GH_REPO` defaulted to the server repo (stale v1.1.x agent) instead of `leuffen-rmm-agent` (current v2.x with the secure-connection code). Defaults now point at the agent repo.

### Changed
- **Unified Policies page.** Script policies and template-based rules now appear together in a single list (so the page count always matches what's on screen, instead of hiding rules behind a separate "Template rules" tab). The toggle is gone; the **New policy** button now opens a small menu to choose between adding a standard **from a template** or a **script policy**, and templates are picked from a focused gallery dialog.
- **Delete device moved to a "Danger zone".** The Remove action was sitting in the device Actions grid right next to Lock / Restart, where it was easy to hit by accident. It's now a clearly-labelled red button in a "Danger zone" section at the bottom of the Actions tab, and it opens a confirmation dialog that names the device and warns the removal can't be undone before anything is deleted.
- **Branded emails** — All outgoing email (invites, the email-verification code, alert/resolved notifications, and test emails) now uses a single dashboard-styled template: dark card, the Leuffen RMM logo and wordmark, a primary action button on invites, a coloured status header on alerts, and an "Open dashboard" footer link. The wordmark and footer follow your configured server name.
- **Vendored agent synced to v2.2.22** — the agent bundled in the server image (served via `agent.zip`, and used to assemble the Synology `.spk`) now matches the canonical agent: the new Synology DSM agent (`syno_agent.py` / `syno_inventory.py`) + SPK packaging, the SNMP v1/v2c monitoring client, the opt-in Windows CPU-temp driver, the Defender-false-positive hardening (onedir build + install-time exclusion), the improved nmap/OUI network scan, accurate Windows CPU temperature via LibreHardwareMonitor, Hyper-V VM collection, GPU/temperature collection, the CPU model-name fix, cert pinning, per-device secret, login/lock-screen capture, the Ctrl+Alt+Del SAS fix, the winreg-based software scan, and silent one-shot screenshots.

---

## [1.5.1] - 2026-06-19

### Added
- **Shareable MSI download links** — Admins can generate time-limited download links (1/3/7/14/30 days) for the Windows MSI installer that work without a login session. Links track their download count and can be revoked. Settings → Downloads.

### Fixed
- **Server startup crash** — The shareable-links feature referenced `BaseModel` without importing it, crashing the server on boot with `NameError`. Added the missing `pydantic` import.
- **Agent update button showed the wrong "Latest" version** — It displayed the server release tag (e.g. v1.5.0) instead of the agent version. The release endpoint now returns the canonical agent version, which the UI prefers.
- **Remote control stuck on "Connecting…"** — The Windows MSI build did not bundle `mss`/`pynput`, so the agent could never start screen capture; the failure was reported on the wrong channel so the viewer hung. The build now bundles the deps and the agent surfaces screen errors to the viewer.
- **Installed software not showing** — The agent collected installed software synchronously inside the event loop; the Windows registry scan could exceed the server's 60s request timeout. The scan now runs off-loop and the server timeout was raised to 100s.

### Changed
- Agent bumped to **v1.1.8**.

---

## [1.5.0] - 2026-06-19

### Added
- **Web remote desktop** — Click "Remote control" on any online Windows device to open a full-screen remote session in a new browser tab. Renders the live screen at 8 fps, forwards mouse (move, click, scroll) and keyboard input, and includes a clipboard paste button and a Ctrl+Alt+Del button (via Windows `sas.dll`).
- **Agent connection test** — The agent settings dialog now tests the server connection before saving. Shows a specific error message for timeout, refused connection, or TLS errors. The save button shows "Testing connection…" during the check.

### Fixed
- **Bell notification button** — The red ping dot is now hidden when there are no active alerts. Clicking the bell opens a dropdown listing open monitor alerts; clicking an entry navigates to that org's Monitors tab. Previously the button did nothing and always showed the ping.

### Changed
- Agent bumped to **v1.1.7**.

---

## [1.4.0] - 2026-06-19

### Added
- **SSO access control** — Microsoft 365 users can no longer sign in unless an admin has provisioned them a local account (via invite). Unauthorised SSO logins now show a clear "Access denied — contact your administrator" page instead of silently creating a session.
- **Access groups** — Admins create named groups, assign users to them, and assign each group to organisations with a base role (admin / member / viewer). Settings → Users & roles → Access groups.
- **Per-action permission overrides** — On top of a group's base role, each action (remote terminal, run scripts, power, Wake-on-LAN, delete device, remove agent) can be explicitly set to Allow or Deny per org.
- **Deny overrides allow** — When a user belongs to multiple groups, any Deny for an action wins over all other groups' Allow. The permission toggle cycles: inherit → Deny → Allow (deny is offered first). The UI shows conflicts — "Denied by Group A · Group B would allow".

### Changed
- Users & roles info callout updated to explain SSO now requires a pre-provisioned account.

---

## [1.3.0] - 2026-06-19

### Added
- **User invites** — Settings → Users & roles has an "Invite user" button. Generates a link (emailed + shown as fallback) that expires after 2 days. Invitees pick a username and password on the accept page. Pending invites are listed with a revoke button.
- **User delete** — Delete button on each row in the users table.
- **SSO credentials in Settings** — Settings → Authentication now has a Microsoft 365 credentials block (tenant ID, client ID, client secret, redirect URI) so you can update them without re-running the setup wizard.
- **Permissions guidance** — The SSO credentials block and Graph mail section now show a callout explaining exactly which Entra app permissions are required (Mail.Send, redirect URI).
- **Validation on save** — Saving SSO or Graph mail settings highlights which required fields are missing before making any changes.
- **Changelog pagination** — The "What's new" block in Settings → General shows 5 versions per page with Newer/Older navigation.

### Changed
- Email delivery (SMTP vs Microsoft Graph) is now a single picker block instead of two separate sections — select one and only that method's fields are shown.

---

## [1.2.0] - 2026-06-19

### Added
- **SMTP email support** — alert emails can now be sent via any SMTP server (STARTTLS, SSL/TLS, or plain). Configure host, port, credentials and from-address in Settings → Alerts & email. SMTP takes priority over Microsoft Graph when both are configured; Graph remains available as a fallback.
- **In-UI container updates** — Settings → General now shows a "Check for updates" button that pulls the latest server image and restarts the container in place (requires the Docker socket to be mounted and the prebuilt registry image).

### Changed
- Alert email settings split into three blocks: SMTP, Microsoft Graph, and Recipients — making it clearer which delivery method is active.

### Fixed
- Docker socket group access (`group_add: DOCKER_GID`) so the in-UI updater can reach the socket when the container runs as a non-root user.

---

## [1.1.6] - 2026-06-17

### Added
- Installed-software audit per device.
- venv-based Linux installer (no system Python needed).

### Fixed
- Copy button truncation in the install command snippets.

---

## [1.1.5] - 2026-06-10

### Added
- Wake-on-LAN as a policy template with per-OS support flags.
- Unzip-free installer for Windows agents.

---

## [1.1.4]

### Fixed
- Disable Fast Startup on Windows to ensure Wake-on-LAN works reliably.

---

## [1.1.3]

### Added
- 30-day metric retention with history charts.
- GPU monitoring.
- Wake-on-LAN relay logging.

---

## [1.1.2]

### Added
- Per-drive storage metrics.
- Remote file manager (centered modal).

---

## [1.1.1]

### Added
- Version-gated in-UI update button.
- Trusted-profile ping rule.
- WoL NIC auto-enablement on managed devices.

---

## [1.1.0]

### Added
- Agent and server self-update flow.
- Logs viewer and version display.
- One-time enrollment keys.
- Configurable dashboard widgets (Phase 3).
- Monitor alerts via email with severity levels and per-rule notification toggle.
- Global monitor rules (fleet-wide, managed by global admins).
- User-managed monitor templates replacing hardcoded alert policy.
- Device approval queue.
- Multi-tenant support (multiple organisations in one server).
- Wake-on-LAN directed broadcast for cross-VLAN wake.
- Network node subnet discovery — age hosts to offline between scans.

### Fixed
- Stale Nodes UI after subnet changes.
- Agent preserves config across MSI self-update.
- Deduplicated subnets and auto-allow inbound ping.

---

## [1.0.0]

### Added
- Initial release: fleet dashboard, per-org device view, live CPU/RAM/disk rings.
- Remote terminal, power actions, Wake-on-LAN.
- Network node promotion and subnet scanning.
- Office 365 SSO + Microsoft Graph alert mail.
- Windows MSI installer with tray companion and post-install config dialog.
- Docker and native Linux server deployment.
- Self-signed TLS, Let's Encrypt (Caddy proxy), and bring-your-own-cert modes.

# Changelog

## [Unreleased]

### Added
- **History chart tooltips** — Hovering the CPU / Memory / Disk history charts on a device now shows the exact percentage and timestamp at that point, with a guide line and marker dot.
- **Invite delivery choice** — When inviting a user you now pick how the invite is sent: **email + link**, **email only**, or **link only**. The invite dialog shows the shareable link with a copy button (and falls back to it automatically if email delivery isn't configured or fails).
- **Email verification on signup** — Invitees confirm their email with a 6-digit code when setting up their account. If the server has no mail delivery configured the step is skipped gracefully and the account is flagged unverified. Unverified accounts are marked in Settings → Users & roles.
- **Edit user accounts** — Admins can edit an existing account (display name, email, role, and password reset) from the pencil button on each user row, not just delete it.
- **Secure agent connection** — End-to-end hardening of the agent WebSocket:
  - **TLS certificate pinning.** The server now exposes its certificate's SHA-256 fingerprint (Settings → Security, `GET /api/server-fingerprint`, and the startup log). Pin it on agents via `RMM_SERVER_FINGERPRINT` or the `server_fingerprint` config key so a self-signed deployment is still safe against man-in-the-middle.
  - **Per-device secret (trust-on-first-use).** The server issues each agent a secret on first connect (stored hashed) and requires it on later reconnects, so a stolen `device_id` can't impersonate a device.
  - **Device-secret enforcement is now a Settings toggle** (Security → Device identity), applied live without a restart. **On by default for new installs** (detected by having no enrolled devices); **off for existing installs** so a not-yet-updated fleet isn't locked out. The `RMM_REQUIRE_DEVICE_SECRET` env var still works and overrides the toggle.

### Fixed
- **Web remote "Send Ctrl+Alt+Del" did nothing (e.g. the Windows Server login screen).** The agent's `SendSAS` call from the SYSTEM service is silently ignored unless the `SoftwareSASGeneration` policy permits services, which Windows doesn't enable by default. The agent now enables that policy on demand (no reboot) before sending the SAS, so the button works at the login/lock screen. Requires the updated agent (v2.2.12+).
- **Agent MSI download pointed at the wrong repo.** `RMM_MSI_URL` / `RMM_GH_REPO` defaulted to the server repo (stale v1.1.x agent) instead of `leuffen-rmm-agent` (current v2.x with the secure-connection code). Defaults now point at the agent repo.

### Changed
- **Branded emails** — All outgoing email (invites, the email-verification code, alert/resolved notifications, and test emails) now uses a single dashboard-styled template: dark card, the Leuffen RMM logo and wordmark, a primary action button on invites, a coloured status header on alerts, and an "Open dashboard" footer link. The wordmark and footer follow your configured server name.
- **Vendored agent synced to v2.2.10** — the agent bundled in the server image (served via `agent.zip`) now matches the canonical agent: cert pinning, per-device secret, and login/lock-screen capture.

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

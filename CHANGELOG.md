# Changelog

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

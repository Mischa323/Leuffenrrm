# Leuffen RMM — Development & Release Guide

> This is `CLAUDE.md`, so Claude Code loads it automatically when working in this
> repo — it's the guide to reference every session. It's also the human
> dev/release guide; keep it current.

How this project is laid out and, most importantly, **what to do every time you
push to `main`** so releases don't drift (MSI, versions and the changelog have
gone stale before — this is the checklist to prevent that).

---

## Repositories

| Repo | What lives here |
|---|---|
| **`Mischa323/Leuffenrrm`** (this repo) | FastAPI server (`server/app/`), the vanilla-JS dashboard (`server/app/static/`), the **vendored** copy of the agent (`agent/`), the Synology packaging the server assembles (`packaging/synology/`), and `CHANGELOG.md` + `VERSION`. |
| **`Mischa323/leuffen-rmm-agent`** | The **canonical** cross-platform agent (`agent/`) and packaging (`packaging/windows` WiX MSI, `packaging/synology` SPK) + release workflows. |

The `agent/` code is duplicated in both repos and must stay **byte-identical**
(copy canonical → vendored) **except** the `AGENT_VERSION` constant, which lags
in the vendored copy (see "The auto-update loop" below).

---

## Server architecture (`Leuffenrrm/server/app/`)

- **`main.py`** — every HTTP endpoint, the agent WebSocket (`/ws/agent`), metrics
  ingestion (`_handle_agent_msg`), and `MONITOR_TEMPLATES`. `SERVER_VERSION` and
  the vendored `AGENT_VERSION` are resolved here.
- **`database.py`** — SQLite: schema, **additive `ALTER` migrations run at
  startup** (add a new column by adding it to `CREATE TABLE` *and* an
  `if col not in dcols` migration), CRUD, per-device JSON columns
  (`services_json`, `disk_health_json`, …).
- **`alerts.py`** — `evaluate_once()` runs every rule against each device and
  drives the raise/clear/cooldown email state machine (`_apply`). Status
  monitors read the device's `*_json` columns directly; numeric monitors average
  the metrics timeseries.
- **`auth.py`** — hybrid auth: local password accounts + optional Microsoft 365
  SSO + a dev auto-login. Signed session cookies.
- **`models.py`** (pydantic requests), `mailer.py`, `graph.py` (M365), `unifi.py`
  (UniFi cloud poller), SNMP, etc.
- **`static/`** — the UI, **vanilla HTML/CSS/JS, no build step**:
  - `index.html` + `app.js` — the dashboard (org view, device drawer, all tabs).
  - `settings.html/js` + `settings.css`, `account.html/js`, `setup.*`, `remote.*`.
  - `chrome.js` — shared top header for the sub-pages; `appearance.js` — theming
    (applied before paint); `icons.js` — inline SVG icon set; `styles.css` — the
    design system tokens + components.
  - ⚠️ **`chrome.js`, `settings.js`, `account.js` are plain `<script>`s that share
    ONE global lexical scope.** A top-level `const`/`let` in one that collides
    with another (e.g. `esc`, `initials`) throws `Identifier already declared`
    and blanks the page. Scope shared helpers **inside** a function.

## Agent architecture (`leuffen-rmm-agent/agent/`)

- **`agent.py`** — main loop: opens the WS, sends metrics each heartbeat
  (`_metrics_with_sensors`), handles control messages, self-update.
- **`inventory.py`** — hardware/software inventory. **`AGENT_VERSION` lives here
  (single source of truth).**
- **`monitors.py`** — extended health monitors (disk/SMART, reboot-pending,
  Windows security, event log, processes), throttled + best-effort.
- `snmp.py`, `screen.py` (remote control), `netscan.py`, `updater.py`,
  `tray.py` (Windows tray dialog), `handlers.py`.
- **`syno_agent.py` + `syno_inventory.py`** — the slim, stdlib-only Synology DSM
  agent. It carries its **own** `AGENT_VERSION` copy (the `.spk` doesn't bundle
  `inventory.py`) — kept in lockstep with `inventory.py`.

---

## Two independent version numbers

| | File | Example | Used for |
|---|---|---|---|
| **Server** | `Leuffenrrm/VERSION` | `1.5.63` | app version; the agent-versions widget; **cache-busting** — `_serve_html` appends `?v=<SERVER_VERSION>` to every local asset URL. |
| **Agent** | `agent/inventory.py` `AGENT_VERSION` (mirror in `syno_inventory.py`) | `2.2.34` | stamped into the MSI/SPK. The **server advertises the vendored** `Leuffenrrm/agent/inventory.py` value. |

> **Cache-busting depends on `VERSION`.** Static JS/CSS is served with
> `?v=<SERVER_VERSION>`. If `VERSION` doesn't change, browsers keep serving the
> **old cached** files after a deploy. `VERSION` auto-bumps on every server push
> (below), so a normal push handles this — but it's why a UI change can look
> "not deployed" until the version moves.

## Automation (GitHub Actions)

- **Leuffenrrm `auto-version.yml`** — every push to `main` touching `server/` or
  `VERSION` bumps the `VERSION` **patch** and commits `chore: … [version bump]`.
  So **you don't hand-bump `VERSION`.**
- **leuffen-rmm-agent `auto-version.yml`** — every push touching `agent/` bumps
  `AGENT_VERSION` in `inventory.py` **and** `syno_inventory.py`, commits
  `[version bump]`.
- **leuffen-rmm-agent `windows-agent-msi.yml`** — builds the PyInstaller exe +
  WiX MSI and **publishes GitHub Release `v<AGENT_VERSION>`**. Manual:
  `gh workflow run windows-agent-msi.yml --ref main -R Mischa323/leuffen-rmm-agent`.
  **It does NOT run automatically after the version bump — you trigger it.**
- **Leuffenrrm `server-image.yml`** — builds the server container image.

## ⚠️ The auto-update loop (do not trip this)

Agents decide they are "outdated" by comparing their version to the **server's
advertised version** (the **vendored** `Leuffenrrm/agent/inventory.py`). If the
vendored `AGENT_VERSION` is **higher than the newest published MSI**, agents
"update" to an older MSI, reconnect still-outdated, and update **forever**.

> **Rule: keep vendored `Leuffenrrm/agent/inventory.py` == the latest PUBLISHED
> MSI release.** Building/publishing the MSI does **not** roll out to agents by
> itself — the rollout happens only when the server starts advertising the new
> vendored version.

---

## ✅ Checklist — every push to `main`

### A. Server-only change (nothing under `agent/`)

1. Make the change under `server/`.
2. **Add a `CHANGELOG.md` entry** under `## [Unreleased]` → `### Added / Fixed / Changed`.
3. Branch → PR → **squash-merge**. `auto-version.yml` bumps `VERSION` (this
   cache-busts the static assets).
4. Deploy the server.
5. **Cut the changelog** (see below) so shipped work isn't left under `[Unreleased]`.

### B. Agent change (touches `agent/`) — the full dance

1. Change the **canonical** agent (`leuffen-rmm-agent/agent/…`). PR → merge.
   `auto-version.yml` bumps `AGENT_VERSION` (e.g. `2.2.33 → 2.2.34`).
   **Confirm the bump landed** (`git pull`, `grep AGENT_VERSION agent/inventory.py`)
   before continuing — the bump run is async; an old completed run can fool a
   "wait for green" loop.
2. **Build + publish the MSI:**
   `gh workflow run windows-agent-msi.yml --ref main -R Mischa323/leuffen-rmm-agent`,
   then verify `gh release view v<new> -R Mischa323/leuffen-rmm-agent` shows the
   `.msi` asset.
3. **Sync the vendored agent** into this repo: copy the changed `agent/*.py`
   canonical → `Leuffenrrm/agent/` (byte-identical), and **bump the vendored
   `inventory.py` + `syno_inventory.py` `AGENT_VERSION` to the just-published
   MSI version.**
4. Add a `CHANGELOG.md` entry.
5. Branch → PR → merge the server change → **deploy**. The server now advertises
   the new version and online agents auto-update to the matching, published MSI.

> Order matters: **publish the MSI *before* bumping the vendored version**, or
> you hit the loop above. Building the MSI early is safe (no rollout); the
> vendored bump + deploy is the actual go-live.

## Changelog (keep-a-changelog)

- Pending work accumulates under `## [Unreleased]`.
- **Cut a release:** rename `## [Unreleased]` → `## [<VERSION>] - <YYYY-MM-DD>`
  (VERSION = current `VERSION` file value) and add a **fresh empty
  `## [Unreleased]`** at the top. Do this when you deploy a notable batch —
  don't let shipped work sit under `[Unreleased]` (it's served at
  `GET /api/changelog` and shown to users).

## Conventions

- Verify before a PR: `python -m py_compile` the changed server + agent `.py`;
  `node --check` the changed static `.js`.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- PR bodies end with the "Generated with Claude Code" line.
- PR-and-merge (**squash**). Branch first if on `main`. Don't commit the
  `design center/` reference bundle.

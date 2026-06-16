# Leuffen RMM

A simple, self-hosted **Remote Monitoring & Management** tool. It monitors system
statistics across devices, gives you remote control (terminal, screen, power,
files), wakes devices via **Wake-on-LAN** (including across LANs/VLANs through
relay *nodes*), discovers devices on the network, and sends monitoring email
alerts via Microsoft Graph. The dashboard is protected by **Office 365 SSO**.

> Web UI: a modern dark/light dashboard (fleet overview, per-org devices, slide-in
> device drawer with live rings/terminal/actions), a 5-step setup wizard, admin
> settings, and a user account page — with a two-tier appearance system (workspace
> default + per-user override). Dependency-free vanilla HTML/CSS/JS in `server/app/static/`.

> Phase 2 (in progress): **scripts** (in-app editor, categories, attached files),
> **scheduled jobs**, and Datto-style **monitoring policies** — a monitor script
> that auto-runs a **remediation** script on failure, with policy **variables**
> passed to both as environment.

> Status: **Phase 1** (core RMM) is implemented and runnable. Phase 2 features
> (monitors library + auto-response, scripts/scheduled jobs, patch management,
> compliance evaluation, software audit, scheduled reports) are planned — see
> [Roadmap](#roadmap).

---

## Installation

The fastest way to get a working RMM is: **run the server with Docker**, open the
dashboard, then **install the agent** on each machine you want to monitor.

### Prerequisites
- A host for the **server** with **Docker** + **Docker Compose** (or Python 3.11+
  to run it natively on Linux).
- Machines to monitor (**Windows or Linux**) with **Python 3.9+** for the agent.
- *(Optional, for production)* a Microsoft **Entra/Office 365** app registration
  for SSO + Graph alert email — see [SSO setup](#office-365-sso--graph-mail-setup).
  Without it, the server starts in **dev-auth mode** so you can try it right away.

### Step 1 — Start the server

```bash
git clone https://github.com/Mischa323/Leuffenrrm.git
cd Leuffenrrm
docker compose up --build -d
```

No env editing needed — configuration happens in the browser (next step).

### Step 2 — Run the setup wizard

Open **https://localhost:8000**. The server uses HTTPS by default with a
self-signed cert, so your browser warns once — accept it. On first boot you're
taken to a **setup wizard** where you enter:

- **Public URL** — what agents/browsers use to reach the server (baked into installers).
- **Administrator email** — the global admin.
- **HTTPS / TLS** — self-signed (default), your own cert files, or behind a reverse proxy.
- **Sign-in** — *Local + Microsoft 365* (default): username/password accounts
  (PBKDF2-hashed) plus **optional** Microsoft 365 SSO, matched by email. Or
  *Dev mode* (no login, evaluation only).

Settings are saved to the `/data` volume. Some (SSO, TLS, session secret) take
effect after a quick `docker compose restart`. A **Default** organisation is
created automatically.

> Prefer environment variables and want to skip the wizard? Set `SESSION_SECRET`
> (and any of `RMM_PUBLIC_URL`, `RMM_BOOTSTRAP_ADMIN`, `MS_*`) in
> `docker-compose.yml` — the wizard is then bypassed. See [Configuration reference](#configuration-reference).
>
> Prefer no Docker? See [Run natively on Linux](#run-natively-on-linux-no-docker).

### Step 3 — Install an agent

In the dashboard open an organisation → **Downloads**, then run the one-liner it
shows (the server URL and that org's enrollment key are already baked in):

```bash
# Linux (run as root)
curl -fsSL http://YOUR_SERVER:8000/api/orgs/default/install.sh | sudo bash
```

```powershell
# Windows (PowerShell, as administrator)
iwr http://YOUR_SERVER:8000/api/orgs/default/install.ps1 -UseBasicParsing | iex
```

The device appears under **Devices** within a few seconds, auto-placed in its OS
group (Windows / Linux / Windows Server), streaming live CPU/RAM/disk stats.

> **Windows MSI:** the Downloads tab also offers a standalone **MSI** (no Python
> needed). It's built in CI (`.github/workflows/windows-agent-msi.yml`, Windows
> runner) — push a `v*` tag to publish it to Releases — and configured at install
> via `msiexec` properties (`RMM_SERVER_URL`, `RMM_API_KEY`, `RMM_INSECURE_TLS`).

### Step 4 — Use it
- Click a device for inventory, an interactive **terminal**, **power** actions and
  **Wake-on-LAN**.
- To wake or scan machines on a remote LAN, open a device → **Actions →
  Promote to network node**, add its subnet(s), then use **Network**.

Full details (native install, Docker agent, SSO/Graph, cross-LAN WoL, config
reference) are below.

---

## Architecture

```
 Agents / Nodes (Win/Linux) ──WS──► Server (FastAPI) ◄──WS/REST── Browser dashboard
   psutil + control handlers          SQLite, alert engine,         polished vanilla UI
   node: WoL relay + scan             MSAL SSO, Graph mailer         (global + per-org)
```

- **Server** — FastAPI + SQLite. Runs in **Docker** or natively on **Linux**.
- **Agent** — one cross-platform Python agent (**Windows + Linux**) that holds a
  single **outbound WebSocket** to the server (works through NAT/firewalls). It is
  low-footprint: event-driven, cheap metrics, heavy screen deps loaded only on
  demand, runs at below-normal priority.
- **Nodes** — any agent can be **promoted to a node** from the dashboard to relay
  Wake-on-LAN to its local subnets and scan the network for devices.
- **Multi-org** — organisations are logical tenants inside your single Office 365
  tenant; users are scoped to orgs by role. A **global dashboard** aggregates all
  your orgs and drills into each org's own dashboard.
- **Groups** — each org auto-creates **Windows**, **Linux**, and **Windows Server**
  groups; devices are auto-assigned on enrollment by OS. Add custom groups too.

---

## Quick start (Docker)

```bash
# 1. Edit docker-compose.yml — at minimum change RMM_API_KEY and SESSION_SECRET.
docker compose up --build
# 2. Open http://localhost:8000
```

With no `MS_*` values set, the server runs in **dev-auth mode** (auto-signs in a
local admin) so you can try it immediately. Configure SSO for real use (below).

### Run natively on Linux (no Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt
cd server
RMM_API_KEY=change-me RMM_DEV_AUTH=1 \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

A systemd unit for the server:

```ini
# /etc/systemd/system/leuffen-rmm.service
[Unit]
Description=Leuffen RMM Server
After=network-online.target

[Service]
WorkingDirectory=/opt/leuffen-rmm/server
Environment=RMM_API_KEY=change-me
Environment=RMM_DB_PATH=/var/lib/leuffen-rmm/rmm.db
ExecStart=/opt/leuffen-rmm/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Installing the agent

Open the **Downloads** tab in an organisation. The agent is a single,
self-contained download with the **server URL + that org's enrollment key baked
in**, so it connects and registers with no manual configuration and lands in its
default OS group.

```bash
# Linux (one-liner)
curl -fsSL http://YOUR_SERVER/api/orgs/<org>/install.sh | sudo bash
```

```powershell
# Windows (PowerShell, as admin)
iwr http://YOUR_SERVER/api/orgs/<org>/install.ps1 -UseBasicParsing | iex
```

Manual: download `…/api/orgs/<org>/agent.zip`, then `python agent.py` (the bundled
`rmm_config.json` carries the connection settings). The agent needs Python 3 with
`psutil` and `websockets`; screen control additionally uses `mss`, `Pillow`,
`pynput` (installed from `agent/requirements.txt`, imported only when used).

### Agent in Docker (Linux hosts)

```bash
docker run -d --name leuffen-agent --pid=host --network=host \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e RMM_SERVER_URL=http://YOUR_SERVER -e RMM_API_KEY=<org-enroll-key> \
  leuffen-rmm-agent
```

`--pid=host` + `--network=host` give the agent host visibility and let a **node**
broadcast Wake-on-LAN. With the Docker socket mounted, the agent also reports the
host's running containers.

---

## Wake-on-LAN across networks

Wake-on-LAN is a layer-2 broadcast and cannot cross subnets on its own. Install
the agent on a machine in the target LAN and **Promote it to a node** (device
drawer → Actions). Add the LAN's **subnets/VLANs (CIDR)**; the server then sends
the magic packet *through that node* onto the right broadcast address — so the
server itself never needs to be on the target LAN. Nodes also **scan** their
subnets and report discovered hosts (IP/MAC/hostname/manufacturer) under
**Network**, where wake-able hosts get a Wake button.

---

## Office 365 SSO & Graph mail setup

1. **App registration** (Entra admin center → App registrations):
   - Redirect URI (Web): `https://YOUR_SERVER/auth/callback`
   - Note the **Application (client) ID** and **Directory (tenant) ID**.
   - Create a **client secret**.
   - API permissions → **Microsoft Graph → Application → `Mail.Send`** → *Grant
     admin consent* (used to send alert email from a service mailbox).
2. **Server config** (env / compose):

   | Variable | Purpose |
   |---|---|
   | `MS_TENANT_ID` | Your tenant id (single-tenant ⇒ only your org can sign in) |
   | `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | App registration credentials |
   | `MS_REDIRECT_URI` | `https://YOUR_SERVER/auth/callback` |
   | `RMM_BOOTSTRAP_ADMIN` | Comma-separated emails granted global admin |
   | `GRAPH_SENDER` | Service mailbox to send alerts from |
   | `SESSION_SECRET` | Random secret for signing session cookies |

   When `MS_CLIENT_ID` is set, real SSO is enforced; otherwise dev-auth mode is
   used. Set `RMM_DEV_AUTH=1` to force dev mode.

### Monitors

There's no built-in alerting policy — every check is a monitor you add yourself
from the **Monitors** tab, and every monitor can be scoped either to one site or
**globally** (applies fleet-wide, manageable only by global admins). Two kinds:

- **Template rules** — pick a template (low disk space, high CPU, high memory,
  device offline) from the gallery, tweak its threshold/sustained-duration/target,
  and it's added as an ordinary rule. Internally this is just a row in
  `monitor_rules`; a background loop evaluates every device against its
  effective rules (site-scoped + global) on `RMM_ALERT_INTERVAL`, and a
  per-device/per-rule state machine emails once on raise and once on clear.
- **Script policies** — run any of your own scripts on a schedule, alert on a
  non-zero exit, and optionally auto-run a remediation script. Same site/global
  scoping applies.

Every monitor/rule has a **severity** (info / warning / critical, shown as a
badge wherever it alerts) and a per-monitor **email notifications** toggle —
turn it off to keep tracking state without sending mail. Edit any monitor or
rule at any time (its scope stays fixed — delete and recreate to move it
between site and global); delete or disable it just as freely. Nothing is
hardcoded or seeded by environment variables.

---

## Configuration reference

All server settings can be entered in the **first-run setup wizard** (saved to the
DB) instead of via environment variables. The wizard is shown until completed;
setting `SESSION_SECRET` in the environment (or `RMM_SKIP_SETUP=1`) bypasses it.
Explicit environment variables always take precedence over wizard-saved values.

**Server**

| Variable | Default | Notes |
|---|---|---|
| `RMM_API_KEY` | *(random)* | Enrollment key for the seeded *Default* org |
| `RMM_SKIP_SETUP` | `0` | Skip the first-run setup wizard |
| `RMM_AUTH_MODE` | `hybrid` | Sign-in: `hybrid` (local + optional M365, default) \| `dev` |
| `RMM_LOGIN_MAX_FAILS` | `5` | Failed local logins (per IP+user) before a temporary lock |
| `RMM_LOGIN_WINDOW` | `300` | Rate-limit window / lock duration (seconds) |
| `RMM_PUBLIC_URL` | `https://localhost:8000` | Baked into agent downloads / SSO |
| `RMM_TLS_MODE` | `self-signed` | `self-signed` \| `file` \| `proxy` |
| `RMM_TLS_CERT` / `RMM_TLS_KEY` | `<data>/tls/*` | Cert/key paths (self-signed/file) |
| `RMM_HOST` / `RMM_PORT` | `0.0.0.0` / `8000` | Bind address/port |
| `RMM_DB_PATH` | `server/data/rmm.db` | SQLite location (mount a volume) |
| `RMM_OFFLINE_AFTER` | `120` | Seconds before a device is "offline" |
| `RMM_METRIC_RETENTION` | `604800` | Metric retention (seconds) |
| `RMM_ALERT_INTERVAL` | `60` | Alert evaluation interval (seconds) |
| `MS_*`, `GRAPH_SENDER`, `SESSION_SECRET`, `RMM_BOOTSTRAP_ADMIN` | — | SSO / mail |

**Agent**

| Variable | Default | Notes |
|---|---|---|
| `RMM_SERVER_URL` | — | e.g. `http://server:8000` (also from `rmm_config.json`) |
| `RMM_API_KEY` | — | Org enrollment key (also from `rmm_config.json`) |
| `RMM_INTERVAL` | `30` | Metric report interval (seconds) |

---

## HTTPS / TLS

The server speaks **HTTPS by default**. Choose a mode with `RMM_TLS_MODE`:

| Mode | What it does | Use when |
|---|---|---|
| `self-signed` *(default)* | Generates a self-signed cert into the data volume on first boot and serves HTTPS. | Quick start, internal/LAN use. Browsers warn once; agents are auto-configured to trust it. |
| `file` | Serves HTTPS using your own `RMM_TLS_CERT` / `RMM_TLS_KEY`. | You already have a real cert (e.g. from **Let's Encrypt**/certbot, or your CA). |
| `proxy` | Serves plain HTTP and trusts `X-Forwarded-*`. | Behind a **reverse proxy** (Caddy/nginx/Traefik) that terminates TLS. |

### Self-signed (default)
Nothing to do — just open `https://YOUR_SERVER:8000`. Agents installed from this
server are configured with `RMM_INSECURE_TLS=1` so they accept the self-signed
cert; everything between agent and server is still encrypted.

### Let's Encrypt — automatic, via the bundled Caddy proxy
A ready-made deployment in `deploy/` uses **Caddy** to obtain and auto-renew a
Let's Encrypt certificate and reverse-proxy to the server (which runs in `proxy`
mode):

```bash
cd deploy
export RMM_DOMAIN=rmm.example.com      # must resolve to this host; ports 80+443 open
docker compose -f docker-compose.letsencrypt.yml up --build -d
```

Caddy fetches the cert on first request and renews it automatically. Agents
enrolled from this server verify the real certificate normally.

### Let's Encrypt — bring your own cert files
If you run certbot yourself, mount the files and use `file` mode:

```yaml
environment:
  RMM_TLS_MODE: "file"
  RMM_TLS_CERT: "/certs/fullchain.pem"
  RMM_TLS_KEY:  "/certs/privkey.pem"
volumes:
  - /etc/letsencrypt/live/rmm.example.com:/certs:ro
```

### Behind your own reverse proxy
Set `RMM_TLS_MODE=proxy` and have nginx/Traefik/Caddy terminate TLS and forward
to the server on port 8000 with `X-Forwarded-Proto`/`X-Forwarded-For` headers.

> Always set `RMM_PUBLIC_URL` (and `MS_REDIRECT_URI` for SSO) to the **https://**
> URL clients actually use, so agent downloads and SSO redirects are correct.

---

## Security notes

SSO protects the dashboard, REST and dashboard WebSockets, and scopes users to
their orgs by role. Agents authenticate with a **per-org enrollment key** — keep
each secret and rotatable. Remote shell/screen/files are powerful: run behind a
reverse proxy with **TLS**, and don't expose enrollment keys publicly. The Graph
permission is least-privilege (`Mail.Send` only).

---

## Roadmap

- **Phase 1 (done):** monitoring, inventory, two-level dashboards, remote control,
  Wake-on-LAN + nodes + discovery, orgs/groups/standards, SSO + alert mail,
  Docker + native Linux, container monitoring.
- **Phase 2:** monitors library + auto-response; scripts/components + scheduled
  jobs with history; compliance baseline evaluation; installed-software audit;
  patch management (scan/report/scheduled apply); scheduled & on-demand reports.
- **Phase 3 (optional):** user-configurable dashboard widgets.

The full design lives in the approved implementation plan.

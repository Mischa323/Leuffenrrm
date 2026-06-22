"""Leuffen RMM server — FastAPI application.

Wires together: Office 365 SSO (with a dev fallback), the agent control
WebSocket, dashboard REST + interactive WebSocket bridges (terminal/screen),
Wake-on-LAN (direct + via relay nodes), network discovery, multi-org/group
management, and the self-contained agent download.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import time

from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerts, auth, database as db, graph, mailer, totp
from . import wol as wol_local
from .manager import manager
from .models import (AccessGroupMemberRequest, AccessGroupOrgRequest, AccessGroupPermRequest,
                     AccessGroupRequest, GroupRequest, InviteRequest, MonitorRequest,
                     MonitorRuleRequest, MoveDeviceRequest, MoveOrgRequest, OrgRequest,
                     OrgUserRequest, PowerRequest, ScheduleRequest, ScriptFileRequest,
                     ScriptRequest, ScriptRunRequest, ShellRequest, SubnetRequest,
                     UserUpdateRequest, WakeRequest)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rmm")

def _resolve_version() -> str:
    """Server version, resolved automatically at startup (first hit wins):

    1. ``RMM_VERSION`` env var — injected at deploy (compose/Dockge build arg).
    2. A ``VERSION`` file baked next to the app or at the repo root.
    3. ``git describe`` at runtime — for native/source installs.
    4. A static fallback.
    """
    env = os.environ.get("RMM_VERSION", "").strip()
    if env:
        return env[1:] if env[:1] == "v" else env
    here = os.path.dirname(__file__)
    for path in (os.path.join(here, "VERSION"), os.path.join(here, "..", "VERSION"),
                 os.path.join(here, "..", "..", "VERSION")):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    try:
        import subprocess
        v = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"],
                           cwd=here, capture_output=True, text=True, timeout=3)
        if v.returncode == 0 and v.stdout.strip():
            return v.stdout.strip().lstrip("v")
    except Exception:
        pass
    return "1.1.6"


SERVER_VERSION = _resolve_version()


def _resolve_agent_version() -> str:
    """The agent's version, read from the agent source so the server never holds
    a duplicated constant (the cause of past drift). The agent payload is baked
    into the image at /agent; fall back to the repo tree for source runs."""
    import re
    here = os.path.dirname(__file__)
    for path in ("/agent/inventory.py",
                 os.path.join(here, "..", "..", "agent", "inventory.py")):
        try:
            with open(path) as f:
                m = re.search(r'AGENT_VERSION\s*=\s*"([^"]+)"', f.read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return "1.1.9"


AGENT_VERSION = _resolve_agent_version()


class _RingLogHandler(logging.Handler):
    """Keep the most recent log records in memory so the Settings → Logs view can
    show them without a filesystem dependency (works the same in Docker)."""

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        from collections import deque
        self.records: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({"t": record.created, "level": record.levelname,
                                 "name": record.name, "msg": record.getMessage()})
        except Exception:
            pass


_ring_log = _RingLogHandler()


def _install_ring_log() -> None:
    """Attach the ring buffer to the root + uvicorn loggers (idempotent)."""
    targets = ["", "uvicorn", "uvicorn.error", "uvicorn.access"]
    for name in targets:
        lg = logging.getLogger(name)
        if not any(isinstance(h, _RingLogHandler) for h in lg.handlers):
            lg.addHandler(_ring_log)


_install_ring_log()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _serve_html(filename: str) -> HTMLResponse:
    """Read an HTML file and append ?v=<SERVER_VERSION> to all local asset URLs
    so browsers automatically pick up new files after a server upgrade."""
    with open(os.path.join(STATIC_DIR, filename)) as f:
        html = f.read()
    v = SERVER_VERSION
    for ext in (".js", ".css"):
        html = html.replace(f'{ext}"', f'{ext}?v={v}"')
    return HTMLResponse(html)
AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agent")
OFFLINE_AFTER = float(os.environ.get("RMM_OFFLINE_AFTER", "120"))
METRIC_RETENTION = float(os.environ.get("RMM_METRIC_RETENTION", str(30 * 24 * 3600)))
ALERT_INTERVAL = float(os.environ.get("RMM_ALERT_INTERVAL", "60"))
# TLS mode the process booted with (used to decide if a restart is needed after
# the setup wizard changes it). Read live values via the helpers below.
BOOT_TLS_MODE = os.environ.get("RMM_TLS_MODE", "self-signed").lower()


def public_url() -> str:
    """Public base URL, read live so the setup wizard applies without a restart."""
    return os.environ.get("RMM_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def agent_insecure_tls() -> bool:
    """Agents skip TLS verification only against a self-signed server cert."""
    mode = os.environ.get("RMM_TLS_MODE", "self-signed").lower()
    return mode in ("self-signed", "self_signed", "selfsigned")


def require_approval() -> bool:
    """Whether brand-new devices land in the approval queue (default: yes)."""
    return os.environ.get("RMM_REQUIRE_APPROVAL", "1").lower() in ("1", "true", "yes")



app = FastAPI(title="Leuffen RMM", version=SERVER_VERSION)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _resolve_setup_state()
    _ensure_default_org()
    _seed_device_secret_default()
    asyncio.create_task(_alert_loop())
    asyncio.create_task(_prune_loop())
    asyncio.create_task(_schedule_loop())
    asyncio.create_task(_auto_update_loop())


def _resolve_setup_state() -> None:
    """Decide whether the first-run wizard is still needed.

    Skip it when the operator clearly configured the server via the environment
    (an explicit SESSION_SECRET) or set RMM_SKIP_SETUP — preserving the pure-env
    workflow and not forcing the wizard on existing deployments.
    """
    if db.setup_complete():
        return
    env_configured = bool(os.environ.get("SESSION_SECRET")) or \
        os.environ.get("RMM_SKIP_SETUP", "").lower() in ("1", "true", "yes")
    if env_configured:
        db.set_setting("SETUP_COMPLETE", "1")


def _ensure_default_org() -> None:
    """Seed a 'Default' org as enrolment target (idempotent)."""
    if db.list_orgs():
        return
    key = os.environ.get("RMM_API_KEY") or None  # None → DB generates a random key
    org = db.create_org("Default", enroll_key=key, org_id="default")
    for admin in auth.BOOTSTRAP_ADMINS or {auth.DEV_USER}:
        db.add_org_user(org["id"], admin, "admin")
    log.info("Seeded default organisation 'Default'")


def _require_device_secret() -> bool:
    """Whether to reject agents that present no per-device secret.

    Configurable from Settings → Security (stored in the DB) or via the
    ``RMM_REQUIRE_DEVICE_SECRET`` env var, which takes precedence. Read live on
    each connect so the toggle applies without a restart."""
    val = os.environ.get("RMM_REQUIRE_DEVICE_SECRET")
    if val is None:
        val = db.get_setting("RMM_REQUIRE_DEVICE_SECRET") or ""
    return val.strip().lower() in ("1", "true", "yes")


def _seed_device_secret_default() -> None:
    """New installs require the per-device secret by default; existing fleets opt in.

    A new install is detected by the absence of any enrolled device, so upgrading a
    server that may still run legacy (pre-secret) agents is never locked out — the
    admin enables it from Settings there. Skipped if it's already configured via env
    or a previous save."""
    if os.environ.get("RMM_REQUIRE_DEVICE_SECRET") is not None:
        return
    if db.get_setting("RMM_REQUIRE_DEVICE_SECRET") is not None:
        return
    if db.all_devices():
        return  # existing install — don't risk rejecting not-yet-updated agents
    db.set_setting("RMM_REQUIRE_DEVICE_SECRET", "1")
    os.environ["RMM_REQUIRE_DEVICE_SECRET"] = "1"
    log.info("New install: requiring per-device secret by default")


async def _alert_loop() -> None:
    while True:
        await asyncio.sleep(ALERT_INTERVAL)
        try:
            alerts.evaluate_once()
        except Exception as exc:  # pragma: no cover
            log.warning("alert loop error: %s", exc)


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            db.prune_metrics(METRIC_RETENTION)
            db.prune_incidents()
        except Exception:  # pragma: no cover
            pass


async def _schedule_loop() -> None:
    """Run due scheduled jobs and monitoring policies, then compute their next run."""
    while True:
        await asyncio.sleep(30)
        try:
            for sched in db.due_schedules():
                try:
                    await _execute_schedule(sched)
                except Exception as exc:
                    log.warning("schedule %s failed: %s", sched["id"], exc)
                db.mark_schedule_ran(sched["id"], _next_run(sched))
            for mon in db.due_monitors():
                old = mon.get("last_status")
                try:
                    status = await _execute_monitor(mon)
                except Exception as exc:
                    status = "error"
                    log.warning("monitor %s failed: %s", mon["id"], exc)
                _monitor_notify(mon, old, status)
                db.mark_monitor_ran(mon["id"], _next_run(mon), status)
        except Exception as exc:  # pragma: no cover
            log.warning("schedule loop error: %s", exc)


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
def _sso_redirect() -> RedirectResponse:
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(auth.login_url(state))
    resp.set_cookie("oauth_state", state, httponly=True, max_age=600,
                    samesite="lax", secure=auth.SECURE_COOKIES)
    return resp


@app.get("/auth/login")
def auth_login(request: Request):
    if auth.AUTH_MODE == "dev":
        return RedirectResponse("/")
    # SSO-only: go straight to Microsoft. Otherwise show the sign-in page
    # (local form, plus a Microsoft button in hybrid mode).
    if auth.SSO_ENABLED and not auth.LOCAL_ENABLED:
        return _sso_redirect()
    return _serve_html("login.html")


@app.get("/auth/sso")
def auth_sso():
    if not auth.SSO_ENABLED:
        raise HTTPException(status_code=404, detail="SSO is not enabled")
    return _sso_redirect()


@app.get("/api/auth/config")
def auth_config():
    return {"mode": auth.AUTH_MODE, "local": auth.LOCAL_ENABLED, "sso": auth.SSO_ENABLED}


# --- Login rate limiting (in-memory; single-process server) ------------------ #
LOGIN_MAX_FAILS = int(os.environ.get("RMM_LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW = float(os.environ.get("RMM_LOGIN_WINDOW", "300"))  # seconds
_login_fails: dict[str, list[float]] = {}


def _login_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{ip}|{(username or '').lower()}"


def _login_throttled(key: str) -> bool:
    now = time.time()
    arr = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW]
    _login_fails[key] = arr
    return len(arr) >= LOGIN_MAX_FAILS


def _record_login_fail(key: str) -> None:
    _login_fails.setdefault(key, []).append(time.time())


@app.post("/api/auth/local-login")
async def local_login(request: Request):
    if not auth.LOCAL_ENABLED:
        raise HTTPException(status_code=403, detail="Local sign-in is disabled")
    data = await request.json()
    username = (data.get("username") or "").strip()
    key = _login_key(request, username)
    if _login_throttled(key):
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Wait a few minutes and try again.")
    try:
        u = auth.verify_local(username, data.get("password") or "")
    except HTTPException:
        _record_login_fail(key)
        raise
    # Second factor (TOTP, or a single-use recovery code) if enabled.
    if u.get("totp_enabled"):
        code = (data.get("code") or "").strip()
        if not code:
            from fastapi.responses import JSONResponse
            return JSONResponse({"mfa_required": True}, status_code=200)
        ok = totp.verify(u.get("totp_secret") or "", code) or \
            db.consume_recovery_code(u["username"], code)
        if not ok:
            _record_login_fail(key)
            raise HTTPException(status_code=401, detail="Invalid authentication or recovery code")
    _login_fails.pop(key, None)
    db.touch_user(u["username"])
    resp = Response(status_code=204)
    resp.set_cookie(auth.COOKIE, auth.make_cookie(u["username"]), httponly=True,
                    samesite="lax", secure=auth.SECURE_COOKIES)
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if request.cookies.get("oauth_state") != state:
        raise HTTPException(status_code=400, detail="state mismatch")
    email = auth.exchange_code(code)
    if not auth.sso_permitted(email):
        return HTMLResponse(_sso_denied_page(email), status_code=403)
    # In hybrid mode, fold the SSO user onto a matching local account (by email).
    identity = auth.resolve_sso_identity(email)
    resp = RedirectResponse("/")
    resp.set_cookie(auth.COOKIE, auth.make_cookie(identity), httponly=True,
                    samesite="lax", secure=auth.SECURE_COOKIES)
    resp.delete_cookie("oauth_state")
    return resp


def _sso_denied_page(email: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Access denied — Leuffen RMM</title>
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/styles.css"/>
</head><body>
<div style="min-height:100vh;display:grid;place-items:center;padding:24px">
  <div style="width:100%;max-width:420px;background:var(--surface);border:1px solid var(--border);
              border-radius:var(--r-xl);box-shadow:var(--shadow-lg);padding:32px;text-align:center">
    <div style="width:56px;height:56px;border-radius:50%;background:color-mix(in srgb,var(--bad) 15%,transparent);
                display:inline-grid;place-items:center;margin-bottom:20px">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--bad)" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>
        <line x1="9" y1="9" x2="15" y2="15"/>
      </svg>
    </div>
    <h2 style="margin:0 0 8px;font-size:20px">Access denied</h2>
    <p style="color:var(--text-dim);font-size:14px;margin:0 0 24px">
      <strong style="color:var(--text)">{email}</strong> is not authorised to sign in.<br>
      Contact your administrator to be invited.
    </p>
    <a href="/auth/login" style="display:inline-block;padding:10px 24px;background:var(--accent);
       color:#fff;border-radius:var(--r-md);text-decoration:none;font-size:14px;font-weight:600">
      Back to sign in
    </a>
  </div>
</div>
</body></html>"""


@app.get("/auth/logout")
def auth_logout():
    resp = RedirectResponse("/auth/login")
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/me")
def me(user: dict = Depends(auth.current_user)):
    orgs = db.orgs_for_user(user["email"], user["is_global_admin"])
    return {"email": user["email"], "is_global_admin": user["is_global_admin"],
            "orgs": [{"id": o["id"], "name": o["name"]} for o in orgs]}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _device_for_user(device_id: str, user: dict) -> dict:
    dev = db.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="Device not found")
    auth.require_org(user, dev["org_id"])
    return dev


def _decorate(dev: dict) -> dict:
    dev["online"] = manager.is_online(dev["id"])
    return dev


# --------------------------------------------------------------------------- #
# Overview (global + per-org)
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def overview(user: dict = Depends(auth.current_user)):
    orgs = db.orgs_for_user(user["email"], user["is_global_admin"])
    online = manager.online_ids()
    out = []
    for o in orgs:
        devs = db.list_devices(o["id"])
        on = sum(1 for d in devs if d["id"] in online)
        noncompliant = sum(1 for d in devs if d.get("compliant") == 0)
        out.append({"id": o["id"], "name": o["name"], "devices": len(devs),
                    "online": on, "offline": len(devs) - on,
                    "noncompliant": noncompliant, "members": db.org_member_count(o["id"])})
    return {"orgs": out,
            "totals": {"devices": sum(o["devices"] for o in out),
                       "online": sum(o["online"] for o in out)}}


# --------------------------------------------------------------------------- #
# Configurable dashboard widgets (per user)
# --------------------------------------------------------------------------- #
DASHBOARD_WIDGETS = [
    {"id": "totals", "title": "Fleet totals", "desc": "Org, device and online counts"},
    {"id": "orgs", "title": "Organisations", "desc": "Per-organisation health cards"},
    {"id": "attention", "title": "Needs attention", "desc": "Offline & non-compliant devices"},
    {"id": "approvals", "title": "Pending approvals", "desc": "Devices awaiting approval"},
    {"id": "disk", "title": "Storage pressure", "desc": "Devices low on disk space"},
    {"id": "monitors", "title": "Monitor alerts", "desc": "Monitors currently in alert"},
    {"id": "versions", "title": "Agent versions", "desc": "Agent version spread across the fleet"},
]
_DEFAULT_ENABLED = {"totals", "orgs", "attention", "approvals"}


def _default_layout() -> list[dict]:
    return [{"id": w["id"], "enabled": w["id"] in _DEFAULT_ENABLED} for w in DASHBOARD_WIDGETS]


def _normalise_layout(layout: list | None) -> list[dict]:
    """Keep only known widgets, in saved order, then append any new ones (off)."""
    known = {w["id"] for w in DASHBOARD_WIDGETS}
    out, seen = [], set()
    for w in (layout or []):
        wid = w.get("id")
        if wid in known and wid not in seen:
            out.append({"id": wid, "enabled": bool(w.get("enabled"))})
            seen.add(wid)
    for w in DASHBOARD_WIDGETS:
        if w["id"] not in seen:
            out.append({"id": w["id"], "enabled": w["id"] in _DEFAULT_ENABLED})
    return out


def _dashboard_data(user: dict) -> dict:
    import json as _json
    orgs = db.orgs_for_user(user["email"], user["is_global_admin"])
    online = manager.online_ids()
    totals = {"orgs": len(orgs), "devices": 0, "online": 0, "offline": 0, "noncompliant": 0}
    org_cards, attention, disk, pending, mon_alerts = [], [], [], [], []
    seen_monitor_ids: set[str] = set()
    versions: dict[str, int] = {}
    for o in orgs:
        devs = db.list_devices(o["id"])
        on = sum(1 for d in devs if d["id"] in online)
        nc = sum(1 for d in devs if d.get("compliant") == 0)
        org_cards.append({"id": o["id"], "name": o["name"], "devices": len(devs),
                          "online": on, "offline": len(devs) - on, "noncompliant": nc})
        totals["devices"] += len(devs); totals["online"] += on
        totals["offline"] += len(devs) - on; totals["noncompliant"] += nc
        for d in devs:
            is_on = d["id"] in online
            if not is_on or d.get("compliant") == 0:
                attention.append({"id": d["id"], "hostname": d["hostname"], "org": o["name"],
                                  "org_id": o["id"], "online": is_on, "last_seen": d.get("last_seen"),
                                  "reason": "offline" if not is_on else "non-compliant"})
            try:
                disks = _json.loads(d["disks_json"]) if d.get("disks_json") else []
            except (ValueError, TypeError):
                disks = []
            prim = next((x for x in disks if x.get("primary")), disks[0] if disks else None)
            if prim and (prim.get("percent") or 0) >= 85:
                disk.append({"id": d["id"], "hostname": d["hostname"], "org": o["name"],
                             "org_id": o["id"], "mount": prim.get("mount"),
                             "percent": prim.get("percent")})
            ver = d.get("agent_version") or "unknown"
            versions[ver] = versions.get(ver, 0) + 1
        for m in db.list_monitors(o["id"]):
            if m.get("last_status") == "alert" and m["id"] not in seen_monitor_ids:
                seen_monitor_ids.add(m["id"])
                mon_alerts.append({"id": m["id"], "name": m["name"],
                                   "org": o["name"] if m.get("org_id") else "Global",
                                   "severity": m.get("severity") or "warning"})
        for ra in db.list_raised_rule_alerts(o["id"]):
            mon_alerts.append({"id": ra["rule"], "name": f"{ra['hostname']}: {ra['rule_name']}",
                               "org": o["name"], "severity": ra.get("severity") or "warning"})
        for p in db.list_pending(o["id"]):
            pending.append({"id": p["id"], "hostname": p["hostname"], "org": o["name"],
                            "org_id": o["id"], "os": p.get("os"), "created_at": p.get("created_at")})
    attention.sort(key=lambda x: (x["online"], x["hostname"].lower()))
    disk.sort(key=lambda x: -(x["percent"] or 0))
    _sev_rank = {"critical": 0, "warning": 1, "info": 2}
    mon_alerts.sort(key=lambda x: _sev_rank.get(x.get("severity"), 1))
    return {"totals": totals, "orgs": org_cards, "attention": attention[:10],
            "disk": disk[:10], "monitors": mon_alerts[:10], "approvals": pending[:10],
            "versions": {"counts": versions, "latest": SERVER_VERSION}}


@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(auth.current_user)):
    layout = _normalise_layout(db.get_dashboard_layout(user["email"]) or _default_layout())
    return {"layout": layout, "catalog": DASHBOARD_WIDGETS, "data": _dashboard_data(user)}


@app.put("/api/dashboard")
async def save_dashboard(request: Request, user: dict = Depends(auth.current_user)):
    body = await request.json()
    db.set_dashboard_layout(user["email"], _normalise_layout(body.get("layout")))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Organisations & groups
# --------------------------------------------------------------------------- #
@app.get("/api/orgs")
def list_orgs(user: dict = Depends(auth.current_user)):
    return db.orgs_for_user(user["email"], user["is_global_admin"])


@app.post("/api/orgs")
def create_org(req: OrgRequest, user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    org = db.create_org(req.name)
    db.add_org_user(org["id"], user["email"], "admin")
    return org


@app.delete("/api/orgs/{org_id}")
def delete_org(org_id: str, user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    if not db.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    if len(db.list_orgs()) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the only organisation")
    db.delete_org(org_id)
    return {"status": "deleted"}


@app.post("/api/orgs/{org_id}/users")
def add_user(org_id: str, req: OrgUserRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    db.add_org_user(org_id, req.email, req.role)
    return {"status": "ok"}


@app.get("/api/orgs/{org_id}/pending")
def list_pending(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return [_decorate(d) for d in db.list_pending(org_id)]


@app.get("/api/pending-count")
def pending_count(user: dict = Depends(auth.current_user)):
    """Pending-approval totals across the orgs the user can see (for the header)."""
    orgs = db.orgs_for_user(user["email"], user["is_global_admin"])
    out, total = [], 0
    for o in orgs:
        c = len(db.list_pending(o["id"]))
        if c:
            out.append({"id": o["id"], "name": o["name"], "count": c})
            total += c
    return {"total": total, "orgs": out}


@app.get("/api/orgs/{org_id}/groups")
def list_groups(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_groups(org_id)


@app.post("/api/orgs/{org_id}/groups")
def create_group(org_id: str, req: GroupRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.create_group(org_id, req.name)


@app.get("/api/orgs/{org_id}/tokens")
def list_tokens(org_id: str, user: dict = Depends(auth.current_user)):
    """One-time enrolment keys for this org (metadata only — never the secret).

    Each used key is annotated with the device it enrolled, so you can see which
    key belongs to which device."""
    auth.require_org(user, org_id)
    tokens = db.list_enroll_tokens(org_id)
    for t in tokens:
        dev = db.get_device(t["device_id"]) if t.get("device_id") else None
        t["device_hostname"] = dev["hostname"] if dev else None
    return {"tokens": tokens, "insecure_tls": agent_insecure_tls()}


@app.post("/api/orgs/{org_id}/tokens")
def create_token(org_id: str, user: dict = Depends(auth.current_user)):
    """Mint a one-time enrolment key. The plaintext is returned ONCE and never
    stored or shown again (only its hash is kept). Single-use: enrols one device."""
    auth.require_org(user, org_id)
    if not db.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    t = db.create_enroll_token(org_id, label=f"by {user['email']}")
    return {"id": t["id"], "token": t["token"], "insecure_tls": agent_insecure_tls()}


@app.delete("/api/orgs/tokens/{token_id}")
def delete_token(token_id: str, user: dict = Depends(auth.current_user)):
    t = db.get_enroll_token(token_id)
    if not t:
        raise HTTPException(status_code=404, detail="Token not found")
    auth.require_org(user, t["org_id"])
    db.delete_enroll_token(token_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Shareable download-link tokens
# --------------------------------------------------------------------------- #
@app.get("/api/orgs/{org_id}/download-links")
def list_download_links(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return {"links": db.list_download_tokens(org_id)}


class DownloadLinkRequest(BaseModel):
    label: str | None = None
    ttl_days: float = 7


@app.post("/api/orgs/{org_id}/download-links")
def create_download_link(org_id: str, body: DownloadLinkRequest,
                         user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    ttl = max(0.5, min(body.ttl_days, 90))
    result = db.create_download_token(org_id, label=body.label, ttl_days=ttl)
    pub = public_url()
    result["msi_url"] = f"{pub}/api/orgs/{org_id}/install.msi?token={result['token']}"
    return result


@app.delete("/api/orgs/{org_id}/download-links/{link_id}")
def delete_download_link(org_id: str, link_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    db.delete_enroll_token(link_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #
@app.get("/api/orgs/{org_id}/devices")
def list_devices(org_id: str, group_id: str | None = None,
                 user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return [_decorate(d) for d in db.list_devices(org_id, group_id)]


@app.get("/api/devices/{device_id}")
def get_device(device_id: str, user: dict = Depends(auth.current_user)):
    dev = _decorate(_device_for_user(device_id, user))
    dev["policies"] = _effective_policies(dev)
    return dev


_METRIC_RANGES = {"24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600}


@app.get("/api/devices/{device_id}/metrics")
def device_metrics(device_id: str, limit: int = 200, range: str | None = Query(None),
                   user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    if range:
        secs = _METRIC_RANGES.get(range)
        if not secs:
            raise HTTPException(status_code=400, detail="range must be 24h, 7d or 30d")
        return db.get_metrics_series(device_id, time.time() - secs, points=120)
    return db.get_metrics(device_id, limit=limit)


def _alert_detail(a: dict) -> str:
    metric = a.get("metric")
    if metric == "offline":
        return f"No heartbeat for {int(a.get('threshold') or 0)}s"
    if metric == "wol":
        return "Wake-on-LAN policy"
    return (f"{(metric or '').replace('_percent', '')} ≥ {float(a.get('threshold') or 0):.0f}% "
            f"for {float(a.get('duration_minutes') or 0):.0f} min")


@app.get("/api/devices/{device_id}/incidents")
def device_incidents(device_id: str, user: dict = Depends(auth.current_user)):
    """Policy-issue history for a device: currently raised alerts plus resolved ones."""
    _device_for_user(device_id, user)
    active = []
    for a in db.device_active_alerts(device_id):
        a["detail"] = _alert_detail(a)
        a["opened_at"] = a.pop("since", None)
        active.append(a)
    return {"active": active, "resolved": db.list_incidents(device_id, limit=100)}


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.delete_device(device_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Remote file management (bridged to the agent over its socket)
# --------------------------------------------------------------------------- #
async def _agent_file_op(device_id: str, user: dict, msg: dict, timeout: float = 30.0) -> dict:
    _device_for_user(device_id, user)
    if not manager.is_online(device_id):
        raise HTTPException(status_code=409, detail="Device offline")
    try:
        res = await manager.request(device_id, msg, timeout=timeout)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "File operation failed"))
    return res


@app.get("/api/devices/{device_id}/software")
async def device_software(device_id: str, refresh: bool = Query(False),
                          user: dict = Depends(auth.current_user)):
    """Installed software for a device. Pulls a fresh list from the agent when it's
    online (and caches it); otherwise returns the last cached list."""
    _device_for_user(device_id, user)
    if manager.is_online(device_id):
        try:
            res = await manager.request(device_id, {"type": "software_list"}, timeout=100)
            if res.get("ok"):
                db.set_device_software(device_id, res.get("software", []))
        except Exception:
            pass
    cached = db.get_device_software(device_id)
    return {"software": cached["software"], "collected_at": cached["collected_at"],
            "online": manager.is_online(device_id)}


@app.get("/api/devices/{device_id}/files")
async def files_list(device_id: str, path: str = Query(""),
                     user: dict = Depends(auth.current_user)):
    """List a directory (empty path → drive/root list)."""
    return await _agent_file_op(device_id, user, {"type": "file_list", "path": path})


@app.get("/api/devices/{device_id}/files/size")
async def files_size(device_id: str, path: str = Query(...),
                     user: dict = Depends(auth.current_user)):
    """Compute the total size of a folder tree (bounded server-side)."""
    return await _agent_file_op(device_id, user, {"type": "dir_size", "path": path}, timeout=40)


@app.post("/api/devices/{device_id}/files/mkdir")
async def files_mkdir(device_id: str, req: Request, user: dict = Depends(auth.current_user)):
    body = await req.json()
    return await _agent_file_op(device_id, user, {"type": "file_mkdir", "path": body.get("path", "")})


@app.post("/api/devices/{device_id}/files/delete")
async def files_delete(device_id: str, req: Request, user: dict = Depends(auth.current_user)):
    body = await req.json()
    return await _agent_file_op(device_id, user, {"type": "file_delete", "path": body.get("path", "")})


@app.get("/api/devices/{device_id}/files/download")
async def files_download(device_id: str, path: str = Query(...),
                         user: dict = Depends(auth.current_user)):
    import base64
    res = await _agent_file_op(device_id, user, {"type": "file_get", "path": path}, timeout=120)
    try:
        data = base64.b64decode(res.get("data", ""))
    except Exception:
        raise HTTPException(status_code=502, detail="Bad file data from agent")
    name = res.get("name") or os.path.basename(path.rstrip("\\/")) or "download"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


@app.post("/api/devices/{device_id}/files/upload")
async def files_upload(device_id: str, path: str = Query(...), file: UploadFile = File(...),
                       user: dict = Depends(auth.current_user)):
    import base64
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (25 MB max)")
    sep = "\\" if ("\\" in path or (len(path) >= 2 and path[1] == ":")) else "/"
    dest = path.rstrip("\\/") + sep + file.filename
    return await _agent_file_op(device_id, user,
                                {"type": "file_put", "path": dest,
                                 "data": base64.b64encode(raw).decode()}, timeout=120)


@app.post("/api/devices/{device_id}/move")
def move_device(device_id: str, req: MoveDeviceRequest, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.set_device_group(device_id, req.group_id)
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/move-org")
def move_device_org(device_id: str, req: MoveOrgRequest, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)          # access to the current org
    auth.require_org(user, req.org_id)          # and the destination org
    if not db.get_org(req.org_id):
        raise HTTPException(status_code=404, detail="Target organisation not found")
    db.set_device_org(device_id, req.org_id)
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/approve")
def approve_device(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.set_device_approved(device_id, True)
    return {"status": "approved"}


@app.post("/api/devices/{device_id}/reject")
def reject_device(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.delete_device(device_id)
    return {"status": "rejected"}


@app.post("/api/devices/{device_id}/power")
async def power(device_id: str, req: PowerRequest, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    if not manager.is_online(device_id):
        raise HTTPException(status_code=409, detail="Device offline")
    try:
        res = await manager.request(device_id, {"type": "power", "action": req.action})
    except Exception as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    return res


@app.post("/api/devices/{device_id}/shell")
async def shell(device_id: str, req: ShellRequest, user: dict = Depends(auth.current_user)):
    """Run a single command and return its output (non-interactive)."""
    _device_for_user(device_id, user)
    if not manager.is_online(device_id):
        raise HTTPException(status_code=409, detail="Device offline")
    try:
        res = await manager.request(device_id, {"type": "shell_run", "cmd": req.cmd}, timeout=60)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    return res


def _update_message(org_id: str) -> dict:
    """Build the self-update instruction sent to an agent over its socket.

    Carries a short-lived one-time token so the agent can pull the latest
    installer (MSI on Windows, agent.zip on Linux) without a browser session,
    keeping its existing config + device id across the upgrade.
    """
    pub = public_url()
    token = db.create_enroll_token(org_id, label="agent-update", ttl_hours=2,
                                   kind="internal")["token"]
    return {
        "type": "update_agent",
        "server_url": pub,
        "org_id": org_id,
        "insecure_tls": agent_insecure_tls(),
        "msi_url": f"{pub}/api/orgs/{org_id}/install.msi?token={token}",
        "zip_url": f"{pub}/api/orgs/{org_id}/agent.zip?token={token}",
    }


# --------------------------------------------------------------------------- #
# Agent auto-update policy (global default + per-org override).
# --------------------------------------------------------------------------- #
def _ver_tuple(v: str | None) -> tuple:
    """Parse a dotted version into a comparable 4-tuple ('v2.2.13' -> (2,2,13,0))."""
    parts = []
    for part in str(v or "").lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(num) if num else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _agent_outdated(version: str | None) -> bool:
    """True if an agent's version is older than the server's canonical AGENT_VERSION."""
    return _ver_tuple(version) < _ver_tuple(AGENT_VERSION)


def _auto_update_default() -> bool:
    """Global default for agent auto-update (Settings -> Agents)."""
    return os.environ.get("RMM_AUTO_UPDATE_AGENTS", "0").lower() in ("1", "true", "yes")


def _org_auto_update_enabled(org: dict | None) -> bool:
    """Effective auto-update for an org: per-org override wins, else the global default."""
    mode = (org or {}).get("auto_update") or "inherit"
    if mode == "on":
        return True
    if mode == "off":
        return False
    return _auto_update_default()


async def _maybe_auto_update(device_id: str, org: dict, dev: dict) -> None:
    """Push an in-place self-update if the org policy is on and the agent is outdated.
    Best-effort; never raises into the caller."""
    try:
        if not _org_auto_update_enabled(org) or not _agent_outdated(dev.get("agent_version")):
            return
        conn = manager.get(device_id)
        if conn:
            await conn.send(_update_message(org["id"]))
            log.info("auto-update: pushed update to %s (v%s -> v%s)",
                     dev.get("hostname"), dev.get("agent_version") or "?", AGENT_VERSION)
    except Exception as exc:  # pragma: no cover
        log.warning("auto-update push failed for %s: %s", device_id, exc)


async def _auto_update_loop() -> None:
    """Periodically push self-updates to online, outdated agents in orgs whose
    auto-update policy is effectively on. On-connect handles freshly-connected
    agents; this sweep catches always-on devices after a new version ships."""
    interval = float(os.environ.get("RMM_AUTO_UPDATE_INTERVAL", str(6 * 3600)))
    while True:
        await asyncio.sleep(interval)
        try:
            for org in db.list_orgs():
                if not _org_auto_update_enabled(org):
                    continue
                for dev in db.list_devices(org["id"]):
                    if manager.is_online(dev["id"]) and _agent_outdated(dev.get("agent_version")):
                        await _maybe_auto_update(dev["id"], org, dev)
        except Exception as exc:  # pragma: no cover
            log.warning("auto-update loop error: %s", exc)


@app.get("/api/orgs/{org_id}/auto-update")
def get_org_auto_update(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    org = db.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    return {"mode": org.get("auto_update") or "inherit",
            "default": _auto_update_default(),
            "effective": _org_auto_update_enabled(org),
            "agent_version": AGENT_VERSION}


@app.post("/api/orgs/{org_id}/auto-update")
async def set_org_auto_update(org_id: str, request: Request,
                              user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    if not db.get_org(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    data = await request.json()
    mode = str(data.get("mode", "inherit"))
    if mode not in ("inherit", "on", "off"):
        raise HTTPException(status_code=400, detail="mode must be inherit|on|off")
    db.set_org_auto_update(org_id, mode)
    org = db.get_org(org_id)
    # Apply right away to any online, outdated agents if the policy is now on.
    if _org_auto_update_enabled(org):
        for dev in db.list_devices(org_id):
            if manager.is_online(dev["id"]) and _agent_outdated(dev.get("agent_version")):
                await _maybe_auto_update(dev["id"], org, dev)
    return {"mode": mode, "default": _auto_update_default(),
            "effective": _org_auto_update_enabled(org)}


@app.post("/api/devices/{device_id}/update-agent")
async def update_agent_device(device_id: str, user: dict = Depends(auth.current_user)):
    """Tell an online agent to download and apply the latest installer in place."""
    dev = _device_for_user(device_id, user)
    if not manager.is_online(device_id):
        raise HTTPException(status_code=409, detail="Device offline")
    try:
        res = await manager.request(device_id, _update_message(dev["org_id"]), timeout=120)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    return res


@app.post("/api/orgs/{org_id}/update-agents")
async def update_agents_org(org_id: str, user: dict = Depends(auth.current_user)):
    """Push a self-update to every online agent in the organisation that is not already
    on the current server version (best effort)."""
    auth.require_org(user, org_id)
    msg = _update_message(org_id)
    all_online = [d for d in db.list_devices(org_id) if manager.is_online(d["id"])]
    targets = [d["id"] for d in all_online if _agent_outdated(d.get("agent_version"))]
    skipped = len(all_online) - len(targets)
    started = 0
    for did in targets:
        try:
            await manager.get(did).send(msg)
            started += 1
        except Exception:
            pass
    return {"started": started, "online": len(all_online), "skipped": skipped}


# --------------------------------------------------------------------------- #
# Scripts (Phase 2): a per-org library + run-on-device with stored history.
# --------------------------------------------------------------------------- #
@app.get("/api/orgs/{org_id}/scripts")
def list_scripts(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_scripts(org_id)


@app.post("/api/orgs/{org_id}/scripts")
def create_script(org_id: str, req: ScriptRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    if req.shell not in ("shell", "powershell"):
        raise HTTPException(status_code=400, detail="shell must be 'shell' or 'powershell'")
    return db.create_script(org_id, req.name, req.content, req.shell, req.description, req.category)


@app.put("/api/scripts/{script_id}")
def update_script(script_id: str, req: ScriptRequest, user: dict = Depends(auth.current_user)):
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    if req.shell not in ("shell", "powershell"):
        raise HTTPException(status_code=400, detail="shell must be 'shell' or 'powershell'")
    return db.update_script(script_id, req.name, req.content, req.shell,
                            req.description, req.category)


@app.delete("/api/scripts/{script_id}")
def delete_script(script_id: str, user: dict = Depends(auth.current_user)):
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    db.delete_script(script_id)
    return {"status": "deleted"}


MAX_FILE_BYTES = 8 * 1024 * 1024


@app.get("/api/scripts/{script_id}/files")
def list_files(script_id: str, user: dict = Depends(auth.current_user)):
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    return db.list_script_files(script_id)


@app.post("/api/scripts/{script_id}/files")
def upload_file(script_id: str, req: ScriptFileRequest, user: dict = Depends(auth.current_user)):
    import base64
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    name = os.path.basename((req.name or "").strip())
    if not name:
        raise HTTPException(status_code=400, detail="A filename is required")
    try:
        raw = base64.b64decode(req.content_b64 or "", validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 8 MB limit")
    return db.add_script_file(script_id, name, req.content_b64, len(raw))


@app.delete("/api/scripts/files/{file_id}")
def delete_file(file_id: str, user: dict = Depends(auth.current_user)):
    f = db.get_script_file(file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    script = db.get_script(f["script_id"])
    auth.require_org(user, script["org_id"])
    db.delete_script_file(file_id)
    return {"status": "deleted"}


async def _exec_script_on_device(script: dict, device_id: str, timeout: float = 120,
                                 variables: dict | None = None, run_name: str | None = None) -> dict:
    """Run a script on one device with its attached files + variables, recording a run.

    Logged against the *device's* org (not the script's) so a global monitor's
    runs land in each affected organisation's own run history.
    """
    dev = db.get_device(device_id)
    run_org_id = dev["org_id"] if dev else script["org_id"]
    run_id = db.create_run(run_org_id, device_id, run_name or script["name"], script["id"])
    try:
        res = await manager.request(device_id, {
            "type": "script_run", "content": script["content"], "shell": script["shell"],
            "timeout": timeout, "env": variables or {},
            "files": db.files_payload(script["id"])}, timeout=timeout + 10)
    except Exception as exc:
        db.finish_run(run_id, "failed", None, str(exc))
        raise
    code = res.get("code")
    db.finish_run(run_id, "ok" if code == 0 else "failed", code, res.get("output", ""))
    return {"run_id": run_id, "exit_code": code, "output": res.get("output", ""),
            "status": "ok" if code == 0 else "failed"}


@app.post("/api/scripts/{script_id}/run")
async def run_script(script_id: str, req: ScriptRunRequest,
                     user: dict = Depends(auth.current_user)):
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    dev = _device_for_user(req.device_id, user)
    if dev["org_id"] != script["org_id"]:
        raise HTTPException(status_code=400, detail="Device is in a different organisation")
    if not manager.is_online(req.device_id):
        raise HTTPException(status_code=409, detail="Device offline")
    try:
        return await _exec_script_on_device(script, req.device_id, req.timeout)
    except Exception as exc:
        raise HTTPException(status_code=504, detail=str(exc))


@app.get("/api/orgs/{org_id}/runs")
def list_runs(org_id: str, device_id: str | None = None,
              user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_runs(org_id, device_id)


# --------------------------------------------------------------------------- #
# Scheduled jobs (Phase 2): run scripts on a cadence.
# --------------------------------------------------------------------------- #
def _next_run(sched: dict, from_ts: float | None = None) -> float | None:
    """Compute the next run timestamp for a schedule."""
    import datetime
    now = from_ts if from_ts is not None else time.time()
    if sched["trigger"] == "interval":
        mins = max(int(sched.get("interval_minutes") or 0), 1)
        return now + mins * 60
    if sched["trigger"] == "daily" and sched.get("at_time"):
        try:
            hh, mm = (int(x) for x in str(sched["at_time"]).split(":"))
        except ValueError:
            return now + 86400
        base = datetime.datetime.fromtimestamp(now)
        nxt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt.timestamp() <= now:
            nxt += datetime.timedelta(days=1)
        return nxt.timestamp()
    return None


def _schedule_targets(sched: dict) -> list[str]:
    """Resolve a schedule's online target device ids.

    ``org_id`` is None for a global monitor — it targets every organisation's
    devices (always with target_type 'all', enforced at creation).
    """
    online = manager.online_ids()
    org_id = sched.get("org_id")
    if org_id is None:
        return [d["id"] for d in db.all_devices() if d["id"] in online]
    if sched["target_type"] == "device":
        return [sched["target_id"]] if sched["target_id"] in online else []
    devs = db.list_devices(org_id, sched["target_id"] if sched["target_type"] == "group" else None)
    return [d["id"] for d in devs if d["id"] in online]


async def _execute_schedule(sched: dict) -> None:
    script = db.get_script(sched["script_id"])
    if not script:
        return
    for device_id in _schedule_targets(sched):
        try:
            await _exec_script_on_device(script, device_id)
        except Exception as exc:
            log.warning("scheduled run on %s failed: %s", device_id, exc)


@app.get("/api/orgs/{org_id}/schedules")
def list_schedules(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_schedules(org_id)


@app.post("/api/orgs/{org_id}/schedules")
def create_schedule(org_id: str, req: ScheduleRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    script = db.get_script(req.script_id)
    if not script or script["org_id"] != org_id:
        raise HTTPException(status_code=404, detail="Script not found")
    if req.target_type not in ("device", "group", "all"):
        raise HTTPException(status_code=400, detail="Invalid target type")
    if req.trigger == "interval" and not req.interval_minutes:
        raise HTTPException(status_code=400, detail="interval_minutes is required")
    if req.trigger == "daily" and not req.at_time:
        raise HTTPException(status_code=400, detail="at_time is required for a daily schedule")
    if req.trigger not in ("interval", "daily"):
        raise HTTPException(status_code=400, detail="Invalid trigger")
    name = req.name or script["name"]
    nxt = _next_run({"trigger": req.trigger, "interval_minutes": req.interval_minutes,
                     "at_time": req.at_time})
    return db.create_schedule(org_id, req.script_id, name, req.target_type, req.target_id,
                              req.trigger, req.interval_minutes, req.at_time, nxt)


@app.post("/api/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str, user: dict = Depends(auth.current_user)):
    sched = db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    auth.require_org(user, sched["org_id"])
    enabled = not sched["enabled"]
    db.set_schedule_enabled(schedule_id, enabled, _next_run(sched) if enabled else None)
    return {"enabled": enabled}


@app.post("/api/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, user: dict = Depends(auth.current_user)):
    sched = db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    auth.require_org(user, sched["org_id"])
    targets = _schedule_targets(sched)
    await _execute_schedule(sched)
    return {"status": "ran", "devices": len(targets)}


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, user: dict = Depends(auth.current_user)):
    sched = db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schedule not found")
    auth.require_org(user, sched["org_id"])
    db.delete_schedule(schedule_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Monitoring policies (Phase 2): monitor script + auto-remediation + variables.
# --------------------------------------------------------------------------- #
def _monitor_notify(mon: dict, old_status: str | None, new_status: str) -> None:
    """Email the org's alert recipients when a monitor flips to/from alerting.

    A global monitor (``org_id`` None) spans every organisation, so it notifies
    the recipients of every org rather than a separate global list.
    """
    if old_status == new_status:
        return
    if not mon.get("notify_email", 1):
        return
    severity = mon.get("severity") or "warning"
    tag = f"[{severity.upper()}] " if severity != "info" else ""
    if new_status == "alert" and old_status != "alert":
        subject = f"{tag}Monitor alerting: {mon['name']}"
        kind = "bad" if severity in ("critical", "error") else ("info" if severity == "info" else "warn")
        body = mailer.status_block(
            f"Monitor alerting: {mon['name']}",
            f"<p style='margin:0 0 6px'>The monitoring policy <b>{mon['name']}</b> is now <b>alerting</b>.</p>"
            f"<p style='margin:0'>The monitor script exited non-zero on one or more devices"
            + (" and the remediation script was run." if mon.get('remediation_script_id')
               else ".") + "</p>",
            kind)
    elif new_status == "ok" and old_status == "alert":
        subject = f"Monitor resolved: {mon['name']}"
        body = mailer.status_block(
            f"Monitor resolved: {mon['name']}",
            f"<p style='margin:0'>The monitoring policy <b>{mon['name']}</b> is healthy again.</p>",
            "good")
    else:
        return
    org_ids = [mon["org_id"]] if mon.get("org_id") else [o["id"] for o in db.list_orgs()]
    recipients: set[str] = set()
    for oid in org_ids:
        recipients.update(db.alert_config(oid).get("recipients") or [])
    if not recipients:
        recipients = ({e.strip() for e in os.environ.get("RMM_ALERT_RECIPIENTS", "").split(",") if e.strip()}
                      or set(auth.BOOTSTRAP_ADMINS))
    if recipients:
        mailer.send_mail(f"[RMM] {subject}", body, sorted(recipients))


async def _execute_monitor(mon: dict) -> str:
    """Run the monitor script on each online target; on failure run remediation.

    The monitor "fails" (alerts) when its script exits non-zero. Variables are
    passed to both scripts as environment variables; attached files come along.
    Returns 'ok' | 'alert' | 'error'.
    """
    monitor_script = db.get_script(mon["monitor_script_id"])
    if not monitor_script:
        return "error"
    import json as _json
    variables = _json.loads(mon["variables_json"]) if mon.get("variables_json") else {}
    remediation = db.get_script(mon["remediation_script_id"]) if mon.get("remediation_script_id") else None
    overall = "ok"
    for device_id in _schedule_targets(mon):
        try:
            res = await _exec_script_on_device(monitor_script, device_id, variables=variables,
                                               run_name=f"monitor: {mon['name']}")
        except Exception as exc:
            log.warning("monitor run on %s failed: %s", device_id, exc)
            overall = "alert"
            continue
        if res["status"] != "ok":
            overall = "alert"
            if remediation:
                try:
                    await _exec_script_on_device(remediation, device_id, variables=variables,
                                                 run_name=f"remediation: {mon['name']}")
                except Exception as exc:
                    log.warning("remediation on %s failed: %s", device_id, exc)
    return overall


@app.get("/api/orgs/{org_id}/monitors")
def list_monitors(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_monitors(org_id)


VALID_SEVERITIES = ("info", "warning", "critical")


def _validate_severity(severity: str) -> None:
    if severity not in VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid severity")


def _validate_monitor_req(req: MonitorRequest) -> None:
    if req.target_type not in ("device", "group", "all"):
        raise HTTPException(status_code=400, detail="Invalid target type")
    if req.trigger not in ("interval", "daily"):
        raise HTTPException(status_code=400, detail="Invalid trigger")
    if req.trigger == "interval" and not req.interval_minutes:
        raise HTTPException(status_code=400, detail="interval_minutes is required")
    if req.trigger == "daily" and not req.at_time:
        raise HTTPException(status_code=400, detail="at_time is required for a daily monitor")
    _validate_severity(req.severity)


@app.post("/api/orgs/{org_id}/monitors")
def create_monitor(org_id: str, req: MonitorRequest, user: dict = Depends(auth.current_user)):
    import json as _json
    auth.require_org(user, org_id)
    ms = db.get_script(req.monitor_script_id)
    if not ms or ms["org_id"] != org_id:
        raise HTTPException(status_code=404, detail="Monitor script not found")
    if req.remediation_script_id:
        rs = db.get_script(req.remediation_script_id)
        if not rs or rs["org_id"] != org_id:
            raise HTTPException(status_code=404, detail="Remediation script not found")
    _validate_monitor_req(req)
    nxt = _next_run({"trigger": req.trigger, "interval_minutes": req.interval_minutes,
                     "at_time": req.at_time})
    return db.create_monitor(org_id, req.name, req.monitor_script_id, req.remediation_script_id,
                             req.target_type, req.target_id, req.trigger, req.interval_minutes,
                             req.at_time, _json.dumps(req.variables or {}), nxt,
                             req.notify_email, req.severity)


@app.post("/api/monitors/global")
def create_global_monitor(req: MonitorRequest, user: dict = Depends(auth.current_user)):
    """A global monitor runs its script against every organisation's devices.

    Only a global admin can create one; the script just needs to exist (a
    global admin can already see and use scripts from any organisation).
    """
    import json as _json
    auth.require_global(user)
    ms = db.get_script(req.monitor_script_id)
    if not ms:
        raise HTTPException(status_code=404, detail="Monitor script not found")
    if req.remediation_script_id and not db.get_script(req.remediation_script_id):
        raise HTTPException(status_code=404, detail="Remediation script not found")
    if req.target_type != "all":
        raise HTTPException(status_code=400, detail="Global monitors must target all devices")
    _validate_monitor_req(req)
    nxt = _next_run({"trigger": req.trigger, "interval_minutes": req.interval_minutes,
                     "at_time": req.at_time})
    return db.create_monitor(None, req.name, req.monitor_script_id, req.remediation_script_id,
                             "all", None, req.trigger, req.interval_minutes,
                             req.at_time, _json.dumps(req.variables or {}), nxt,
                             req.notify_email, req.severity)


@app.put("/api/monitors/{monitor_id}")
def update_monitor(monitor_id: str, req: MonitorRequest, user: dict = Depends(auth.current_user)):
    """Edit an existing monitor. Scope (site vs. global) is immutable — delete
    and recreate to change it."""
    import json as _json
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_scope(user, mon["org_id"])
    org_id = mon["org_id"]
    ms = db.get_script(req.monitor_script_id)
    if not ms or (org_id is not None and ms["org_id"] != org_id):
        raise HTTPException(status_code=404, detail="Monitor script not found")
    if req.remediation_script_id:
        rs = db.get_script(req.remediation_script_id)
        if not rs or (org_id is not None and rs["org_id"] != org_id):
            raise HTTPException(status_code=404, detail="Remediation script not found")
    if org_id is None and req.target_type != "all":
        raise HTTPException(status_code=400, detail="Global monitors must target all devices")
    _validate_monitor_req(req)
    target_type = req.target_type if org_id is not None else "all"
    target_id = req.target_id if org_id is not None else None
    nxt = _next_run({"trigger": req.trigger, "interval_minutes": req.interval_minutes,
                     "at_time": req.at_time}) if mon["enabled"] else mon.get("next_run")
    return db.update_monitor(monitor_id, req.name, req.monitor_script_id, req.remediation_script_id,
                             target_type, target_id, req.trigger, req.interval_minutes,
                             req.at_time, _json.dumps(req.variables or {}), nxt,
                             req.notify_email, req.severity)


@app.post("/api/monitors/{monitor_id}/toggle")
def toggle_monitor(monitor_id: str, user: dict = Depends(auth.current_user)):
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_scope(user, mon["org_id"])
    enabled = not mon["enabled"]
    db.set_monitor_enabled(monitor_id, enabled, _next_run(mon) if enabled else None)
    return {"enabled": enabled}


@app.post("/api/monitors/{monitor_id}/run")
async def run_monitor_now(monitor_id: str, user: dict = Depends(auth.current_user)):
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_scope(user, mon["org_id"])
    old = mon.get("last_status")
    status = await _execute_monitor(mon)
    _monitor_notify(mon, old, status)
    db.mark_monitor_ran(monitor_id, _next_run(mon) if mon["enabled"] else mon.get("next_run"), status)
    return {"status": status}


@app.delete("/api/monitors/{monitor_id}")
def delete_monitor(monitor_id: str, user: dict = Depends(auth.current_user)):
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_scope(user, mon["org_id"])
    db.delete_monitor(monitor_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Monitor templates + rules: the metric-threshold monitor library (e.g.
# "alert if disk usage stays above 90% for 5 minutes"). Each rule is an
# ordinary, user-managed record — site-scoped or global, just like monitors.
# --------------------------------------------------------------------------- #
MONITOR_TEMPLATES = [
    {"id": "disk", "name": "Low disk space", "category": "Storage", "kind": "monitor",
     "description": "Alert when disk usage stays above the threshold for a sustained period.",
     "metric": "disk_percent", "unit": "%", "default_threshold": 90, "default_duration_minutes": 5,
     "default_severity": "warning", "os_support": None},
    {"id": "cpu", "name": "High CPU usage", "category": "Performance", "kind": "monitor",
     "description": "Alert when CPU usage stays above the threshold for a sustained period.",
     "metric": "cpu_percent", "unit": "%", "default_threshold": 90, "default_duration_minutes": 10,
     "default_severity": "warning", "os_support": None},
    {"id": "mem", "name": "High memory usage", "category": "Performance", "kind": "monitor",
     "description": "Alert when memory usage stays above the threshold for a sustained period.",
     "metric": "mem_percent", "unit": "%", "default_threshold": 90, "default_duration_minutes": 10,
     "default_severity": "warning", "os_support": None},
    {"id": "gpu", "name": "High GPU usage", "category": "Performance", "kind": "monitor",
     "description": "Alert when GPU usage stays above the threshold for a sustained period. "
                    "Reported for NVIDIA GPUs and most Windows/Linux GPUs.",
     "metric": "gpu_percent", "unit": "%", "default_threshold": 90, "default_duration_minutes": 10,
     "default_severity": "warning", "os_support": None},
    {"id": "cpu_temp", "name": "High CPU temperature", "category": "Performance", "kind": "monitor",
     "description": "Alert when CPU temperature stays above the threshold for a sustained period. "
                    "Requires a readable temperature sensor (not available on every machine).",
     "metric": "cpu_temp", "unit": "°C", "default_threshold": 90, "default_duration_minutes": 5,
     "default_severity": "warning", "os_support": None},
    {"id": "gpu_temp", "name": "High GPU temperature", "category": "Performance", "kind": "monitor",
     "description": "Alert when GPU temperature stays above the threshold for a sustained period. "
                    "Reported for NVIDIA GPUs.",
     "metric": "gpu_temp", "unit": "°C", "default_threshold": 90, "default_duration_minutes": 5,
     "default_severity": "warning", "os_support": None},
    {"id": "offline", "name": "Device offline", "category": "Availability", "kind": "monitor",
     "description": "Alert when a device hasn't sent a heartbeat for a while.",
     "metric": "offline", "unit": "s", "default_threshold": 120, "default_duration_minutes": None,
     "default_severity": "critical", "os_support": None},
    {"id": "wol", "name": "Wake-on-LAN", "category": "Power", "kind": "policy",
     "description": "Configure agents' NICs for Wake-on-LAN and disable Fast Startup so they can be "
                    "woken. Windows only.",
     "metric": "wol", "unit": None, "default_threshold": 0, "default_duration_minutes": None,
     "default_severity": "info", "os_support": ["windows", "windows_server"]},
]


def _template(template_id: str) -> dict | None:
    return next((t for t in MONITOR_TEMPLATES if t["id"] == template_id), None)


def _os_supported(template_id: str, os_kind: str | None) -> bool:
    """Whether a template/policy supports a device's OS (None = all OSes)."""
    tmpl = _template(template_id)
    support = tmpl.get("os_support") if tmpl else None
    return support is None or (os_kind in support)


def _device_wol_enabled(dev: dict | None) -> bool:
    """True if an enabled Wake-on-LAN policy targets this (supported) device."""
    if not dev or not _os_supported("wol", dev.get("os_kind")):
        return False
    return any(r["template_id"] == "wol" for r in db.list_effective_monitor_rules(dev))


def _effective_policies(dev: dict) -> list[dict]:
    """All policy/monitor rules that apply to a device, with OS-support status,
    for the device drawer."""
    out = []
    for r in db.list_effective_monitor_rules(dev):
        tmpl = _template(r["template_id"]) or {}
        unit = tmpl.get("unit") or ("s" if r["metric"] == "offline" else "%")
        out.append({"name": r["name"], "template_id": r["template_id"],
                    "kind": tmpl.get("kind", "monitor"), "severity": r.get("severity"),
                    "supported": _os_supported(r["template_id"], dev.get("os_kind")),
                    "value": ("Windows only" if r["template_id"] == "wol"
                              else f"{r['metric']} ≥ {r['threshold']:.0f}{unit}")})
    return out


async def _push_wol_policy() -> None:
    """Re-push the Wake-on-LAN policy to every online agent (after a rule change)."""
    for did in list(manager.online_ids()):
        dev = db.get_device(did)
        conn = manager.get(did)
        if dev and conn:
            try:
                await conn.send({"type": "agent_policy", "enable_wol": _device_wol_enabled(dev)})
            except Exception:
                pass


@app.get("/api/monitor-templates")
def get_monitor_templates(user: dict = Depends(auth.current_user)):
    return MONITOR_TEMPLATES


def _build_monitor_rule(org_id: str | None, req: MonitorRuleRequest) -> dict:
    tmpl = next((t for t in MONITOR_TEMPLATES if t["id"] == req.template_id), None)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Unknown monitor template")
    if req.target_type not in ("device", "group", "all"):
        raise HTTPException(status_code=400, detail="Invalid target type")
    if org_id is None and req.target_type != "all":
        raise HTTPException(status_code=400, detail="Global rules must target all devices")
    severity = req.severity if req.severity is not None else tmpl["default_severity"]
    _validate_severity(severity)
    threshold = req.threshold if req.threshold is not None else tmpl["default_threshold"]
    duration = (req.duration_minutes if req.duration_minutes is not None
                else tmpl["default_duration_minutes"])
    name = (req.name or tmpl["name"]).strip() or tmpl["name"]
    return db.create_monitor_rule(org_id, tmpl["id"], name, tmpl["metric"], threshold, duration,
                                  req.target_type, req.target_id, req.notify_email, severity)


@app.get("/api/orgs/{org_id}/monitor-rules")
def list_monitor_rules(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_monitor_rules(org_id)


@app.post("/api/orgs/{org_id}/monitor-rules")
async def add_monitor_rule(org_id: str, req: MonitorRuleRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    res = _build_monitor_rule(org_id, req)
    if req.template_id == "wol":
        await _push_wol_policy()
    return res


@app.post("/api/monitor-rules/global")
async def add_global_monitor_rule(req: MonitorRuleRequest, user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    res = _build_monitor_rule(None, req)
    if req.template_id == "wol":
        await _push_wol_policy()
    return res


@app.put("/api/monitor-rules/{rule_id}")
def update_monitor_rule(rule_id: str, req: MonitorRuleRequest, user: dict = Depends(auth.current_user)):
    """Edit an existing monitor rule. The template/metric and scope (site vs.
    global) are immutable — delete and recreate to change either."""
    rule = db.get_monitor_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Monitor rule not found")
    auth.require_scope(user, rule["org_id"])
    tmpl = next((t for t in MONITOR_TEMPLATES if t["id"] == rule["template_id"]), None)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Unknown monitor template")
    if req.target_type not in ("device", "group", "all"):
        raise HTTPException(status_code=400, detail="Invalid target type")
    if rule["org_id"] is None and req.target_type != "all":
        raise HTTPException(status_code=400, detail="Global rules must target all devices")
    severity = req.severity if req.severity is not None else tmpl["default_severity"]
    _validate_severity(severity)
    threshold = req.threshold if req.threshold is not None else tmpl["default_threshold"]
    duration = (req.duration_minutes if req.duration_minutes is not None
                else tmpl["default_duration_minutes"])
    name = (req.name or tmpl["name"]).strip() or tmpl["name"]
    return db.update_monitor_rule(rule_id, name, threshold, duration, req.target_type,
                                  req.target_id, req.notify_email, severity)


@app.post("/api/monitor-rules/{rule_id}/toggle")
async def toggle_monitor_rule(rule_id: str, user: dict = Depends(auth.current_user)):
    rule = db.get_monitor_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Monitor rule not found")
    auth.require_scope(user, rule["org_id"])
    enabled = not rule["enabled"]
    db.set_monitor_rule_enabled(rule_id, enabled)
    if rule["template_id"] == "wol":
        await _push_wol_policy()
    return {"enabled": enabled}


@app.delete("/api/monitor-rules/{rule_id}")
async def delete_monitor_rule(rule_id: str, user: dict = Depends(auth.current_user)):
    rule = db.get_monitor_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Monitor rule not found")
    auth.require_scope(user, rule["org_id"])
    db.delete_monitor_rule(rule_id)
    if rule["template_id"] == "wol":
        await _push_wol_policy()
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Wake-on-LAN (direct or via a relay node)
# --------------------------------------------------------------------------- #
def _resolve_wake(org_id: str, target_ip: str | None,
                  node_id: str | None) -> tuple[dict | None, str | None]:
    """Pick the relay node and the directed broadcast to use for a wake.

    The broadcast is the target subnet's directed broadcast (e.g. 192.168.50.255)
    so a node in a *different* VLAN can have the packet routed into the target's
    segment — a limited 255.255.255.255 broadcast would never leave the node's
    own subnet."""
    nodes = [n for n in db.list_nodes(org_id) if manager.is_online(n["id"])]
    forced = None
    if node_id:
        forced = next((n for n in nodes if n["id"] == node_id), None) or db.get_device(node_id)
    if target_ip:
        for n in ([forced] if forced else nodes):
            if not n:
                continue
            for s in (n.get("subnets") or []):
                try:
                    net = ipaddress.ip_network(s["cidr"], False)
                    if ipaddress.ip_address(target_ip) in net:
                        return n, (s.get("broadcast") or str(net.broadcast_address))
                except ValueError:
                    continue
    return (forced or (nodes[0] if nodes else None)), None


async def _wake(org_id: str, mac: str, broadcast: str | None, port: int,
                target_ip: str | None, node_id: str | None) -> dict:
    node, sub_bcast = _resolve_wake(org_id, target_ip, node_id)
    bcast = broadcast or sub_bcast or "255.255.255.255"
    if node and manager.is_online(node["id"]):
        log.info("WoL: sending magic packet for %s to broadcast %s:%s via node %s (%s)",
                 mac, bcast, port, node["hostname"], node["id"])
        res = await manager.request(node["id"], {"type": "wol", "mac": mac,
                                                 "broadcast": bcast, "port": port})
        ok = isinstance(res, dict) and res.get("ok")
        log.info("WoL: node %s %s sending to %s (target %s)", node["hostname"],
                 "confirmed" if ok else f"reported {res}", bcast, target_ip or "?")
        return {"status": "sent", "via": "node", "node": node["hostname"],
                "broadcast": bcast, "mac": mac, "agent_ok": bool(ok)}
    bc = broadcast or "255.255.255.255"
    log.info("WoL: no relay node online for org %s — broadcasting %s to %s from the server",
             org_id, mac, bc)
    wol_local.send_magic_packet(mac, broadcast_ip=bc, port=port)
    return {"status": "sent", "via": "server", "broadcast": bc, "mac": mac}


@app.post("/api/devices/{device_id}/wake")
async def wake_device(device_id: str, req: WakeRequest | None = None,
                      user: dict = Depends(auth.current_user)):
    dev = _device_for_user(device_id, user)
    req = req or WakeRequest()
    mac = req.mac or dev.get("mac")
    if not mac:
        raise HTTPException(status_code=400, detail="No MAC address known for this device")
    try:
        return await _wake(dev["org_id"], mac, req.broadcast_ip, req.port, dev.get("ip"), req.node_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/orgs/{org_id}/network/wake")
async def wake_host(org_id: str, req: WakeRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    if not req.mac:
        raise HTTPException(status_code=400, detail="MAC required")
    try:
        return await _wake(org_id, req.mac, req.broadcast_ip, req.port, None, req.node_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# Nodes & network discovery
# --------------------------------------------------------------------------- #
@app.get("/api/orgs/{org_id}/nodes")
def list_nodes(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return [_decorate(n) for n in db.list_nodes(org_id)]


@app.post("/api/devices/{device_id}/promote")
async def promote(device_id: str, user: dict = Depends(auth.current_user)):
    dev = _device_for_user(device_id, user)
    db.set_node(device_id, True)
    if manager.is_online(device_id):
        subs = [s["cidr"] for s in db.list_subnets(device_id)]
        await manager.get(device_id).send({"type": "set_role", "role": "node", "subnets": subs})
    return {"status": "promoted"}


@app.post("/api/devices/{device_id}/demote")
async def demote(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.set_node(device_id, False)
    if manager.is_online(device_id):
        await manager.get(device_id).send({"type": "set_role", "role": "agent", "subnets": []})
    return {"status": "demoted"}


@app.post("/api/devices/{device_id}/subnets")
async def add_subnet(device_id: str, req: SubnetRequest, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    try:
        net = ipaddress.ip_network(req.cidr, strict=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Bad CIDR: {exc}")
    # Normalise to the network address so 10.0.0.5/24 and 10.0.0.0/24 dedupe.
    cidr = str(net)
    bcast = req.broadcast or str(net.broadcast_address)
    added = db.add_subnet(device_id, cidr, bcast)
    if manager.is_online(device_id):
        subs = [s["cidr"] for s in db.list_subnets(device_id)]
        await manager.get(device_id).send({"type": "set_role", "role": "node", "subnets": subs})
    return {"status": "ok", "cidr": cidr, "broadcast": bcast, "added": added}


@app.delete("/api/subnets/{subnet_id}")
def delete_subnet(subnet_id: int, user: dict = Depends(auth.current_user)):
    db.delete_subnet(subnet_id)
    return {"status": "ok"}


@app.post("/api/devices/{device_id}/scan")
async def scan(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    if not manager.is_online(device_id):
        raise HTTPException(status_code=409, detail="Node offline")
    subs = [s["cidr"] for s in db.list_subnets(device_id)]
    await manager.get(device_id).send({"type": "scan", "subnets": subs})
    return {"status": "scan started", "subnets": subs}


@app.get("/api/orgs/{org_id}/network/hosts")
def network_hosts(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_network_hosts(org_id)


# --------------------------------------------------------------------------- #
# Agent WebSocket
# --------------------------------------------------------------------------- #
@app.websocket("/api/agents/ws")
async def agent_ws(ws: WebSocket, key: str = Query(...)):
    await ws.accept()
    device_id: str | None = None
    try:
        # First message must be a register.
        first = await ws.receive_json()
        if first.get("type") != "register":
            await ws.close(code=4400)
            return
        device_id = first["id"]
        # Already-registered devices reconnect by identity (no key needed even after
        # rotation). New devices must present a valid one-time enrolment token;
        # the legacy reusable org key only works if RMM_LEGACY_ENROLL is on.
        existing = db.get_device(device_id)
        if existing:
            org = db.get_org(existing["org_id"])
        else:
            org_id = db.consume_enroll_token(key, device_id)
            if org_id is None and os.environ.get("RMM_LEGACY_ENROLL", "").lower() in ("1", "true", "yes"):
                o = db.get_org_by_key(key)
                org_id = o["id"] if o else None
            org = db.get_org(org_id) if org_id else None
        if org is None:
            await ws.close(code=4401)
            return
        # Per-device secret: defends reconnect against device_id impersonation.
        # Trust-on-first-use — issue a secret to agents that support it and then
        # require it on later reconnects. Legacy agents (no support) are allowed
        # unless the device-secret requirement is on (Settings → Security, or the
        # RMM_REQUIRE_DEVICE_SECRET env var; default on for new installs).
        import hashlib as _hl, hmac as _hmac, secrets as _secrets
        _dsk = f"devsecret:{device_id}"
        _stored = db.get_setting(_dsk)
        _presented = first.get("device_secret") or ""
        _issue = None
        if _stored:
            if not (_presented and _hmac.compare_digest(
                    _hl.sha256(_presented.encode()).hexdigest(), _stored)):
                log.warning("Agent %s failed device-secret check; rejecting", device_id)
                await ws.close(code=4401)
                return
        elif first.get("supports_secret"):
            _issue = _secrets.token_urlsafe(32)
            db.set_setting(_dsk, _hl.sha256(_issue.encode()).hexdigest())
        elif _require_device_secret():
            await ws.close(code=4401)
            return
        db.upsert_device(org["id"], first, require_approval=require_approval())
        await manager.register(device_id, org["id"], ws)
        if _issue:
            await ws.send_json({"type": "device_secret", "secret": _issue})
        # Push admin-controlled device policy (Wake-on-LAN), per the policies that
        # target this device and whether its OS supports it.
        await ws.send_json({"type": "agent_policy",
                            "enable_wol": _device_wol_enabled(db.get_device(device_id))})
        if db.get_device(device_id).get("is_node"):
            subs = [s["cidr"] for s in db.list_subnets(device_id)]
            await ws.send_json({"type": "set_role", "role": "node", "subnets": subs})
        log.info("Agent connected: %s (org %s)", first.get("hostname"), org["name"])
        # Auto-update policy: if this org keeps agents up to date and the one that
        # just connected is on an older build, push an in-place self-update now.
        await _maybe_auto_update(device_id, org, db.get_device(device_id))

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                # Binary screen frame -> fan out to screen subscribers.
                await manager.fanout(device_id, "screen", msg["bytes"])
                continue
            import json as _json
            data = _json.loads(msg["text"])
            await _handle_agent_msg(device_id, org["id"], data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        log.warning("agent ws error: %s", exc)
    finally:
        if device_id:
            await manager.unregister(device_id)
            log.info("Agent disconnected: %s", device_id)


async def _handle_agent_msg(device_id: str, org_id: str, data: dict) -> None:
    mtype = data.get("type")
    if mtype == "metrics":
        m = data.get("metrics", {})
        db.insert_metric(device_id, m)
        db.touch_device(device_id)
        if "logged_in_user" in m:
            db.set_logged_in_user(device_id, m.get("logged_in_user"))
        if m.get("disks"):
            db.set_device_disks(device_id, m.get("disks"))
        if m.get("hyperv"):
            db.set_device_hyperv(device_id, m.get("hyperv"))
    elif mtype == "ack":
        manager.resolve(data.get("rid", ""), data.get("payload", data))
    elif mtype == "shell_output":
        await manager.fanout(device_id, "terminal", data)
    elif mtype == "screen_error":
        await manager.fanout(device_id, "screen",
                             {"type": "error", "error": data.get("error", "screen capture failed")})
    elif mtype == "scan_result":
        db.upsert_network_hosts(org_id, device_id, data.get("hosts", []))
    elif mtype == "register":
        db.upsert_device(org_id, data)


# --------------------------------------------------------------------------- #
# Dashboard interactive WebSockets (terminal, screen)
# --------------------------------------------------------------------------- #
@app.websocket("/api/devices/{device_id}/terminal")
async def terminal_ws(ws: WebSocket, device_id: str):
    await _bridge_ws(ws, device_id, "terminal")


@app.websocket("/api/devices/{device_id}/screen")
async def screen_ws(ws: WebSocket, device_id: str):
    # ?purpose=screenshot tells the agent this is a one-shot still, so it skips
    # the on-device consent banner (a momentary capture, not an ongoing session).
    purpose = "screenshot" if ws.query_params.get("purpose") == "screenshot" else "control"
    await _bridge_ws(ws, device_id, "screen", purpose=purpose)


async def _bridge_ws(ws: WebSocket, device_id: str, channel: str,
                     purpose: str = "control") -> None:
    # Cookie auth (browser sends it automatically); dev mode is always allowed.
    # Authenticate the operator via the signed session cookie ...
    user = None
    if not auth.DEV_AUTH:
        raw = ws.cookies.get(auth.COOKIE)
        data = auth.read_cookie(raw) if raw else None
        if not data:
            await ws.close(code=4401)
            return
        user = {"email": data["email"],
                "is_global_admin": auth.is_global_admin(data["email"])}
    # ... and AUTHORISE: they must have access to this device's organisation, so a
    # signed-in user can't drive a device in another org by guessing its id.
    dev = db.get_device(device_id)
    if user is not None and dev is not None:
        try:
            auth.require_org(user, dev["org_id"])
        except HTTPException:
            await ws.close(code=4403)
            return
    await ws.accept()
    agent = manager.get(device_id)
    if not agent:
        await ws.send_json({"type": "error", "error": "Device offline"})
        await ws.close()
        return
    manager.subscribe(device_id, channel, ws)
    if channel == "screen":
        await agent.send({"type": "screen_start", "fps": 4, "quality": 50,
                          "purpose": purpose})
    try:
        while True:
            data = await ws.receive_json()
            # Relay dashboard input to the agent.
            if channel == "terminal":
                await agent.send({"type": "shell_input", "data": data.get("data", "")})
            else:
                await agent.send({"type": "input", **data})
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(device_id, channel, ws)
        if channel == "screen" and manager.is_online(device_id):
            await manager.get(device_id).send({"type": "screen_stop"})


# --------------------------------------------------------------------------- #
# Agent download (self-contained, config injected)
# --------------------------------------------------------------------------- #
def _org_from_request(org_id: str, user: dict) -> dict:
    auth.require_org(user, org_id)
    org = db.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    return org


def _installer_key(org_id: str, token: str | None, user: dict | None) -> tuple[dict, str]:
    """Resolve (org, enrolment key) for an installer download.

    A valid one-time ``token`` query authorises the download (so the one-liner
    works on a target machine without a session). Otherwise an authenticated admin
    gets a freshly-minted single-use token baked in.
    """
    org = db.get_org(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if token and db.token_valid_for(org_id, token):
        return org, token
    if user is not None:
        auth.require_org(user, org_id)
        return org, db.create_enroll_token(org_id, label="installer", kind="internal")["token"]
    raise HTTPException(status_code=401,
                        detail="A valid enrolment token (?token=) or an admin session is required")


@app.get("/api/orgs/{org_id}/install.sh", response_class=PlainTextResponse)
def install_sh(org_id: str, token: str | None = Query(None),
               user: dict | None = Depends(auth.optional_user)):
    org, key = _installer_key(org_id, token, user)
    pub = public_url()
    insecure = "1" if agent_insecure_tls() else "0"
    return f"""#!/usr/bin/env bash
set -e
# Leuffen RMM agent installer (Linux)
export RMM_SERVER_URL="{pub}"
export RMM_API_KEY="{key}"
export RMM_INSECURE_TLS="{insecure}"
TMP=$(mktemp -d)
DEST=/opt/leuffen-rmm
mkdir -p "$DEST"
curl -fsSL "{pub}/api/orgs/{org_id}/agent.zip?token={key}" -o "$TMP/agent.zip"
# Extract with Python's stdlib (avoids a hard dependency on the 'unzip' package).
if command -v unzip >/dev/null 2>&1; then
  unzip -o "$TMP/agent.zip" -d "$DEST"
else
  python3 -m zipfile -e "$TMP/agent.zip" "$DEST"
fi
# Install into an isolated virtualenv: avoids needing pip3 on the host and the
# PEP 668 "externally managed environment" block on modern Debian/Ubuntu.
if ! python3 -m venv "$DEST/venv" 2>/dev/null; then
  echo "Installing python3 venv support…"
  if command -v apt-get >/dev/null 2>&1; then apt-get update -y && apt-get install -y python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then dnf install -y python3-virtualenv python3-pip || dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then yum install -y python3-virtualenv python3-pip || true
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3 py3-virtualenv py3-pip || true
  elif command -v zypper >/dev/null 2>&1; then zypper install -y python3-venv python3-pip || true; fi
  python3 -m venv "$DEST/venv"
fi
"$DEST/venv/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
# Core deps only — the screen-control extras (mss/Pillow/pynput) are optional and
# need a desktop, so they're skipped for headless installs.
"$DEST/venv/bin/python" -m pip install psutil websockets
PYBIN="$DEST/venv/bin/python"
cat >/etc/systemd/system/leuffen-rmm.service <<UNIT
[Unit]
Description=Leuffen RMM Agent
After=network-online.target
[Service]
Environment=RMM_SERVER_URL={pub}
Environment=RMM_API_KEY={key}
Environment=RMM_INSECURE_TLS={insecure}
ExecStart=$PYBIN $DEST/agent.py
Nice=10
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now leuffen-rmm
echo "Leuffen RMM agent installed and started."
"""


@app.get("/api/orgs/{org_id}/install.ps1", response_class=PlainTextResponse)
def install_ps1(org_id: str, token: str | None = Query(None),
                user: dict | None = Depends(auth.optional_user)):
    org, key = _installer_key(org_id, token, user)
    pub = public_url()
    insecure = "1" if agent_insecure_tls() else "0"
    return f"""# Leuffen RMM agent installer (Windows)
$ErrorActionPreference = "Stop"
$dest = "$env:ProgramFiles\\LeuffenRMM"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Invoke-WebRequest "{pub}/api/orgs/{org_id}/agent.zip?token={key}" -OutFile "$env:TEMP\\agent.zip"
Expand-Archive -Force "$env:TEMP\\agent.zip" -DestinationPath $dest
pip install -r "$dest\\requirements.txt"
[Environment]::SetEnvironmentVariable("RMM_SERVER_URL", "{pub}", "Machine")
[Environment]::SetEnvironmentVariable("RMM_API_KEY", "{key}", "Machine")
[Environment]::SetEnvironmentVariable("RMM_INSECURE_TLS", "{insecure}", "Machine")
$action = New-ScheduledTaskAction -Execute "python" -Argument "$dest\\agent.py"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "LeuffenRMM" -Action $action -Trigger $trigger -RunLevel Highest -Force
Start-ScheduledTask -TaskName "LeuffenRMM"
Write-Host "Leuffen RMM agent installed."
"""


# The agent (incl. the MSI) is built and released from the dedicated agent repo;
# the server only proxies the latest release. Override with RMM_MSI_URL /
# RMM_GH_REPO if you fork or self-host the agent build.
MSI_URL = os.environ.get(
    "RMM_MSI_URL",
    "https://github.com/Mischa323/leuffen-rmm-agent/releases/latest/download/leuffen-rmm-agent.msi")
GH_REPO = os.environ.get("RMM_GH_REPO", "Mischa323/leuffen-rmm-agent")
_release_cache: dict = {"t": 0.0, "data": None}


@app.get("/api/agent-release")
async def agent_release(user: dict = Depends(auth.current_user)):
    """Latest published Windows agent build (name/version, size, date) for the UI."""
    if not (_release_cache["data"] and time.time() - _release_cache["t"] < 60):
        import httpx
        out = {"available": False, "agent_version": AGENT_VERSION}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"https://api.github.com/repos/{GH_REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                d = r.json()
                asset = next((a for a in d.get("assets", []) if a["name"].endswith(".msi")), None)
                out = {"available": bool(asset), "agent_version": AGENT_VERSION,
                       "name": d.get("name") or d.get("tag_name"),
                       "tag": d.get("tag_name"), "published_at": d.get("published_at"),
                       "size": asset["size"] if asset else None}
        except Exception:
            pass
        _release_cache.update(t=time.time(), data=out)
       # Always report the current agent version (re-resolved each request) so the
    # UI's "Latest" never goes stale between releases, even without a restart.
    data = dict(_release_cache["data"] or {})
    data["agent_version"] = _resolve_agent_version()
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/api/orgs/{org_id}/install.msi")
async def install_msi(org_id: str, token: str | None = Query(None),
                      user: dict | None = Depends(auth.optional_user)):
    """Stream the latest Windows MSI from the release, fresh each time.

    Accepts: a one-time enrolment/internal token, a multi-use download-link
    token, or an authenticated admin session.
    """
    authed = False
    if token:
        authed = (db.token_valid_for(org_id, token)       # one-time / internal
                  or db.download_token_valid(org_id, token))  # shareable download link
    if not authed:
        if user is None:
            raise HTTPException(status_code=401,
                                detail="A valid token (?token=) or an admin session is required")
        _org_from_request(org_id, user)
    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            r = await client.get(MSI_URL)
        r.raise_for_status()
    except Exception:
        raise HTTPException(status_code=502,
                            detail="MSI not available yet — build/publish a release first.")
    return Response(r.content, media_type="application/x-msdownload",
                    headers={"Content-Disposition": "attachment; filename=leuffen-rmm-agent.msi",
                             "Cache-Control": "no-store"})


@app.get("/api/orgs/{org_id}/agent.zip")
def agent_zip(org_id: str, token: str | None = Query(None),
              user: dict | None = Depends(auth.optional_user)):
    import io
    import zipfile
    org, key = _installer_key(org_id, token, user)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(AGENT_DIR):
            if "__pycache__" in root:
                continue
            for f in files:
                if f.endswith((".pyc",)):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, AGENT_DIR))
        # Inject ready-to-run config so the agent connects with no manual setup.
        z.writestr("rmm_config.json",
                   f'{{"server_url": "{public_url()}", "api_key": "{key}", '
                   f'"insecure_tls": {str(agent_insecure_tls()).lower()}}}')
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=leuffen-rmm-agent.zip"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Gate the dashboard index behind setup + auth.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not db.setup_complete():
        return RedirectResponse("/setup")
    if not auth.DEV_AUTH:
        raw = request.cookies.get(auth.COOKIE)
        data = auth.read_cookie(raw) if raw else None
        if not data:
            return RedirectResponse("/auth/login")
        # Enforce 2FA enrolment for local accounts when the policy is on.
        if auth.LOCAL_ENABLED and \
                os.environ.get("RMM_ENFORCE_2FA", "").lower() in ("1", "true", "yes"):
            u = db.get_user(data.get("email", ""))
            if u and not u.get("totp_enabled"):
                return RedirectResponse("/account.html#enroll-2fa")
    return _serve_html("index.html")


# --------------------------------------------------------------------------- #
# First-run setup wizard
# --------------------------------------------------------------------------- #
@app.get("/remote/{device_id}", response_class=HTMLResponse)
def remote_page(device_id: str, request: Request):
    if not auth.DEV_AUTH:
        raw = request.cookies.get(auth.COOKIE)
        if not (raw and auth.read_cookie(raw)):
            return RedirectResponse(f"/auth/login?next=/remote/{device_id}")
    return _serve_html("remote.html")


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    if db.setup_complete():
        return RedirectResponse("/")
    return _serve_html("setup.html")


@app.get("/settings.html", response_class=HTMLResponse)
def settings_page(request: Request):
    if not auth.DEV_AUTH:
        raw = request.cookies.get(auth.COOKIE)
        if not (raw and auth.read_cookie(raw)):
            return RedirectResponse("/auth/login")
    return _serve_html("settings.html")


@app.get("/account.html", response_class=HTMLResponse)
def account_page(request: Request):
    if not auth.DEV_AUTH:
        raw = request.cookies.get(auth.COOKIE)
        if not (raw and auth.read_cookie(raw)):
            return RedirectResponse("/auth/login")
    return _serve_html("account.html")


@app.get("/invite.html", response_class=HTMLResponse)
def invite_page():
    return _serve_html("invite.html")


@app.get("/api/setup/status")
def setup_status():
    return {"complete": db.setup_complete(), "public_url": public_url(),
            "tls_mode": os.environ.get("RMM_TLS_MODE", "self-signed"),
            "host": os.environ.get("RMM_HOST", "0.0.0.0"),
            "port": os.environ.get("RMM_PORT", "8000")}


@app.post("/api/setup")
async def do_setup(request: Request):
    """Persist the first-run configuration. Only available until setup completes."""
    if db.setup_complete():
        raise HTTPException(status_code=403, detail="Setup already completed")
    data = await request.json()

    public = (data.get("public_url") or "").strip().rstrip("/")
    tls_mode = (data.get("tls_mode") or "self-signed").strip().lower()
    auth_mode = (data.get("auth_mode") or "hybrid").strip().lower()
    # Only two modes are offered now; legacy values fold into hybrid.
    if auth_mode != "dev":
        auth_mode = "hybrid"
    if tls_mode not in ("self-signed", "file", "proxy"):
        raise HTTPException(status_code=400, detail="Invalid TLS mode")

    settings: dict[str, str] = {"RMM_TLS_MODE": tls_mode, "RMM_AUTH_MODE": auth_mode}
    if public:
        settings["RMM_PUBLIC_URL"] = public
    if data.get("host"):
        settings["RMM_HOST"] = str(data["host"]).strip()
    if data.get("port"):
        settings["RMM_PORT"] = str(data["port"]).strip()
    if tls_mode == "file":
        if data.get("cert_path"):
            settings["RMM_TLS_CERT"] = str(data["cert_path"]).strip()
        if data.get("key_path"):
            settings["RMM_TLS_KEY"] = str(data["key_path"]).strip()

    admins = [e.strip().lower() for e in (data.get("admins") or []) if e.strip()]
    accounts = data.get("accounts") or []

    if auth_mode == "hybrid":
        if not accounts:
            raise HTTPException(status_code=400, detail="Create at least one local account")
        # Microsoft 365 SSO is optional in hybrid — store it only if provided.
        if (data.get("client_id") or "").strip():
            for src, env in (("tenant_id", "MS_TENANT_ID"), ("client_id", "MS_CLIENT_ID"),
                             ("client_secret", "MS_CLIENT_SECRET")):
                val = (data.get(src) or "").strip()
                if not val:
                    raise HTTPException(status_code=400,
                                        detail=f"Missing {src} for Microsoft 365 SSO")
                settings[env] = val
            settings["MS_REDIRECT_URI"] = (data.get("redirect_uri") or "").strip() or \
                (public + "/auth/callback" if public else "")
        settings["RMM_DEV_AUTH"] = "0"
    else:  # dev
        settings["RMM_DEV_AUTH"] = "1"

    if admins:
        settings["RMM_BOOTSTRAP_ADMIN"] = ",".join(admins)

    # Session secret: explicit from the wizard, else keep existing, else generate.
    explicit_secret = (data.get("session_secret") or "").strip()
    new_secret = False
    if explicit_secret:
        settings["SESSION_SECRET"] = explicit_secret
    elif not (os.environ.get("SESSION_SECRET") or db.get_setting("SESSION_SECRET")):
        settings["SESSION_SECRET"] = secrets.token_urlsafe(48)
        new_secret = True

    # Persist and live-apply to the environment (download URLs update immediately).
    for key, value in settings.items():
        db.set_setting(key, value)
        os.environ[key] = value

    # Create local accounts (first account / any flagged one becomes a global admin).
    for i, acc in enumerate(accounts):
        uname = (acc.get("username") or "").strip()
        pw = acc.get("password") or ""
        if not uname or len(pw) < 8:
            continue
        db.create_user(uname, pw, is_admin=bool(acc.get("admin") or i == 0),
                       email=(acc.get("email") or "").strip().lower() or None,
                       email_verified=True)

    # Map bootstrap admin emails onto the default org.
    org = db.get_org("default") or (db.list_orgs() or [None])[0]
    if org:
        for a in admins:
            db.add_org_user(org["id"], a, "admin")

    db.set_setting("SETUP_COMPLETE", "1")

    # Auth/TLS/secret changes only take full effect on restart (read at import).
    restart_recommended = bool(
        auth_mode == "hybrid" or new_secret or explicit_secret
        or tls_mode != BOOT_TLS_MODE)
    return {"ok": True, "restart_recommended": restart_recommended}


# --------------------------------------------------------------------------- #
# Admin settings & users (back the Settings page)
# --------------------------------------------------------------------------- #
SETTINGS_KEYS = [
    "RMM_PUBLIC_URL", "RMM_TLS_MODE", "RMM_AUTH_MODE", "GRAPH_SENDER",
    "GRAPH_FROM", "RMM_SERVER_NAME", "RMM_SECURE_COOKIES", "RMM_ENFORCE_2FA",
    "RMM_REQUIRE_APPROVAL", "RMM_REQUIRE_DEVICE_SECRET", "RMM_AUTO_UPDATE_AGENTS",
    "RMM_ALERT_RECIPIENTS",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TLS",
    "MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_REDIRECT_URI",
]


@app.get("/api/settings")
def get_settings(user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    stored = db.get_all_settings()
    out = {k: (stored.get(k) if stored.get(k) is not None else os.environ.get(k, ""))
           for k in SETTINGS_KEYS}
    out["RMM_AUTH_MODE"] = auth.AUTH_MODE
    out["RMM_VERSION"] = SERVER_VERSION
    return out


@app.get("/api/server-fingerprint")
def server_fingerprint(user: dict = Depends(auth.current_user)):
    """SHA-256 of the server's TLS cert, so an admin can pin it on agents.

    Set the returned value as ``RMM_SERVER_FINGERPRINT`` (or ``server_fingerprint``
    in the agent's ``rmm_config.json``) to pin this exact certificate — MITM-proof
    even with a self-signed cert. ``fingerprint`` is null in proxy mode (TLS is
    terminated upstream, so the app can't see the served cert)."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    from . import tls
    mode = os.environ.get("RMM_TLS_MODE", "self-signed").lower()
    fp = None if mode == "proxy" else tls.cert_fingerprint()
    return {"fingerprint": fp, "tls_mode": mode}


@app.get("/api/version")
def get_version(user: dict = Depends(auth.current_user)):
    return {"version": SERVER_VERSION}


@app.get("/api/changelog")
def get_changelog(user: dict = Depends(auth.current_user)):
    """Return the CHANGELOG.md content."""
    for path in (
        os.path.join(os.path.dirname(__file__), "..", "CHANGELOG.md"),
        os.path.join(os.path.dirname(__file__), "..", "..", "CHANGELOG.md"),
        "/app/CHANGELOG.md",
    ):
        try:
            with open(os.path.normpath(path), encoding="utf-8") as f:
                return {"md": f.read()}
        except OSError:
            continue
    return {"md": ""}


@app.get("/api/server/update")
def server_update_status(user: dict = Depends(auth.current_user)):
    """Whether the server container can self-update via the Docker socket."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    from . import docker_update
    return {"version": SERVER_VERSION, **docker_update.status()}


@app.post("/api/server/update/check")
def server_update_check(user: dict = Depends(auth.current_user)):
    """Pull the latest image and report whether it differs from the running one."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    from . import docker_update
    try:
        return {"version": SERVER_VERSION, **docker_update.check_for_update()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/server/update/apply")
def server_update_apply(user: dict = Depends(auth.current_user)):
    """Pull the latest image and recreate this container (server will restart)."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    from . import docker_update
    if not docker_update.available():
        raise HTTPException(status_code=409,
                            detail="Docker socket not mounted — in-UI update unavailable")
    try:
        return docker_update.start_update()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/logs")
def get_logs(limit: int = 300, level: str | None = None,
             user: dict = Depends(auth.current_user)):
    """Recent in-memory server logs (newest last) for the Settings → Logs view."""
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    records = list(_ring_log.records)
    if level:
        wanted = level.upper()
        records = [r for r in records if r["level"] == wanted]
    return {"version": SERVER_VERSION, "logs": records[-max(1, min(limit, 1000)):]}


@app.post("/api/settings")
async def save_settings(request: Request, user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    data = await request.json()
    saved = {}
    for key, value in data.items():
        if key in SETTINGS_KEYS and value is not None:
            db.set_setting(key, str(value))
            os.environ[key] = str(value)
            saved[key] = str(value)
    return {"ok": True, "saved": saved}


@app.get("/api/users")
def list_app_users(user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    return {"mode": auth.AUTH_MODE, "users": db.list_users(),
            "bootstrap_admins": sorted(auth.BOOTSTRAP_ADMINS)}


@app.post("/api/admin/reset")
def reset_config(user: dict = Depends(auth.current_user)):
    """Wipe server configuration and re-show the setup wizard.

    Clears the settings store (auth mode, TLS, public URL, secrets, …) so the
    first-run wizard reappears. Devices, organisations and accounts are kept.
    """
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    db.clear_settings()
    return {"ok": True, "restart_recommended": True}


@app.get("/api/account")
def account(user: dict = Depends(auth.current_user)):
    local = db.get_user(user["email"]) if auth.LOCAL_ENABLED else None
    return {
        "identity": user["email"],
        "email": local["email"] if local and local.get("email") else user["email"],
        "name": (local and local.get("display_name")) or user["email"].split("@")[0],
        "username": local["username"] if local else None,
        "is_global_admin": user["is_global_admin"],
        "mode": auth.AUTH_MODE,
        "local": local is not None,
        "twofa_enabled": bool(local and local.get("totp_enabled")),
        "twofa_enforced": os.environ.get("RMM_ENFORCE_2FA", "").lower() in ("1", "true", "yes"),
        "recovery_remaining": db.recovery_codes_remaining(local["username"]) if local else 0,
    }


def _local_self(user: dict) -> dict:
    if not auth.LOCAL_ENABLED:
        raise HTTPException(status_code=400, detail="Two-factor applies to local accounts only")
    u = db.get_user(user["email"])
    if not u:
        raise HTTPException(status_code=404, detail="No local account for this identity")
    return u


@app.post("/api/account/2fa/setup")
def twofa_setup(user: dict = Depends(auth.current_user)):
    """Generate a fresh (pending) secret and return it for enrolment."""
    u = _local_self(user)
    secret = totp.generate_secret()
    db.set_totp_secret(u["username"], secret)
    db.set_totp_enabled(u["username"], False)
    return {"secret": secret,
            "otpauth_uri": totp.provisioning_uri(secret, u.get("email") or u["username"])}


@app.post("/api/account/2fa/enable")
async def twofa_enable(request: Request, user: dict = Depends(auth.current_user)):
    """Confirm enrolment by verifying a code against the pending secret."""
    u = _local_self(user)
    code = ((await request.json()).get("code") or "").strip()
    if not u.get("totp_secret"):
        raise HTTPException(status_code=400, detail="Start 2FA setup first")
    if not totp.verify(u["totp_secret"], code):
        raise HTTPException(status_code=400, detail="That code didn't match — try again")
    db.set_totp_enabled(u["username"], True)
    codes = db.generate_recovery_codes(u["username"])
    return {"ok": True, "recovery_codes": codes}


@app.post("/api/account/2fa/recovery")
def twofa_recovery(user: dict = Depends(auth.current_user)):
    """Regenerate recovery codes (invalidates the old set). 2FA must be on."""
    u = _local_self(user)
    if not u.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Enable two-factor first")
    return {"recovery_codes": db.generate_recovery_codes(u["username"])}


@app.post("/api/account/2fa/disable")
async def twofa_disable(request: Request, user: dict = Depends(auth.current_user)):
    """Turn off 2FA after confirming the account password."""
    u = _local_self(user)
    password = (await request.json()).get("password") or ""
    if not db.verify_pw(password, u["pw_hash"]):
        raise HTTPException(status_code=403, detail="Password is incorrect")
    db.set_totp_enabled(u["username"], False)
    db.set_totp_secret(u["username"], None)
    db.clear_recovery_codes(u["username"])
    return {"ok": True}


@app.post("/api/account/email")
async def set_account_email(request: Request, user: dict = Depends(auth.current_user)):
    """Set a local account's email — used to link it to a Microsoft 365 identity."""
    u = _local_self(user)
    email = ((await request.json()).get("email") or "").strip().lower()
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    other = db.get_user_by_email(email) if email else None
    if other and other["username"] != u["username"]:
        raise HTTPException(status_code=409, detail="Another account already uses that email")
    db.set_user_email(u["username"], email)
    return {"ok": True, "email": email}


@app.post("/api/account/password")
async def change_password(request: Request, user: dict = Depends(auth.current_user)):
    if not auth.LOCAL_ENABLED:
        raise HTTPException(status_code=400, detail="Password is managed by your identity provider")
    u = db.get_user(user["email"])
    if not u:
        raise HTTPException(status_code=404, detail="No local account")
    data = await request.json()
    if not db.verify_pw(data.get("current") or "", u["pw_hash"]):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    new = data.get("new") or ""
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    db.set_user_password(u["username"], new)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# User management
# --------------------------------------------------------------------------- #
@app.patch("/api/users/{username}")
def edit_user(username: str, body: UserUpdateRequest,
              user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    target = db.get_user(username)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    is_self = username.lower() == user["email"].lower()

    # Don't let an admin lock themselves out of admin.
    if body.is_admin is False and is_self:
        raise HTTPException(status_code=400, detail="You can't remove your own admin role")

    # Email must stay unique across accounts.
    if body.email is not None:
        new_email = (body.email or "").strip().lower()
        if new_email and "@" not in new_email:
            raise HTTPException(status_code=400, detail="Enter a valid email address")
        if new_email:
            other = db.get_user_by_email(new_email)
            if other and other["username"] != username.lower():
                raise HTTPException(status_code=409, detail="Another account already uses that email")

    if body.password is not None and body.password != "":
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        db.set_user_password(username, body.password)

    db.update_user(username, display_name=body.display_name,
                   email=body.email, is_admin=body.is_admin)
    return {"ok": True}


@app.delete("/api/users/{username}")
def delete_user(username: str, user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    if username.lower() == user["email"].lower():
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    db.delete_user(username)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Invite system
# --------------------------------------------------------------------------- #
@app.post("/api/invites")
async def create_invite(req: InviteRequest, request: Request,
                        user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    email = (req.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    delivery = req.delivery if req.delivery in ("email", "link", "both") else "both"
    token = db.create_invite(email, req.is_admin, delivery)
    public_url = (db.get_setting("RMM_PUBLIC_URL") or "").rstrip("/") or str(request.base_url).rstrip("/")
    invite_url = f"{public_url}/invite/{token}"

    emailed = False
    if delivery in ("email", "both"):
        name = os.environ.get("RMM_SERVER_NAME") or "Leuffen RMM"
        subject = f"You've been invited to {name}"
        html = (
            f"<div style='font-size:18px;font-weight:700;margin:0 0 12px'>You're invited</div>"
            f"<p style='margin:0 0 18px'>You've been invited to sign in to <b>{name}</b>. "
            f"Click below to create your account.</p>"
            f"<p style='margin:0 0 18px'>{mailer.button(invite_url, 'Accept invitation')}</p>"
            f"<p style='margin:0;color:#97a3b4;font-size:12.5px'>This link expires in 2 days. "
            f"You'll confirm this email address with a verification code when you set up your "
            f"account. If you didn't expect this invite, you can ignore this email.</p>"
        )
        emailed = mailer.send_mail(subject, html, [email])

    # If email was the only requested method but delivery failed, surface the link
    # so the admin can still share it manually rather than silently losing the invite.
    show_link = delivery in ("link", "both") or not emailed
    return {"ok": True, "invite_url": invite_url if show_link else None,
            "delivery": delivery, "emailed": emailed,
            "mail_configured": mailer.is_configured()}


@app.get("/api/invites")
def list_invites(user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    return {"invites": db.list_invites()}


@app.delete("/api/invites/{token}")
def revoke_invite(token: str, user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    db.delete_invite(token)
    return {"ok": True}


@app.post("/api/mail/test")
async def test_mail(request: Request, user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    data = await request.json()
    recipient = (data.get("email") or "").strip()
    if not recipient:
        raise HTTPException(status_code=400, detail="Email address required")
    name = os.environ.get("RMM_SERVER_NAME") or "Leuffen RMM"
    ok = mailer.send_mail(
        f"{name} — test email",
        "<div style='font-size:18px;font-weight:700;margin:0 0 10px'>Email delivery works</div>"
        f"<p style='margin:0'>This is a test email from <b>{name}</b>. If you can read this, "
        "your SMTP or Microsoft Graph configuration is working correctly.</p>",
        [recipient],
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Mail delivery failed — check your SMTP / Graph configuration")
    return {"ok": True}


@app.get("/invite/{token}")
async def invite_page(token: str):
    inv = db.get_invite(token)
    if not inv or inv["used_at"] or inv["expires_at"] < __import__("time").time():
        return HTMLResponse("<h2>This invite link is invalid or has expired.</h2>", status_code=410)
    return _serve_html("invite.html")


INVITE_CODE_TTL = 15 * 60  # verification codes are valid for 15 minutes


def _live_invite(token: str) -> dict:
    import time as _time
    inv = db.get_invite(token)
    if not inv or inv["used_at"] or inv["expires_at"] < _time.time():
        raise HTTPException(status_code=410, detail="Invite link is invalid or has expired")
    return inv


def _validate_new_account(data: dict) -> tuple[str, str]:
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    if not username or len(username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.get_user(username):
        raise HTTPException(status_code=409, detail="Username already taken")
    return username, password


@app.post("/invite/{token}/send-code")
async def send_invite_code(token: str, request: Request):
    """Email a 6-digit verification code to the invited address so the invitee
    proves they control it before the account is created."""
    import time as _time
    inv = _live_invite(token)
    data = await request.json()
    _validate_new_account(data)  # surface format/uniqueness errors before sending
    if not mailer.is_configured():
        return {"ok": True, "sent": False, "mail_configured": False, "email": inv["email"]}
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.set_invite_code(token, code, _time.time() + INVITE_CODE_TTL)
    name = os.environ.get("RMM_SERVER_NAME") or "Leuffen RMM"
    html = (
        f"<div style='font-size:18px;font-weight:700;margin:0 0 12px'>Verify your email</div>"
        f"<p style='margin:0 0 10px'>Enter this code to finish setting up your account:</p>"
        f"<div style='font-size:30px;font-weight:800;letter-spacing:8px;color:#3b82f6;"
        f"background:#0a0c11;border:1px solid #232b37;border-radius:12px;padding:16px;"
        f"text-align:center;margin:0 0 16px'>{code}</div>"
        f"<p style='margin:0;color:#97a3b4;font-size:12.5px'>It expires in 15 minutes. "
        f"If you didn't request this, you can ignore this email.</p>"
    )
    sent = mailer.send_mail(f"{name} — verify your email", html, [inv["email"]])
    if not sent:
        db.set_invite_code(token, None, None)
    return {"ok": True, "sent": sent, "mail_configured": True, "email": inv["email"]}


@app.post("/invite/{token}")
async def accept_invite(token: str, request: Request):
    import time as _time
    inv = _live_invite(token)
    data = await request.json()
    username, password = _validate_new_account(data)

    # When mail works, the invitee must confirm the address with the code we sent.
    # When mail isn't configured, verification is skipped (we can't deliver a code).
    email_verified = False
    if mailer.is_configured():
        code = (data.get("code") or "").strip()
        stored, expires = inv.get("verify_code"), inv.get("verify_expires")
        if not stored:
            raise HTTPException(status_code=400, detail="Request a verification code first")
        if not expires or expires < _time.time():
            raise HTTPException(status_code=400, detail="Verification code expired — request a new one")
        if not code or not secrets.compare_digest(code, str(stored)):
            raise HTTPException(status_code=400, detail="Incorrect verification code")
        email_verified = True

    db.create_user(username, password, is_admin=bool(inv["is_admin"]),
                   email=inv["email"], email_verified=email_verified)
    db.set_invite_code(token, None, None)
    db.use_invite(token)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE, auth.make_cookie(username),
        httponly=True, samesite="lax", secure=auth.SECURE_COOKIES,
    )
    return response


# --------------------------------------------------------------------------- #
# Access groups
# --------------------------------------------------------------------------- #
@app.get("/api/access-groups")
def list_access_groups(user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    groups = db.list_access_groups()
    for g in groups:
        g["members"] = db.list_access_group_members(g["id"])
        g["orgs"] = db.list_access_group_orgs(g["id"])
    return {"groups": groups}


@app.post("/api/access-groups")
def create_access_group(body: AccessGroupRequest, user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    return db.create_access_group(body.name)


@app.patch("/api/access-groups/{group_id}")
def rename_access_group(group_id: str, body: AccessGroupRequest,
                        user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    if not db.get_access_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    db.rename_access_group(group_id, body.name)
    return {"ok": True}


@app.delete("/api/access-groups/{group_id}")
def delete_access_group(group_id: str, user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    if not db.get_access_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    db.delete_access_group(group_id)
    return {"ok": True}


@app.post("/api/access-groups/{group_id}/members")
def add_access_group_member(group_id: str, body: AccessGroupMemberRequest,
                            user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    if not db.get_access_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    db.add_access_group_member(group_id, body.user_email)
    return {"ok": True}


@app.delete("/api/access-groups/{group_id}/members/{email}")
def remove_access_group_member(group_id: str, email: str,
                               user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    db.remove_access_group_member(group_id, email)
    return {"ok": True}


@app.post("/api/access-groups/{group_id}/orgs")
def add_access_group_org(group_id: str, body: AccessGroupOrgRequest,
                         user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    if not db.get_access_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    if not db.get_org(body.org_id):
        raise HTTPException(status_code=404, detail="Organisation not found")
    db.set_access_group_org(group_id, body.org_id, body.role)
    return {"ok": True}


@app.delete("/api/access-groups/{group_id}/orgs/{org_id}")
def remove_access_group_org(group_id: str, org_id: str,
                            user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    db.remove_access_group_org(group_id, org_id)
    return {"ok": True}


@app.put("/api/access-groups/{group_id}/orgs/{org_id}/perms")
def set_access_group_perm(group_id: str, org_id: str, body: AccessGroupPermRequest,
                          user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    try:
        db.set_access_group_perm(group_id, org_id, body.permission, body.effect)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/access-groups/{group_id}/orgs/{org_id}/perms/{permission}")
def remove_access_group_perm(group_id: str, org_id: str, permission: str,
                             user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    db.remove_access_group_perm(group_id, org_id, permission)
    return {"ok": True}


@app.get("/api/access-groups/{group_id}/orgs/{org_id}/effective-perms")
def effective_perms(group_id: str, org_id: str, user: dict = Depends(auth.current_user)):
    """Effective permissions for a user email; also usable to preview group impact."""
    auth.require_global(user)
    return db.user_effective_perms(user["email"], org_id)


@app.get("/api/users/{email}/effective-perms/{org_id}")
def user_effective_perms_route(email: str, org_id: str,
                               user: dict = Depends(auth.current_user)):
    auth.require_global(user)
    return {"perms": db.user_effective_perms(email, org_id)}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

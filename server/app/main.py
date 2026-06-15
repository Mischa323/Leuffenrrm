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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

from . import alerts, auth, database as db, graph, totp
from . import wol as wol_local
from .manager import manager
from .models import (GroupRequest, MonitorRequest, MoveDeviceRequest, MoveOrgRequest,
                     OrgRequest, OrgUserRequest, PowerRequest, ScheduleRequest,
                     ScriptFileRequest, ScriptRequest, ScriptRunRequest, ShellRequest,
                     SubnetRequest, WakeRequest)

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
    return "0.1.0"


SERVER_VERSION = _resolve_version()


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
AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agent")
OFFLINE_AFTER = float(os.environ.get("RMM_OFFLINE_AFTER", "120"))
METRIC_RETENTION = float(os.environ.get("RMM_METRIC_RETENTION", str(7 * 24 * 3600)))
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
    asyncio.create_task(_alert_loop())
    asyncio.create_task(_prune_loop())
    asyncio.create_task(_schedule_loop())


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
    with open(os.path.join(STATIC_DIR, "login.html")) as f:
        return HTMLResponse(f.read())


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
    # In hybrid mode, fold the SSO user onto a matching local account (by email).
    identity = auth.resolve_sso_identity(email)
    resp = RedirectResponse("/")
    resp.set_cookie(auth.COOKIE, auth.make_cookie(identity), httponly=True,
                    samesite="lax", secure=auth.SECURE_COOKIES)
    resp.delete_cookie("oauth_state")
    return resp


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
# Devices
# --------------------------------------------------------------------------- #
@app.get("/api/orgs/{org_id}/devices")
def list_devices(org_id: str, group_id: str | None = None,
                 user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return [_decorate(d) for d in db.list_devices(org_id, group_id)]


@app.get("/api/devices/{device_id}")
def get_device(device_id: str, user: dict = Depends(auth.current_user)):
    return _decorate(_device_for_user(device_id, user))


@app.get("/api/devices/{device_id}/metrics")
def device_metrics(device_id: str, limit: int = 200, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    return db.get_metrics(device_id, limit=limit)


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: str, user: dict = Depends(auth.current_user)):
    _device_for_user(device_id, user)
    db.delete_device(device_id)
    return {"status": "deleted"}


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
    token = db.create_enroll_token(org_id, label="agent-update", ttl_hours=2)["token"]
    return {
        "type": "update_agent",
        "server_url": pub,
        "org_id": org_id,
        "insecure_tls": agent_insecure_tls(),
        "msi_url": f"{pub}/api/orgs/{org_id}/install.msi?token={token}",
        "zip_url": f"{pub}/api/orgs/{org_id}/agent.zip?token={token}",
    }


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
    """Push a self-update to every online agent in the organisation (best effort)."""
    auth.require_org(user, org_id)
    msg = _update_message(org_id)
    targets = [d["id"] for d in db.list_devices(org_id) if manager.is_online(d["id"])]
    started = 0
    for did in targets:
        try:
            await manager.get(did).send(msg)
            started += 1
        except Exception:
            pass
    return {"started": started, "online": len(targets)}


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
    """Run a script on one device with its attached files + variables, recording a run."""
    run_id = db.create_run(script["org_id"], device_id, run_name or script["name"], script["id"])
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
    """Resolve a schedule's online target device ids."""
    org_id, online = sched["org_id"], manager.online_ids()
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
    """Email the org's alert recipients when a monitor flips to/from alerting."""
    if old_status == new_status:
        return
    if new_status == "alert" and old_status != "alert":
        subject = f"Monitor alerting: {mon['name']}"
        body = (f"<p>The monitoring policy <b>{mon['name']}</b> is now <b>alerting</b>.</p>"
                f"<p>The monitor script exited non-zero on one or more devices"
                + (" and the remediation script was run." if mon.get('remediation_script_id')
                   else ".") + "</p>")
    elif new_status == "ok" and old_status == "alert":
        subject = f"Monitor resolved: {mon['name']}"
        body = f"<p>The monitoring policy <b>{mon['name']}</b> is healthy again.</p>"
    else:
        return
    recipients = (db.alert_config(mon["org_id"]).get("recipients")
                  or [e.strip() for e in os.environ.get("RMM_ALERT_RECIPIENTS", "").split(",") if e.strip()]
                  or sorted(auth.BOOTSTRAP_ADMINS))
    if recipients:
        graph.send_mail(f"[RMM] {subject}", body, recipients)


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
    if req.target_type not in ("device", "group", "all"):
        raise HTTPException(status_code=400, detail="Invalid target type")
    if req.trigger == "interval" and not req.interval_minutes:
        raise HTTPException(status_code=400, detail="interval_minutes is required")
    if req.trigger == "daily" and not req.at_time:
        raise HTTPException(status_code=400, detail="at_time is required for a daily monitor")
    nxt = _next_run({"trigger": req.trigger, "interval_minutes": req.interval_minutes,
                     "at_time": req.at_time})
    return db.create_monitor(org_id, req.name, req.monitor_script_id, req.remediation_script_id,
                             req.target_type, req.target_id, req.trigger, req.interval_minutes,
                             req.at_time, _json.dumps(req.variables or {}), nxt)


@app.post("/api/monitors/{monitor_id}/toggle")
def toggle_monitor(monitor_id: str, user: dict = Depends(auth.current_user)):
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_org(user, mon["org_id"])
    enabled = not mon["enabled"]
    db.set_monitor_enabled(monitor_id, enabled, _next_run(mon) if enabled else None)
    return {"enabled": enabled}


@app.post("/api/monitors/{monitor_id}/run")
async def run_monitor_now(monitor_id: str, user: dict = Depends(auth.current_user)):
    mon = db.get_monitor(monitor_id)
    if not mon:
        raise HTTPException(status_code=404, detail="Monitor not found")
    auth.require_org(user, mon["org_id"])
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
    auth.require_org(user, mon["org_id"])
    db.delete_monitor(monitor_id)
    return {"status": "deleted"}


# --------------------------------------------------------------------------- #
# Wake-on-LAN (direct or via a relay node)
# --------------------------------------------------------------------------- #
def _pick_node(org_id: str, target_ip: str | None) -> dict | None:
    nodes = [n for n in db.list_nodes(org_id) if manager.is_online(n["id"])]
    if target_ip:
        for n in nodes:
            for s in n["subnets"]:
                try:
                    if ipaddress.ip_address(target_ip) in ipaddress.ip_network(s["cidr"], False):
                        return n
                except ValueError:
                    continue
    return nodes[0] if nodes else None


async def _wake(org_id: str, mac: str, broadcast: str | None, port: int,
                target_ip: str | None, node_id: str | None) -> dict:
    node = db.get_device(node_id) if node_id else _pick_node(org_id, target_ip)
    if node and manager.is_online(node["id"]):
        await manager.request(node["id"], {"type": "wol", "mac": mac,
                                           "broadcast": broadcast, "port": port})
        return {"status": "sent", "via": "node", "node": node["hostname"]}
    # Fallback: broadcast from the server itself.
    wol_local.send_magic_packet(mac, broadcast_ip=broadcast or "255.255.255.255", port=port)
    return {"status": "sent", "via": "server"}


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
    bcast = req.broadcast
    if not bcast:
        try:
            bcast = str(ipaddress.ip_network(req.cidr, False).broadcast_address)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Bad CIDR: {exc}")
    db.add_subnet(device_id, req.cidr, bcast)
    if manager.is_online(device_id):
        subs = [s["cidr"] for s in db.list_subnets(device_id)]
        await manager.get(device_id).send({"type": "set_role", "role": "node", "subnets": subs})
    return {"status": "ok", "broadcast": bcast}


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
        db.upsert_device(org["id"], first, require_approval=require_approval())
        await manager.register(device_id, org["id"], ws)
        if db.get_device(device_id).get("is_node"):
            subs = [s["cidr"] for s in db.list_subnets(device_id)]
            await ws.send_json({"type": "set_role", "role": "node", "subnets": subs})
        log.info("Agent connected: %s (org %s)", first.get("hostname"), org["name"])

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
    elif mtype == "ack":
        manager.resolve(data.get("rid", ""), data.get("payload", data))
    elif mtype == "shell_output":
        await manager.fanout(device_id, "terminal", data)
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
    await _bridge_ws(ws, device_id, "screen")


async def _bridge_ws(ws: WebSocket, device_id: str, channel: str) -> None:
    # Cookie auth (browser sends it automatically); dev mode is always allowed.
    if not auth.DEV_AUTH:
        raw = ws.cookies.get(auth.COOKIE)
        if not (raw and auth.read_cookie(raw)):
            await ws.close(code=4401)
            return
    await ws.accept()
    agent = manager.get(device_id)
    if not agent:
        await ws.send_json({"type": "error", "error": "Device offline"})
        await ws.close()
        return
    manager.subscribe(device_id, channel, ws)
    if channel == "screen":
        await agent.send({"type": "screen_start", "fps": 4, "quality": 50})
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
        return org, db.create_enroll_token(org_id, label="installer")["token"]
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
curl -fsSL "{pub}/api/orgs/{org_id}/agent.zip?token={key}" -o "$TMP/agent.zip"
unzip -o "$TMP/agent.zip" -d /opt/leuffen-rmm
pip3 install -r /opt/leuffen-rmm/requirements.txt
cat >/etc/systemd/system/leuffen-rmm.service <<UNIT
[Unit]
Description=Leuffen RMM Agent
After=network-online.target
[Service]
Environment=RMM_SERVER_URL={pub}
Environment=RMM_API_KEY={key}
Environment=RMM_INSECURE_TLS={insecure}
ExecStart=/usr/bin/python3 /opt/leuffen-rmm/agent.py
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


MSI_URL = os.environ.get(
    "RMM_MSI_URL",
    "https://github.com/Mischa323/Leuffenrrm/releases/latest/download/leuffen-rmm-agent.msi")
GH_REPO = os.environ.get("RMM_GH_REPO", "Mischa323/Leuffenrrm")
_release_cache: dict = {"t": 0.0, "data": None}


@app.get("/api/agent-release")
async def agent_release(user: dict = Depends(auth.current_user)):
    """Latest published Windows agent build (name/version, size, date) for the UI."""
    if not (_release_cache["data"] and time.time() - _release_cache["t"] < 60):
        import httpx
        out = {"available": False}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"https://api.github.com/repos/{GH_REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                d = r.json()
                asset = next((a for a in d.get("assets", []) if a["name"].endswith(".msi")), None)
                out = {"available": bool(asset), "name": d.get("name") or d.get("tag_name"),
                       "tag": d.get("tag_name"), "published_at": d.get("published_at"),
                       "size": asset["size"] if asset else None}
        except Exception:
            pass
        _release_cache.update(t=time.time(), data=out)
    return JSONResponse(_release_cache["data"], headers={"Cache-Control": "no-store"})


@app.get("/api/orgs/{org_id}/install.msi")
async def install_msi(org_id: str, token: str | None = Query(None),
                      user: dict | None = Depends(auth.optional_user)):
    """Stream the latest Windows MSI from the release, fresh each time.

    Fetching server-side (rather than redirecting the browser to a fixed URL)
    avoids stale browser/CDN copies, so you always get the newest build.
    Configure it at install via msiexec properties — see the Downloads tab.

    A valid one-time ``token`` query authorises the download (used by the agent
    self-update, which has no browser session); otherwise an org admin session.
    """
    if not (token and db.token_valid_for(org_id, token)):
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
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


# --------------------------------------------------------------------------- #
# First-run setup wizard
# --------------------------------------------------------------------------- #
@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    if db.setup_complete():
        return RedirectResponse("/")
    with open(os.path.join(STATIC_DIR, "setup.html")) as f:
        return HTMLResponse(f.read())


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
                       email=(acc.get("email") or "").strip().lower() or None)

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
    "GRAPH_FROM", "RMM_SERVER_NAME", "ALERT_CPU_PCT", "ALERT_DISK_FREE_PCT",
    "ALERT_MEM_PCT", "ALERT_OFFLINE_AFTER", "RMM_SECURE_COOKIES", "RMM_ENFORCE_2FA",
    "RMM_REQUIRE_APPROVAL", "RMM_ALERT_RECIPIENTS",
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


@app.get("/api/version")
def get_version(user: dict = Depends(auth.current_user)):
    return {"version": SERVER_VERSION}


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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import alerts, auth, database as db, totp
from . import wol as wol_local
from .manager import manager
from .models import (GroupRequest, MoveDeviceRequest, OrgRequest, OrgUserRequest,
                     PowerRequest, ScheduleRequest, ScriptRequest, ScriptRunRequest,
                     ShellRequest, SubnetRequest, WakeRequest)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rmm")

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

app = FastAPI(title="Leuffen RMM", version="0.1.0")


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
    """Run scheduled jobs that have come due, then compute their next run."""
    while True:
        await asyncio.sleep(30)
        try:
            for sched in db.due_schedules():
                try:
                    await _execute_schedule(sched)
                except Exception as exc:
                    log.warning("schedule %s failed: %s", sched["id"], exc)
                db.mark_schedule_ran(sched["id"], _next_run(sched))
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


@app.post("/api/auth/local-login")
async def local_login(request: Request):
    if not auth.LOCAL_ENABLED:
        raise HTTPException(status_code=403, detail="Local sign-in is disabled")
    data = await request.json()
    u = auth.verify_local((data.get("username") or "").strip(), data.get("password") or "")
    # Second factor (TOTP, or a single-use recovery code) if enabled.
    if u.get("totp_enabled"):
        code = (data.get("code") or "").strip()
        if not code:
            from fastapi.responses import JSONResponse
            return JSONResponse({"mfa_required": True}, status_code=200)
        ok = totp.verify(u.get("totp_secret") or "", code) or \
            db.consume_recovery_code(u["username"], code)
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid authentication or recovery code")
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
                    "noncompliant": noncompliant})
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


@app.post("/api/orgs/{org_id}/users")
def add_user(org_id: str, req: OrgUserRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    db.add_org_user(org_id, req.email, req.role)
    return {"status": "ok"}


@app.get("/api/orgs/{org_id}/groups")
def list_groups(org_id: str, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.list_groups(org_id)


@app.post("/api/orgs/{org_id}/groups")
def create_group(org_id: str, req: GroupRequest, user: dict = Depends(auth.current_user)):
    auth.require_org(user, org_id)
    return db.create_group(org_id, req.name)


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
    return db.create_script(org_id, req.name, req.content, req.shell, req.description)


@app.delete("/api/scripts/{script_id}")
def delete_script(script_id: str, user: dict = Depends(auth.current_user)):
    script = db.get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    auth.require_org(user, script["org_id"])
    db.delete_script(script_id)
    return {"status": "deleted"}


async def _exec_script_on_device(script: dict, device_id: str, timeout: float = 120) -> dict:
    """Run a script on one device, recording a run row. Raises on transport error."""
    run_id = db.create_run(script["org_id"], device_id, script["name"], script["id"])
    try:
        res = await manager.request(device_id, {
            "type": "script_run", "content": script["content"],
            "shell": script["shell"], "timeout": timeout}, timeout=timeout + 10)
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
    org = db.get_org_by_key(key)
    if org is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    device_id: str | None = None
    try:
        # First message must be a register.
        first = await ws.receive_json()
        if first.get("type") != "register":
            await ws.close(code=4400)
            return
        device_id = first["id"]
        db.upsert_device(org["id"], first)
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
        db.insert_metric(device_id, data.get("metrics", {}))
        db.touch_device(device_id)
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


@app.get("/api/orgs/{org_id}/install.sh", response_class=PlainTextResponse)
def install_sh(org_id: str, user: dict = Depends(auth.current_user)):
    org = _org_from_request(org_id, user)
    pub = public_url()
    insecure = "1" if agent_insecure_tls() else "0"
    return f"""#!/usr/bin/env bash
set -e
# Leuffen RMM agent installer (Linux)
export RMM_SERVER_URL="{pub}"
export RMM_API_KEY="{org['enroll_key']}"
export RMM_INSECURE_TLS="{insecure}"
TMP=$(mktemp -d)
curl -fsSL "{pub}/api/orgs/{org_id}/agent.zip" -o "$TMP/agent.zip"
unzip -o "$TMP/agent.zip" -d /opt/leuffen-rmm
pip3 install -r /opt/leuffen-rmm/requirements.txt
cat >/etc/systemd/system/leuffen-rmm.service <<UNIT
[Unit]
Description=Leuffen RMM Agent
After=network-online.target
[Service]
Environment=RMM_SERVER_URL={pub}
Environment=RMM_API_KEY={org['enroll_key']}
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
def install_ps1(org_id: str, user: dict = Depends(auth.current_user)):
    org = _org_from_request(org_id, user)
    pub = public_url()
    insecure = "1" if agent_insecure_tls() else "0"
    return f"""# Leuffen RMM agent installer (Windows)
$ErrorActionPreference = "Stop"
$dest = "$env:ProgramFiles\\LeuffenRMM"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Invoke-WebRequest "{pub}/api/orgs/{org_id}/agent.zip" -OutFile "$env:TEMP\\agent.zip"
Expand-Archive -Force "$env:TEMP\\agent.zip" -DestinationPath $dest
pip install -r "$dest\\requirements.txt"
[Environment]::SetEnvironmentVariable("RMM_SERVER_URL", "{pub}", "Machine")
[Environment]::SetEnvironmentVariable("RMM_API_KEY", "{org['enroll_key']}", "Machine")
[Environment]::SetEnvironmentVariable("RMM_INSECURE_TLS", "{insecure}", "Machine")
$action = New-ScheduledTaskAction -Execute "python" -Argument "$dest\\agent.py"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "LeuffenRMM" -Action $action -Trigger $trigger -RunLevel Highest -Force
Start-ScheduledTask -TaskName "LeuffenRMM"
Write-Host "Leuffen RMM agent installed."
"""


@app.get("/api/orgs/{org_id}/agent.zip")
def agent_zip(org_id: str, user: dict = Depends(auth.current_user)):
    import io
    import zipfile
    org = _org_from_request(org_id, user)
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
                   f'{{"server_url": "{public_url()}", "api_key": "{org["enroll_key"]}", '
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
]


@app.get("/api/settings")
def get_settings(user: dict = Depends(auth.current_user)):
    if not user["is_global_admin"]:
        raise HTTPException(status_code=403, detail="Global admin required")
    stored = db.get_all_settings()
    out = {k: (stored.get(k) if stored.get(k) is not None else os.environ.get(k, ""))
           for k in SETTINGS_KEYS}
    out["RMM_AUTH_MODE"] = auth.AUTH_MODE
    return out


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

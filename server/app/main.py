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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import alerts, auth, database as db
from . import wol as wol_local
from .manager import manager
from .models import (GroupRequest, MoveDeviceRequest, OrgRequest, OrgUserRequest,
                     PowerRequest, ShellRequest, SubnetRequest, WakeRequest)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rmm")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agent")
OFFLINE_AFTER = float(os.environ.get("RMM_OFFLINE_AFTER", "120"))
METRIC_RETENTION = float(os.environ.get("RMM_METRIC_RETENTION", str(7 * 24 * 3600)))
ALERT_INTERVAL = float(os.environ.get("RMM_ALERT_INTERVAL", "60"))
PUBLIC_URL = os.environ.get("RMM_PUBLIC_URL", "http://localhost:8000")

app = FastAPI(title="Leuffen RMM", version="0.1.0")


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _ensure_default_org()
    asyncio.create_task(_alert_loop())
    asyncio.create_task(_prune_loop())


def _ensure_default_org() -> None:
    """Seed a 'Default' org using RMM_API_KEY as its enrolment key (idempotent)."""
    key = os.environ.get("RMM_API_KEY", "changeme")
    if db.get_org_by_key(key) is None and not db.list_orgs():
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


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
@app.get("/auth/login")
def auth_login(request: Request):
    if auth.DEV_AUTH:
        return RedirectResponse("/")
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(auth.login_url(state))
    resp.set_cookie("oauth_state", state, httponly=True, max_age=600, samesite="lax")
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if request.cookies.get("oauth_state") != state:
        raise HTTPException(status_code=400, detail="state mismatch")
    email = auth.exchange_code(code)
    resp = RedirectResponse("/")
    resp.set_cookie(auth.COOKIE, auth.make_cookie(email), httponly=True, samesite="lax")
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
    return f"""#!/usr/bin/env bash
set -e
# Leuffen RMM agent installer (Linux)
export RMM_SERVER_URL="{PUBLIC_URL}"
export RMM_API_KEY="{org['enroll_key']}"
TMP=$(mktemp -d)
curl -fsSL "{PUBLIC_URL}/api/orgs/{org_id}/agent.zip" -o "$TMP/agent.zip"
unzip -o "$TMP/agent.zip" -d /opt/leuffen-rmm
pip3 install -r /opt/leuffen-rmm/requirements.txt
cat >/etc/systemd/system/leuffen-rmm.service <<UNIT
[Unit]
Description=Leuffen RMM Agent
After=network-online.target
[Service]
Environment=RMM_SERVER_URL={PUBLIC_URL}
Environment=RMM_API_KEY={org['enroll_key']}
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
    return f"""# Leuffen RMM agent installer (Windows)
$ErrorActionPreference = "Stop"
$dest = "$env:ProgramFiles\\LeuffenRMM"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Invoke-WebRequest "{PUBLIC_URL}/api/orgs/{org_id}/agent.zip" -OutFile "$env:TEMP\\agent.zip"
Expand-Archive -Force "$env:TEMP\\agent.zip" -DestinationPath $dest
pip install -r "$dest\\requirements.txt"
[Environment]::SetEnvironmentVariable("RMM_SERVER_URL", "{PUBLIC_URL}", "Machine")
[Environment]::SetEnvironmentVariable("RMM_API_KEY", "{org['enroll_key']}", "Machine")
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
                   f'{{"server_url": "{PUBLIC_URL}", "api_key": "{org["enroll_key"]}"}}')
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=leuffen-rmm-agent.zip"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Gate the dashboard index behind auth so unauthenticated users are redirected.
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not auth.DEV_AUTH:
        raw = request.cookies.get(auth.COOKIE)
        if not (raw and auth.read_cookie(raw)):
            return RedirectResponse("/auth/login")
    with open(os.path.join(STATIC_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

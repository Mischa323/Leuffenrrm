"""UniFi Site Manager (cloud) API client.

Server-side integration with Ubiquiti's official **Site Manager API**
(``https://api.ui.com/v1``), authenticated with a per-account **API key**
(``X-API-KEY``). One key gives cross-console inventory + ISP/WAN health, so the
RMM server polls UniFi directly — no LAN node/agent required (unlike SNMP).

Kept FastAPI-free and defensive so it's unit-testable and tolerant of the
versioned cloud schema: :func:`collect` normalises whatever the API returns into
a stable snapshot the dashboard + alerter consume::

    {ok, error, hosts:[...], devices:[...], isp:[...], edges:[...]}

The cloud API exposes inventory + ISP metrics but **not** per-port/uplink
topology, so ``edges`` is empty and the dashboard tiers the network map by device
role (Internet → gateway → switches → APs). Richer per-port/uplink data would come
from the Connector Proxy → local Network Integration API as a later enhancement.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("rmm.unifi")

SITE_MGR = "https://api.ui.com/v1"
CONNECTOR = "https://api.ui.com/v1/connector/consoles"   # + /{consoleId}/proxy/network/integration/v1
_TIMEOUT = 20.0
_TOPO_MAX_DEVICES = 60   # cap detail calls per console (bounds a poll's proxy calls)


class UnifiError(Exception):
    """A UniFi API call failed (auth, permission, or transport)."""


# --------------------------------------------------------------------------- #
# Low-level HTTP
# --------------------------------------------------------------------------- #
def _first(d: dict, *keys, default=None):
    """First present, non-None value among ``keys`` in ``d`` (defensive against
    the versioned/renamed cloud fields)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _request(key: str, path: str, *, base: str = SITE_MGR, params: dict | None = None) -> dict:
    """GET ``base+path`` with the API key. Returns the parsed JSON envelope.

    Retries once on a 429 honouring ``Retry-After``. Raises :class:`UnifiError`
    on auth/permission/transport failures.
    """
    url = base + path
    headers = {"X-API-KEY": key, "Accept": "application/json"}
    for attempt in range(2):
        try:
            r = httpx.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        except Exception as exc:  # transport / DNS / TLS
            raise UnifiError(f"connection error: {exc}") from exc
        if r.status_code == 429 and attempt == 0:
            try:
                wait = float(r.headers.get("Retry-After", "2"))
            except ValueError:
                wait = 2.0
            import time as _t
            _t.sleep(min(max(wait, 1.0), 10.0))
            continue
        if r.status_code in (401, 403):
            raise UnifiError("unauthorised — check the API key and its permissions")
        if r.status_code >= 400:
            raise UnifiError(f"HTTP {r.status_code}")
        try:
            body = r.json()
        except Exception as exc:
            raise UnifiError("invalid JSON from UniFi") from exc
        # Site Manager error envelope: {"meta": {"rc": "error", "msg": ...}} on some paths.
        meta = body.get("meta") if isinstance(body, dict) else None
        if isinstance(meta, dict) and meta.get("rc") == "error":
            raise UnifiError(str(meta.get("msg") or "api error"))
        return body if isinstance(body, dict) else {"data": body}
    raise UnifiError("rate limited")


def _paged(key: str, path: str) -> list:
    """Follow ``nextToken`` pagination, concatenating ``data`` lists."""
    out: list = []
    token = None
    for _ in range(50):  # hard cap so a broken cursor can't loop forever
        params = {"pageSize": 200}
        if token:
            params["nextToken"] = token
        body = _request(key, path, params=params)
        data = body.get("data")
        if isinstance(data, list):
            out.extend(data)
        elif data is not None:
            out.append(data)
        token = body.get("nextToken") or (body.get("meta") or {}).get("nextToken")
        if not token:
            break
    return out


# --------------------------------------------------------------------------- #
# Classification / normalisation
# --------------------------------------------------------------------------- #
def _classify(*hints: str) -> str:
    """Map a device's model/product-line/name hints to a role."""
    s = " ".join(h for h in hints if h).lower()
    if any(t in s for t in ("gateway", "udm", "uxg", "ucg", "ugw", "usg", "dream machine",
                            "dream router", "udr", "uck", "cloud key", "router", "console")):
        # Cloud Key is a controller host, not a router, but it still sits at the top tier.
        return "gateway"
    # NB: "flex" alone is NOT a switch signal — FlexHD is an AP and "G3 Flex" is a
    # Protect camera. Real switches all carry usw/us-/poe/switch, so no switch relies
    # on it. (The AP check below owns "flexhd".)
    if any(t in s for t in ("switch", "usw", "us-", "poe", "aggregation")):
        return "switch"
    if any(t in s for t in ("uap", "u6", "u7", "u5", "uwb", "nanohd", "flexhd", "ap ",
                            "access point", "-ap", "lite", "lr", "pro", "mesh", "iw", "swiss")):
        return "ap"
    return "other"


def _norm_state(v) -> str:
    """Normalise a device status to online|offline|pending."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "online" if int(v) == 1 else "offline"
    s = str(v or "").strip().lower()
    if s in ("online", "connected", "up", "ok", "1", "true", "adopted"):
        return "online"
    if s in ("pending", "provisioning", "adopting", "updating", "upgrading"):
        return "pending"
    return "offline"


def _norm_device(d: dict, host_id: str | None, host_name: str | None) -> dict:
    model = _first(d, "model", "shortname", "shortName", default="")
    product = _first(d, "productLine", "product_line", "type", "deviceType", default="")
    name = _first(d, "name", "hostname", "displayName", default="") or model or "device"
    role = _classify(str(model), str(product), str(name))
    return {
        "host_id": host_id,
        "host_name": host_name,
        "mac": (str(_first(d, "mac", "macAddress", default="")).lower() or None),
        "name": name,
        "model": model or None,
        "type": role,
        "state": _norm_state(_first(d, "status", "state", "adoptionStatus")),
        "version": _first(d, "version", "firmwareVersion", "fwVersion"),
        "ip": _first(d, "ip", "ipAddress", "reportedIp"),
        "uptime": _first(d, "uptime", "uptimeSec"),
        "clients": _first(d, "numClients", "clientCount", "num_sta", "numSta", "connectedClients"),
        "uplink_mac": (str(_first((d.get("uplink") or {}), "mac", "uplinkMac", default="")).lower() or None)
        if isinstance(d.get("uplink"), dict) else None,
    }


def _norm_host(h: dict) -> dict:
    rs = h.get("reportedState") if isinstance(h.get("reportedState"), dict) else {}
    hw = h.get("hardware") if isinstance(h.get("hardware"), dict) else {}
    return {
        "id": _first(h, "id", "hostId", "_id"),
        "name": _first(h, "name", default=None) or _first(rs, "hostname", "name")
        or _first(hw, "name") or "console",
        "state": _norm_state(_first(rs, "state", "status") or _first(h, "status", default="online")),
        "ip": _first(rs, "ip", "ipAddress") or _first(h, "ipAddress"),
    }


def _extract_devices(body_data, hosts_by_id: dict) -> list[dict]:
    """List Devices may return a flat device list OR host-grouped entries
    (``[{hostId, hostName, devices:[...]}]``). Handle both."""
    out: list[dict] = []
    for entry in (body_data or []):
        if not isinstance(entry, dict):
            continue
        inner = entry.get("devices")
        if isinstance(inner, list):  # host-grouped
            hid = _first(entry, "hostId", "id")
            hname = _first(entry, "hostName", "name") or (hosts_by_id.get(hid) or {}).get("name")
            for d in inner:
                if isinstance(d, dict):
                    out.append(_norm_device(d, hid, hname))
        else:  # flat device
            hid = _first(entry, "hostId", "host_id")
            hname = (hosts_by_id.get(hid) or {}).get("name")
            out.append(_norm_device(entry, hid, hname))
    return out


def _extract_isp(body_data, hosts_by_id: dict) -> list[dict]:
    """Normalise ISP/WAN metric samples into a per-host WAN health summary."""
    out: list[dict] = []
    for entry in (body_data or []):
        if not isinstance(entry, dict):
            continue
        hid = _first(entry, "hostId", "host_id")
        periods = entry.get("periods") if isinstance(entry.get("periods"), list) else None
        latest = periods[-1] if periods else entry
        m = latest.get("data") if isinstance(latest, dict) and isinstance(latest.get("data"), dict) else latest
        m = m if isinstance(m, dict) else {}
        wan = _first(m, "wan", default={}) if isinstance(_first(m, "wan"), dict) else {}
        merged = {**m, **wan}
        out.append({
            "host_id": hid,
            "host_name": (hosts_by_id.get(hid) or {}).get("name"),
            "isp": _first(merged, "ispName", "isp", "ispAsn"),
            "wan_ip": _first(merged, "wanIp", "ip"),
            "status": _norm_state(_first(merged, "status", "state", default="online")),
            "latency_ms": _first(merged, "latencyAvgMs", "latencyMs", "avgLatency"),
            "download_kbps": _first(merged, "downloadKbps", "download"),
            "upload_kbps": _first(merged, "uploadKbps", "upload"),
            "downtime_sec": _first(merged, "downtime", "downtimeSec"),
            "uptime_pct": _first(merged, "uptime", "uptimePct", "availability"),
        })
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def list_hosts(key: str) -> list[dict]:
    return _paged(key, "/hosts")


def list_devices(key: str) -> list:
    return _paged(key, "/devices")


def isp_metrics(key: str) -> list:
    # Documented as GET /isp-metrics. Best-effort — skipped (empty) on any error,
    # since the exact sample shape varies by console/plan.
    return _paged(key, "/isp-metrics")


def test_key(key: str) -> tuple[bool, str]:
    """Validate an API key with a cheap call. Returns (ok, message)."""
    if not (key or "").strip():
        return False, "API key is empty"
    try:
        _request(key, "/hosts", params={"pageSize": 1})
        return True, "ok"
    except UnifiError as exc:
        return False, str(exc)


def collect(key: str, host_ids: list | None = None) -> dict:
    """Poll a UniFi account and return a normalised snapshot.

    ``host_ids`` restricts which consoles are included: an empty/None list means
    "all consoles the key can see" (the picker still gets the full ``hosts`` list,
    but ``devices``/``isp`` are filtered to the selection so the map, table and
    alerts only cover the chosen consoles).

    Each sub-call is isolated so one failing endpoint (e.g. ISP metrics) doesn't
    sink the whole snapshot. ``ok`` is True when the core inventory was reachable.
    """
    snap: dict = {"ok": False, "error": None, "hosts": [], "devices": [], "isp": [], "edges": []}
    try:
        raw_hosts = list_hosts(key)
    except UnifiError as exc:
        snap["error"] = str(exc)
        return snap  # auth/transport failure — nothing else will work
    hosts = [_norm_host(h) for h in raw_hosts if isinstance(h, dict)]
    hosts_by_id = {h["id"]: h for h in hosts if h.get("id")}
    snap["hosts"] = hosts                       # always ALL consoles, for the picker
    sel = set(host_ids or [])                   # empty => all
    try:
        devs = _extract_devices(list_devices(key), hosts_by_id)
        snap["devices"] = [d for d in devs if not sel or d.get("host_id") in sel]
    except UnifiError as exc:
        snap["error"] = f"devices: {exc}"
    try:
        isp = _extract_isp(isp_metrics(key), hosts_by_id)
        snap["isp"] = [w for w in isp if not sel or w.get("host_id") in sel]
    except UnifiError as exc:
        log.info("UniFi ISP metrics unavailable: %s", exc)  # non-fatal
    # Real topology (which device hangs under which) via the Connector Proxy →
    # local Network Integration API. Best-effort: consoles without the proxy
    # (old firmware) simply keep the role-tiered map.
    try:
        _enrich_topology(key, snap)
    except Exception as exc:  # pragma: no cover - defensive; never fail a poll
        log.info("UniFi topology enrichment skipped: %s", exc)
    snap["ok"] = True
    return snap


# --------------------------------------------------------------------------- #
# Topology enrichment (Connector Proxy → Network Integration API)
# --------------------------------------------------------------------------- #
def _canon_mac(m) -> str:
    """Canonical MAC (lowercased hex only) so cloud (aa:bb:..) and integration
    (aabb..) MACs correlate."""
    return "".join(c for c in str(m or "").lower() if c in "0123456789abcdef")


def proxy_get(key: str, console_id: str, path: str, params: dict | None = None):
    """GET a console's local Network Integration API through the Connector Proxy.

    Returns the ``data`` payload, or ``None`` on any error (console on old
    firmware / proxy unsupported / permission) so enrichment is strictly optional.
    """
    base = f"{CONNECTOR}/{console_id}/proxy/network/integration/v1"
    try:
        body = _request(key, path, base=base, params=params)
    except UnifiError:
        return None
    return body.get("data") if isinstance(body, dict) else None


def _clients_of(d: dict):
    return _first(d, "numClients", "clientCount", "num_sta", "connectedClients",
                  "numberOfConnectedClients", "clients")


def _uplink_mac(d: dict, id_to_mac: dict) -> str | None:
    """Canonical MAC of a device's uplink parent (or None for a root/gateway)."""
    up = d.get("uplink") if isinstance(d.get("uplink"), dict) else {}
    m = _first(up, "mac", "macAddress", "uplinkMac", "deviceMac")
    if m:
        return _canon_mac(m)
    pid = _first(up, "deviceId", "device_id", "uplinkDeviceId", "id")
    if pid is not None and str(pid) in id_to_mac:
        return id_to_mac[str(pid)]
    return None


def _console_topology(key: str, console_id: str, macs_wanted: set) -> dict:
    """Best-effort per-console topology. Returns
    ``{by_mac: {canon_mac: {uplink_mac, clients}}, edges: [{child_mac, parent_mac}]}``
    with canonical MACs. Only enriches devices in ``macs_wanted`` (bounds detail calls)."""
    out = {"by_mac": {}, "edges": []}
    sites = proxy_get(key, console_id, "/sites", params={"limit": 50})
    if not isinstance(sites, list):
        return out                      # no proxy / unsupported → nothing to add
    for site in sites:
        sid = _first(site, "id", "_id", "name") if isinstance(site, dict) else None
        if not sid:
            continue
        devs = proxy_get(key, console_id, f"/sites/{sid}/devices", params={"limit": 200})
        if not isinstance(devs, list):
            continue
        id_to_mac = {}
        for d in devs:
            if isinstance(d, dict):
                cm = _canon_mac(_first(d, "macAddress", "mac"))
                did = _first(d, "id", "_id", "deviceId")
                if cm and did is not None:
                    id_to_mac[str(did)] = cm
        count = 0
        for d in devs:
            if not isinstance(d, dict):
                continue
            cm = _canon_mac(_first(d, "macAddress", "mac"))
            did = _first(d, "id", "_id", "deviceId")
            if not cm or did is None or (macs_wanted and cm not in macs_wanted):
                continue
            if count >= _TOPO_MAX_DEVICES:
                break
            count += 1
            # The list item may already carry uplink; else fetch device detail.
            detail = d if isinstance(d.get("uplink"), dict) else (
                proxy_get(key, console_id, f"/sites/{sid}/devices/{did}") or d)
            up = _uplink_mac(detail, id_to_mac)
            out["by_mac"][cm] = {"uplink_mac": up, "clients": _clients_of(detail)}
            if up and up != cm:
                out["edges"].append({"child_mac": cm, "parent_mac": up})
    return out


def _enrich_topology(key: str, snap: dict) -> None:
    """Attach uplink_mac + client counts to devices and fill snap['edges'] with real
    parent→child links, using the device MAC strings the dashboard already holds."""
    devices = snap.get("devices") or []
    if not devices:
        return
    canon2mac = {}
    by_console: dict = {}
    for d in devices:
        cm = _canon_mac(d.get("mac"))
        if cm:
            canon2mac[cm] = d["mac"]
        hid = d.get("host_id")
        if hid:
            by_console.setdefault(hid, set()).add(cm)
    edges, seen = [], set()
    for console_id, macs in by_console.items():
        topo = _console_topology(key, console_id, macs)
        if not topo["by_mac"]:
            continue
        for d in devices:
            if d.get("host_id") != console_id:
                continue
            info = topo["by_mac"].get(_canon_mac(d.get("mac")))
            if not info:
                continue
            if info.get("uplink_mac"):
                d["uplink_mac"] = canon2mac.get(info["uplink_mac"]) or d.get("uplink_mac")
            if info.get("clients") is not None:
                d["clients"] = info["clients"]
        for e in topo["edges"]:
            cm, pm = canon2mac.get(e["child_mac"]), canon2mac.get(e["parent_mac"])
            if cm and pm and cm != pm and (cm, pm) not in seen:
                seen.add((cm, pm))
                edges.append({"child_mac": cm, "parent_mac": pm})
    if edges:
        snap["edges"] = edges

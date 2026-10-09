"""UniFi Site Manager (cloud) API client.

Server-side integration with Ubiquiti's official **Site Manager API**
(``https://api.ui.com/v1``), authenticated with a per-account **API key**
(``X-API-KEY``). One key gives cross-console inventory + ISP/WAN health, so the
RMM server polls UniFi directly — no LAN node/agent required (unlike SNMP).

Kept FastAPI-free and defensive so it's unit-testable and tolerant of the
versioned cloud schema: :func:`collect` normalises whatever the API returns into
a stable snapshot the dashboard + alerter consume::

    {ok, error, hosts:[...], devices:[...], isp:[...], edges:[...],
     networks:[...], networks_read:[console ids], networks_unread:[console ids],
     vpns:[...], vpns_read:[...], vpns_unread:[...], classic_read:[...]}

A switch's or gateway's device carries ``ports``: state, speed and PoE from
the integration API, and -- where the console's classic API answers through
the connector too -- each port's name, VLANs and what is plugged into it.

The cloud API exposes inventory + ISP metrics but **not** per-port/uplink
topology, so ``edges`` is empty and the dashboard tiers the network map by device
role (Internet → gateway → switches → APs). Richer per-port/uplink data would come
from the Connector Proxy → local Network Integration API as a later enhancement.
"""
from __future__ import annotations

import ipaddress
import logging
import re

import httpx

log = logging.getLogger("rmm.unifi")

SITE_MGR = "https://api.ui.com/v1"
CONNECTOR = "https://api.ui.com/v1/connector/consoles"   # + /{consoleId}/proxy/network/integration/v1
_TIMEOUT = 20.0
_TOPO_MAX_DEVICES = 60   # cap detail calls per console (bounds a poll's proxy calls)
_NET_MAX_DETAILS = 64    # the same for networks: one detail call each


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
# UniFi Protect: recorders first ("UNVR Instant" is no camera), then cameras --
# both before the switch check, which "PoE" in a camera's name would satisfy.
_NVR = re.compile(r"\b[ue]?nvr\b|unvr")
_CAMERA = re.compile(r"\buvc\b|uvc[- ]|\bg[3-6][ -]|camera|bullet|\bdome\b|turret|doorbell|\bptz\b"
                     r"|\bai (pro|360|bullet|theta|dslr|turret|dome|ptz)\b")


def _classify(*hints: str) -> str:
    """Map a device's model/product-line/name hints to a role."""
    s = " ".join(h for h in hints if h).lower()
    product = (hints[1] if len(hints) > 1 else "") or ""
    if _NVR.search(s):
        return "nvr"
    if _CAMERA.search(s) or ("protect" in product.lower() and "cloud key" not in s and "uck" not in s):
        return "camera"
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
    # The networks (subnets, VLANs, DHCP) each console has, the same way: a
    # console on firmware without them is named as not read, never as empty.
    try:
        _read_networks(key, snap)
    except Exception as exc:  # pragma: no cover - defensive; never fail a poll
        log.info("UniFi networks skipped: %s", exc)
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


def proxy_one(key: str, console_id: str, path: str) -> dict | None:
    """One object from a console's integration API: GET by id answers with the
    object itself rather than wrapped in ``data``; either is taken."""
    try:
        body = _request(key, path, base=f"{CONNECTOR}/{console_id}/proxy/network/integration/v1")
    except UnifiError:
        return None
    data = body.get("data")
    if isinstance(data, dict):
        return data
    return body if data is None and any(k in body for k in ("id", "macAddress", "management", "vlanId")) else None


def classic_get(key: str, console_id: str, site_ref: str, path: str) -> list | None:
    """GET from a console's classic Network API (``/api/s/<site>/...``) through
    the connector. What it says that the integration API does not -- a port's
    VLANs, what is plugged into it, a VPN's settings -- is a bonus: None when
    the console or the connector does not let it through."""
    try:
        body = _request(key, path, base=f"{CONNECTOR}/{console_id}/proxy/network/api/s/{site_ref}")
    except UnifiError:
        return None
    data = body.get("data")
    return data if isinstance(data, list) else None


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
            # The list item may already carry uplink and ports; else fetch the
            # device's detail, which has both.
            ready = isinstance(d.get("uplink"), dict) and isinstance(d.get("interfaces"), dict)
            detail = d if ready else (proxy_one(key, console_id, f"/sites/{sid}/devices/{did}") or d)
            up = _uplink_mac(detail, id_to_mac)
            out["by_mac"][cm] = {"uplink_mac": up, "clients": _clients_of(detail),
                                 "ports": _integration_ports(detail)}
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
            if info.get("ports") and d.get("type") in ("gateway", "switch"):
                d["ports"] = info["ports"]
        for e in topo["edges"]:
            cm, pm = canon2mac.get(e["child_mac"]), canon2mac.get(e["parent_mac"])
            if cm and pm and cm != pm and (cm, pm) not in seen:
                seen.add((cm, pm))
                edges.append({"child_mac": cm, "parent_mac": pm})
    if edges:
        snap["edges"] = edges


# --------------------------------------------------------------------------- #
# Networks (Connector Proxy -> Network Integration API, UniFi Network 10+)
# --------------------------------------------------------------------------- #
_MANAGEMENT = {"GATEWAY": "gateway", "SWITCH": "switch", "UNMANAGED": "vlan"}


def _strings(v) -> list:
    return [str(x) for x in v if x] if isinstance(v, list) else []


def _norm_network(n: dict, detail: dict | None, site: str, console_id: str, console: str) -> dict:
    """One network as the RMM keeps it: its subnet as a network address, the
    gateway's address in it, and how DHCP hands out addresses. ``dhcp`` is
    ``server``, ``relay`` or ``off``; None when nothing says (a VLAN only, or
    a detail that could not be read)."""
    d = detail if isinstance(detail, dict) else n
    v4 = d.get("ipv4Configuration") if isinstance(d.get("ipv4Configuration"), dict) else None
    management = str(_first(n, "management") or _first(d, "management") or "")
    out = {
        "id": str(_first(n, "id", "_id") or ""), "console_id": console_id, "console": console, "site": site,
        "name": _first(d, "name") or _first(n, "name") or "",
        "vlan": _first(d, "vlanId", "vlan"),
        "management": _MANAGEMENT.get(management.upper(), management.lower()),
        "enabled": _first(d, "enabled", default=True), "default": bool(_first(d, "default", default=False)),
        "isolated": _first(d, "isolationEnabled"), "internet": _first(d, "internetAccessEnabled"),
        "device_id": _first(d, "deviceId"), "detail": isinstance(detail, dict),
        "gateway": "", "prefix": None, "subnet": "", "extra_subnets": [], "dhcp": None,
        "dhcp_start": "", "dhcp_stop": "", "lease_seconds": None, "dns": [], "domain": "", "relay_servers": [],
    }
    if v4 is None:
        return out
    host, prefix = _first(v4, "hostIpAddress"), _first(v4, "prefixLength")
    out["gateway"] = str(host or "")
    try:
        out["prefix"] = int(prefix)
        out["subnet"] = str(ipaddress.ip_interface(f"{host}/{int(prefix)}").network)
    except (TypeError, ValueError):
        pass
    out["extra_subnets"] = _strings(v4.get("additionalHostIpSubnets"))
    dhcp = v4.get("dhcpConfiguration") if isinstance(v4.get("dhcpConfiguration"), dict) else None
    if dhcp is None:
        # Read in full and no DHCP: addresses are set by hand, or come from elsewhere.
        out["dhcp"] = "off" if out["detail"] else None
        return out
    mode = str(_first(dhcp, "mode") or "").upper()
    out["dhcp"] = "relay" if mode == "RELAY" else "off" if mode in ("NONE", "DISABLED", "OFF") else "server"
    rng = dhcp.get("ipAddressRange") if isinstance(dhcp.get("ipAddressRange"), dict) else {}
    out["dhcp_start"], out["dhcp_stop"] = str(rng.get("start") or ""), str(rng.get("stop") or "")
    lease = _first(dhcp, "leaseTimeSeconds")
    out["lease_seconds"] = int(lease) if isinstance(lease, (int, float)) else None
    out["dns"] = _strings(_first(dhcp, "dnsServerIpAddressesOverride", "dnsServers"))
    out["domain"] = str(_first(dhcp, "domainName") or "")
    out["relay_servers"] = _strings(_first(dhcp, "dhcpServerIpAddresses"))
    return out


def _network_detail(key: str, console_id: str, site_id, network_id) -> dict | None:
    """A network's detail. GET by id answers with the object itself rather
    than wrapped in ``data``; either is taken."""
    try:
        body = _request(key, f"/sites/{site_id}/networks/{network_id}",
                        base=f"{CONNECTOR}/{console_id}/proxy/network/integration/v1")
    except UnifiError:
        return None
    if isinstance(body.get("data"), dict):
        return body["data"]
    return body if ("ipv4Configuration" in body or "management" in body or "vlanId" in body) else None


def _sites(key: str, console_id: str) -> list | None:
    sites = proxy_get(key, console_id, "/sites", params={"limit": 50})
    if not isinstance(sites, list):
        return None
    return [x for x in sites if isinstance(x, dict) and _first(x, "id", "_id")]


def _console_networks(key: str, console_id: str, console: str, sites: list) -> list | None:
    """Every network of one console, with the detail that holds its subnet and
    DHCP. None when the console does not answer (no proxy, or firmware older
    than UniFi Network 10, which does not list networks)."""
    out, read_any, details = [], False, 0
    for site in sites:
        sid = _first(site, "id", "_id")
        nets = proxy_get(key, console_id, f"/sites/{sid}/networks", params={"limit": 200})
        if not isinstance(nets, list):
            continue
        read_any = True
        site_name = str(_first(site, "name", "internalReference") or "")
        switch_macs = None
        for n in nets:
            nid = _first(n, "id", "_id") if isinstance(n, dict) else None
            if not nid:
                continue
            detail = None
            # The list leaves the subnet out; a VLAN-only network has none.
            if str(n.get("management") or "").upper() != "UNMANAGED" and details < _NET_MAX_DETAILS:
                details += 1
                detail = _network_detail(key, console_id, sid, nid)
            net = _norm_network(n, detail, site_name, console_id, console)
            # A network a switch routes: which switch, by its MAC.
            if net["device_id"]:
                if switch_macs is None:
                    devs = proxy_get(key, console_id, f"/sites/{sid}/devices", params={"limit": 200}) or []
                    switch_macs = {str(_first(x, "id", "_id")): _canon_mac(_first(x, "macAddress", "mac"))
                                   for x in devs if isinstance(x, dict)}
                net["router_mac"] = switch_macs.get(str(net["device_id"])) or ""
            net.pop("device_id", None)
            out.append(net)
    return out if read_any else None


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #
def _integration_ports(detail) -> list:
    """A device's ports as the integration API has them: up or down, speed, PoE."""
    ifs = detail.get("interfaces") if isinstance(detail, dict) else None
    out = []
    for p in (ifs.get("ports") if isinstance(ifs, dict) else None) or []:
        if not isinstance(p, dict) or p.get("idx") is None:
            continue
        poe = p.get("poe") if isinstance(p.get("poe"), dict) else None
        try:
            idx = int(p["idx"])
        except (TypeError, ValueError):
            continue
        out.append({"idx": idx, "up": str(p.get("state") or "").upper() == "UP",
                    "speed": p.get("speedMbps"), "max_speed": p.get("maxSpeedMbps"),
                    "connector": p.get("connector") or "",
                    "poe": bool(poe.get("enabled")) if poe else None,
                    "poe_on": str(poe.get("state") or "").upper() in ("UP", "LIMITED") if poe else False})
    return out


_VLAN_PURPOSES = ("corporate", "guest", "vlan-only")


def _classic_vlans(confs: list) -> dict:
    """The networks that can ride a port, by their classic id: name and VLAN
    (an untagged network is VLAN 1)."""
    nets = {}
    for n in confs or []:
        if not isinstance(n, dict) or n.get("purpose") not in _VLAN_PURPOSES or not n.get("_id"):
            continue
        vlan = n.get("vlan") if (n.get("vlan_enabled") or n.get("purpose") == "vlan-only") else None
        try:
            vlan = int(vlan) if vlan not in (None, "") else 1
        except (TypeError, ValueError):
            vlan = 1
        nets[str(n["_id"])] = {"name": str(n.get("name") or ""), "vlan": vlan}
    return nets


def _vlan_label(net: dict) -> str:
    return f"{net['name']} ({net['vlan']})"


def _port_vlans(entry: dict, override: dict, profiles: dict, nets: dict) -> dict:
    """Which VLANs a port carries: its native (untagged) network, and the tagged
    ones -- ``all``, a list, or none. A port profile decides when the port has
    one; otherwise what is set on the port itself."""
    prof = profiles.get(str(override.get("portconf_id") or entry.get("portconf_id") or "")) or {}
    srcs = [prof, override, entry] if prof else [override, entry]

    def pick(k):
        return next((x[k] for x in srcs if x.get(k) not in (None, "")), None)

    forward = str(pick("forward") or "")
    if forward == "disabled":
        return {"disabled": True, "native": "", "tagged": [], "profile": prof.get("name") or ""}
    native_id = str(pick("native_networkconf_id") or "")
    native = nets.get(native_id) or next((n for n in nets.values() if n["vlan"] == 1), None)
    mgmt = str(pick("tagged_vlan_mgmt") or "")
    excluded = {str(x) for x in pick("excluded_networkconf_ids") or []}
    ordered = sorted(nets.items(), key=lambda kv: (kv[1]["vlan"], kv[1]["name"]))
    if mgmt == "block_all" or forward == "native":
        tagged = []
    elif forward == "customize" and not mgmt:
        wanted = {str(x) for x in pick("tagged_networkconf_ids") or []}
        tagged = [_vlan_label(n) for i, n in ordered if i in wanted]
    elif mgmt == "custom" or excluded:
        tagged = [_vlan_label(n) for i, n in ordered if i not in excluded and n is not native]
    else:
        tagged = "all"
    return {"disabled": False, "native": _vlan_label(native) if native else "", "tagged": tagged,
            "profile": prof.get("name") or ""}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _classic_ports(dev: dict, profiles: dict, nets: dict, clients: dict, below: dict) -> dict:
    """A device's ports as the classic API has them, by number: name, VLANs,
    PoE draw, and what is plugged in -- a UniFi device below it, or clients."""
    cm = _canon_mac(dev.get("mac"))
    overrides = {}
    for o in dev.get("port_overrides") or []:
        try:
            overrides[int(o["port_idx"])] = o
        except (KeyError, TypeError, ValueError):
            continue
    out = {}
    for e in dev.get("port_table") or []:
        try:
            idx = int(e["port_idx"])
        except (KeyError, TypeError, ValueError):
            continue
        o = overrides.get(idx, {})
        poe_mode = str(e.get("poe_mode") or "")
        out[idx] = {"name": str(o.get("name") or e.get("name") or ""), "up": bool(e.get("up")),
                    "speed": e.get("speed") if e.get("up") else None, "uplink": bool(e.get("is_uplink")),
                    "media": str(e.get("media") or ""),
                    "poe": (poe_mode not in ("", "off")) if e.get("port_poe") else None,
                    "poe_watts": _num(e.get("poe_power")) if e.get("port_poe") else None,
                    "mode": "" if str(e.get("op_mode") or "switch") == "switch" else str(e["op_mode"]),
                    "lag": bool(e.get("aggregated_by")),
                    **_port_vlans(e, o, profiles, nets),
                    "clients": clients.get((cm, idx), [])[:20], "device_mac": below.get((cm, idx), "")}
    return out


# --------------------------------------------------------------------------- #
# VPN
# --------------------------------------------------------------------------- #
_VPN_PURPOSE = {"site-vpn": "site-to-site", "remote-user-vpn": "remote-access", "vpn-client": "client"}


def _protocol(text) -> str:
    low = str(text or "").lower()
    for word in ("ipsec", "wireguard", "openvpn", "l2tp", "teleport", "uid"):
        if word in low:
            return word
    return low


def _new_vpn(vid, name, kind, protocol, enabled) -> dict:
    return {"id": str(vid), "name": str(name or ""), "kind": kind, "protocol": protocol,
            "enabled": enabled is not False, "peer": "", "local_ip": "", "local_nets": [], "remote_nets": [],
            "client_pool": "", "port": None, "settings": "", "interface": "", "detail": False}


def _cidr_or(text) -> str:
    try:
        return str(ipaddress.ip_interface(str(text)).network)
    except ValueError:
        return str(text or "")


def _ipsec_settings(n: dict) -> str:
    """IKE and ESP as they must match at the other end: IKEv2 · IKE AES256/SHA256,
    DH 14 · ESP AES256/SHA256, PFS · route-based."""
    def up(*keys):
        return "/".join(str(n[k]).upper() for k in keys if n.get(k))
    parts = []
    if n.get("ipsec_key_exchange"):
        parts.append(str(n["ipsec_key_exchange"]).upper().replace("IKEV", "IKEv"))
    ike = [x for x in (up("ipsec_ike_encryption", "ipsec_ike_hash"),
                       f"DH {n['ipsec_ike_dh_group']}" if n.get("ipsec_ike_dh_group") else "",
                       f"{n['ipsec_ike_lifetime']} s" if n.get("ipsec_ike_lifetime") else "") if x]
    if ike:
        parts.append("IKE " + ", ".join(ike))
    esp = [x for x in (up("ipsec_esp_encryption", "ipsec_esp_hash"),
                       f"DH {n['ipsec_esp_dh_group']}" if n.get("ipsec_esp_dh_group") else "",
                       "PFS" if n.get("ipsec_pfs") else "",
                       f"{n['ipsec_esp_lifetime']} s" if n.get("ipsec_esp_lifetime") else "") if x]
    if esp:
        parts.append("ESP " + ", ".join(esp))
    if n.get("ipsec_dynamic_routing") is not None:
        parts.append("route-based" if n.get("ipsec_dynamic_routing") else "policy-based")
    return " · ".join(parts)


def _classic_vpn(n: dict) -> dict:
    """A VPN as the classic API keeps it, among the networks. Its keys and
    secrets (``x_...``) are never taken."""
    vpn = _new_vpn(n.get("_id"), n.get("name"), _VPN_PURPOSE.get(n.get("purpose"), "site-to-site"),
                   _protocol(n.get("vpn_type")), n.get("enabled"))
    vpn["detail"] = True
    vpn["peer"] = str(_first(n, "ipsec_peer_ip", "openvpn_remote_host", "openvpn_remote_address",
                             "wireguard_client_peer_ip", "remote_host", default="") or "")
    vpn["local_ip"] = str(_first(n, "ipsec_local_ip", "ipsec_local_identifier", "openvpn_local_address",
                                 default="") or "")
    remote = _first(n, "remote_vpn_subnets", "ipsec_remote_subnets", "remote_subnets", default=[])
    vpn["remote_nets"] = [str(x) for x in remote if x] if isinstance(remote, list) else []
    local = _first(n, "ipsec_local_subnets", "local_vpn_subnets", default=[])
    vpn["local_nets"] = [str(x) for x in local if x] if isinstance(local, list) else []
    if vpn["kind"] == "remote-access" and n.get("ip_subnet"):
        vpn["client_pool"] = _cidr_or(n["ip_subnet"])
    port = _first(n, "local_port", "openvpn_local_port", "wireguard_local_port", "wireguard_client_peer_port",
                  "openvpn_remote_port")
    try:
        vpn["port"] = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        vpn["port"] = None
    vpn["interface"] = str(_first(n, "ipsec_interface", "wireguard_interface", "l2tp_interface",
                                  "openvpn_interface", default="") or "")
    vpn["settings"] = _ipsec_settings(n) if vpn["protocol"] == "ipsec" else ""
    return vpn


def _console_vpns(key: str, console_id: str, sites: list, classic: dict) -> list | None:
    """The VPN servers and site-to-site tunnels a console has, as the
    integration API lists them (name, kind, on or off) -- with their settings
    where the classic API gives them. None when neither answers."""
    out, by_name, read = [], {}, False
    for site in sites:
        sid = _first(site, "id", "_id")
        for path, kind in ((f"/sites/{sid}/vpn/servers", "remote-access"),
                           (f"/sites/{sid}/vpn/site-to-site-tunnels", "site-to-site")):
            rows = proxy_get(key, console_id, path, params={"limit": 200})
            if not isinstance(rows, list):
                continue
            read = True
            for r in rows:
                if isinstance(r, dict) and r.get("id"):
                    vpn = _new_vpn(r["id"], r.get("name"), kind, _protocol(r.get("type")), r.get("enabled"))
                    out.append(vpn)
                    by_name[vpn["name"].strip().lower()] = vpn
    confs = classic.get("networkconf")
    if confs is not None:
        read = True
        for n in confs:
            if not isinstance(n, dict) or n.get("purpose") not in _VPN_PURPOSE:
                continue
            extra = _classic_vpn(n)
            listed = by_name.get(extra["name"].strip().lower())
            if listed:
                # Known by its integration id; the classic API adds the settings.
                listed.update({k: v for k, v in extra.items() if k not in ("id", "name", "kind")}
                              | ({"kind": extra["kind"]} if extra["kind"] == "client" else {}))
            else:
                out.append(extra)
    return out if read else None


# --------------------------------------------------------------------------- #
# Per console: networks, VPN, and what the classic API adds to the ports
# --------------------------------------------------------------------------- #
def _classic_of(key: str, console_id: str, sites: list) -> dict:
    """The classic API's networks, port profiles, devices and wired clients of
    a console's sites, as far as they answer."""
    got = {"networkconf": None, "portconf": None, "device": None, "sta": None}
    for site in sites:
        ref = str(_first(site, "internalReference", default="") or "")
        if not ref:
            continue
        for name, path in (("networkconf", "/rest/networkconf"), ("portconf", "/rest/portconf"),
                           ("device", "/stat/device"), ("sta", "/stat/sta")):
            rows = classic_get(key, console_id, ref, path)
            if rows is not None:
                got[name] = (got[name] or []) + [r for r in rows if isinstance(r, dict)]
    return got


def _apply_classic_ports(devices: list, classic: dict) -> bool:
    """Lay the classic API's port detail over the devices' ports. False when
    it gave none."""
    if classic.get("device") is None:
        return False
    nets = _classic_vlans(classic.get("networkconf") or [])
    profiles = {str(p.get("_id")): p for p in classic.get("portconf") or [] if p.get("_id")}
    clients: dict = {}
    for c in classic.get("sta") or []:
        if not c.get("is_wired") or not c.get("sw_mac") or c.get("sw_port") is None:
            continue
        try:
            spot = (_canon_mac(c["sw_mac"]), int(c["sw_port"]))
        except (TypeError, ValueError):
            continue
        clients.setdefault(spot, []).append({"mac": str(c.get("mac") or ""), "name": str(_first(
            c, "name", "hostname", default="") or ""), "ip": str(c.get("ip") or "")})
    below: dict = {}
    for dev in classic["device"]:
        up = dev.get("uplink") if isinstance(dev.get("uplink"), dict) else dev.get("last_uplink")
        if isinstance(up, dict) and up.get("uplink_mac") and up.get("uplink_remote_port") is not None:
            try:
                below[(_canon_mac(up["uplink_mac"]), int(up["uplink_remote_port"]))] = _canon_mac(dev.get("mac"))
            except (TypeError, ValueError):
                pass
    by_mac = {_canon_mac(dev.get("mac")): dev for dev in classic["device"]}
    for d in devices:
        if d.get("type") not in ("gateway", "switch"):
            continue
        dev = by_mac.get(_canon_mac(d.get("mac")))
        if not dev:
            continue
        extra = _classic_ports(dev, profiles, nets, clients, below)
        ports = {p["idx"]: p for p in d.get("ports") or []}
        for idx, more in extra.items():
            port = ports.setdefault(idx, {"idx": idx, "up": more["up"], "speed": more["speed"],
                                          "max_speed": None, "connector": more["media"], "poe": more["poe"],
                                          "poe_on": bool(more["poe_watts"])})
            port.update({k: v for k, v in more.items() if k not in ("up", "speed", "poe")})
        d["ports"] = [ports[i] for i in sorted(ports)]
        d["ports_vlans"] = True
    return True


def _read_networks(key: str, snap: dict) -> None:
    """Per console with devices in the snapshot: its networks, its VPNs, and
    what the classic API adds to the ports. ``*_read`` and ``*_unread`` say
    which consoles answered, so one that could not be read is not taken as
    having none."""
    names = {h.get("id"): h.get("name") for h in snap.get("hosts") or [] if isinstance(h, dict)}
    consoles, gateways = [], {}
    for d in snap.get("devices") or []:
        hid = d.get("host_id")
        if hid and hid not in consoles:
            consoles.append(hid)
        # A network the gateway routes, and a VPN, hang on that console's gateway.
        if hid and d.get("type") == "gateway" and hid not in gateways:
            gateways[hid] = _canon_mac(d.get("mac"))
    networks, read, unread = [], [], []
    vpns, vpn_read, vpn_unread, classic_read = [], [], [], []
    for console_id in consoles:
        console = names.get(console_id) or ""
        sites = _sites(key, console_id)
        if sites is None:
            unread.append(console_id)
            vpn_unread.append(console_id)
            continue
        nets = _console_networks(key, console_id, console, sites)
        if nets is None:
            unread.append(console_id)
        else:
            read.append(console_id)
            for n in nets:
                if n["management"] == "gateway" and not n.get("router_mac"):
                    n["router_mac"] = gateways.get(console_id, "")
                networks.append(n)
        classic = _classic_of(key, console_id, sites)
        if _apply_classic_ports([d for d in snap.get("devices") or [] if d.get("host_id") == console_id], classic):
            classic_read.append(console_id)
        found = _console_vpns(key, console_id, sites, classic)
        if found is None:
            vpn_unread.append(console_id)
            continue
        vpn_read.append(console_id)
        for v in found:
            vpns.append({**v, "console_id": console_id, "console": console,
                         "router_mac": gateways.get(console_id, "")})
    snap["networks"], snap["networks_read"], snap["networks_unread"] = networks, read, unread
    snap["vpns"], snap["vpns_read"], snap["vpns_unread"] = vpns, vpn_read, vpn_unread
    snap["classic_read"] = classic_read

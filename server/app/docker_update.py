"""Self-update the server container via the Docker socket (watchtower-style).

When ``/var/run/docker.sock`` is mounted and the server runs from a registry
image, the dashboard can pull the latest image and recreate this container in
place. Because a container can't cleanly recreate itself (stopping it kills the
process mid-call), the swap is performed by a short-lived **helper** container
launched from the freshly-pulled image: the server prepares a create-spec, kicks
off the helper, and exits; the helper stops the old container, recreates it from
the new image preserving its config, and starts it (rolling back on failure).

Everything is best-effort and gated on the socket being present, so a server
without the socket simply reports that in-UI updates are unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import socket as _socket
import time

log = logging.getLogger("rmm.update")

SOCK = os.environ.get("DOCKER_HOST_SOCK", "/var/run/docker.sock")
# Where the server writes the create-spec the helper reads (shared /data volume).
SPEC_PATH = os.environ.get("RMM_UPDATE_SPEC", "/data/.server_update.json")
# Registry image to track; defaults to the running container's own image.
IMAGE_OVERRIDE = os.environ.get("RMM_SERVER_IMAGE", "").strip()


def available() -> bool:
    """True when the Docker socket is reachable (feature can work)."""
    try:
        return os.path.exists(SOCK) and os.access(SOCK, os.R_OK | os.W_OK)
    except OSError:
        return False


def _request(method: str, path: str, body: dict | None = None,
             stream: bool = False, timeout: float = 120.0):
    """Minimal HTTP/1.1 client over the Docker unix socket (no extra deps)."""
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(SOCK)
    data = json.dumps(body).encode() if body is not None else b""
    headers = [f"{method} {path} HTTP/1.1", "Host: docker", "Accept: application/json",
               "Connection: close"]
    if body is not None:
        headers += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    req = ("\r\n".join(headers) + "\r\n\r\n").encode() + data
    conn.sendall(req)
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    conn.close()
    head, _, raw = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    try:
        code = int(status_line.split(" ")[1])
    except (IndexError, ValueError):
        code = 0
    # De-chunk transfer-encoded bodies (best effort).
    body_bytes = _dechunk(raw) if b"transfer-encoding: chunked" in head.lower() else raw
    return code, body_bytes


def _dechunk(raw: bytes) -> bytes:
    out, i = b"", 0
    try:
        while i < len(raw):
            j = raw.find(b"\r\n", i)
            if j < 0:
                break
            size = int(raw[i:j].split(b";")[0], 16)
            if size == 0:
                break
            out += raw[j + 2:j + 2 + size]
            i = j + 2 + size + 2
    except ValueError:
        return raw
    return out


def _json(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    code, raw = _request(method, path, body)
    try:
        return code, (json.loads(raw) if raw.strip() else None)
    except json.JSONDecodeError:
        return code, raw.decode(errors="replace")


def _self_container_id() -> str | None:
    """This container's id — from the hostname (Docker sets it to the id)."""
    hostid = _socket.gethostname()
    code, _ = _json("GET", f"/containers/{hostid}/json")
    if code == 200:
        return hostid
    # Fallback: scan cgroup for a 64-hex id.
    for p in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        try:
            with open(p) as f:
                txt = f.read()
        except OSError:
            continue
        import re
        m = re.search(r"\b([0-9a-f]{64})\b", txt)
        if m:
            return m.group(1)
    return None


def _inspect(cid: str) -> dict | None:
    code, data = _json("GET", f"/containers/{cid}/json")
    return data if code == 200 and isinstance(data, dict) else None


def _image_ref(inspect: dict) -> str:
    return IMAGE_OVERRIDE or inspect.get("Config", {}).get("Image", "")


def _pull(image: str) -> bool:
    if ":" in image.rsplit("/", 1)[-1]:
        name, _, tag = image.rpartition(":")
    else:
        name, tag = image, "latest"
    code, _ = _request("POST", f"/images/create?fromImage={name}&tag={tag}", body=None,
                       timeout=600)
    log.info("pull %s:%s -> HTTP %s", name, tag, code)
    return code == 200


def _image_id(image: str) -> str | None:
    code, data = _json("GET", f"/images/{image}/json")
    return data.get("Id") if code == 200 and isinstance(data, dict) else None


def status() -> dict:
    """Report whether an in-UI update is possible and whether one is staged."""
    if not available():
        return {"available": False, "reason": "Docker socket not mounted"}
    cid = _self_container_id()
    inspect = _inspect(cid) if cid else None
    if not inspect:
        return {"available": False, "reason": "Could not inspect own container"}
    image = _image_ref(inspect)
    if image.startswith("sha256:") or "@" in image:
        return {"available": False, "reason": "Server runs a pinned image digest"}
    running_id = inspect.get("Image")
    local_latest = _image_id(image)
    return {"available": True, "image": image, "container": cid,
            "running_image_id": running_id, "local_image_id": local_latest,
            "update_staged": bool(local_latest and running_id and local_latest != running_id)}


def check_for_update() -> dict:
    """Pull the tracked image and report if it differs from the running one."""
    st = status()
    if not st.get("available"):
        return st
    image = st["image"]
    pulled = _pull(image)
    new_id = _image_id(image)
    st["pulled"] = pulled
    st["local_image_id"] = new_id
    st["update_staged"] = bool(new_id and st.get("running_image_id") and
                               new_id != st["running_image_id"])
    return st


def start_update() -> dict:
    """Pull the latest image and launch the helper that recreates this container."""
    st = status()
    if not st.get("available"):
        raise RuntimeError(st.get("reason", "Update not available"))
    cid, image = st["container"], st["image"]
    inspect = _inspect(cid)
    if not inspect:
        raise RuntimeError("Could not inspect own container")
    if not _pull(image):
        raise RuntimeError("Failed to pull the latest image")

    name = inspect.get("Name", "").lstrip("/") or cid
    spec = _build_create_spec(inspect, image)
    plan = {"name": name, "old_id": cid, "image": image, "spec": spec}

    helper = _launch_helper(inspect, image, plan)
    log.info("update helper %s launched to recreate %s from %s", helper, name, image)
    return {"ok": True, "image": image, "container": name, "helper": helper,
            "note": "Server is updating and will restart shortly."}


def _build_create_spec(inspect: dict, new_image: str) -> dict:
    """Clone the running container's config onto the new image (watchtower-style)."""
    cfg = dict(inspect.get("Config", {}))
    cfg["Image"] = new_image
    host = inspect.get("HostConfig", {})
    nets = inspect.get("NetworkSettings", {}).get("Networks", {}) or {}
    netmode = str(host.get("NetworkMode", ""))
    # Host/container network mode disallows an explicit hostname + custom endpoints.
    if netmode.startswith(("host", "container")):
        cfg["Hostname"] = ""
        nets = {}
    body = dict(cfg)
    body["HostConfig"] = host
    if nets:
        # Strip runtime-only fields that can't be sent back on create.
        clean = {}
        for k, v in nets.items():
            v = dict(v)
            for drop in ("Aliases", "IPAMConfig", "Links", "MacAddress", "DriverOpts"):
                v.pop(drop, None)
            clean[k] = {"NetworkID": v.get("NetworkID")} if v.get("NetworkID") else {}
        body["NetworkingConfig"] = {"EndpointsConfig": clean}
    return body


def _launch_helper(inspect: dict, image: str, plan: dict) -> str | None:
    """Create + start a detached helper (the new image) that performs the swap.

    The recreate plan is passed entirely via an env var, so the helper needs only
    the Docker socket — no shared data volume to reason about."""
    # Only the socket is needed; pass the plan inline so there's no volume coupling.
    helper_body = {
        "Image": image,
        "Entrypoint": ["python", "-c", "from app import docker_update as d; d.run_helper()"],
        "Env": [f"RMM_UPDATE_PLAN={json.dumps(plan)}", f"DOCKER_HOST_SOCK={SOCK}"],
        "HostConfig": {"Binds": [f"{SOCK}:{SOCK}"], "AutoRemove": True,
                       "RestartPolicy": {"Name": "no"}},
        "Labels": {"com.leuffen.rmm.role": "updater"},
    }
    code, data = _json("POST", "/containers/create", helper_body)
    if code not in (200, 201) or not isinstance(data, dict):
        raise RuntimeError(f"Helper create failed (HTTP {code}): {data}")
    hid = data["Id"]
    code, _ = _json("POST", f"/containers/{hid}/start")
    if code not in (200, 204):
        raise RuntimeError(f"Helper start failed (HTTP {code})")
    return hid


# --------------------------------------------------------------------------- #
# Helper entrypoint — runs *inside* the throwaway helper container.
# --------------------------------------------------------------------------- #
def run_helper() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    time.sleep(3)  # let the old server flush its response and settle
    raw = os.environ.get("RMM_UPDATE_PLAN")
    try:
        if raw:
            plan = json.loads(raw)
        else:
            with open(SPEC_PATH) as f:
                plan = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("helper: cannot read update plan: %s", exc)
        return
    name, old_id, spec = plan["name"], plan["old_id"], plan["spec"]
    backup = f"{name}_old"
    try:
        _request("POST", f"/containers/{old_id}/stop?t=20")
        _json("POST", f"/containers/{old_id}/rename?name={backup}")
        code, data = _json("POST", f"/containers/create?name={name}", spec)
        if code not in (200, 201):
            raise RuntimeError(f"create new failed (HTTP {code}): {data}")
        new_id = data["Id"]
        code, _ = _json("POST", f"/containers/{new_id}/start")
        if code not in (200, 204):
            raise RuntimeError(f"start new failed (HTTP {code})")
        log.info("helper: recreated %s as %s", name, new_id)
        _request("DELETE", f"/containers/{old_id}?force=1")
    except Exception as exc:
        log.error("helper: update failed (%s) — rolling back", exc)
        _json("POST", f"/containers/{backup}/rename?name={name}")
        _request("POST", f"/containers/{old_id}/start")
    finally:
        try:
            os.remove(SPEC_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    run_helper()

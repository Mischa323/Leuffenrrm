"""Self-update the server container via the Docker socket (watchtower-style).

When ``/var/run/docker.sock`` is mounted and the server runs from a registry
image, the dashboard can pull the latest image and recreate this container in
place. Because a container can't cleanly recreate itself (stopping it kills the
process mid-call), the swap is performed by a short-lived **helper** container
launched from the freshly-pulled image: the server hands it a plan and keeps
answering; the helper stops the old container, recreates it from the new image,
waits for it to be healthy, and only then removes the old one -- putting the
old one back if the new one never comes up.

Three things this gets right that an earlier version did not:

  * **Only what was set on the container is carried over.** A container's
    Config is the image's defaults with the operator's settings on top; copying
    it whole ran the new image with the *old* image's command, environment
    defaults and health check. Now only what differs from the old image goes
    across. Once the tag has moved, Docker's newer image store cannot always
    look the old image up, so its defaults are noted at start-up; when they are
    unknown, none of them are carried.
  * **Started is not the same as working.** A release that crashes on boot
    starts fine. The old container stays until the new one reports healthy
    (the image has a HEALTHCHECK), or has run a while without restarting.
  * **Rolling back cannot fail on the name.** A new container that was created
    but did not work still holds the name; it is removed before the old one is
    renamed back.

Everything is best-effort and gated on the socket being present, so a server
without the socket simply reports that in-UI updates are unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket as _socket
import time

log = logging.getLogger("rmm.update")

SOCK = os.environ.get("DOCKER_HOST_SOCK", "/var/run/docker.sock")
# Registry image to track; defaults to the running container's own image.
IMAGE_OVERRIDE = os.environ.get("RMM_SERVER_IMAGE", "").strip()
VERSION_LABEL = "org.opencontainers.image.version"
# The running image's defaults, noted while they can still be read. A file and
# not a setting: settings are copied into the environment at start-up.
DEFAULTS_FILE = os.path.join(
    os.path.dirname(os.environ.get("RMM_DB_PATH", "/data/rmm.db")), ".image_defaults.json")
IMAGE_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck",
              "ExposedPorts", "Volumes", "StopSignal", "Shell", "OnBuild")


def available() -> bool:
    """True when the Docker socket is reachable (feature can work)."""
    try:
        return os.path.exists(SOCK) and os.access(SOCK, os.R_OK | os.W_OK)
    except OSError:
        return False


def _request(method: str, path: str, body: dict | None = None, timeout: float = 120.0):
    """Minimal HTTP/1.1 client over the Docker unix socket (no extra deps)."""
    conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(SOCK)
    data = json.dumps(body).encode() if body is not None else b""
    headers = [f"{method} {path} HTTP/1.1", "Host: docker", "Accept: application/json",
               "Connection: close"]
    if body is not None:
        headers += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + data)
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    conn.close()
    head, _, raw = buf.partition(b"\r\n\r\n")
    try:
        code = int(head.split(b"\r\n", 1)[0].decode(errors="replace").split(" ")[1])
    except (IndexError, ValueError):
        code = 0
    if b"transfer-encoding: chunked" in head.lower():
        raw = _dechunk(raw)
    return code, raw


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
    for p in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        try:
            with open(p) as f:
                found = re.search(r"\b([0-9a-f]{64})\b", f.read())
        except OSError:
            continue
        if found:
            return found.group(1)
    return None


def _inspect(cid: str) -> dict | None:
    code, data = _json("GET", f"/containers/{cid}/json")
    return data if code == 200 and isinstance(data, dict) else None


def _image_ref(inspect: dict) -> str:
    return IMAGE_OVERRIDE or inspect.get("Config", {}).get("Image", "")


def _image(image: str) -> dict | None:
    code, data = _json("GET", f"/images/{image}/json")
    return data if code == 200 and isinstance(data, dict) else None


def _version_of(info: dict | None) -> str | None:
    labels = ((info or {}).get("Config") or {}).get("Labels") or {}
    return (labels.get(VERSION_LABEL) or "").lstrip("v") or None


def _pull(image: str) -> bool:
    if ":" in image.rsplit("/", 1)[-1]:
        name, _, tag = image.rpartition(":")
    else:
        name, tag = image, "latest"
    code, _ = _request("POST", f"/images/create?fromImage={name}&tag={tag}", body=None,
                       timeout=600)
    log.info("pull %s:%s -> HTTP %s", name, tag, code)
    return code == 200


# --------------------------------------------------------------------------- #
# The running image's own defaults
# --------------------------------------------------------------------------- #
def remember_own_image() -> None:
    """Note the running image's defaults while they can still be read."""
    if not available():
        return
    try:
        cid = _self_container_id()
        inspect = _inspect(cid) if cid else None
        if not inspect:
            return
        try:
            with open(DEFAULTS_FILE) as fh:
                if json.load(fh).get("id") == inspect.get("Image"):
                    return
        except (OSError, ValueError):
            pass
        info = _image(inspect.get("Image", ""))
        if info:
            with open(DEFAULTS_FILE, "w") as fh:
                json.dump({"id": inspect.get("Image"), "config": info.get("Config") or {}}, fh)
    except Exception as exc:                       # never stop the server over this
        log.warning("could not note the image defaults: %r", exc)


def _image_defaults(inspect: dict) -> dict | None:
    info = _image(inspect.get("Image", ""))
    if info:
        return info.get("Config") or {}
    try:
        with open(DEFAULTS_FILE) as fh:
            saved = json.load(fh)
        if saved.get("id") == inspect.get("Image"):
            return saved.get("config") or {}
    except (OSError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------- #
# Status, check, start
# --------------------------------------------------------------------------- #
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
    local = _image(image)
    return {"available": True, "image": image, "container": cid,
            "running_image_id": running_id,
            "local_image_id": (local or {}).get("Id"),
            "waiting_version": _version_of(local),
            "update_staged": bool(local and running_id and local.get("Id") != running_id)}


def check_for_update() -> dict:
    """Pull the tracked image and report if it differs from the running one."""
    remember_own_image()
    st = status()
    if not st.get("available"):
        return st
    st["pulled"] = _pull(st["image"])
    local = _image(st["image"])
    st["local_image_id"] = (local or {}).get("Id")
    st["waiting_version"] = _version_of(local)
    st["update_staged"] = bool(local and st.get("running_image_id")
                               and local.get("Id") != st["running_image_id"])
    return st


def start_update() -> dict:
    """Pull the latest image and launch the helper that recreates this container."""
    remember_own_image()
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
    plan = {"name": name, "old_id": cid, "image": image,
            "spec": _build_create_spec(inspect, image)}
    helper = _launch_helper(inspect, image, plan)
    log.info("update helper %s launched to recreate %s from %s", helper, name, image)
    return {"ok": True, "image": image, "container": name, "helper": helper,
            "note": "Server is updating and will restart shortly."}


def _build_create_spec(inspect: dict, new_image: str) -> dict:
    """The running container's own configuration, pointed at the new image.

    Only what differs from the old image is carried: the operator's ports,
    volumes, networks, restart policy and environment. The command, health
    check and built-in defaults come from the new image.
    """
    cfg = dict(inspect.get("Config", {}))
    base = _image_defaults(inspect)
    if base is not None:
        image_env = set(base.get("Env") or [])
        cfg["Env"] = [e for e in (cfg.get("Env") or []) if e not in image_env]
        for key in IMAGE_KEYS:
            if key in cfg and cfg.get(key) == base.get(key):
                cfg.pop(key)
        image_labels = base.get("Labels") or {}
    else:
        # The old image's defaults are unknown. Taking none of them is the
        # safe side: the new image brings its own command and health check,
        # and of the environment only what is clearly this installation's --
        # its RMM_ settings, and anything the new image does not define.
        log.warning("image defaults unknown; carrying over only this installation's settings")
        fresh = (_image(new_image) or {}).get("Config") or {}
        fresh_keys = {e.split("=", 1)[0] for e in (fresh.get("Env") or [])}
        cfg["Env"] = [e for e in (cfg.get("Env") or [])
                      if e.startswith("RMM_") or e.split("=", 1)[0] not in fresh_keys]
        for key in IMAGE_KEYS:
            cfg.pop(key, None)
        image_labels = fresh.get("Labels") or {}
    cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items()
                     if image_labels.get(k) != v}
    cfg["Image"] = new_image

    host = inspect.get("HostConfig", {})
    nets = inspect.get("NetworkSettings", {}).get("Networks", {}) or {}
    # Host/container network mode disallows an explicit hostname + custom endpoints.
    if str(host.get("NetworkMode", "")).startswith(("host", "container")):
        cfg["Hostname"] = ""
        nets = {}
    body = dict(cfg)
    body["HostConfig"] = host
    if nets:
        body["NetworkingConfig"] = {"EndpointsConfig": {
            k: ({"NetworkID": v.get("NetworkID")} if v.get("NetworkID") else {})
            for k, v in nets.items()}}
    return body


def _launch_helper(inspect: dict, image: str, plan: dict) -> str | None:
    """Create + start a detached helper (the new image) that performs the swap.

    The recreate plan is passed entirely via an env var, so the helper needs only
    the Docker socket — no shared data volume to reason about."""
    helper_body = {
        "Image": image,
        "Entrypoint": ["python", "-c", "from app import docker_update as d; d.run_helper()"],
        "Env": [f"RMM_UPDATE_PLAN={json.dumps(plan)}", f"DOCKER_HOST_SOCK={SOCK}"],
        "HostConfig": {"Binds": [f"{SOCK}:{SOCK}"], "AutoRemove": True,
                       "RestartPolicy": {"Name": "no"}},
        "Labels": {"com.leuffen.rmm.role": "updater"},
        # The new image's health check is about the server, not this helper.
        "Healthcheck": {"Test": ["NONE"]},
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
def _settled(cid: str, wait: float = 120.0) -> tuple[bool, str]:
    """Whether a freshly started container is really up.

    With a health check (the image has one) the answer is Docker's; without
    one, running for a while without a restart is the best signal there is.
    """
    deadline = time.time() + wait
    running_since = None
    while time.time() < deadline:
        info = _inspect(cid) or {}
        state = info.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        if state.get("Status") in ("exited", "dead") or state.get("Restarting") \
                or (info.get("RestartCount") or 0) > 0:
            return False, f"it stopped (exit code {state.get('ExitCode')})"
        if health == "healthy":
            return True, "healthy"
        if health == "unhealthy":
            return False, "its health check failed"
        if health is None and state.get("Running"):
            running_since = running_since or time.time()
            if time.time() - running_since >= 15:
                return True, "running"
        time.sleep(2)
    return False, "it did not become ready in time"


def run_helper() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    time.sleep(3)  # let the old server flush its response and settle
    try:
        plan = json.loads(os.environ["RMM_UPDATE_PLAN"])
    except (KeyError, json.JSONDecodeError) as exc:
        log.error("helper: cannot read update plan: %s", exc)
        return
    name, old_id, spec = plan["name"], plan["old_id"], plan["spec"]
    backup = f"{name}_old"
    new_id = None
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
        # The previous version is only thrown away once the new one works.
        up, why = _settled(new_id)
        if not up:
            raise RuntimeError(f"the new version did not come up: {why}")
        log.info("helper: recreated %s as %s (%s)", name, new_id, why)
        _request("DELETE", f"/containers/{old_id}?force=1")
    except Exception as exc:
        log.error("helper: update failed (%s) — rolling back", exc)
        # A new container that was made but did not work still holds the name,
        # and then the old one cannot have it back -- leaving nothing running.
        if new_id:
            _request("DELETE", f"/containers/{new_id}?force=1")
        _json("POST", f"/containers/{old_id}/rename?name={name}")
        _request("POST", f"/containers/{old_id}/start")


if __name__ == "__main__":
    run_helper()

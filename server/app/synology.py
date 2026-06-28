"""Synology NAS support: on-the-fly .spk assembly + Package Center catalog.

Kept free of FastAPI/DB imports so it stays unit-testable on its own. The server
(:mod:`main`) wires these into HTTP endpoints; the .spk is assembled per download
from the vendored slim agent (``agent/syno_*.py`` + ``handlers.py``) and the
packaging assets (``packaging/synology/``), with the enrolment config baked in —
nothing is committed or released, so an ``AGENT_VERSION`` bump surfaces in Package
Center as an available upgrade.
"""
from __future__ import annotations

import io
import json
import math
import os
import struct
import tarfile
import time
import zlib

# Agent payload bundled into the SPK (pure stdlib — no psutil/websockets needed).
AGENT_FILES = ("syno_agent.py", "syno_inventory.py", "handlers.py")

_ICON_CACHE: dict[int, bytes] = {}


def icon_png(size: int) -> bytes:
    """Render the Leuffen shield icon as a PNG (pure stdlib — no Pillow)."""
    if size in _ICON_CACHE:
        return _ICON_CACHE[size]
    top, bot = (59, 130, 246), (37, 99, 235)          # brand gradient
    radius = size * 0.18
    s = size / 64.0
    p = [(20 * s, 33 * s), (28 * s, 42 * s), (45 * s, 22 * s)]  # check polyline
    stroke = max(2.0, 4.5 * s)

    def seg_dist(px, py, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 (none) per scanline
        t = y / size
        r0 = int(top[0] * (1 - t) + bot[0] * t)
        g0 = int(top[1] * (1 - t) + bot[1] * t)
        b0 = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(size):
            a = 255
            cx, cy = min(x, size - 1 - x), min(y, size - 1 - y)
            if cx < radius and cy < radius:           # rounded corners (1px AA)
                d = math.hypot(radius - cx, radius - cy)
                if d >= radius:
                    a = 0
                elif d > radius - 1:
                    a = int(255 * (radius - d))
            r, g, b = r0, g0, b0
            dd = min(seg_dist(x, y, p[0], p[1]), seg_dist(x, y, p[1], p[2]))
            if dd < stroke:                           # white check overlay (AA edge)
                e = 1.0 if dd < stroke - 1 else max(0.0, stroke - dd)
                r = int(r * (1 - e) + 255 * e)
                g = int(g * (1 - e) + 255 * e)
                b = int(b * (1 - e) + 255 * e)
            raw += bytes((min(255, r), min(255, g), min(255, b), min(255, a)))

    def chunk(typ, data):
        return (struct.pack("!I", len(data)) + typ + data +
                struct.pack("!I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack("!IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           chunk(b"IEND", b""))
    _ICON_CACHE[size] = png
    return png


def _add(tf: tarfile.TarFile, name: str, data: bytes, mode: int, mtime: int) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    tf.addfile(ti, io.BytesIO(data))


def build_spk(*, agent_dir: str, pkg_dir: str, version: str,
              server_url: str, api_key: str, insecure: bool) -> bytes:
    """Assemble a noarch Synology .spk in memory with config baked in.

    SPK = an uncompressed tar of INFO + package.tgz (gzip tar of the agent) +
    scripts + conf + icons. Scripts are normalised to LF + mode 0755 so a Windows
    checkout's CRLF never breaks the DSM shebang."""
    now = int(time.time())

    # Inner payload (extracted to the package target dir on the NAS).
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.USTAR_FORMAT) as tf:
        for fn in AGENT_FILES:
            with open(os.path.join(agent_dir, fn), "rb") as f:
                _add(tf, fn, f.read(), 0o644, now)
        cfg = json.dumps({"server_url": server_url, "api_key": api_key,
                          "insecure_tls": bool(insecure)}).encode()
        _add(tf, "rmm_config.json", cfg, 0o600, now)
    payload_bytes = payload.getvalue()

    with open(os.path.join(pkg_dir, "INFO"), encoding="utf-8") as f:
        info = f.read().replace("__VERSION__", version)

    def text(rel):
        with open(os.path.join(pkg_dir, rel), encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n").encode("utf-8")

    spk = io.BytesIO()
    with tarfile.open(fileobj=spk, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        _add(tf, "INFO", info.encode("utf-8"), 0o644, now)
        _add(tf, "package.tgz", payload_bytes, 0o644, now)
        for name in ("start-stop-status", "postinst", "preuninst", "postuninst"):
            _add(tf, f"scripts/{name}", text(f"scripts/{name}"), 0o755, now)
        _add(tf, "conf/privilege", text("conf/privilege"), 0o644, now)
        _add(tf, "PACKAGE_ICON.PNG", icon_png(72), 0o644, now)
        _add(tf, "PACKAGE_ICON_256.PNG", icon_png(256), 0o644, now)
    return spk.getvalue()


def catalog(*, pub: str, org_id: str, token: str, version: str) -> dict:
    """The Package Center source response (SynoCommunity-compatible JSON)."""
    base = f"{pub}/syno/{org_id}/{token}"
    pkg = {
        "package": "LeuffenRMM",
        "version": version,
        "dname": "Leuffen RMM",
        "desc": "Leuffen RMM monitoring agent — reports NAS health (CPU, memory, "
                "storage, temperature, volume status) to your RMM server and enables "
                "remote management.",
        "link": f"{base}/leuffen-rmm.spk",
        "thumbnail": [f"{base}/icon.png"],
        "thumbnail_retina": [f"{base}/icon.png"],
        "qinst": True, "qstart": True, "qupgrade": True,
        "maintainer": "Leuffen", "maintainer_url": pub,
        "distributor": "Leuffen RMM", "distributor_url": pub,
        # No deppkgs: there is no DSM package literally named "Python3" (the real
        # ones are Python3.9 / SynoCommunity Python3.10+), so declaring it makes
        # Package Center fail with "packages are missing in the package server".
        # The agent discovers any installed Python 3 at runtime instead.
        "beta": False, "model": [], "changelog": "",
    }
    return {"packages": [pkg]}

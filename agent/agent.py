"""Leuffen RMM agent — cross-platform (Windows + Linux), low-footprint.

Holds a single outbound WebSocket to the server (works through NAT/firewalls). It
registers with full inventory, pushes metrics on an interval, and handles control
messages: shell, power, files, screen, Wake-on-LAN relay, and network scans (when
promoted to a node).

Design for low impact: event-driven (mostly asleep), cheap non-blocking metrics,
heavy screen deps imported only on demand, and below-normal process priority.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid

import psutil
import websockets

import handlers
import inventory
import netscan
from screen import ScreenSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rmm.agent")

HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    """Writable directory for config + device id (stable across runs).

    When packaged as a PyInstaller exe, ``__file__`` is a temp extraction dir, so
    use a fixed location (``RMM_DATA_DIR``, else %ProgramData%\\LeuffenRMM on
    Windows, else the executable's folder)."""
    env = os.environ.get("RMM_DATA_DIR")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            d = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "LeuffenRMM")
        else:
            d = os.path.dirname(sys.executable)
        os.makedirs(d, exist_ok=True)
        return d
    return HERE


def _load_config() -> dict:
    """Config precedence: env vars > bundled rmm_config.json."""
    cfg = {"server_url": os.environ.get("RMM_SERVER_URL"),
           "api_key": os.environ.get("RMM_API_KEY")}
    path = os.path.join(_data_dir(), "rmm_config.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                filecfg = json.load(f)
            cfg["server_url"] = cfg["server_url"] or filecfg.get("server_url")
            cfg["api_key"] = cfg["api_key"] or filecfg.get("api_key")
            cfg["insecure_tls"] = cfg.get("insecure_tls", filecfg.get("insecure_tls", False))
        except Exception:
            pass
    cfg["interval"] = float(os.environ.get("RMM_INTERVAL", "30"))
    # Env overrides file. Used to accept the server's self-signed certificate.
    env_insecure = os.environ.get("RMM_INSECURE_TLS")
    if env_insecure is not None:
        cfg["insecure_tls"] = env_insecure.lower() in ("1", "true", "yes")
    cfg["insecure_tls"] = bool(cfg.get("insecure_tls", False))
    return cfg


def _device_id() -> str:
    """Stable per-machine id, persisted in the data directory."""
    path = os.path.join(_data_dir(), "rmm_device_id")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    did = uuid.uuid4().hex
    try:
        with open(path, "w") as f:
            f.write(did)
    except Exception:
        pass
    return did


def _ws_url(server_url: str, api_key: str) -> str:
    base = server_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    return f"{base}/api/agents/ws?key={api_key}"


def _lower_priority() -> None:
    try:
        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:
        pass


def _collect_metrics() -> dict:
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
    except Exception:
        disk = None
    net = psutil.net_io_counters()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": vm.percent, "mem_total": vm.total, "mem_used": vm.used,
        "disk_percent": disk.percent if disk else None,
        "disk_total": disk.total if disk else None,
        "disk_used": disk.used if disk else None,
        "uptime": (__import__("time").time() - psutil.boot_time()),
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
    }


def _status_path() -> str:
    return os.path.join(_data_dir(), "status.json")


def _sync_flag_path() -> str:
    return os.path.join(_data_dir(), "sync_request")


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = _device_id()
        self.role = "agent"
        self.subnets: list[str] = []
        self.ws = None
        self.screen: ScreenSession | None = None
        self.last_sync: float | None = None

    def _write_status(self, connected: bool) -> None:
        """Publish a small status file the tray app reads (best effort)."""
        try:
            with open(_status_path(), "w") as f:
                json.dump({"connected": connected, "last_sync": self.last_sync,
                           "server_url": self.cfg.get("server_url"),
                           "hostname": socket.gethostname(), "updated": time.time()}, f)
        except Exception:
            pass

    def _ssl_context(self, url: str):
        """SSL context for wss:// connections.

        For a server with a self-signed cert (``insecure_tls``) verification is
        disabled; with a real cert (Let's Encrypt / reverse proxy) it stays on.
        """
        if not url.startswith("wss://"):
            return None
        import ssl
        ctx = ssl.create_default_context()
        if self.cfg.get("insecure_tls"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def run(self) -> None:
        _lower_priority()
        url = _ws_url(self.cfg["server_url"], self.cfg["api_key"])
        ssl_ctx = self._ssl_context(url)
        backoff = 2
        self._write_status(False)
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=30,
                                              ssl=ssl_ctx) as ws:
                    self.ws = ws
                    await self._register()
                    self.last_sync = time.time()
                    self._write_status(True)
                    backoff = 2
                    await asyncio.gather(self._metrics_loop(), self._recv_loop(),
                                         self._control_loop())
            except Exception as exc:
                log.warning("connection lost (%s); retrying in %ss", exc, backoff)
                self._write_status(False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _sync_now(self) -> None:
        """Force an immediate push of inventory + metrics (used by the tray)."""
        await self._register()
        await self._send({"type": "metrics", "metrics": _collect_metrics()})
        self.last_sync = time.time()
        self._write_status(True)

    async def _control_loop(self) -> None:
        """Watch for a sync-request flag dropped by the tray app."""
        flag = _sync_flag_path()
        while True:
            await asyncio.sleep(2)
            if os.path.exists(flag):
                try:
                    os.remove(flag)
                except OSError:
                    pass
                try:
                    await self._sync_now()
                    log.info("forced sync")
                except Exception:
                    return

    async def _send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def _register(self) -> None:
        inv = inventory.collect()
        await self._send({"type": "register", "id": self.id,
                          "hostname": inv["hostname"], "inventory": inv})
        log.info("registered as %s (%s)", inv["hostname"], self.id)

    async def _metrics_loop(self) -> None:
        psutil.cpu_percent(interval=None)  # prime the delta
        while True:
            await asyncio.sleep(self.cfg["interval"])
            try:
                await self._send({"type": "metrics", "metrics": _collect_metrics()})
                self.last_sync = time.time()
                self._write_status(True)
            except Exception:
                return

    async def _recv_loop(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            await self._handle(msg)

    async def _ack(self, rid: str | None, payload: dict) -> None:
        if rid:
            await self._send({"type": "ack", "rid": rid, "payload": payload})

    async def _handle(self, msg: dict) -> None:
        t = msg.get("type")
        rid = msg.get("rid")
        if t == "set_role":
            self.role = msg.get("role", "agent")
            self.subnets = msg.get("subnets", [])
            log.info("role set to %s, subnets=%s", self.role, self.subnets)
        elif t == "shell_run":
            res = await handlers.run_command(msg.get("cmd", ""))
            await self._ack(rid, res)
        elif t == "script_run":
            res = await handlers.run_script(msg.get("content", ""), msg.get("shell", "shell"),
                                            float(msg.get("timeout", 120)),
                                            env=msg.get("env"), files=msg.get("files"))
            await self._ack(rid, res)
        elif t == "shell_input":
            res = await handlers.run_command(msg.get("data", ""))
            await self._send({"type": "shell_output", "data": res["output"],
                              "code": res["code"]})
        elif t == "power":
            await self._ack(rid, handlers.power_action(msg.get("action", "")))
        elif t == "file_get":
            await self._ack(rid, handlers.file_get(msg.get("path", "")))
        elif t == "file_put":
            await self._ack(rid, handlers.file_put(msg.get("path", ""), msg.get("data", "")))
        elif t == "wol":
            try:
                netscan.send_magic_packet(msg["mac"], msg.get("broadcast") or "255.255.255.255",
                                          msg.get("port", 9))
                await self._ack(rid, {"ok": True})
            except Exception as exc:
                await self._ack(rid, {"ok": False, "error": str(exc)})
        elif t == "scan":
            asyncio.create_task(self._do_scan(msg.get("subnets") or self.subnets))
        elif t == "screen_start":
            await self._screen_start(msg)
        elif t == "screen_stop":
            if self.screen:
                self.screen.stop()
                self.screen = None
        elif t == "input":
            if self.screen:
                self.screen.input(msg)

    async def _do_scan(self, subnets: list[str]) -> None:
        if not subnets:
            return
        log.info("scanning %s", subnets)
        hosts = await netscan.scan(subnets)
        await self._send({"type": "scan_result", "hosts": hosts})

    async def _screen_start(self, msg: dict) -> None:
        if self.screen:
            self.screen.stop()

        async def send_bytes(b: bytes) -> None:
            await self.ws.send(b)

        self.screen = ScreenSession(send_bytes, fps=msg.get("fps", 4),
                                    quality=msg.get("quality", 50))
        err = await self.screen.start()
        if err:
            await self._send({"type": "shell_output", "data": f"[screen] {err}\n", "code": 1})
            self.screen = None


def _grant_users_writable(path: str) -> None:
    """On Windows, let the interactive user write to the data dir (the agent runs
    as SYSTEM; the tray runs per-user and drops a sync-request flag here)."""
    if os.name != "nt":
        return
    try:
        import subprocess
        subprocess.run(["icacls", path, "/grant", "*S-1-5-32-545:(OI)(CI)M", "/T", "/Q"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def main() -> None:
    cfg = _load_config()
    if not cfg.get("server_url") or not cfg.get("api_key"):
        log.error("Missing server_url/api_key. Set RMM_SERVER_URL and RMM_API_KEY "
                  "or ship rmm_config.json next to the agent.")
        sys.exit(1)
    _grant_users_writable(_data_dir())
    asyncio.run(Agent(cfg).run())


if __name__ == "__main__":
    main()

"""Connection manager: tracks agent WebSockets and bridges dashboard sessions.

Each agent holds one outbound WebSocket. The server keeps a ``device_id → socket``
registry so it can push control messages. REST-triggered actions that expect a
result (wake, power, file ops, scan) use a request/ack correlation table keyed by
a generated ``rid``. Interactive dashboard sockets (terminal, screen) are attached
so agent output frames can be fanned out to them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

_log = logging.getLogger("rmm.ws")


class AgentConn:
    def __init__(self, device_id: str, org_id: str, ws: WebSocket):
        self.device_id = device_id
        self.org_id = org_id
        self.ws = ws
        # dashboard sockets subscribed to this agent, by channel ('terminal'|'screen')
        self.subscribers: dict[str, set[WebSocket]] = {"terminal": set(), "screen": set()}

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send_json(msg)


class ConnectionManager:
    def __init__(self) -> None:
        self._agents: dict[str, AgentConn] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    # -- agent lifecycle ---------------------------------------------------- #
    async def register(self, device_id: str, org_id: str, ws: WebSocket) -> AgentConn:
        async with self._lock:
            conn = AgentConn(device_id, org_id, ws)
            self._agents[device_id] = conn
            return conn

    async def unregister(self, device_id: str, conn: "AgentConn | None" = None) -> None:
        """Remove a device's connection. When ``conn`` is given, only remove it if
        it is still the *current* connection — a newer reconnect (e.g. right after
        an agent self-update) may have already replaced it, and the stale handler's
        cleanup must not evict the live one (which would show the device offline
        while it's actually connected)."""
        async with self._lock:
            if conn is None or self._agents.get(device_id) is conn:
                self._agents.pop(device_id, None)

    def get(self, device_id: str) -> AgentConn | None:
        return self._agents.get(device_id)

    def is_online(self, device_id: str) -> bool:
        return device_id in self._agents

    def online_ids(self) -> set[str]:
        return set(self._agents.keys())

    # -- request/ack correlation ------------------------------------------- #
    async def request(self, device_id: str, msg: dict[str, Any], timeout: float = 15.0) -> dict:
        """Send a message expecting a correlated reply identified by ``rid``."""
        conn = self.get(device_id)
        if conn is None:
            raise RuntimeError("Agent not connected")
        rid = msg.setdefault("rid", _new_rid())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        try:
            await conn.send(msg)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, rid: str, payload: dict) -> None:
        fut = self._pending.get(rid)
        if fut and not fut.done():
            fut.set_result(payload)

    # -- dashboard bridging ------------------------------------------------ #
    def subscribe(self, device_id: str, channel: str, ws: WebSocket) -> None:
        conn = self.get(device_id)
        if conn:
            conn.subscribers.setdefault(channel, set()).add(ws)

    def unsubscribe(self, device_id: str, channel: str, ws: WebSocket) -> None:
        conn = self.get(device_id)
        if conn:
            conn.subscribers.get(channel, set()).discard(ws)

    async def fanout(self, device_id: str, channel: str, data: Any) -> None:
        conn = self.get(device_id)
        if not conn:
            return
        is_bytes = isinstance(data, (bytes, bytearray))
        dead = []
        for ws in list(conn.subscribers.get(channel, set())):
            try:
                if is_bytes:
                    await ws.send_bytes(data)
                    # Per-subscriber counters, read by _bridge_ws when it logs the
                    # session close (frames/throughput help diagnose drops).
                    ws._relay_frames = getattr(ws, "_relay_frames", 0) + 1
                    ws._relay_bytes = getattr(ws, "_relay_bytes", 0) + len(data)
                else:
                    await ws.send_json(data)
            except Exception as e:
                ws._relay_drops = getattr(ws, "_relay_drops", 0) + 1
                _log.warning("fanout %s send failed dev=%s: %r", channel, device_id, e)
                dead.append(ws)
        for ws in dead:
            conn.subscribers[channel].discard(ws)


def _new_rid() -> str:
    import uuid
    return uuid.uuid4().hex


manager = ConnectionManager()

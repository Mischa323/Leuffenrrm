"""Connection manager: tracks agent WebSockets and bridges dashboard sessions.

Each agent holds one outbound WebSocket. The server keeps a ``device_id → socket``
registry so it can push control messages. REST-triggered actions that expect a
result (wake, power, file ops, scan) use a request/ack correlation table keyed by
a generated ``rid``. Interactive dashboard sockets (terminal, screen) are attached
so agent output frames can be fanned out to them.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


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

    async def unregister(self, device_id: str) -> None:
        async with self._lock:
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
        dead = []
        for ws in list(conn.subscribers.get(channel, set())):
            try:
                if isinstance(data, (bytes, bytearray)):
                    await ws.send_bytes(data)
                else:
                    await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            conn.subscribers[channel].discard(ws)


def _new_rid() -> str:
    import uuid
    return uuid.uuid4().hex


manager = ConnectionManager()

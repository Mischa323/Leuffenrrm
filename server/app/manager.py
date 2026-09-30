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
        # dashboard sockets subscribed to this agent, by channel ('terminal'|'screen'),
        # each with the pipe that feeds it (see _ViewerPipe)
        self.subscribers: dict[str, dict[WebSocket, "_ViewerPipe"]] = {"terminal": {}, "screen": {}}

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
            old = self._agents.get(device_id)
            self._agents[device_id] = conn
            if old is not None:
                _release_viewers(old)
            return conn

    async def unregister(self, device_id: str, conn: "AgentConn | None" = None) -> None:
        """Remove a device's connection. When ``conn`` is given, only remove it if
        it is still the *current* connection — a newer reconnect (e.g. right after
        an agent self-update) may have already replaced it, and the stale handler's
        cleanup must not evict the live one (which would show the device offline
        while it's actually connected)."""
        async with self._lock:
            if conn is None or self._agents.get(device_id) is conn:
                gone = self._agents.pop(device_id, None)
                if gone is not None:
                    _release_viewers(gone)

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
            pipe = _ViewerPipe(ws, channel, device_id)
            ws._relay_pipe = pipe
            conn.subscribers.setdefault(channel, {})[ws] = pipe

    def unsubscribe(self, device_id: str, channel: str, ws: WebSocket) -> None:
        conn = self.get(device_id)
        if conn:
            conn.subscribers.get(channel, {}).pop(ws, None)
        # The pipe goes too when the agent reconnected in the meantime and this
        # viewer was subscribed to the connection before.
        pipe = getattr(ws, "_relay_pipe", None)
        if pipe:
            pipe.close()

    async def fanout(self, device_id: str, channel: str, data: Any) -> None:
        """Hand what the agent sent to everyone watching. Never waits on a
        viewer: the agent's socket is read by the same loop that calls this, and
        a viewer that is slow to receive used to stall it -- the frames, but
        also that device's heartbeats and metrics -- until the agent reported
        "frame send stalled" and the picture froze."""
        conn = self.get(device_id)
        if not conn:
            return
        pipes = conn.subscribers.get(channel, {})
        for ws, pipe in list(pipes.items()):
            if pipe.dead:
                pipes.pop(ws, None)
                continue
            pipe.offer(data)


# --------------------------------------------------------------------------- #
# One viewer's outgoing stream
# --------------------------------------------------------------------------- #
_CLIP_MAGIC = b"LRMMCLIP"


def _frame_kind(data: bytes) -> str:
    """'clip' (clipboard text, never dropped), 'key' or 'delta' (H.264 access
    units), or 'still' (a JPEG, or anything else that stands on its own)."""
    if data[:len(_CLIP_MAGIC)] == _CLIP_MAGIC:
        return "clip"
    if data[:2] == b"\xff\xd8":
        return "still"
    # Walk the NAL units up to the first slice. Cheap: a start code cannot occur
    # inside a unit, so this visits a handful of places, not every byte -- and
    # it reads past a long SEI, which x264 can put first.
    i = data.find(b"\x00\x00\x01")
    if i == -1:
        return "still"
    while i != -1 and i + 3 < len(data):
        nal = data[i + 3] & 0x1F
        if nal in (5, 7, 8):          # IDR slice, SPS, PPS: a keyframe starts here
            return "key"
        if nal == 1:                  # an ordinary slice: needs what came before
            return "delta"
        i = data.find(b"\x00\x00\x01", i + 3)
    return "delta"


class _ViewerPipe:
    """Feeds one viewer from a short queue, on a task of its own.

    When a viewer falls behind -- a slow link, a busy browser -- the queue fills.
    Then what is queued is thrown away and the stream picks up again at the next
    keyframe (the agent sends one every two seconds), so the viewer catches up
    instead of lagging further and further, and nobody else waits for it.
    Clipboard text and control messages are never thrown away.
    """

    MAX_FRAMES = 8          # about a quarter of a second at 30 fps

    def __init__(self, ws: WebSocket, channel: str, device_id: str) -> None:
        self.ws = ws
        self.channel = channel
        self.device_id = device_id
        self.q: asyncio.Queue = asyncio.Queue()
        self.frames = 0                 # video frames in the queue
        self.waiting_key = False
        self.dead = False
        self.task = asyncio.create_task(self._run())

    def offer(self, data: Any) -> None:
        if self.dead:
            return
        if isinstance(data, (bytes, bytearray)):
            kind = _frame_kind(data)
            if kind != "clip":
                if self.waiting_key and kind == "delta":
                    self._skipped(1)
                    return
                if kind == "key":
                    self.waiting_key = False
                if self.frames >= self.MAX_FRAMES:
                    self._skipped(self._purge())
                    # Say so: a viewer told this again and again asks for the
                    # lighter stream, rather than seeing a burst every keyframe.
                    self.q.put_nowait(("json", {"type": "behind"}))
                    if kind == "delta":
                        self.waiting_key = True
                        self._skipped(1)
                        return
                self.frames += 1
                self.q.put_nowait((kind, data))
                return
            self.q.put_nowait(("clip", data))
            return
        self.q.put_nowait(("json", data))

    def _purge(self) -> int:
        """Drop the queued frames, keep everything else, in order."""
        kept, dropped = [], 0
        while not self.q.empty():
            item = self.q.get_nowait()
            if item[0] in ("key", "delta", "still"):
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            self.q.put_nowait(item)
        self.frames = 0
        return dropped

    def _skipped(self, n: int) -> None:
        if n:
            self.ws._relay_skipped = getattr(self.ws, "_relay_skipped", 0) + n

    async def _run(self) -> None:
        try:
            while True:
                kind, data = await self.q.get()
                if kind == "json":
                    await self.ws.send_json(data)
                    continue
                if kind != "clip":
                    self.frames = max(0, self.frames - 1)
                await self.ws.send_bytes(data)
                # Per-subscriber counters, read by _bridge_ws when it logs the
                # session close (frames/throughput help diagnose drops).
                self.ws._relay_frames = getattr(self.ws, "_relay_frames", 0) + 1
                self.ws._relay_bytes = getattr(self.ws, "_relay_bytes", 0) + len(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.dead = True
            self.ws._relay_drops = getattr(self.ws, "_relay_drops", 0) + 1
            _log.warning("fanout %s send failed dev=%s: %r", self.channel, self.device_id, e)

    def close(self) -> None:
        self.dead = True
        self.task.cancel()


def _release_viewers(conn: AgentConn) -> None:
    """The agent behind ``conn`` went away, or came back on a new connection.
    Its viewers would otherwise wait on the old one for ever -- a picture that
    froze and stayed frozen. Closed instead, they reconnect by themselves and
    start the capture again on whatever connection the agent has now."""
    for pipes in conn.subscribers.values():
        for ws, pipe in list(pipes.items()):
            pipe.close()
            asyncio.create_task(_close_quietly(ws))
        pipes.clear()


async def _close_quietly(ws: WebSocket) -> None:
    try:
        # 1012: service restart -- "come back in a moment".
        await ws.close(code=1012, reason="agent connection changed")
    except Exception:
        pass


def _new_rid() -> str:
    import uuid
    return uuid.uuid4().hex


manager = ConnectionManager()

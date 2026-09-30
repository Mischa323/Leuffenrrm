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
import time
from collections import deque
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
        self.key_asked = 0.0        # when a keyframe was last asked of this agent

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
    def subscribe(self, device_id: str, channel: str, ws: WebSocket, label: str = "") -> None:
        conn = self.get(device_id)
        if conn:
            pipe = _ViewerPipe(ws, channel, device_id, label)
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
        kind = _frame_kind(data) if isinstance(data, (bytes, bytearray)) and pipes else None
        for ws, pipe in list(pipes.items()):
            if pipe.dead:
                pipes.pop(ws, None)
                continue
            pipe.offer(data, kind)
        # A viewer waiting for a keyframe asks the device for one, rather than
        # waiting for the next it would send anyway (agent 2.2.47+ sends them
        # only when asked, and every 30 s; an older agent ignores the request
        # and sends one every two seconds, as it always did).
        asking = [p for p in pipes.values() if p.wants_key]
        if asking:
            now = time.monotonic()
            for p in asking:
                p.wants_key = False
            if now - conn.key_asked >= 1.0:
                conn.key_asked = now
                for p in asking:
                    p.asked_at = now
                    p.out.add("keys_asked")
                asyncio.create_task(_ask_keyframe(conn))


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


class _Counters:
    """Numbers for the session log: summed or at their highest over a period,
    taken (and reset) for each periodic line, with running totals kept for the
    line that closes the session."""

    def __init__(self) -> None:
        self.now: dict = {}
        self.total: dict = {}

    def add(self, key: str, n: float = 1) -> None:
        self.now[key] = self.now.get(key, 0) + n
        self.total[key] = self.total.get(key, 0) + n

    def peak(self, key: str, value: float) -> None:
        if value > self.now.get(key, 0):
            self.now[key] = value
        if value > self.total.get(key, 0):
            self.total[key] = value

    def take(self) -> dict:
        out, self.now = self.now, {}
        return out


class _ViewerPipe:
    """Feeds one viewer from a queue of its own, on a task of its own.

    What decides whether a viewer has fallen behind is how long a frame has
    been waiting for it, not how many are waiting: a keyframe of a whole screen
    can be several hundred kB, and on an ordinary link sending it takes longer
    than a handful of small frames that follow it. (Counting frames threw the
    queue away right after nearly every keyframe, and then waited for the next
    one -- a stream of one or two frames a second.)

    * A keyframe (or a JPEG, which stands on its own) that arrives while the
      queue is more than CATCH_UP behind replaces what is queued: the decoder
      needs nothing before it, so the viewer skips ahead without a glitch.
    * A frame that has waited longer than LAG_LIMIT means the link cannot keep
      up. The viewer is told ("behind"); what is queued still goes out, and new
      frames are set aside until the next keyframe (the agent sends one every
      two seconds), which then replaces whatever is still waiting.

    Clipboard text and control messages are never thrown away. Everything that
    happens is counted, for the session log.
    """

    LAG_LIMIT = 1.0                 # seconds a frame may wait before the viewer is behind
    CATCH_UP = 0.25                 # backlog a keyframe may skip over
    MAX_FRAMES = 150                # hard limits, whatever the clock says
    MAX_BYTES = 48 * 1024 * 1024
    BEHIND_LOG_EVERY = 10.0         # at most one line a period about falling behind

    def __init__(self, ws: WebSocket, channel: str, device_id: str, label: str = "") -> None:
        self.ws = ws
        self.channel = channel
        self.device_id = device_id
        self.label = label or f"device={device_id}"
        self.items: deque = deque()     # (kind, data, queued at)
        self.wake = asyncio.Event()
        self.frames = 0                 # video frames in the queue
        self.bytes = 0
        self.waiting_key = False
        self.dead = False
        self.inn = _Counters()          # what arrived from the device for this viewer
        self.out = _Counters()          # what went to the viewer, and what did not
        self.last_in = 0.0              # when the device last sent a frame
        self.long_gap = 0.0             # the last time it went quiet, how long for
        self.opened = time.monotonic()
        self._behind_logged = 0.0
        self._behind_quiet = 0
        self.wants_key = False          # waiting for a keyframe: ask the device for one
        self.asked_at = 0.0
        self.task = asyncio.create_task(self._run())

    # -- in ---------------------------------------------------------------- #
    def offer(self, data: Any, kind: str | None = None) -> None:
        if self.dead:
            return
        now = time.monotonic()
        if not isinstance(data, (bytes, bytearray)):
            self._put(("json", data, now))
            return
        kind = kind or _frame_kind(data)
        if kind == "clip":
            self._put(("clip", data, now))
            return
        self._arrived(kind, len(data), now)
        if self.waiting_key and kind == "delta":
            self.out.add("skipped")
            if now - self.asked_at > 3.0:
                self.wants_key = True           # asked before, and still none: ask again
            return
        lag = now - self._oldest() if self.frames else 0.0
        if kind in ("key", "still") and lag > self.CATCH_UP:
            self.out.add("skipped", self._purge())
            self.out.add("caught_up")
        elif lag > self.LAG_LIMIT or self.frames >= self.MAX_FRAMES or self.bytes >= self.MAX_BYTES:
            # This link cannot keep up. What is queued still goes out -- it
            # follows on from a keyframe, so it decodes; what comes after is set
            # aside until the next keyframe, which then replaces whatever is
            # still waiting. (Emptying the queue instead left a thin link with
            # nothing but keyframes: one every two seconds.)
            self.out.add("fell_behind")
            self._note_behind(lag, self.frames, self.bytes, now)
            # Said to the viewer too: one told this again and again asks for
            # the lighter stream.
            self._put(("json", {"type": "behind"}, now))
            if kind == "delta":
                self.waiting_key = True
                self.wants_key = True
                self.out.add("skipped")
                return
            self.out.add("skipped", self._purge())
        if kind in ("key", "still"):
            self.waiting_key = False
        self.frames += 1
        self.bytes += len(data)
        self.out.peak("queue", self.frames)
        self._put((kind, data, now))

    def _arrived(self, kind: str, size: int, now: float) -> None:
        i = self.inn
        i.add("frames")
        i.add("bytes", size)
        if kind == "key":
            i.add("keys")
            i.peak("key_bytes", size)
        if self.last_in:
            gap = now - self.last_in
            i.peak("gap_ms", gap * 1000)
            if gap >= 2.0:
                self.long_gap = gap         # for the session log: how long it was quiet
        self.last_in = now

    def _put(self, item: tuple) -> None:
        self.items.append(item)
        self.wake.set()

    def _oldest(self) -> float:
        for kind, _data, at in self.items:
            if kind in ("key", "delta", "still"):
                return at
        return time.monotonic()

    def _purge(self) -> int:
        """Drop the queued frames, keep everything else, in order."""
        kept = deque(item for item in self.items if item[0] not in ("key", "delta", "still"))
        dropped = len(self.items) - len(kept)
        self.items = kept
        self.frames = 0
        self.bytes = 0
        return dropped

    def _note_behind(self, lag: float, frames: int, queued: int, now: float) -> None:
        if now - self._behind_logged < self.BEHIND_LOG_EVERY:
            self._behind_quiet += 1
            return
        more = f" (and {self._behind_quiet} more times since the last line)" if self._behind_quiet else ""
        _log.warning("remote %s BEHIND %s: the oldest frame waited %.0f ms for this viewer "
                     "(%d frames, %.0f KB queued); new frames set aside until the next keyframe%s",
                     self.channel, self.label, lag * 1000, frames, queued / 1024, more)
        self._behind_logged = now
        self._behind_quiet = 0

    # -- out --------------------------------------------------------------- #
    async def _run(self) -> None:
        try:
            while True:
                while not self.items:
                    self.wake.clear()
                    await self.wake.wait()
                kind, data, at = self.items.popleft()
                if kind == "json":
                    await self.ws.send_json(data)
                    continue
                video = kind != "clip"
                if video:
                    self.frames -= 1
                    self.bytes -= len(data)
                started = time.monotonic()
                await self.ws.send_bytes(data)
                if video:
                    took = time.monotonic() - started
                    o = self.out
                    o.add("sent")
                    o.add("bytes", len(data))
                    o.add("busy", took)
                    if kind == "key":
                        o.add("keys")
                    o.peak("wait_ms", (started - at) * 1000)
                    o.peak("send_ms", took * 1000)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.dead = True
            self.ws._relay_drops = getattr(self.ws, "_relay_drops", 0) + 1
            _log.warning("fanout %s send failed %s: %r", self.channel, self.label, e)

    def close(self) -> None:
        self.dead = True
        self.task.cancel()


async def _ask_keyframe(conn: AgentConn) -> None:
    try:
        await conn.send({"type": "input", "kind": "keyframe"})
    except Exception:
        pass                        # the agent went away; its viewers are closed anyway


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

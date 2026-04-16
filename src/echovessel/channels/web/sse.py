"""SSE broadcast layer for the Web channel.

Stage 2 of the web v1 release (`develop-docs/web-v1/02-stage-2-tracker.md`)
introduces this module. It owns the fan-out from the in-process
``WebChannel`` to every connected browser tab holding a long-lived
``GET /api/chat/events`` SSE stream.

Design:

- Each connected client gets its own ``asyncio.Queue``. The client's
  HTTP handler drains the queue into an ``sse_starlette.EventSourceResponse``.
- ``broadcast(event, payload)`` puts the event onto every client queue
  with per-queue error isolation — one stalled or closed client must
  never block the rest of the fan-out.
- A background ``heartbeat_task`` emits ``chat.connection.heartbeat``
  frames so reverse proxies and browsers keep the SSE stream open
  during long silences.

The broadcaster is deliberately transport-agnostic: it only speaks
``{"event": str, "data": dict}`` dicts. The FastAPI route layer in
``routes/chat.py`` is the only place that knows about
``sse_starlette`` and ``EventSourceResponse``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TypedDict

log = logging.getLogger(__name__)


class SSEFrame(TypedDict):
    """Internal queue item — becomes one SSE frame on the wire."""

    event: str
    data: dict


# Per-client queue cap. If a client falls this far behind we assume it's
# dead, drop it, and let the browser reconnect. Keeps one slow tab from
# pinning unbounded memory.
_CLIENT_QUEUE_MAXSIZE = 256


class SSEBroadcaster:
    """Fan-out hub from WebChannel to connected SSE clients.

    Single event loop, no locking. Every public method assumes it is
    called from the runtime's event loop (the one uvicorn is running
    on). The broadcaster does NOT own any threading primitive.

    Usage:

        broadcaster = SSEBroadcaster()
        asyncio.create_task(broadcaster.heartbeat_task())

        # In a chat/events route:
        queue = await broadcaster.register()
        try:
            async for frame in _drain(queue):
                yield frame
        finally:
            await broadcaster.unregister(queue)
    """

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[SSEFrame]] = set()

    async def register(self) -> asyncio.Queue[SSEFrame]:
        """Allocate a fresh client queue and add it to the fan-out set.

        The caller is responsible for eventually calling :meth:`unregister`
        in a ``finally`` clause so disconnected clients don't leak.
        """

        q: asyncio.Queue[SSEFrame] = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAXSIZE)
        self._clients.add(q)
        log.debug("SSE client registered (total=%d)", len(self._clients))
        return q

    async def unregister(self, queue: asyncio.Queue[SSEFrame]) -> None:
        """Remove a client queue from the fan-out set.

        Idempotent: unregistering an unknown queue is a no-op with a
        debug log line.
        """

        if queue in self._clients:
            self._clients.discard(queue)
            log.debug("SSE client unregistered (total=%d)", len(self._clients))

    async def broadcast(self, event: str, payload: dict) -> None:
        """Fan-out ``{event, payload}`` to every registered client queue.

        Per-queue errors (full queue, closed, etc.) are caught and the
        offending queue is dropped from the set. One dead client never
        blocks the rest of the broadcast.
        """

        self.publish_nowait(event, payload)

    def publish_nowait(self, event: str, payload: dict) -> None:
        """Synchronous variant of :meth:`broadcast`.

        Worker X introduced this so runtime callers on the turn hot
        path don't need to schedule an async task just to push one
        frame. Behaviour is identical to ``broadcast`` — queue puts
        are already non-blocking, per-queue errors isolated — but
        calling sites that are themselves sync (post-send hooks,
        cross-channel mirroring) stay simpler.
        """

        frame: SSEFrame = {"event": event, "data": payload}
        dead: list[asyncio.Queue[SSEFrame]] = []
        # Snapshot the set — dropping from the live set while iterating
        # raises RuntimeError.
        for q in list(self._clients):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                log.warning(
                    "SSE client queue full; dropping client (event=%s)", event
                )
                dead.append(q)
            except Exception as e:  # noqa: BLE001
                log.warning("SSE broadcast to client failed: %s", e)
                dead.append(q)
        for q in dead:
            self._clients.discard(q)

    async def heartbeat_task(self, interval_seconds: float = 30.0) -> None:
        """Emit a periodic heartbeat so proxies keep the SSE stream open.

        Runs until cancelled. The outer lifespan in ``app.py`` creates
        the task on startup and cancels it on shutdown.
        """

        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.broadcast("chat.connection.heartbeat", {})
        except asyncio.CancelledError:
            log.debug("heartbeat task cancelled")
            raise

    @property
    def client_count(self) -> int:
        """Current number of connected clients (test/debug helper)."""
        return len(self._clients)


__all__ = ["SSEBroadcaster", "SSEFrame"]

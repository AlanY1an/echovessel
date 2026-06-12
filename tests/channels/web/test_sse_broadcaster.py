"""Tests for :class:`echovessel.channels.web.sse.SSEBroadcaster`.

Stage 2 of web-v1 introduces the broadcaster. These tests exercise the
core fan-out semantics: single- and multi-client registration, per-
client error isolation, unregister idempotency, and the heartbeat
task.
"""

from __future__ import annotations

import asyncio

import pytest

from echovessel.channels.web.sse import SSEBroadcaster


async def test_register_broadcast_unregister_roundtrip() -> None:
    b = SSEBroadcaster()
    q = await b.register()
    assert b.client_count == 1

    await b.broadcast("chat.connection.ready", {"channel_id": "web"})
    frame = q.get_nowait()
    assert frame["event"] == "chat.connection.ready"
    assert frame["data"] == {"channel_id": "web"}

    await b.unregister(q)
    assert b.client_count == 0


async def test_broadcast_fans_out_to_all_clients() -> None:
    b = SSEBroadcaster()
    q1 = await b.register()
    q2 = await b.register()
    q3 = await b.register()

    await b.broadcast("chat.message.token", {"message_id": 1, "delta": "he"})

    for q in (q1, q2, q3):
        frame = q.get_nowait()
        assert frame["event"] == "chat.message.token"
        assert frame["data"]["delta"] == "he"

    assert b.client_count == 3


async def test_unregister_is_idempotent() -> None:
    b = SSEBroadcaster()
    q = await b.register()
    await b.unregister(q)
    # Second unregister must not raise.
    await b.unregister(q)
    assert b.client_count == 0


async def test_full_client_queue_is_dropped_without_blocking_others() -> None:
    """A stalled tab must not wedge the rest of the fan-out."""

    b = SSEBroadcaster()
    # Register a queue we deliberately fill so the next put_nowait
    # raises QueueFull.
    alive = await b.register()
    stalled: asyncio.Queue = asyncio.Queue(maxsize=1)
    stalled.put_nowait({"event": "x", "data": {}})  # prefill
    b._clients.add(stalled)  # type: ignore[attr-defined]

    await b.broadcast("chat.message.user_appended", {"content": "hi"})

    # Alive client received the frame.
    alive_frame = alive.get_nowait()
    assert alive_frame["data"] == {"content": "hi"}

    # Stalled client was dropped from the set.
    assert stalled not in b._clients  # type: ignore[attr-defined]
    assert b.client_count == 1


async def test_overflowed_client_receives_close_sentinel() -> None:
    """Dropping an overflowed client must also end its stream.

    The consumer drains the backlog and then needs an EOF marker —
    otherwise it blocks on ``get()`` forever while the connection stays
    open and the browser never reconnects.
    """

    b = SSEBroadcaster()
    q = await b.register()
    while not q.full():
        q.put_nowait({"event": "chat.message.token", "data": {}})

    await b.broadcast("chat.message.token", {"delta": "x"})

    assert b.client_count == 0
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    # Oldest backlog frame was evicted to make room for the sentinel.
    assert items[-1] is None
    assert all(item is not None for item in items[:-1])
    assert len(items) == q.maxsize


async def test_heartbeat_task_emits_heartbeat_events() -> None:
    b = SSEBroadcaster()
    q = await b.register()
    task = asyncio.create_task(b.heartbeat_task(interval_seconds=0.02))
    try:
        frame = await asyncio.wait_for(q.get(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert frame["event"] == "chat.connection.heartbeat"
    assert frame["data"] == {}


async def test_broadcast_with_zero_clients_is_a_noop() -> None:
    b = SSEBroadcaster()
    # Should not raise.
    await b.broadcast("chat.message.done", {"message_id": 42})
    assert b.client_count == 0

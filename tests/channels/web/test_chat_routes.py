"""Tests for the FastAPI chat routes (Stage 2).

Uses FastAPI's synchronous ``TestClient`` throughout. Streaming
responses (SSE events) are exercised via ``TestClient.stream`` with an
explicit ``break`` after the first matching frame — this avoids the
endless generator loop inside :class:`sse_starlette.EventSourceResponse`
from wedging the test.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.routes.chat import build_chat_router
from echovessel.channels.web.sse import SSEBroadcaster


def _build_app() -> tuple[WebChannel, SSEBroadcaster, object]:
    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    app = build_web_app(
        channel=channel, broadcaster=broadcaster, heartbeat_seconds=0.5
    )
    return channel, broadcaster, app


def test_post_send_valid_body_returns_202_and_lands_on_channel() -> None:
    channel, _broadcaster, app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/send",
            json={"content": "hello", "user_id": "self"},
        )
    assert resp.status_code == 202
    assert resp.json() == {"ok": True}
    # The debounce state machine pushed the message onto _current_turn.
    assert len(channel._current_turn) == 1  # type: ignore[attr-defined]
    assert channel._current_turn[0].content == "hello"  # type: ignore[attr-defined]


def test_post_send_malformed_body_returns_422() -> None:
    _channel, _broadcaster, app = _build_app()
    with TestClient(app) as client:
        # Missing required `content` field — pydantic returns 422.
        resp = client.post("/api/chat/send", json={"user_id": "self"})
    assert resp.status_code == 422


def test_post_send_empty_content_rejected() -> None:
    _channel, _broadcaster, app = _build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/chat/send", json={"content": "", "user_id": "self"}
        )
    assert resp.status_code == 422


def test_get_events_route_is_registered() -> None:
    """Smoke: the ``/api/chat/events`` route exists on the app.

    Directly exercising the SSE stream with TestClient wedges because
    sse_starlette's EventSourceResponse never yields control back to
    TestClient's sync break. Real end-to-end SSE behavior is covered
    by ``tests/runtime/test_app_web_integration.py`` which runs a real
    uvicorn server on a free port and reads the raw HTTP stream.
    """

    _channel, _broadcaster, app = _build_app()
    routes = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/api/chat/events" in routes
    assert "/api/chat/send" in routes
    assert "/api/chat/retry" not in routes


async def test_events_stream_ends_when_broadcaster_drops_client() -> None:
    """A client dropped for queue overflow must see its stream end.

    The route generator is driven directly (TestClient wedges on SSE,
    see :func:`test_get_events_route_is_registered`): once the
    broadcaster drops the overflowed queue and pushes the ``None``
    sentinel, the generator must drain the backlog and then return —
    closing the response so the browser's EventSource reconnects.
    """

    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    router = build_chat_router(channel=channel, broadcaster=broadcaster)
    endpoint = next(
        r.endpoint  # type: ignore[attr-defined]
        for r in router.routes
        if r.path == "/api/chat/events"  # type: ignore[attr-defined]
    )

    response = await endpoint()
    (queue,) = broadcaster._clients  # type: ignore[attr-defined]

    async def _drain() -> list:
        return [frame async for frame in response.body_iterator]

    consumer = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # let the consumer pull the ready frame

    # Stall the client: fill its queue, then one more broadcast
    # overflows it and the broadcaster drops the client.
    while not queue.full():
        queue.put_nowait({"event": "chat.message.token", "data": {}})
    await broadcaster.broadcast("chat.message.token", {"delta": "x"})

    frames = await asyncio.wait_for(consumer, timeout=2.0)
    assert frames  # backlog was drained before the stream ended
    assert broadcaster.client_count == 0

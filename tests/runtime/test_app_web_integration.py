"""Runtime × Web channel integration smoke tests (Stage 2).

Verifies that Runtime.start() launches a real uvicorn server on the
configured host/port when ``[channels.web].enabled=true``, registers
the WebChannel with the channel registry, and tears it all down
cleanly on Runtime.stop().

Uses port 0 so the OS picks a free ephemeral port — avoids test
flakes from a fixed port being held by another process.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile

import httpx

from echovessel.channels.web.channel import WebChannel
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _toml(*, web_enabled: bool, port: int, data_dir: str) -> str:
    web = ""
    if web_enabled:
        web = f"""
[channels.web]
enabled = true
host = "127.0.0.1"
port = {port}
debounce_ms = 50
"""
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "web-integ"
display_name = "WebInteg"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
{web}
"""


def _make_runtime(*, web_enabled: bool, port: int) -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-web-integ-")
    cfg = load_config_from_str(_toml(web_enabled=web_enabled, port=port, data_dir=tmp))
    return Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )


async def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    """Poll the port until uvicorn accepts connections."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=0.5
            ) as client:
                resp = await client.post(
                    "/api/chat/send",
                    json={"content": "ping", "user_id": "self"},
                )
                if resp.status_code in (202, 422):
                    return
        except (httpx.ConnectError, httpx.ReadError, OSError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"uvicorn server on 127.0.0.1:{port} never came up")


async def test_runtime_with_web_enabled_starts_uvicorn_and_registers_channel() -> None:
    port = _pick_free_port()
    rt = _make_runtime(web_enabled=True, port=port)
    await rt.start(register_signals=False)
    try:
        await _wait_for_server(port)

        # Channel is registered under "web".
        channel = rt.ctx.registry.get("web")
        assert channel is not None
        assert isinstance(channel, WebChannel)

        # Server responds to a real POST /api/chat/send.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client:
            resp = await client.post(
                "/api/chat/send",
                json={"content": "hello runtime", "user_id": "self"},
            )
        assert resp.status_code == 202
    finally:
        await rt.stop()


async def test_runtime_with_web_disabled_does_not_start_uvicorn() -> None:
    rt = _make_runtime(web_enabled=False, port=0)
    await rt.start(register_signals=False)
    try:
        assert rt._web_uvicorn_task is None
        assert rt.ctx.registry.get("web") is None
    finally:
        await rt.stop()


async def test_runtime_web_sse_stream_emits_connection_ready() -> None:
    """Real HTTP: connect to ``GET /api/chat/events`` and read the first
    SSE frame from a live uvicorn server.

    The runtime integration test is the right place for this because
    the in-process ``TestClient`` wedges on sse_starlette's endless
    generator. A real HTTP socket lets us close the connection with
    a straightforward deadline.
    """

    port = _pick_free_port()
    rt = _make_runtime(web_enabled=True, port=port)
    await rt.start(register_signals=False)
    try:
        await _wait_for_server(port)
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client,
            client.stream("GET", "/api/chat/events") as resp,
        ):
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                if "chat.connection.ready" in buf:
                    break
                if len(buf) > 4096:
                    break
        assert "event: chat.connection.ready" in buf
    finally:
        await rt.stop()


async def test_runtime_stop_tears_down_uvicorn_cleanly() -> None:
    port = _pick_free_port()
    rt = _make_runtime(web_enabled=True, port=port)
    await rt.start(register_signals=False)
    await _wait_for_server(port)
    assert rt._web_uvicorn_task is not None

    await rt.stop()

    assert rt._web_uvicorn_task is None
    assert rt._web_uvicorn_server is None
    # Port is free again — a fresh bind should succeed.
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()

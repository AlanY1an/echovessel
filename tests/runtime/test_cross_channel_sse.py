"""Worker X · cross-channel SSE mirroring tests.

Verifies that turns originating from non-Web channels (Discord in these
tests; iMessage / WeChat follow the same path) fan out to the
runtime-level SSE broadcaster so Web browsers subscribed to
``GET /api/chat/events`` see cross-channel activity live.

The helpers avoid spinning uvicorn — they drive Runtime directly and
read the broadcaster's client queues. A full end-to-end SSE stream is
covered by ``tests/channels/web/test_admin_import_routes.py`` and is
out of scope here.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from typing import Any

import pytest

from echovessel.channels.base import IncomingMessage, IncomingTurn, OutgoingMessage
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str, *, web_port: int = 0) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "xsse-test"
display_name = "X"

[memory]
db_path = "memory.db"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60

[channels.web]
enabled = true
host = "127.0.0.1"
port = {web_port}
debounce_ms = 50
"""


def _build_runtime() -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-xsse-")
    cfg = load_config_from_str(_toml(tmp))
    return Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="hi from persona"),
        embed_fn=build_zero_embedder(),
    )


class _StubDiscordChannel:
    """Minimal channel stub for cross-channel tests.

    ``channel_id`` is the "foreign" side of the test — Web is handled
    by the runtime-built WebChannel. ``send`` records outgoing
    messages so tests can assert on what the channel saw.
    """

    channel_id = "discord"
    name = "Discord"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.in_flight_turn_id: str | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def incoming(self):  # pragma: no cover — dispatcher not used here
        if False:
            yield None

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append(msg)

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


class _ExplodingDiscordChannel(_StubDiscordChannel):
    """Like _StubDiscordChannel but ``send`` always raises."""

    async def send(self, msg: OutgoingMessage) -> None:
        raise RuntimeError("simulated send failure")


def _make_turn(
    *, channel_id: str = "discord", content: str = "hi from discord"
) -> IncomingTurn:
    msg = IncomingMessage(
        channel_id=channel_id,
        user_id="self",
        content=content,
        received_at=datetime.now(),
        external_ref="ext-1",
    )
    return IncomingTurn.from_single_message(msg)


async def _drain_queue(
    queue: asyncio.Queue, *, deadline: float = 0.5
) -> list[dict[str, Any]]:
    """Pull every frame currently on ``queue`` until it's empty or
    ``deadline`` elapses since the last frame."""

    out: list[dict[str, Any]] = []
    loop = asyncio.get_event_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        try:
            frame = queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.02)
            continue
        out.append(frame)
        end = loop.time() + deadline
    return out


# ---------------------------------------------------------------------------
# Runtime construction + broadcaster lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_broadcaster_is_set_after_start() -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        assert rt.broadcaster is not None
    finally:
        await rt.stop()
    assert rt.broadcaster is None


# ---------------------------------------------------------------------------
# Non-Web turns → runtime broadcaster publishes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_turn_mirrors_user_appended_and_done_to_web_broadcaster() -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        # Inject a Discord-like channel into the registry so
        # ``runtime.ctx.registry.get("discord")`` returns it.
        discord = _StubDiscordChannel()
        rt.ctx.registry.register(discord)

        broadcaster = rt.broadcaster
        assert broadcaster is not None
        # Register a subscriber queue BEFORE we drive the turn so the
        # broadcaster sees us.
        queue = await broadcaster.register()

        await rt._handle_turn(_make_turn())
        frames = await _drain_queue(queue)
    finally:
        await rt.stop()

    event_names = [f["event"] for f in frames]
    assert "chat.message.user_appended" in event_names
    assert "chat.message.done" in event_names

    user_frame = next(
        f for f in frames if f["event"] == "chat.message.user_appended"
    )
    done_frame = next(f for f in frames if f["event"] == "chat.message.done")
    assert user_frame["data"]["source_channel_id"] == "discord"
    assert user_frame["data"]["content"] == "hi from discord"
    assert done_frame["data"]["source_channel_id"] == "discord"


@pytest.mark.asyncio
async def test_web_turn_does_not_double_publish_via_runtime() -> None:
    """Web-sourced turns: WebChannel's own broadcast handles it.
    Runtime must NOT republish the same events.

    We verify there is exactly one ``chat.message.done`` frame for the
    turn even though both channel and runtime know how to emit one.
    """

    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        broadcaster = rt.broadcaster
        assert broadcaster is not None
        queue = await broadcaster.register()

        web = rt.ctx.registry.get("web")
        assert web is not None
        msg = IncomingMessage(
            channel_id="web",
            user_id="self",
            content="hello from web",
            received_at=datetime.now(),
            external_ref="ext-web",
        )
        turn = IncomingTurn.from_single_message(msg)
        await rt._handle_turn(turn)
        frames = await _drain_queue(queue)
    finally:
        await rt.stop()

    done_frames = [f for f in frames if f["event"] == "chat.message.done"]
    assert len(done_frames) == 1
    assert done_frames[0]["data"]["source_channel_id"] == "web"


# ---------------------------------------------------------------------------
# Broadcaster failures must not break channel.send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcaster_publish_failure_does_not_break_channel_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        discord = _StubDiscordChannel()
        rt.ctx.registry.register(discord)

        broadcaster = rt.broadcaster
        assert broadcaster is not None

        def _raising(event: str, payload: dict) -> None:
            raise RuntimeError("broadcaster misbehaves")

        monkeypatch.setattr(broadcaster, "publish_nowait", _raising)
        await rt._handle_turn(_make_turn())
    finally:
        await rt.stop()

    # Channel.send still received the reply despite the broadcaster
    # blowing up on every publish attempt.
    assert len(discord.sent) == 1
    assert discord.sent[0].source_channel_id == "discord"


@pytest.mark.asyncio
async def test_channel_send_failure_does_not_publish_done() -> None:
    """When channel.send raises, runtime must NOT publish
    ``chat.message.done`` — the reply never actually reached the
    transport, so Web subscribers shouldn't claim it did.

    ``chat.message.user_appended`` still fires (the message did arrive).
    """

    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        discord = _ExplodingDiscordChannel()
        rt.ctx.registry.register(discord)

        broadcaster = rt.broadcaster
        assert broadcaster is not None
        queue = await broadcaster.register()

        await rt._handle_turn(_make_turn())
        frames = await _drain_queue(queue)
    finally:
        await rt.stop()

    event_names = [f["event"] for f in frames]
    assert "chat.message.user_appended" in event_names
    assert "chat.message.done" not in event_names


# ---------------------------------------------------------------------------
# source_channel_id propagation to OutgoingMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outgoing_message_carries_source_channel_id() -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        discord = _StubDiscordChannel()
        rt.ctx.registry.register(discord)
        await rt._handle_turn(_make_turn(channel_id="discord"))
    finally:
        await rt.stop()

    assert len(discord.sent) == 1
    outgoing = discord.sent[0]
    assert outgoing.source_channel_id == "discord"

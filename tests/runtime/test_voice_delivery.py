"""Voice delivery wiring in ``Runtime._handle_turn`` (Stage 7).

Tests 1-4 exercise the voice-generation branch in the runtime turn
tail: when ``persona.voice_enabled`` is True and a voice service is
available, ``generate_voice`` is called and the result is carried
on the ``OutgoingMessage``. Failure is non-fatal: the turn still
completes with text-only delivery.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from echovessel.channels.base import (
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider
from echovessel.voice.models import VoiceResult


def _toml(data_dir: str, *, voice_enabled: bool = False) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "voice-test"
display_name = "VoiceTest"
voice_id = "vid_test"
voice_enabled = {"true" if voice_enabled else "false"}

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

[voice]
enabled = false

[proactive]
enabled = false
"""


class _RecordingChannel:
    """Minimal channel stub that records what send() receives."""

    channel_id = "test"
    name = "Test"
    in_flight_turn_id: str | None = None

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._queue.put_nowait(None)

    async def incoming(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append(msg)

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None

    async def push(self, content: str) -> None:
        msg = IncomingMessage(
            channel_id=self.channel_id,
            user_id="self",
            content=content,
            received_at=datetime.now(),
            external_ref="ref",
        )
        await self._queue.put(IncomingTurn.from_single_message(msg))


def _make_voice_result(*, cached: bool = False) -> VoiceResult:
    return VoiceResult(
        url="/api/chat/voice/123.mp3",
        cache_path=Path("/tmp/fake/123.mp3"),
        duration_seconds=2.5,
        provider="stub",
        cost_usd=0.001,
        cached=cached,
    )


async def _send_and_wait(
    rt: Runtime, channel: _RecordingChannel, *, timeout: float = 5.0
) -> OutgoingMessage:
    await channel.push("hello")
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if channel.sent:
            return channel.sent[-1]
        await asyncio.sleep(0.05)
    raise AssertionError("channel.send never called")


async def test_handle_turn_no_voice_when_disabled():
    """voice_enabled=false → delivery='text', voice_result=None."""
    tmp = tempfile.mkdtemp()
    cfg = load_config_from_str(_toml(tmp, voice_enabled=False))
    rt = Runtime.build(
        None, config_override=cfg, llm=StubProvider(fallback="ok"), embed_fn=build_zero_embedder()
    )
    ch = _RecordingChannel()
    await rt.start(channels=[ch], register_signals=False)
    try:
        msg = await _send_and_wait(rt, ch)
        assert msg.delivery == "text"
        assert msg.voice_result is None
    finally:
        await rt.stop()


async def test_handle_turn_voice_when_enabled():
    """voice_enabled=true + mock voice_service → delivery='voice_neutral'."""
    tmp = tempfile.mkdtemp()
    cfg = load_config_from_str(_toml(tmp, voice_enabled=True))
    rt = Runtime.build(
        None, config_override=cfg, llm=StubProvider(fallback="ok"), embed_fn=build_zero_embedder()
    )

    mock_vs = AsyncMock()
    mock_vs.generate_voice = AsyncMock(return_value=_make_voice_result())
    rt.ctx.voice_service = mock_vs

    ch = _RecordingChannel()
    await rt.start(channels=[ch], register_signals=False)
    try:
        msg = await _send_and_wait(rt, ch)
        assert msg.delivery == "voice_neutral"
        assert msg.voice_result is not None
        assert msg.voice_result.url == "/api/chat/voice/123.mp3"
        mock_vs.generate_voice.assert_awaited_once()
    finally:
        await rt.stop()


async def test_handle_turn_voice_failure_falls_back_to_text():
    """voice generation raises → delivery='text', voice_result=None."""
    tmp = tempfile.mkdtemp()
    cfg = load_config_from_str(_toml(tmp, voice_enabled=True))
    rt = Runtime.build(
        None, config_override=cfg, llm=StubProvider(fallback="ok"), embed_fn=build_zero_embedder()
    )

    mock_vs = AsyncMock()
    mock_vs.generate_voice = AsyncMock(side_effect=RuntimeError("TTS down"))
    rt.ctx.voice_service = mock_vs

    ch = _RecordingChannel()
    await rt.start(channels=[ch], register_signals=False)
    try:
        msg = await _send_and_wait(rt, ch)
        assert msg.delivery == "text"
        assert msg.voice_result is None
    finally:
        await rt.stop()


async def test_handle_turn_voice_disabled_no_voice_service():
    """voice_service is None → no crash, text delivery."""
    tmp = tempfile.mkdtemp()
    cfg = load_config_from_str(_toml(tmp, voice_enabled=True))
    rt = Runtime.build(
        None, config_override=cfg, llm=StubProvider(fallback="ok"), embed_fn=build_zero_embedder()
    )
    rt.ctx.voice_service = None

    ch = _RecordingChannel()
    await rt.start(channels=[ch], register_signals=False)
    try:
        msg = await _send_and_wait(rt, ch)
        assert msg.delivery == "text"
        assert msg.voice_result is None
    finally:
        await rt.stop()

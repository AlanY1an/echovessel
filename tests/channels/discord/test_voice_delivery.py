"""Discord voice delivery tests (Stage 7).

Tests 10-12: when ``OutgoingMessage.voice_result`` is present and the
cached file exists, DiscordChannel uploads it as a ``discord.File``
attachment. When absent or the file is missing, text-only send.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Optional discord.py extra — skip cleanly when not installed.
discord = pytest.importorskip("discord")

from echovessel.channels.base import (  # noqa: E402
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)
from echovessel.channels.discord.channel import DiscordChannel  # noqa: E402
from echovessel.voice.types import VoiceResult  # noqa: E402

DEBOUNCE_MS = 50


def _make_voice_result(*, cache_path: Path, cached: bool = False) -> VoiceResult:
    return VoiceResult(
        url="/api/chat/voice/42.mp3",
        cache_path=cache_path,
        duration_seconds=2.5,
        provider="stub",
        cost_usd=0.001,
        cached=cached,
    )


def _make_dm_channel() -> MagicMock:
    dm = MagicMock(spec=discord.DMChannel)
    dm.send = AsyncMock()
    return dm


def _msg(content: str, *, user_id: str = "1001") -> IncomingMessage:
    return IncomingMessage(
        channel_id="discord",
        user_id=user_id,
        content=content,
        received_at=datetime.now(),
    )


async def _next_turn(ch: DiscordChannel, *, timeout: float = 1.0) -> IncomingTurn:
    async def _pull() -> IncomingTurn:
        async for turn in ch.incoming():
            return turn
        raise AssertionError("incoming() exhausted")

    return await asyncio.wait_for(_pull(), timeout=timeout)


async def test_discord_send_with_voice_uploads_file():
    """When voice_result present + file exists, send includes a
    discord.File attachment alongside the text content.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cache_file = Path(tmp) / "42.mp3"
        cache_file.write_bytes(b"\xff\xfb" + b"\x00" * 50)

        ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
        dm = _make_dm_channel()
        await ch._handle_dm(_msg("hey"), dm)
        await _next_turn(ch)

        vr = _make_voice_result(cache_path=cache_file)
        msg = OutgoingMessage(
            content="hi back",
            delivery="voice_neutral",
            voice_result=vr,
        )
        await ch.send(msg)

        dm.send.assert_awaited_once()
        kw = dm.send.await_args
        assert kw.kwargs.get("content") == "hi back" or kw.args == ("hi back",) or kw.kwargs.get("content") is not None
        # Verify a file was passed
        file_arg = kw.kwargs.get("file")
        assert file_arg is not None
        assert isinstance(file_arg, discord.File)


async def test_discord_send_no_voice_text_only():
    """No voice_result → plain text send, no file attachment."""
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm = _make_dm_channel()
    await ch._handle_dm(_msg("hey"), dm)
    await _next_turn(ch)

    msg = OutgoingMessage(content="plain reply", delivery="text")
    await ch.send(msg)

    dm.send.assert_awaited_once_with("plain reply")


async def test_discord_send_voice_file_missing_falls_back_to_text():
    """voice_result present but cache_path doesn't exist on disk →
    text-only send (graceful degradation).
    """
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm = _make_dm_channel()
    await ch._handle_dm(_msg("hey"), dm)
    await _next_turn(ch)

    nonexistent = Path("/tmp/this/does/not/exist/42.mp3")
    vr = _make_voice_result(cache_path=nonexistent)
    msg = OutgoingMessage(
        content="fallback",
        delivery="voice_neutral",
        voice_result=vr,
    )
    await ch.send(msg)

    # Should fall back to text-only (no file kwarg).
    dm.send.assert_awaited_once_with("fallback")

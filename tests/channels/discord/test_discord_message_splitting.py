"""Message-splitting tests for the Discord 2000-character limit.

Discord rejects messages over 2000 characters with an HTTP 400 —
``discord.py`` raises, it never truncates. ``split_message`` breaks a
long reply into boundary-aligned chunks and ``DiscordChannel.send``
delivers them sequentially, including on the voice-fallback path that
sends content alongside an MP3 attachment.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Optional ``discord.py`` extra — skip cleanly when not installed.
discord = pytest.importorskip("discord")

from echovessel.channels.base import (  # noqa: E402
    IncomingMessage,
    OutgoingMessage,
)
from echovessel.channels.discord.channel import (  # noqa: E402
    DISCORD_MESSAGE_LIMIT,
    DiscordChannel,
    split_message,
)
from echovessel.voice.types import VoiceResult  # noqa: E402

DEBOUNCE_MS = 50


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


# ---------------------------------------------------------------------------
# split_message unit behaviour
# ---------------------------------------------------------------------------


def test_short_content_is_a_single_chunk():
    assert split_message("hello") == ["hello"]


def test_content_at_exact_limit_is_a_single_chunk():
    content = "x" * DISCORD_MESSAGE_LIMIT
    assert split_message(content) == [content]


def test_splits_on_paragraph_boundaries_first():
    p1 = "alpha " * 250  # 1500 chars
    p2 = "bravo " * 250
    content = p1.strip() + "\n\n" + p2.strip()
    chunks = split_message(content)
    assert len(chunks) == 2
    assert chunks[0].startswith("alpha")
    assert chunks[1].startswith("bravo")


def test_oversized_paragraph_splits_on_sentence_boundaries():
    sentence = "This sentence is exactly the kind of filler an LLM produces. "
    content = (sentence * 60).strip()  # ~3700 chars, no paragraph breaks
    chunks = split_message(content)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= DISCORD_MESSAGE_LIMIT
        assert chunk.endswith(".")


def test_cjk_sentence_punctuation_is_a_boundary():
    sentence = "这是一个很长的中文句子，用来测试分段。"
    content = sentence * 150  # ~3000 chars, no Latin punctuation
    chunks = split_message(content)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= DISCORD_MESSAGE_LIMIT
        assert chunk.endswith("。")


def test_hard_split_when_no_boundaries_exist():
    content = "a" * 5000
    chunks = split_message(content)
    assert [len(c) for c in chunks] == [2000, 2000, 1000]
    assert "".join(chunks) == content


def test_every_chunk_fits_and_content_is_preserved():
    paragraphs = [f"Paragraph {i}. " + "word " * 300 for i in range(6)]
    content = "\n\n".join(p.strip() for p in paragraphs)
    chunks = split_message(content)
    assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks)
    assert _squash("".join(chunks)) == _squash(content)


def test_custom_limit_is_honoured():
    content = "one. two. three. four."
    chunks = split_message(content, limit=10)
    assert all(len(c) <= 10 for c in chunks)
    assert _squash("".join(chunks)) == _squash(content)


# ---------------------------------------------------------------------------
# send() delivers chunks sequentially
# ---------------------------------------------------------------------------


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
        external_ref=None,
    )


async def _channel_with_flushed_turn() -> tuple[DiscordChannel, MagicMock]:
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm = _make_dm_channel()
    await ch._handle_dm(_msg("hey"), dm)
    async for _turn in ch.incoming():
        break
    return ch, dm


async def test_send_splits_long_reply_into_sequential_messages():
    ch, dm = await _channel_with_flushed_turn()
    long_reply = "\n\n".join(f"Paragraph {i}. " + "x" * 600 for i in range(8))
    assert len(long_reply) > DISCORD_MESSAGE_LIMIT

    await ch.send(OutgoingMessage(content=long_reply))

    sent = [call.args[0] for call in dm.send.await_args_list]
    assert len(sent) >= 2
    assert all(len(s) <= DISCORD_MESSAGE_LIMIT for s in sent)
    assert _squash("".join(sent)) == _squash(long_reply)


async def test_send_short_reply_is_a_single_message():
    ch, dm = await _channel_with_flushed_turn()
    await ch.send(OutgoingMessage(content="short and sweet"))
    dm.send.assert_awaited_once_with("short and sweet")


async def test_voice_fallback_sends_all_chunks_alongside_attachment(tmp_path: Path, monkeypatch):
    """When voice falls back to an MP3 attachment, the first chunk rides
    with the file and the remaining chunks follow as plain messages."""
    # No ffmpeg → the OGG conversion bails and the fallback path runs.
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    mp3 = tmp_path / "reply.mp3"
    mp3.write_bytes(b"ID3\x03\x00\x00\x00fake-mp3-bytes")
    voice = VoiceResult(
        url="/api/chat/voice/42.mp3",
        cache_path=mp3,
        duration_seconds=2.5,
        provider="stub",
        cost_usd=0.001,
        cached=False,
    )

    ch, dm = await _channel_with_flushed_turn()
    long_reply = "\n\n".join(f"Paragraph {i}. " + "y" * 600 for i in range(8))
    expected_chunks = split_message(long_reply)
    assert len(expected_chunks) >= 2

    await ch.send(OutgoingMessage(content=long_reply, voice_result=voice))

    assert dm.send.await_count == len(expected_chunks)
    first = dm.send.await_args_list[0]
    assert first.kwargs["content"] == expected_chunks[0]
    assert first.kwargs["file"] is not None
    rest = [call.args[0] for call in dm.send.await_args_list[1:]]
    assert rest == expected_chunks[1:]

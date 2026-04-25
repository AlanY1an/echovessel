"""WebChannel SSE broadcast for voice delivery (Stage 7).

Tests 5-6: verify that ``channel.send(OutgoingMessage)`` broadcasts
``chat.message.voice_ready`` alongside ``chat.message.done`` when
``msg.voice_result`` is not None, and omits it when None.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from echovessel.channels.base import OutgoingMessage
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.voice.types import VoiceResult


def _make_voice_result() -> VoiceResult:
    return VoiceResult(
        url="/api/chat/voice/42.mp3",
        cache_path=Path("/tmp/fake/42.mp3"),
        duration_seconds=3.0,
        provider="stub",
        cost_usd=0.0,
        cached=True,
    )


async def test_webchannel_send_broadcasts_voice_ready_event():
    """When voice_result is present, send broadcasts both
    ``chat.message.done`` and ``chat.message.voice_ready``.
    """
    ch = WebChannel(debounce_ms=50)
    broadcaster = SSEBroadcaster()
    broadcaster.broadcast = AsyncMock()
    ch.attach_broadcaster(broadcaster)

    msg = OutgoingMessage(
        content="hello",
        in_reply_to_turn_id="turn-xyz",
        delivery="voice_neutral",
        voice_result=_make_voice_result(),
    )
    await ch.send(msg)

    # Two broadcasts: done + voice_ready
    assert broadcaster.broadcast.await_count == 2
    calls = [c.args for c in broadcaster.broadcast.await_args_list]
    event_names = [c[0] for c in calls]
    assert "chat.message.done" in event_names
    assert "chat.message.voice_ready" in event_names

    # Find the voice_ready payload
    voice_call = next(c for c in calls if c[0] == "chat.message.voice_ready")
    payload = voice_call[1]
    assert payload["url"] == "/api/chat/voice/42.mp3"
    assert payload["duration_seconds"] == 3.0
    assert payload["cached"] is True
    # message_id should be stable hash of turn_id
    assert isinstance(payload["message_id"], int)


async def test_webchannel_send_no_voice_skips_voice_ready():
    """When voice_result is None, only ``chat.message.done`` is
    broadcast — no ``chat.message.voice_ready``.
    """
    ch = WebChannel(debounce_ms=50)
    broadcaster = SSEBroadcaster()
    broadcaster.broadcast = AsyncMock()
    ch.attach_broadcaster(broadcaster)

    msg = OutgoingMessage(
        content="hello",
        in_reply_to_turn_id="turn-abc",
        delivery="text",
    )
    await ch.send(msg)

    assert broadcaster.broadcast.await_count == 1
    event_name = broadcaster.broadcast.await_args.args[0]
    assert event_name == "chat.message.done"

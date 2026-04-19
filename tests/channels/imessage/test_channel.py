"""Tests for :class:`IMessageChannel`.

Uses an in-process fake ``ImsgRpcClient`` so no real subprocess is
spawned — exercises the inbound pipeline, debounce state machine, send
path and echo/rate-limit integration purely in memory.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from echovessel.channels.base import Channel, OutgoingMessage
from echovessel.channels.imessage.channel import IMessageChannel


class FakeRpcClient:
    """Drop-in replacement for :class:`ImsgRpcClient` used in tests."""

    def __init__(self, *, send_response: dict[str, Any] | None = None) -> None:
        self._started = False
        self._subscribers: dict[str, list] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._send_response = send_response or {"ok": "fake-guid-1"}
        # Mimic the real client's attribute so IMessageChannel.is_ready
        # can introspect without caring about the implementation.
        self._proc = None

    async def start(self) -> None:
        self._started = True
        self._proc = object()  # truthy sentinel

    async def stop(self) -> None:
        self._started = False
        self._proc = None

    def subscribe(self, method: str, handler) -> None:
        self._subscribers.setdefault(method, []).append(handler)

    async def request(self, method: str, params: dict[str, Any], **_: Any) -> Any:
        self.calls.append((method, dict(params)))
        if method == "watch.subscribe":
            return {"ok": True}
        if method == "send":
            return self._send_response
        return {}

    async def inject(self, method: str, params: dict[str, Any]) -> None:
        """Simulate the imsg daemon pushing a notification."""
        for handler in self._subscribers.get(method, []):
            await handler(params)


async def _make_channel(**overrides) -> tuple[IMessageChannel, FakeRpcClient]:
    client = FakeRpcClient()
    ch = IMessageChannel(
        persona_apple_id="anya-persona@icloud.com",
        cli_path="fake-imsg",
        allowed_handles=overrides.pop("allowed_handles", None),
        default_service=overrides.pop("default_service", "auto"),
        region=overrides.pop("region", "US"),
        debounce_ms=overrides.pop("debounce_ms", 50),
        client=client,
    )
    await ch.start()
    return ch, client


# ---------------------------------------------------------------------------
# Protocol compliance + lifecycle
# ---------------------------------------------------------------------------


async def test_satisfies_channel_protocol():
    ch, _ = await _make_channel()
    try:
        assert isinstance(ch, Channel)
        assert ch.channel_id == "imessage"
        assert ch.name == "iMessage"
        assert ch.is_ready() is True
    finally:
        await ch.stop()


async def test_start_subscribes_and_requests_watch():
    ch, client = await _make_channel()
    try:
        assert "message" in client._subscribers
        methods = [m for m, _ in client.calls]
        assert "watch.subscribe" in methods
    finally:
        await ch.stop()


async def test_constructor_accepts_empty_persona_apple_id():
    """Single-account mode · no destination filter, matches openclaw fast path."""
    ch = IMessageChannel(persona_apple_id="", client=FakeRpcClient())
    assert ch._persona_apple_id == ""


async def test_single_account_mode_accepts_any_destination():
    """With persona_apple_id='', the destination filter is skipped."""
    client = FakeRpcClient()
    ch = IMessageChannel(
        persona_apple_id="",  # single-account fast path
        cli_path="fake-imsg",
        debounce_ms=50,
        client=client,
    )
    await ch.start()
    try:
        # Destination is some random other Apple ID — single-account
        # mode doesn't care, so this message should make it through.
        await client.inject(
            "message",
            {
                "destination_caller_id": "alan@icloud.com",
                "sender": "+14155551234",
                "text": "hello",
                "guid": "m-1",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.messages[0].content == "hello"
    finally:
        await ch.stop()


# ---------------------------------------------------------------------------
# Inbound pipeline · drop reasons
# ---------------------------------------------------------------------------


async def _collect_turn(ch: IMessageChannel, *, timeout: float = 1.0):
    """Read one turn from ch.incoming() or raise asyncio.TimeoutError."""
    it = ch.incoming()
    return await asyncio.wait_for(it.__anext__(), timeout=timeout)


async def test_wrong_destination_dropped():
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "SOMEONE-ELSE@icloud.com",
                "sender": "+14155551234",
                "text": "hi",
                "guid": "m-1",
                "is_from_me": False,
                "is_group": False,
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_is_from_me_dropped():
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hi",
                "guid": "m-1",
                "is_from_me": True,
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_group_dropped_at_mvp():
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hey everyone",
                "guid": "m-1",
                "is_from_me": False,
                "is_group": True,
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_allowlist_enforced_when_set():
    ch, client = await _make_channel(allowed_handles={"+14155551234"})
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+19998887777",  # not on list
                "text": "hi",
                "guid": "m-1",
                "is_from_me": False,
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_empty_text_dropped():
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "   ",
                "guid": "m-1",
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_empty_sender_dropped():
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "",
                "text": "hello",
                "guid": "m-1",
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


# ---------------------------------------------------------------------------
# Inbound · happy path + debounce
# ---------------------------------------------------------------------------


async def test_single_message_flushes_after_debounce():
    ch, client = await _make_channel(debounce_ms=50)
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hello",
                "guid": "m-1",
                "is_from_me": False,
                "is_group": False,
                "created_at": "2026-04-19T03:00:00.000Z",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.channel_id == "imessage"
        assert turn.user_id == "+14155551234"
        assert len(turn.messages) == 1
        assert turn.messages[0].content == "hello"
        assert turn.messages[0].external_ref == "m-1"
        assert ch.in_flight_turn_id == turn.turn_id
    finally:
        await ch.stop()


async def test_burst_collapses_into_single_turn():
    ch, client = await _make_channel(debounce_ms=100)
    try:
        for i in range(3):
            await client.inject(
                "message",
                {
                    "destination_caller_id": "anya-persona@icloud.com",
                    "sender": "+14155551234",
                    "text": f"msg-{i}",
                    "guid": f"m-{i}",
                },
            )
            await asyncio.sleep(0.02)  # within debounce window
        turn = await _collect_turn(ch, timeout=2.0)
        assert [m.content for m in turn.messages] == ["msg-0", "msg-1", "msg-2"]
    finally:
        await ch.stop()


async def test_accepts_service_prefixed_sender():
    """imsg may emit ``imessage:+1...`` — handle normalization should strip it."""
    ch, client = await _make_channel(allowed_handles={"+14155551234"})
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "imessage:+1 (415) 555-1234",
                "text": "hi",
                "guid": "m-1",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.user_id == "+14155551234"
    finally:
        await ch.stop()


async def test_accepts_nested_envelope_from_real_imsg():
    """Real imsg v0.5.0 wraps notifications in a ``{subscription, message}``
    envelope. Regression guard: the channel must unwrap it before running
    the inbound pipeline. Payload shape captured from live `imsg rpc`
    stdio on 2026-04-19."""
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "subscription": 1,
                "message": {
                    "chat_identifier": "+14155551234",
                    "destination_caller_id": "anya-persona@icloud.com",
                    "chat_name": "",
                    "id": 63090,
                    "chat_guid": "any;-;+14155551234",
                    "chat_id": 654,
                    "guid": "6A694702-3EE4-4ACA-B27A-781146429761",
                    "sender": "+14155551234",
                    "attachments": [],
                    "is_group": False,
                    "created_at": "2026-04-19T06:22:10.280Z",
                    "is_from_me": False,
                    "participants": ["+14155551234"],
                    "reactions": [],
                    "text": "1",
                },
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.messages[0].content == "1"
        assert turn.messages[0].user_id == "+14155551234"
        assert turn.messages[0].external_ref == "6A694702-3EE4-4ACA-B27A-781146429761"
    finally:
        await ch.stop()


async def test_accepts_imsg_destination_caller_id_field_name():
    """Pin the upstream field name — imsg emits ``destination_caller_id``,
    not ``destination``. Regression guard against the earlier mistake."""
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hi via caller_id",
                "guid": "m-1",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.messages[0].content == "hi via caller_id"
    finally:
        await ch.stop()


async def test_parsed_created_at_is_naive_datetime():
    """Regression guard · `datetime.now()` elsewhere in the runtime
    returns naive datetimes, so `_parse_iso` must also return naive.
    Returning an aware value here triggers
    `TypeError: can't compare offset-naive and offset-aware datetimes`
    inside `memory.sessions._is_stale`. Caught in the wild 2026-04-19."""
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "subscription": 1,
                "message": {
                    "destination_caller_id": "anya-persona@icloud.com",
                    "sender": "+14155551234",
                    "text": "hi",
                    "guid": "m-1",
                    "created_at": "2026-04-19T06:22:10.280Z",
                },
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        received_at = turn.messages[0].received_at
        assert received_at.tzinfo is None, (
            f"received_at must be naive; got tzinfo={received_at.tzinfo!r}"
        )
    finally:
        await ch.stop()


async def test_accepts_imsg_created_at_field_name():
    """Pin the upstream field name — imsg emits ``created_at``, not ``date``.

    Uses 12:00 UTC so the resulting local-time naive datetime stays
    on the same calendar day in every reasonable test-runner timezone.
    """
    ch, client = await _make_channel()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hello",
                "guid": "m-1",
                "created_at": "2026-04-19T12:00:00.000Z",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.messages[0].received_at.year == 2026
        assert turn.messages[0].received_at.month == 4
        assert turn.messages[0].received_at.day == 19
    finally:
        await ch.stop()


async def test_persona_apple_id_normalization():
    """Destination comparison should be robust to case / prefix variations."""
    client = FakeRpcClient()
    ch = IMessageChannel(
        persona_apple_id="Anya-Persona@iCloud.COM",  # mixed case
        cli_path="fake-imsg",
        debounce_ms=50,
        client=client,
    )
    await ch.start()
    try:
        await client.inject(
            "message",
            {
                "destination_caller_id": "imessage:anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "hi",
                "guid": "m-1",
            },
        )
        turn = await _collect_turn(ch, timeout=2.0)
        assert turn.messages[0].content == "hi"
    finally:
        await ch.stop()


# ---------------------------------------------------------------------------
# Echo cache · seeded by outbound send
# ---------------------------------------------------------------------------


async def test_send_seeds_echo_cache_and_blocks_echo_inbound():
    ch, client = await _make_channel()
    # Simulate an earlier flushed turn so ``send`` has a target.
    ch._current_user_id = "+14155551234"
    try:
        await ch.send(OutgoingMessage(content="tell me more"))
        # Now the imsg daemon echoes our message back via watch:
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "tell me more",
                "guid": "fake-guid-1",  # matches FakeRpcClient default send response
            },
        )
        # It should be dropped — no turn should surface.
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


async def test_send_calls_imsg_with_correct_params():
    ch, client = await _make_channel()
    ch._current_user_id = "+14155551234"
    try:
        await ch.send(OutgoingMessage(content="hello there"))
        send_calls = [p for (m, p) in client.calls if m == "send"]
        assert len(send_calls) == 1
        assert send_calls[0]["to"] == "+14155551234"
        assert send_calls[0]["text"] == "hello there"
        assert send_calls[0]["service"] == "auto"
    finally:
        await ch.stop()


async def test_send_attaches_voice_file_when_voice_result_present(tmp_path):
    """Voice delivery · if the runtime passes a VoiceResult with an
    existing cache_path, the channel should add it as a ``file`` param
    on the ``send`` RPC so imsg attaches it to the iMessage. Text is
    still sent alongside the audio."""
    from echovessel.voice.models import VoiceResult

    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"ID3\x03\x00\x00\x00fake-mp3-bytes")

    ch, client = await _make_channel()
    ch._current_user_id = "+14155551234"
    try:
        result = VoiceResult(
            url="/fake/url.mp3",
            cache_path=audio_path,
            duration_seconds=1.5,
            provider="fake",
            cost_usd=0.0,
            cached=False,
        )
        await ch.send(
            OutgoingMessage(
                content="hi there",
                delivery="voice_neutral",
                voice_result=result,
            ),
        )
        send_calls = [p for (m, p) in client.calls if m == "send"]
        assert len(send_calls) == 1
        assert send_calls[0]["to"] == "+14155551234"
        assert send_calls[0]["text"] == "hi there"
        assert send_calls[0]["file"] == str(audio_path)
    finally:
        await ch.stop()


async def test_send_omits_file_when_voice_result_cache_missing(tmp_path):
    """Graceful fallback · if the cache_path doesn't exist (e.g. GC'd
    between generation and send), we MUST NOT pass a ``file`` param —
    otherwise imsg would reject the send with a file-not-found error."""
    from echovessel.voice.models import VoiceResult

    missing_path = tmp_path / "vanished.mp3"  # deliberately never created

    ch, client = await _make_channel()
    ch._current_user_id = "+14155551234"
    try:
        result = VoiceResult(
            url="/fake/url.mp3",
            cache_path=missing_path,
            duration_seconds=1.0,
            provider="fake",
            cost_usd=0.0,
            cached=False,
        )
        await ch.send(
            OutgoingMessage(
                content="text-only fallback",
                delivery="voice_neutral",
                voice_result=result,
            ),
        )
        send_calls = [p for (m, p) in client.calls if m == "send"]
        assert len(send_calls) == 1
        assert "file" not in send_calls[0]
    finally:
        await ch.stop()


async def test_send_without_current_user_is_dropped():
    ch, client = await _make_channel()
    try:
        # No _current_user_id set.
        await ch.send(OutgoingMessage(content="orphan"))
        assert not any(m == "send" for (m, _) in client.calls)
    finally:
        await ch.stop()


# ---------------------------------------------------------------------------
# Rate limiter · echo storm triggers suppression
# ---------------------------------------------------------------------------


async def test_repeated_echo_triggers_rate_limit():
    """Seed enough echo hits that the limiter suppresses the conversation."""
    ch, client = await _make_channel()
    ch._current_user_id = "+14155551234"
    try:
        await ch.send(OutgoingMessage(content="hi"))
        # Inject 5 identical echoes — each trips the echo cache and
        # records a drop. Threshold is 5 by default.
        for i in range(5):
            await client.inject(
                "message",
                {
                    "destination_caller_id": "anya-persona@icloud.com",
                    "sender": "+14155551234",
                    "text": "hi",
                    "guid": f"dup-{i}",
                },
            )
        # The 6th message — new text, but same peer — should be
        # suppressed by the rate limiter.
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "a totally fresh message",
                "guid": "fresh-1",
            },
        )
        with pytest.raises(asyncio.TimeoutError):
            await _collect_turn(ch, timeout=0.3)
    finally:
        await ch.stop()


# ---------------------------------------------------------------------------
# on_turn_done promotes next_turn via normal debounce
# ---------------------------------------------------------------------------


async def test_on_turn_done_promotes_next_turn():
    ch, client = await _make_channel(debounce_ms=50)
    try:
        # First burst — flushes and sets in_flight_turn_id.
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "first",
                "guid": "m-1",
            },
        )
        turn1 = await _collect_turn(ch, timeout=2.0)

        # Second message arrives WHILE runtime is "processing" turn1 —
        # in_flight_turn_id is set, so it lands in _next_turn.
        await client.inject(
            "message",
            {
                "destination_caller_id": "anya-persona@icloud.com",
                "sender": "+14155551234",
                "text": "second",
                "guid": "m-2",
            },
        )
        assert len(ch._next_turn) == 1

        # Runtime finishes turn1 — _next_turn should promote via normal
        # debounce cycle (not instant flush).
        await ch.on_turn_done(turn1.turn_id)
        turn2 = await _collect_turn(ch, timeout=2.0)
        assert [m.content for m in turn2.messages] == ["second"]
    finally:
        await ch.stop()

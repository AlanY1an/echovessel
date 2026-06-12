"""DM routing tests for :meth:`DiscordChannel.send`.

Covers:

1. A DM arrives, debounces, flushes as a turn; runtime then calls
   ``send`` — the content should land on the exact ``DMChannel`` the
   original message came from.
2. Two users DM the bot concurrently. Each user's reply must land on
   their own DM channel, not cross-wired.
3. ``send`` with no current turn / no mapped DM channel should log
   and drop instead of raising.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Optional ``discord.py`` extra — skip cleanly when not installed.
discord = pytest.importorskip("discord")

from echovessel.channels.base import (  # noqa: E402
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)
from echovessel.channels.discord.channel import DiscordChannel  # noqa: E402

DEBOUNCE_MS = 50


def _make_dm_channel() -> MagicMock:
    dm = MagicMock(spec=discord.DMChannel)
    dm.send = AsyncMock()
    return dm


def _msg(content: str, *, user_id: str) -> IncomingMessage:
    return IncomingMessage(
        channel_id="discord",
        user_id=user_id,
        content=content,
        received_at=datetime.now(),
        external_ref=None,
    )


async def _next_turn(ch: DiscordChannel, *, timeout: float = 1.0) -> IncomingTurn:
    async def _pull() -> IncomingTurn:
        async for turn in ch.incoming():
            return turn
        raise AssertionError("incoming() exhausted before a turn arrived")

    return await asyncio.wait_for(_pull(), timeout=timeout)


async def test_send_routes_to_the_correct_dm_channel():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm = _make_dm_channel()
    # Simulate the bot handler: record the DM channel mapping and
    # push the message into the debounce state machine.
    await ch._handle_dm(_msg("hey", user_id="1001"), dm)
    turn = await _next_turn(ch)
    assert turn.user_id == "1001"

    # Runtime hands back a reply via send.
    reply = OutgoingMessage(content="hi back", kind="reply", delivery="text")
    await ch.send(reply)

    dm.send.assert_awaited_once_with("hi back")


async def test_send_routes_to_distinct_users_without_crosswiring():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm_alice = _make_dm_channel()
    dm_bob = _make_dm_channel()

    # Alice messages first.
    await ch._handle_dm(_msg("hey from alice", user_id="1001"), dm_alice)
    turn_alice = await _next_turn(ch)
    assert turn_alice.user_id == "1001"

    # Runtime replies to Alice.
    await ch.send(OutgoingMessage(content="hello alice"))
    dm_alice.send.assert_awaited_once_with("hello alice")
    dm_bob.send.assert_not_called()

    # Alice's turn finishes.
    await ch.on_turn_done(turn_alice.turn_id)

    # Bob messages next.
    await ch._handle_dm(_msg("hey from bob", user_id="2002"), dm_bob)
    turn_bob = await _next_turn(ch)
    assert turn_bob.user_id == "2002"

    # Runtime replies to Bob — must land on dm_bob, not dm_alice.
    await ch.send(OutgoingMessage(content="hello bob"))
    dm_bob.send.assert_awaited_once_with("hello bob")
    # Alice's mock was not called a second time.
    assert dm_alice.send.await_count == 1


async def test_interleaved_users_get_replies_on_their_own_dms():
    """Bob DMing inside Alice's debounce window must not leak Alice's
    reply to Bob or Bob's message into Alice's turn."""
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm_alice = _make_dm_channel()
    dm_bob = _make_dm_channel()

    # Both messages land before the debounce window elapses.
    await ch._handle_dm(_msg("from alice", user_id="1001"), dm_alice)
    await ch._handle_dm(_msg("from bob", user_id="2002"), dm_bob)

    turn1 = await _next_turn(ch)
    assert turn1.user_id == "1001"
    assert [m.content for m in turn1.messages] == ["from alice"]

    await ch.send(OutgoingMessage(content="reply to alice"))
    dm_alice.send.assert_awaited_once_with("reply to alice")
    dm_bob.send.assert_not_called()

    await ch.on_turn_done(turn1.turn_id)
    turn2 = await _next_turn(ch)
    assert turn2.user_id == "2002"

    await ch.send(OutgoingMessage(content="reply to bob"))
    dm_bob.send.assert_awaited_once_with("reply to bob")
    assert dm_alice.send.await_count == 1


async def test_send_with_no_current_user_drops_gracefully(caplog):
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    # No turn has flushed, so _current_user_id is None.
    await ch.send(OutgoingMessage(content="nowhere to go"))
    # Nothing raised; check the warning surfaced.
    assert any(
        "no current user_id" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    )


async def test_send_with_unmapped_user_drops_gracefully(caplog):
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    # Flush a turn whose author has no DM channel in the map (i.e.
    # the message went through ``push_user_message`` directly,
    # bypassing ``_handle_dm``). This is a synthetic edge case
    # exercised only by tests / recovery paths.
    await ch.push_user_message(_msg("orphan", user_id="9999"))
    turn = await _next_turn(ch)
    assert turn.user_id == "9999"
    await ch.send(OutgoingMessage(content="reply"))
    assert any(
        "no DM channel mapped for user_id=9999" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    )


async def test_send_swallows_discord_errors_and_logs(caplog):
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    dm = _make_dm_channel()
    dm.send.side_effect = RuntimeError("rate limited")
    await ch._handle_dm(_msg("hey", user_id="1001"), dm)
    await _next_turn(ch)
    # This should NOT raise even though the send path blew up —
    # runtime depends on ``send`` being safe.
    await ch.send(OutgoingMessage(content="reply"))
    assert any(
        "discord send failed" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    )

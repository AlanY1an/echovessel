"""Debounce state machine tests for :class:`DiscordChannel`.

DiscordChannel copies WebChannel's debounce state machine verbatim
(see the concurrent-editing note at the top of
``src/echovessel/channels/discord/channel.py``). These tests assert
the copied behaviour matches the spec §2.3a contract independently of
WebChannel's own test suite, so a future refactor of either channel
cannot regress the shared invariants unnoticed.

The covered transitions are:

1. Single message → flush after one debounce window
2. Burst of three messages within the window → one turn
3. Message arrives while in-flight → lands in ``_next_turn``, no
   premature flush
4. ``on_turn_done`` with non-empty ``_next_turn`` → **normal** debounce
   cycle (review M1 iron rule), NOT instant flush
5. Hard per-turn message-count limit → immediate flush

Every test uses a 50 ms debounce window for fast CI; SETTLE_S adds
headroom for the ``call_later`` callback to fire cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

# Optional ``discord.py`` extra — skip cleanly when not installed.
pytest.importorskip("discord")

from echovessel.channels.base import IncomingMessage, IncomingTurn  # noqa: E402
from echovessel.channels.discord.channel import (  # noqa: E402
    MAX_MESSAGES_PER_TURN,
    DiscordChannel,
)

DEBOUNCE_MS = 50
DEBOUNCE_S = DEBOUNCE_MS / 1000.0
SETTLE_S = DEBOUNCE_S * 0.75


def _msg(content: str, *, user_id: str = "1001") -> IncomingMessage:
    return IncomingMessage(
        channel_id="discord",
        user_id=user_id,
        content=content,
        received_at=datetime.now(),
        external_ref=None,
    )


async def _next_turn(ch: DiscordChannel, *, timeout: float = 1.0) -> IncomingTurn:
    """Pull the next IncomingTurn off ``ch.incoming()`` with a timeout."""

    async def _pull() -> IncomingTurn:
        async for turn in ch.incoming():
            return turn
        raise AssertionError("incoming() exhausted before a turn arrived")

    return await asyncio.wait_for(_pull(), timeout=timeout)


async def test_single_message_flushes_after_debounce_window():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    await ch.push_user_message(_msg("hello"))
    # Not yet flushed
    assert ch.in_flight_turn_id is None
    turn = await _next_turn(ch)
    assert ch.in_flight_turn_id == turn.turn_id
    assert turn.channel_id == "discord"
    assert turn.user_id == "1001"
    assert [m.content for m in turn.messages] == ["hello"]
    # Each wrapped IncomingMessage carries the same turn_id.
    assert all(m.turn_id == turn.turn_id for m in turn.messages)


async def test_burst_of_messages_within_window_produces_single_turn():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    await ch.push_user_message(_msg("one"))
    await ch.push_user_message(_msg("two"))
    await ch.push_user_message(_msg("three"))
    turn = await _next_turn(ch)
    assert [m.content for m in turn.messages] == ["one", "two", "three"]
    # A subsequent ``_next_turn`` would hang waiting for a second
    # turn — asserting len via the collected result is enough.
    assert len(turn.messages) == 3


async def test_in_flight_parks_new_messages_in_next_turn():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    # Simulate an already-in-flight turn by seeding the state.
    ch.in_flight_turn_id = "previous-turn"
    await ch.push_user_message(_msg("late-arrival"))
    # Nothing should land on the out queue.
    assert ch._out_queue.qsize() == 0
    assert len(ch._next_turn) == 1
    assert ch._next_turn[0].content == "late-arrival"


async def test_on_turn_done_promotes_next_turn_through_normal_debounce():
    """Review M1 iron rule: promotion goes through the timer, not
    an instant flush. This test is the regression guard for that.
    """
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)

    # Start by pushing one message through a normal cycle so the
    # state machine is in ``in_flight`` state.
    await ch.push_user_message(_msg("first"))
    turn1 = await _next_turn(ch)
    assert ch.in_flight_turn_id == turn1.turn_id

    # Meanwhile the user sends another message while runtime is
    # still thinking about turn1 — it lands in _next_turn.
    await ch.push_user_message(_msg("second"))
    assert len(ch._next_turn) == 1
    assert ch._out_queue.qsize() == 0

    # Runtime finishes turn1.
    await ch.on_turn_done(turn1.turn_id)

    # IRON RULE: nothing is on the queue yet — the second message
    # must wait for a full normal debounce window before flushing.
    assert ch._out_queue.qsize() == 0
    assert ch._current_turn and ch._current_turn[0].content == "second"
    assert ch._debounce_handle is not None

    # After the debounce window elapses, turn2 arrives.
    turn2 = await _next_turn(ch)
    assert turn2.turn_id != turn1.turn_id
    assert [m.content for m in turn2.messages] == ["second"]


async def test_hard_message_limit_flushes_immediately():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=10_000)
    # Fill the current buffer to exactly the limit; the push that
    # crosses the limit should trigger an immediate flush rather
    # than waiting 10 seconds.
    for i in range(MAX_MESSAGES_PER_TURN):
        await ch.push_user_message(_msg(f"msg-{i}"))
    # As soon as we hit the limit, the flush fires synchronously.
    turn = await asyncio.wait_for(_next_turn(ch), timeout=0.5)
    assert len(turn.messages) == MAX_MESSAGES_PER_TURN


async def test_messages_from_different_users_never_share_a_turn():
    """The IncomingTurn invariant: every message in a turn belongs to
    one sender. A second user landing inside the first user's debounce
    window gets their own turn instead of being merged (and having the
    reply routed to the first sender)."""
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    await ch.push_user_message(_msg("a-1", user_id="1001"))
    await ch.push_user_message(_msg("from bob", user_id="2002"))
    await ch.push_user_message(_msg("a-2", user_id="1001"))

    turn1 = await _next_turn(ch)
    assert turn1.user_id == "1001"
    assert [m.content for m in turn1.messages] == ["a-1"]
    assert all(m.user_id == "1001" for m in turn1.messages)
    assert ch._current_user_id == "1001"

    await ch.on_turn_done(turn1.turn_id)
    turn2 = await _next_turn(ch)
    assert turn2.user_id == "2002"
    assert [m.content for m in turn2.messages] == ["from bob"]

    await ch.on_turn_done(turn2.turn_id)
    turn3 = await _next_turn(ch)
    assert turn3.user_id == "1001"
    assert [m.content for m in turn3.messages] == ["a-2"]


async def test_stop_terminates_incoming_iterator():
    ch = DiscordChannel(token="xxx.fake.token", debounce_ms=DEBOUNCE_MS)
    await ch.push_user_message(_msg("before stop"))
    first = await _next_turn(ch)
    assert [m.content for m in first.messages] == ["before stop"]
    await ch.stop()
    # After stop, pulling the next turn should finish (the None
    # sentinel raises AssertionError in our helper because the
    # iterator exhausts).
    with pytest.raises(AssertionError):
        await _next_turn(ch)

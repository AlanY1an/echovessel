"""Tests for ChannelRegistry and TurnDispatcher."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from echovessel.channels.base import IncomingMessage, OutgoingMessage
from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.turn.dispatcher import TurnDispatcher


class FakeChannel:
    """Channel Protocol v0.2 stub for registry + dispatcher tests."""

    name = "Fake"

    def __init__(self, channel_id: str, envelopes: list[IncomingMessage]):
        self.channel_id = channel_id
        self._envelopes = list(envelopes)
        self._started = False
        self.in_flight_turn_id: str | None = None
        self.sent: list[tuple[str, str]] = []

    async def start(self):
        self._started = True

    async def stop(self):
        self._started = False

    async def incoming(self):
        for e in self._envelopes:
            yield e

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append((msg.in_reply_to or "", msg.content))

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


def _env(channel_id: str, content: str) -> IncomingMessage:
    return IncomingMessage(
        channel_id=channel_id,
        user_id="self",
        content=content,
        received_at=datetime(2026, 4, 14),
    )


async def test_registry_register_and_start_all():
    reg = ChannelRegistry()
    c1 = FakeChannel("web", [_env("web", "hi")])
    c2 = FakeChannel("discord", [_env("discord", "yo")])
    reg.register(c1)
    reg.register(c2)

    ok = await reg.start_all()
    assert set(ok) == {"web", "discord"}
    assert c1._started and c2._started


async def test_registry_duplicate_raises():
    reg = ChannelRegistry()
    reg.register(FakeChannel("web", []))
    with pytest.raises(ValueError):
        reg.register(FakeChannel("web", []))


async def test_registry_all_incoming_merges_sources():
    reg = ChannelRegistry()
    c1 = FakeChannel("web", [_env("web", "a"), _env("web", "b")])
    c2 = FakeChannel("discord", [_env("discord", "c")])
    reg.register(c1)
    reg.register(c2)
    await reg.start_all()

    received: list[str] = []
    async for env in reg.all_incoming():
        received.append(env.content)
    assert sorted(received) == ["a", "b", "c"]


async def test_dispatcher_runs_handler_for_each_message():
    reg = ChannelRegistry()
    reg.register(FakeChannel("web", [_env("web", "hi"), _env("web", "hi2")]))
    await reg.start_all()

    handled: list[str] = []

    async def handler(env: IncomingMessage) -> None:
        handled.append(env.content)

    shutdown = asyncio.Event()
    dispatcher = TurnDispatcher(reg, handler, shutdown_event=shutdown)

    task = asyncio.create_task(dispatcher.run())
    # Let dispatcher drain
    for _ in range(20):
        if len(handled) >= 2:
            break
        await asyncio.sleep(0.01)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert sorted(handled) == ["hi", "hi2"]


async def test_dispatcher_times_out_hung_handler_and_continues():
    """Hung handler must not block the queue.

    Regression test for P1-2 (audit 2026-04-17). Prior to the timeout
    wrapper, a handler stuck on an unresponsive `llm.stream` call would
    hold the dispatcher indefinitely and every later message on every
    channel queued behind it with no signal.
    """
    reg = ChannelRegistry()
    reg.register(FakeChannel("web", [_env("web", "hung"), _env("web", "next")]))
    await reg.start_all()

    handled: list[str] = []

    async def handler(env: IncomingMessage) -> None:
        if env.content == "hung":
            # Simulate a hung LLM: await something that will never
            # complete within the test's lifetime. The dispatcher's
            # per-turn timeout must cancel this and continue.
            await asyncio.sleep(60)
        else:
            handled.append(env.content)

    shutdown = asyncio.Event()
    dispatcher = TurnDispatcher(
        registry=reg,
        handler=handler,
        shutdown_event=shutdown,
        turn_timeout_seconds=0.1,
    )

    task = asyncio.create_task(dispatcher.run())
    for _ in range(40):
        if handled:
            break
        await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert handled == ["next"], "dispatcher must abandon hung handler and process the next envelope"

"""Worker γ · RuntimeMemoryObserver SSE broadcast tests.

Covers the three memory-lifecycle hooks plumbing SSE events through
every channel in the registry that exposes ``push_sse``:

- ``on_mood_updated``          → ``chat.mood.update``
- ``on_session_closed``        → ``chat.session.boundary`` (closed_session_id set)
- ``on_new_session_started``   → ``chat.session.boundary`` (new_session_id set)

Each test spins up a fake channel, registers it, invokes the sync hook,
then drives the runtime loop forward (``run_until_complete`` on a
``sleep(0)``) so the ``run_coroutine_threadsafe`` fan-out actually
executes before we assert.
"""

from __future__ import annotations

import asyncio

import pytest

from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.memory_observers import RuntimeMemoryObserver


class _FakeChannel:
    """Channel stub that records every push_sse call."""

    def __init__(self, channel_id: str = "web") -> None:
        self.channel_id = channel_id
        self.calls: list[tuple[str, dict]] = []

    async def push_sse(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


class _SilentChannel:
    """Channel without ``push_sse`` — must be skipped, not raise."""

    def __init__(self, channel_id: str = "discord") -> None:
        self.channel_id = channel_id


async def _drain_loop() -> None:
    """Yield once to the event loop so scheduled tasks can run."""
    for _ in range(3):
        await asyncio.sleep(0)


def _make_observer_with_channel() -> tuple[RuntimeMemoryObserver, ChannelRegistry, _FakeChannel]:
    registry = ChannelRegistry()
    channel = _FakeChannel()
    registry.register(channel)
    loop = asyncio.get_event_loop()
    observer = RuntimeMemoryObserver(registry=registry, loop=loop)
    return observer, registry, channel


async def test_on_mood_updated_broadcasts_chat_mood_update() -> None:
    observer, _registry, channel = _make_observer_with_channel()

    observer.on_mood_updated(
        persona_id="p1",
        user_id="self",
        new_mood_text="愿意慢慢听,语气温和",
    )

    await _drain_loop()

    assert len(channel.calls) == 1
    event, payload = channel.calls[0]
    assert event == "chat.mood.update"
    assert payload == {
        "persona_id": "p1",
        "user_id": "self",
        "mood_summary": "愿意慢慢听,语气温和",
    }


async def test_on_session_closed_broadcasts_boundary_with_closed_id() -> None:
    observer, _registry, channel = _make_observer_with_channel()

    observer.on_session_closed(
        session_id="sess-old",
        persona_id="p1",
        user_id="self",
    )

    await _drain_loop()

    assert len(channel.calls) == 1
    event, payload = channel.calls[0]
    assert event == "chat.session.boundary"
    assert payload["closed_session_id"] == "sess-old"
    assert payload["new_session_id"] is None
    assert payload["persona_id"] == "p1"
    assert payload["user_id"] == "self"
    assert isinstance(payload["at"], str) and len(payload["at"]) > 0


async def test_on_new_session_started_broadcasts_boundary_with_new_id() -> None:
    observer, _registry, channel = _make_observer_with_channel()

    observer.on_new_session_started(
        session_id="sess-new",
        persona_id="p1",
        user_id="self",
    )

    await _drain_loop()

    assert len(channel.calls) == 1
    event, payload = channel.calls[0]
    assert event == "chat.session.boundary"
    assert payload["closed_session_id"] is None
    assert payload["new_session_id"] == "sess-new"


async def test_channel_without_push_sse_is_skipped_not_errored() -> None:
    registry = ChannelRegistry()
    registry.register(_SilentChannel())  # has no push_sse
    web = _FakeChannel(channel_id="web")
    registry.register(web)

    loop = asyncio.get_event_loop()
    observer = RuntimeMemoryObserver(registry=registry, loop=loop)

    observer.on_mood_updated(persona_id="p1", user_id="self", new_mood_text="neutral")

    await _drain_loop()

    # Silent channel: skipped without error. Web channel: still got the event.
    assert len(web.calls) == 1
    assert web.calls[0][0] == "chat.mood.update"


async def test_push_sse_raising_on_one_channel_does_not_block_others() -> None:
    class _BadChannel:
        channel_id = "bad"

        async def push_sse(self, event: str, payload: dict) -> None:
            raise RuntimeError("boom")

    registry = ChannelRegistry()
    registry.register(_BadChannel())
    good = _FakeChannel(channel_id="web")
    registry.register(good)

    loop = asyncio.get_event_loop()
    observer = RuntimeMemoryObserver(registry=registry, loop=loop)

    observer.on_mood_updated(persona_id="p1", user_id="self", new_mood_text="fine")

    await _drain_loop()

    assert len(good.calls) == 1, "good channel should still receive broadcast"


def test_hooks_are_sync_callable_without_loop_attached() -> None:
    """If the observer has no loop yet (startup race), hooks must still
    be callable without raising — the broadcast is simply dropped."""
    registry = ChannelRegistry()
    registry.register(_FakeChannel())
    observer = RuntimeMemoryObserver(registry=registry, loop=None)

    # Must not raise:
    observer.on_mood_updated(persona_id="p1", user_id="self", new_mood_text="x")
    observer.on_session_closed(session_id="s", persona_id="p1", user_id="self")
    observer.on_new_session_started(session_id="s", persona_id="p1", user_id="self")


@pytest.mark.parametrize(
    "hook_name, kwargs",
    [
        (
            "on_mood_updated",
            {"persona_id": "p1", "user_id": "self", "new_mood_text": "q"},
        ),
        (
            "on_session_closed",
            {"session_id": "s", "persona_id": "p1", "user_id": "self"},
        ),
        (
            "on_new_session_started",
            {"session_id": "s", "persona_id": "p1", "user_id": "self"},
        ),
    ],
)
async def test_hook_with_no_channels_registered_is_noop(hook_name: str, kwargs: dict) -> None:
    """Observer with an empty registry must not raise — and must schedule
    a no-op broadcast that completes silently."""
    registry = ChannelRegistry()
    loop = asyncio.get_event_loop()
    observer = RuntimeMemoryObserver(registry=registry, loop=loop)

    hook = getattr(observer, hook_name)
    hook(**kwargs)

    await _drain_loop()  # must not raise

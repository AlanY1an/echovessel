"""Proactive wiring round 3 tests (spec §17a + proactive spec §3.5a).

Covers:
    - `ChannelRegistry.any_channel_in_flight()` reads in_flight_turn_id
      live on every call
    - `build_proactive_scheduler` is now called with `persona=` and
      `is_turn_in_flight=`, NOT with the legacy `voice_id=` kwarg
"""

from __future__ import annotations

import inspect

from echovessel.channels.base import OutgoingMessage
from echovessel.runtime.app import RuntimeContextPersonaView
from echovessel.runtime.channel_registry import ChannelRegistry


class _FakeChannel:
    channel_id = "web"
    name = "Web"

    def __init__(self) -> None:
        self.in_flight_turn_id: str | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def incoming(self):
        async def _gen():
            if False:
                yield None

        return _gen()

    async def send(self, msg: OutgoingMessage) -> None:
        pass

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


def test_any_channel_in_flight_live_reads():
    reg = ChannelRegistry()
    ch = _FakeChannel()
    reg.register(ch)

    # No turn in flight yet.
    assert reg.any_channel_in_flight() is False

    ch.in_flight_turn_id = "t1"
    assert reg.any_channel_in_flight() is True

    ch.in_flight_turn_id = None
    assert reg.any_channel_in_flight() is False


def test_any_channel_in_flight_handles_missing_attribute():
    class _NoAttr:
        channel_id = "cli"
        name = "CLI"

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        def incoming(self):
            async def _gen():
                if False:
                    yield None

            return _gen()

        async def send(self, msg: OutgoingMessage) -> None:
            pass

        async def on_turn_done(self, turn_id: str) -> None:
            pass

    reg = ChannelRegistry()
    reg.register(_NoAttr())
    assert reg.any_channel_in_flight() is False


def test_runtime_context_persona_view_is_live():
    """The property implementation reads ctx.persona on every access —
    assert at the `inspect.getsource` level so future refactors that
    cache into a field get flagged."""
    src = inspect.getsource(RuntimeContextPersonaView)
    # Both properties route through self._ctx.persona — no cached copies.
    assert "self._ctx.persona.voice_enabled" in src
    assert "self._ctx.persona.voice_id" in src


def test_build_proactive_scheduler_no_legacy_voice_id_kwarg():
    """Ensure Runtime's internal _build_proactive_scheduler no longer
    passes `voice_id=` to `build_proactive_scheduler` — the PROACTIVE
    round2 legacy shim is dead for RT round3.
    """
    import echovessel.runtime.app as app_mod

    src = inspect.getsource(app_mod.Runtime._build_proactive_scheduler)
    # No actual voice_id=<value> argument in the call (the docstring
    # comment is allowed to mention the legacy name, so we match the
    # `voice_id=self` shape that the real call would have used).
    assert "voice_id=self" not in src
    assert "persona=" in src
    assert "is_turn_in_flight=" in src

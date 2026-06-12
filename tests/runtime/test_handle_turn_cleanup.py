"""In-flight turn state cleanup tests for Runtime._handle_turn_body.

The channels' debounce state machines and the proactive
``any_channel_in_flight`` gate both depend on ``in_flight_turn_id``
being cleared after EVERY turn. Three exit paths besides the happy
path must each clear it:

  1. ``result.skipped`` (routine LLM-call failure inside the
     coordinator)
  2. an exception escaping the turn body
  3. dispatcher turn-timeout cancellation (``asyncio.CancelledError``)

Each must still fire ``channel.on_turn_done`` (documented idempotent /
never-raising) so queued ``_next_turn`` messages get promoted and the
proactive scheduler is not suppressed forever.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime

import pytest

from echovessel.channels.base import IncomingMessage, IncomingTurn, OutgoingMessage
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.turn.coordinator import AssembledTurn


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "cleanup-test"
display_name = "Cleanup"

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
"""


def _build_runtime() -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-cleanup-")
    cfg = load_config_from_str(_toml(tmp))
    return Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )


class _RecordingChannel:
    channel_id = "discord"
    name = "Discord"

    def __init__(self) -> None:
        self.in_flight_turn_id: str | None = None
        self.turn_done_calls: list[str] = []
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def incoming(self):  # pragma: no cover — dispatcher not used here
        if False:
            yield None

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append(msg)

    async def on_turn_done(self, turn_id: str) -> None:
        self.turn_done_calls.append(turn_id)
        self.in_flight_turn_id = None


def _make_turn(*, channel_id: str = "discord") -> IncomingTurn:
    msg = IncomingMessage(
        channel_id=channel_id,
        user_id="self",
        content="hello",
        received_at=datetime.now(),
        external_ref="ext-1",
    )
    return IncomingTurn.from_single_message(msg)


async def test_on_turn_done_fires_on_skipped_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        channel = _RecordingChannel()
        rt.ctx.registry.register(channel)

        async def _skipping_assemble(turn_ctx, turn, llm, *, on_token=None, on_turn_done=None):
            return AssembledTurn(
                reply="",
                system_prompt="",
                user_prompt="",
                used_model="stub",
                skipped=True,
            )

        monkeypatch.setattr("echovessel.runtime.app.assemble_turn", _skipping_assemble)

        turn = _make_turn()
        await rt._handle_turn(turn)

        assert channel.turn_done_calls == [turn.turn_id]
        assert channel.in_flight_turn_id is None
        assert rt.ctx.registry.any_channel_in_flight() is False
        assert channel.sent == []
    finally:
        await rt.stop()


async def test_on_turn_done_fires_on_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        channel = _RecordingChannel()
        rt.ctx.registry.register(channel)

        async def _exploding_assemble(turn_ctx, turn, llm, *, on_token=None, on_turn_done=None):
            raise RuntimeError("simulated turn failure")

        monkeypatch.setattr("echovessel.runtime.app.assemble_turn", _exploding_assemble)

        turn = _make_turn()
        with pytest.raises(RuntimeError, match="simulated turn failure"):
            await rt._handle_turn(turn)

        assert channel.turn_done_calls == [turn.turn_id]
        assert channel.in_flight_turn_id is None
        assert rt.ctx.registry.any_channel_in_flight() is False
    finally:
        await rt.stop()


async def test_on_turn_done_fires_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatcher turn-timeout path: ``asyncio.wait_for`` cancels the
    handler mid-body. Cleanup must run and the CancelledError must
    still propagate so the task is observed as cancelled."""
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        channel = _RecordingChannel()
        rt.ctx.registry.register(channel)

        started = asyncio.Event()

        async def _hanging_assemble(turn_ctx, turn, llm, *, on_token=None, on_turn_done=None):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("echovessel.runtime.app.assemble_turn", _hanging_assemble)

        turn = _make_turn()
        task = asyncio.create_task(rt._handle_turn(turn))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert channel.in_flight_turn_id == turn.turn_id

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert channel.turn_done_calls == [turn.turn_id]
        assert channel.in_flight_turn_id is None
        assert rt.ctx.registry.any_channel_in_flight() is False
    finally:
        await rt.stop()


async def test_on_turn_done_fires_once_on_successful_send() -> None:
    """Happy path stays single-fire: exactly one on_turn_done after
    channel.send, no defensive double-call."""
    rt = _build_runtime()
    await rt.start(register_signals=False)
    try:
        channel = _RecordingChannel()
        rt.ctx.registry.register(channel)

        turn = _make_turn()
        await rt._handle_turn(turn)

        assert channel.turn_done_calls == [turn.turn_id]
        assert channel.in_flight_turn_id is None
        assert len(channel.sent) == 1
    finally:
        await rt.stop()

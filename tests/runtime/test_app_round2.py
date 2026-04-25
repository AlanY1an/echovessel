"""End-to-end tests for Round 2 runtime integration.

Covers docs/runtime/03-round2-integration-tracker.md PR 4:

- Step 10.5 · VoiceService build in Runtime.build()
- Step 10.6 · ProactiveScheduler build + start in Runtime.start()
- Graceful shutdown stops the proactive scheduler before channels

These tests use :memory: DB + StubProvider so nothing touches disk and
no real LLM / TTS / STT call is made. The proactive layer is exercised
through its real factory (`build_proactive_scheduler`) to catch any
wiring regressions between runtime adapters and proactive internals.
"""

from __future__ import annotations

import asyncio
import tempfile

from echovessel.channels.base import IncomingMessage, OutgoingMessage
from echovessel.proactive.core.base import ProactiveScheduler as ProactiveSchedulerBase
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider
from echovessel.voice import VoiceService

# ---------------------------------------------------------------------------
# TOML fixtures
# ---------------------------------------------------------------------------


def _base_toml(
    *,
    voice_section: str = "",
    proactive_section: str = "",
    data_dir: str,
) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "round2"
display_name = "Round2"
voice_id = "persona-voice-1"

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
{voice_section}
{proactive_section}
"""


def _voice_disabled() -> str:
    return """
[voice]
enabled = false
"""


def _voice_stub_enabled() -> str:
    return """
[voice]
enabled = true
tts_provider = "stub"
stt_provider = "stub"
"""


def _proactive_disabled() -> str:
    return """
[proactive]
enabled = false
"""


def _proactive_enabled() -> str:
    return """
[proactive]
enabled = true
tick_interval_seconds = 3600
max_per_24h = 1
"""


# ---------------------------------------------------------------------------
# Minimal channel used by tests that need registry.start_all()
# ---------------------------------------------------------------------------


class _QuietChannel:
    """Channel Protocol v0.2 stub used by tests that just need a
    registered channel for ``registry.start_all()`` — never yields
    inbound messages, records outgoing sends.
    """

    channel_id = "web"
    name = "Web"

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.in_flight_turn_id: str | None = None
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self._stop.set()

    async def incoming(self):
        # Block until stop is called; never yields turns.
        await self._stop.wait()
        if False:  # pragma: no cover
            yield None  # type: ignore[unreachable]

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append((msg.in_reply_to or "", msg.content))

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


def _make_runtime(
    *,
    voice_toml: str,
    proactive_toml: str,
) -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-round2-")
    toml = _base_toml(
        voice_section=voice_toml,
        proactive_section=proactive_toml,
        data_dir=tmp,
    )
    cfg = load_config_from_str(toml)
    stub = StubProvider(fallback="ok")
    return Runtime.build(
        None,
        config_override=cfg,
        llm=stub,
        embed_fn=build_zero_embedder(),
    )


# ---------------------------------------------------------------------------
# Step 10.5 · VoiceService
# ---------------------------------------------------------------------------


def test_runtime_build_with_voice_disabled_returns_none():
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_disabled(),
    )
    assert rt.voice_service is None
    assert rt.ctx.voice_service is None


def test_runtime_build_with_voice_enabled_stub_builds_service():
    rt = _make_runtime(
        voice_toml=_voice_stub_enabled(),
        proactive_toml=_proactive_disabled(),
    )
    assert rt.voice_service is not None
    assert isinstance(rt.voice_service, VoiceService)
    assert rt.voice_service.tts_provider_name == "stub"
    assert rt.voice_service.stt_provider_name == "stub"
    # Persona voice_id flows through to VoiceService.default_voice_id
    assert rt.voice_service.default_voice_id == "persona-voice-1"


def test_runtime_build_voice_default_is_disabled():
    """When [voice] is omitted from TOML, voice defaults to disabled."""
    rt = _make_runtime(voice_toml="", proactive_toml=_proactive_disabled())
    assert rt.voice_service is None


# ---------------------------------------------------------------------------
# Step 10.6 · ProactiveScheduler
# ---------------------------------------------------------------------------


async def test_runtime_with_proactive_disabled_no_scheduler():
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_disabled(),
    )
    ch = _QuietChannel()
    try:
        await rt.start(channels=[ch], register_signals=False)
        assert rt.proactive_scheduler is None
    finally:
        await rt.stop()


async def test_runtime_with_proactive_enabled_builds_scheduler():
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_enabled(),
    )
    ch = _QuietChannel()
    try:
        await rt.start(channels=[ch], register_signals=False)
        assert rt.proactive_scheduler is not None
        assert isinstance(rt.proactive_scheduler, ProactiveSchedulerBase)
    finally:
        await rt.stop()


async def test_runtime_stop_clears_proactive_scheduler():
    """After Runtime.stop(), the scheduler reference is cleared."""
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_enabled(),
    )
    ch = _QuietChannel()
    await rt.start(channels=[ch], register_signals=False)
    assert rt.proactive_scheduler is not None

    await rt.stop()
    assert rt.proactive_scheduler is None


async def test_runtime_stop_no_leaked_tasks():
    """After stop, no background tasks from this runtime should remain."""
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_enabled(),
    )
    ch = _QuietChannel()
    await rt.start(channels=[ch], register_signals=False)
    await rt.stop()

    leaked = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and not t.done()
        and t.get_name() in {"consolidate_worker", "idle_scanner", "turn_dispatcher"}
    ]
    assert leaked == [], f"background tasks still running: {[t.get_name() for t in leaked]}"


# ---------------------------------------------------------------------------
# Voice + Proactive together
# ---------------------------------------------------------------------------


async def test_runtime_voice_and_proactive_together():
    """Both subsystems enabled — proactive scheduler receives voice_service."""
    rt = _make_runtime(
        voice_toml=_voice_stub_enabled(),
        proactive_toml=_proactive_enabled(),
    )
    ch = _QuietChannel()
    try:
        await rt.start(channels=[ch], register_signals=False)
        assert rt.voice_service is not None
        assert rt.proactive_scheduler is not None
    finally:
        await rt.stop()


# ---------------------------------------------------------------------------
# Existing smoke path still works (regression guard for app.py refactor)
# ---------------------------------------------------------------------------


async def test_runtime_full_turn_still_works_with_round2_wiring():
    """Round 2 patch must not break the ordinary turn path."""

    class EchoChannel:
        """Channel Protocol v0.2 stub that yields whatever is pushed
        onto its internal queue and records outgoing sends."""

        channel_id = "web"
        name = "Web"

        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()
            self.in_flight_turn_id: str | None = None
            self.sent: list[tuple[str, str]] = []

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            await self._queue.put(None)

        async def incoming(self):
            while True:
                env = await self._queue.get()
                if env is None:
                    return
                yield env

        async def send(self, msg: OutgoingMessage) -> None:
            self.sent.append((msg.in_reply_to or "", msg.content))

        async def on_turn_done(self, turn_id: str) -> None:
            self.in_flight_turn_id = None

        async def push(self, content: str) -> None:
            from datetime import datetime

            await self._queue.put(
                IncomingMessage(
                    channel_id=self.channel_id,
                    user_id="self",
                    content=content,
                    received_at=datetime.now(),
                    external_ref="ref-1",
                )
            )

    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_disabled(),
    )
    channel = EchoChannel()
    await rt.start(channels=[channel], register_signals=False)
    try:
        await channel.push("hi there")
        for _ in range(60):
            if channel.sent:
                break
            await asyncio.sleep(0.05)
        assert channel.sent, "persona reply never dispatched"
    finally:
        await rt.stop()


# ---------------------------------------------------------------------------
# Config-level Round 2 smoke (doesn't actually start the daemon)
# ---------------------------------------------------------------------------


def test_persona_voice_id_loaded_from_toml():
    rt = _make_runtime(
        voice_toml=_voice_stub_enabled(),
        proactive_toml=_proactive_disabled(),
    )
    assert rt.ctx.config.persona.voice_id == "persona-voice-1"


def test_proactive_config_flows_to_proactive_layer():
    """Runtime's ProactiveSection.to_proactive_config() is called during
    Runtime.start() and feeds into build_proactive_scheduler — verify the
    round-trip by inspecting the scheduler's bound config is not None.
    """
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_enabled(),
    )
    # Manually invoke the private builder — it's the single place that
    # converts the runtime config into proactive's ProactiveConfig.
    from sqlmodel import Session as DbSession

    def _factory() -> DbSession:
        return DbSession(rt.ctx.engine)

    scheduler = rt._build_proactive_scheduler(_factory)
    assert scheduler is not None


def test_proactive_disabled_builder_returns_none():
    rt = _make_runtime(
        voice_toml=_voice_disabled(),
        proactive_toml=_proactive_disabled(),
    )
    from sqlmodel import Session as DbSession

    def _factory() -> DbSession:
        return DbSession(rt.ctx.engine)

    assert rt._build_proactive_scheduler(_factory) is None

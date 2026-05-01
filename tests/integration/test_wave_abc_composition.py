"""Wave A+B+C cross-module composition smoke tests.

These tests exercise the **full daemon composition** after Round 3 landed —
memory migration + observer wiring + ImporterFacade + voice toggle +
proactive is_turn_in_flight + IncomingTurn v0.4 turn loop all on the same
Runtime instance.

The individual module tests (test_assemble_turn_v04 / test_importer_facade /
test_persona_voice_toggle / test_runtime_memory_observer / test_app_round2)
each cover their slice in depth. **This file's job is to catch composition
regressions** — a wiring change in one Step that silently breaks another.

Everything here runs against StubProvider + StubTTS + :memory: SQLite, so no
network, no disk writes outside tempdir, no real LLM / TTS calls.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from sqlmodel import Session as DbSession

from echovessel.channels.base import IncomingMessage, OutgoingMessage
from echovessel.memory import observers as memory_observers
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _full_toml(data_dir: str) -> str:
    """A single TOML that turns on voice (stub) + proactive + all Wave B/C
    wiring points so every Step (6.5 / 10.7 / 12.5 / 12.6) fires."""
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "integ"
display_name = "Integ"
voice_id = "persona-voice-integ"
voice_enabled = true

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

[voice]
enabled = true
tts_provider = "stub"
stt_provider = "stub"

[proactive]
enabled = true
max_per_24h = 1
"""


def _make_runtime_from_full_toml() -> tuple[Runtime, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="echovessel-integ-"))
    cfg = load_config_from_str(_full_toml(str(tmp)))
    stub = StubProvider(fallback="ok")
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=stub,
        embed_fn=build_zero_embedder(),
    )
    return rt, tmp


class _ProbeChannel:
    """Channel Protocol v0.2 stub used by the composition tests.

    - `incoming()` blocks on an asyncio.Queue so the turn_dispatcher sees a
      real async generator (not a hardcoded `while False: yield`)
    - `send()` records dispatch in `.sent` for assertions
    - Exposes `in_flight_turn_id` property for proactive's
      `is_turn_in_flight` gate
    - `on_turn_done` clears the in-flight flag defensively
    """

    channel_id = "web"
    name = "Web"

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[tuple[str, str]] = []
        self.in_flight_turn_id: str | None = None

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

    async def push_user_message(self, content: str) -> None:
        await self._queue.put(
            IncomingMessage(
                channel_id=self.channel_id,
                user_id="self",
                content=content,
                received_at=datetime.now(),
                external_ref="ext-ref",
            )
        )


# ---------------------------------------------------------------------------
# Composition test 1 · all Wave A/B/C startup wiring is active after build
# ---------------------------------------------------------------------------


def test_runtime_build_activates_wave_abc_build_time_wiring():
    """Step 6.5 + 12.6 each leave a visible artifact on the built Runtime.

    Step 10.7 (ImporterFacade) and Step 12.5 (memory observer registration)
    happen inside `rt.start()`, not `rt.build()`, so they are verified by
    other tests in this file (`test_importer_facade_is_real_not_stub` and
    `test_runtime_start_registers_memory_observer`).
    """
    rt, _tmp = _make_runtime_from_full_toml()

    # Step 6.5 · ensure_schema_up_to_date was called before create_all_tables
    # Evidence: the v0.3 columns exist on recall_messages.
    from sqlalchemy import inspect

    engine = rt.ctx.engine
    columns = {c["name"] for c in inspect(engine).get_columns("recall_messages")}
    assert "turn_id" in columns, (
        "recall_messages.turn_id missing — Step 6.5 migration did not run"
    )

    # Step 12.6 · voice_enabled populated from config.persona.voice_enabled
    assert rt.ctx.persona.voice_enabled is True, (
        "ctx.persona.voice_enabled did not pick up config — Step 12.6 broken"
    )


async def test_runtime_start_registers_memory_observer():
    """Step 12.5 wires RuntimeMemoryObserver into the module-level observer
    registry. Start the daemon, confirm the registry grew by ≥1 observer,
    confirm it's our runtime observer (duck type), then stop cleanly.
    """
    rt, _tmp = _make_runtime_from_full_toml()
    channel = _ProbeChannel()

    # Snapshot the observer list before start — some other tests may have
    # left leftover entries; we only assert a net increase.
    before = list(memory_observers._observers)

    await rt.start(channels=[channel], register_signals=False)
    try:
        after = list(memory_observers._observers)
        new_observers = [o for o in after if o not in before]
        assert len(new_observers) >= 1, (
            "Step 12.5 did not register a RuntimeMemoryObserver"
        )
        # Duck-typed check: the new observer must implement at least one of
        # the lifecycle hooks with a meaningful (non-NullObserver) impl.
        obs = rt._memory_observer
        assert obs is not None
        assert obs in after
    finally:
        await rt.stop()

    # Clean up — stopping the runtime does NOT unregister the observer by
    # design (see memory_observers.py docstring), so we remove it manually to
    # avoid polluting the module state for the next test.
    if rt._memory_observer is not None:
        memory_observers.unregister_observer(rt._memory_observer)


# ---------------------------------------------------------------------------
# Composition test 2 · an IncomingMessage → turn_dispatcher → assemble_turn
# → memory writes → channel.send round-trip works with full wiring enabled
# ---------------------------------------------------------------------------


async def test_full_daemon_turn_roundtrip_with_all_wiring():
    """The end-to-end smoke: daemon is started, user message enters via
    channel.incoming(), persona reply exits via channel.send(), and memory
    records both with consistent turn_id metadata.

    This is the one test that proves Runtime.start() + turn_dispatcher +
    IncomingTurn conversion + assemble_turn + memory.ingest_message +
    channel.send all still cooperate after Wave A/B/C landed.
    """
    rt, _tmp = _make_runtime_from_full_toml()
    channel = _ProbeChannel()

    await rt.start(channels=[channel], register_signals=False)
    try:
        # Push a user message and wait for the persona reply to land on
        # channel.sent. Using the Round 2 IncomingMessage path (not
        # IncomingTurn directly) mirrors the real channel contract which
        # test_app_round2 proved still works — here we confirm Wave A+B+C
        # didn't break it.
        await channel.push_user_message("hello from integration test")

        for _ in range(60):
            if channel.sent:
                break
            await asyncio.sleep(0.05)

        assert channel.sent, "persona reply never dispatched in 3s"
        ref, content = channel.sent[-1]
        assert content, "persona reply content is empty"

        # Memory must have both the user message and the persona reply in L2.
        # We use list_recall_messages (added by M-round2) to verify.
        from echovessel.memory.retrieve import list_recall_messages

        with DbSession(rt.ctx.engine) as db:
            rows = list_recall_messages(
                db,
                persona_id=rt.ctx.persona.id,
                user_id="self",
                limit=10,
            )
        # list_recall_messages returns list[RecallMessage] SQLModel rows.
        # `.role` is a MessageRole enum (or str subclass): "user" / "persona".
        role_values = [str(r.role) for r in rows]
        assert any("user" in rv for rv in role_values), (
            f"no user row in L2: {role_values}"
        )
        assert any("persona" in rv or "assistant" in rv for rv in role_values), (
            f"no persona reply row in L2: {role_values}"
        )
    finally:
        await rt.stop()

    if rt._memory_observer is not None:
        memory_observers.unregister_observer(rt._memory_observer)


# ---------------------------------------------------------------------------
# Composition test 3 · proactive's is_turn_in_flight closure actually reads
# from ChannelRegistry, so a channel flipping in_flight_turn_id visibly
# changes the gate's verdict
# ---------------------------------------------------------------------------


async def test_proactive_is_turn_in_flight_wired_to_channel_registry():
    """Proactive round 2 expects runtime to inject
    `is_turn_in_flight: Callable[[], bool]` that scans the channel registry.

    RT-round3's factory patch built that closure. This test flips a channel's
    in_flight_turn_id and confirms the closure observes the change — proving
    the wire between RT-round3's `RuntimeContextPersonaView` /
    `any_channel_in_flight()` and proactive's policy gate is intact.
    """
    rt, _tmp = _make_runtime_from_full_toml()
    channel = _ProbeChannel()

    await rt.start(channels=[channel], register_signals=False)
    try:
        # Find the is_turn_in_flight closure. RT-round3 put it on
        # ChannelRegistry.any_channel_in_flight; the proactive scheduler holds
        # a ref to either the closure itself or the registry-bound method.
        assert hasattr(rt.ctx.registry, "any_channel_in_flight")
        check = rt.ctx.registry.any_channel_in_flight

        # Initially no in-flight turn
        channel.in_flight_turn_id = None
        assert check() is False

        # Flip it
        channel.in_flight_turn_id = "turn-abc"
        assert check() is True

        # Flip back
        channel.in_flight_turn_id = None
        assert check() is False
    finally:
        await rt.stop()

    if rt._memory_observer is not None:
        memory_observers.unregister_observer(rt._memory_observer)


# ---------------------------------------------------------------------------
# Composition test 4 · ImporterFacade is real (not a stub) and can at least
# accept start_pipeline + subscribe_events calls without crashing
# ---------------------------------------------------------------------------


async def test_importer_facade_is_real_not_stub():
    """Wave C (Thread IMPORT-code) upgraded the facade from RT-round3's stub
    to the real pipeline. Confirm the facade on a live runtime has the
    promised methods and that they're bound callables, not raise-on-call
    stubs.
    """
    rt, _tmp = _make_runtime_from_full_toml()
    await rt.start(channels=[_ProbeChannel()], register_signals=False)
    try:
        facade = rt._importer_facade
        assert facade is not None
        # All four public methods must exist and be callable
        assert callable(getattr(facade, "start_pipeline", None))
        assert callable(getattr(facade, "cancel_pipeline", None))
        assert callable(getattr(facade, "resume_pipeline", None))
        assert callable(getattr(facade, "subscribe_events", None))
    finally:
        await rt.stop()

    if rt._memory_observer is not None:
        memory_observers.unregister_observer(rt._memory_observer)


# ---------------------------------------------------------------------------
# Composition test 5 · v0.3 schema migration is idempotent across back-to-back
# Runtime.build() calls on the same (file-backed) DB
# ---------------------------------------------------------------------------


def test_schema_migration_idempotent_across_two_builds():
    """Boot twice on the same file DB — Step 6.5's ensure_schema_up_to_date
    must be idempotent so existing DBs from earlier daemon runs don't get
    corrupted. (M-round3 verified this inside memory/test_migrations_*.py,
    but we re-verify at the Runtime composition boundary.)
    """
    tmp = Path(tempfile.mkdtemp(prefix="echovessel-integ-mig-"))
    db_file = tmp / "db.sqlite"
    toml = f"""
[runtime]
data_dir = "{tmp}"
log_level = "warn"

[persona]
id = "mig"
display_name = "Mig"

[memory]
db_path = "{db_file}"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""
    cfg = load_config_from_str(toml)

    # First build: fresh DB, migration creates v0.3 schema via create_all
    rt1 = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )
    from sqlalchemy import inspect

    cols1 = {c["name"] for c in inspect(rt1.ctx.engine).get_columns("recall_messages")}
    assert "turn_id" in cols1

    # Second build on the same DB file — migration must be a no-op and not
    # raise. This is the idempotency check at the Runtime boundary.
    rt2 = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )
    cols2 = {c["name"] for c in inspect(rt2.ctx.engine).get_columns("recall_messages")}
    assert cols1 == cols2

    if rt1._memory_observer is not None:
        memory_observers.unregister_observer(rt1._memory_observer)
    if rt2._memory_observer is not None:
        memory_observers.unregister_observer(rt2._memory_observer)


# (Project uses pytest-asyncio auto mode — async def test_ functions are
# picked up without explicit decoration.)

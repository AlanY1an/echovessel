"""Engagement-maintenance wiring · proactive v2 Stage 4 BA loop.

``make_engagement_maintenance_fn`` builds the closure; the composition
root threads it into the ConsolidateWorker, whose idle branch runs it
every cycle. These tests pin both halves:

  1. the worker idle branch actually invokes the closure and the
     closure persists ``ProactiveState`` rows
  2. ``Runtime.start()`` wires the closure into the worker when
     proactive is enabled, and leaves it None when disabled
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest
from sqlmodel import Session

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import Persona, User
from echovessel.proactive.core.models import ProactiveState
from echovessel.proactive.execution.engagement_updater import ENGAGEMENT_INITIAL
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker
from echovessel.runtime.wiring.proactive import make_engagement_maintenance_fn

# ---------------------------------------------------------------------------
# Worker-level: idle branch drives the closure
# ---------------------------------------------------------------------------


def _make_engine():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        db.add(Persona(id="p1", display_name="P"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()
    return engine


def _make_worker(engine, **kwargs) -> ConsolidateWorker:
    def factory() -> Session:
        return Session(engine)

    async def _unused(*args, **kw):  # pragma: no cover — queue stays empty
        raise AssertionError("not expected in these tests")

    return ConsolidateWorker(
        db_factory=factory,
        backend=None,  # type: ignore[arg-type]
        extract_fn=_unused,
        reflect_fn=_unused,
        embed_fn=_unused,  # type: ignore[arg-type]
        **kwargs,
    )


async def test_engagement_closure_persists_proactive_state() -> None:
    engine = _make_engine()

    def factory() -> Session:
        return Session(engine)

    fn = make_engagement_maintenance_fn(db_factory=factory, persona_id="p1", user_id="self")
    worker = _make_worker(engine, engagement_maintenance_fn=fn)

    await worker._maybe_maintain_engagement()

    with Session(engine) as db:
        state = db.get(ProactiveState, ("p1", "self"))
        assert state is not None
        assert state.engagement_score == pytest.approx(ENGAGEMENT_INITIAL)


async def test_worker_run_invokes_maintainer_on_idle_cycle() -> None:
    engine = _make_engine()
    shutdown = asyncio.Event()
    calls: list[object] = []

    async def fn(now) -> None:
        calls.append(now)
        shutdown.set()

    worker = _make_worker(
        engine,
        poll_seconds=0.01,
        shutdown_event=shutdown,
        engagement_maintenance_fn=fn,
    )
    await asyncio.wait_for(worker.run(), timeout=2.0)
    assert calls, "idle branch never invoked the engagement maintainer"


async def test_maintainer_failure_does_not_break_idle_loop() -> None:
    engine = _make_engine()

    async def fn(now) -> None:
        raise RuntimeError("simulated maintenance failure")

    worker = _make_worker(engine, engagement_maintenance_fn=fn)
    # Must not raise — failure is logged and the loop continues.
    await worker._maybe_maintain_engagement()


async def test_worker_without_maintainer_is_a_noop() -> None:
    engine = _make_engine()
    worker = _make_worker(engine)
    await worker._maybe_maintain_engagement()
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")) is None


# ---------------------------------------------------------------------------
# Runtime-level: composition root threads the closure into the worker
# ---------------------------------------------------------------------------


def _runtime_toml(*, data_dir: str, proactive_enabled: bool) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "engage-wire"
display_name = "EngageWire"

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

[proactive]
enabled = {str(proactive_enabled).lower()}
"""


def _build_runtime(*, proactive_enabled: bool) -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-engage-wire-")
    cfg = load_config_from_str(_runtime_toml(data_dir=tmp, proactive_enabled=proactive_enabled))
    return Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )


async def test_runtime_wires_maintainer_when_proactive_enabled() -> None:
    from sqlmodel import Session as DbSession

    rt = _build_runtime(proactive_enabled=True)
    await rt.start(register_signals=False)
    try:
        assert rt._worker is not None
        assert rt._worker.engagement_maintenance_fn is not None

        # One idle cycle creates/updates the ProactiveState row through
        # the daemon's own db_factory.
        await rt._worker._maybe_maintain_engagement()
        with DbSession(rt.ctx.engine) as db:
            state = db.get(ProactiveState, ("engage-wire", "self"))
            assert state is not None
            assert state.engagement_score == pytest.approx(ENGAGEMENT_INITIAL)
    finally:
        await rt.stop()


async def test_runtime_skips_maintainer_when_proactive_disabled() -> None:
    rt = _build_runtime(proactive_enabled=False)
    await rt.start(register_signals=False)
    try:
        assert rt._worker is not None
        assert rt._worker.engagement_maintenance_fn is None
    finally:
        await rt.stop()

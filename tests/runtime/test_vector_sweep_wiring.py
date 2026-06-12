"""Dead-vector sweep wiring — ConsolidateWorker idle branch, daily cadence.

The worker's idle branch fires ``sweep_dead_vectors`` at most once per
calendar day (last-run-date guard), swallows failures like the
engagement hook, and skips entirely when no backend is wired.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.models import ConceptNode, Persona, User
from echovessel.runtime.loops import consolidate_worker as worker_mod
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _make_engine():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="p1", display_name="P"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()
    return engine


def _make_worker(engine, *, backend=None, **kwargs) -> ConsolidateWorker:
    def factory() -> DbSession:
        return DbSession(engine)

    async def _unused(*args, **kw):  # pragma: no cover — queue stays empty
        raise AssertionError("not expected in these tests")

    return ConsolidateWorker(
        db_factory=factory,
        backend=backend,  # type: ignore[arg-type]
        extract_fn=_unused,
        reflect_fn=_unused,
        embed_fn=_unused,  # type: ignore[arg-type]
        **kwargs,
    )


async def test_sweep_fires_at_most_once_per_day(monkeypatch) -> None:
    engine = _make_engine()
    backend = SQLiteBackend(engine)
    clock = _Clock(datetime(2026, 6, 12, 9, 0, 0))
    calls: list[datetime] = []

    def fake_sweep(db, b, *, now=None, **kw):
        calls.append(now)

        class _Counts:
            soft_deleted = 0
            superseded = 0

        return _Counts()

    monkeypatch.setattr(worker_mod, "sweep_dead_vectors", fake_sweep)
    worker = _make_worker(engine, backend=backend, now_fn=clock)

    await worker._maybe_sweep_dead_vectors()
    await worker._maybe_sweep_dead_vectors()
    assert len(calls) == 1, "same calendar day must not trigger a second sweep"

    clock.now += timedelta(hours=5)  # still the same day
    await worker._maybe_sweep_dead_vectors()
    assert len(calls) == 1

    clock.now += timedelta(days=1)
    await worker._maybe_sweep_dead_vectors()
    assert len(calls) == 2, "next calendar day must trigger the sweep again"


async def test_sweep_failure_is_swallowed_and_not_retried_same_day(monkeypatch) -> None:
    engine = _make_engine()
    backend = SQLiteBackend(engine)
    clock = _Clock(datetime(2026, 6, 12, 9, 0, 0))
    calls: list[datetime] = []

    def broken_sweep(db, b, *, now=None, **kw):
        calls.append(now)
        raise RuntimeError("simulated sweep failure")

    monkeypatch.setattr(worker_mod, "sweep_dead_vectors", broken_sweep)
    worker = _make_worker(engine, backend=backend, now_fn=clock)

    # Must not raise.
    await worker._maybe_sweep_dead_vectors()
    await worker._maybe_sweep_dead_vectors()
    assert len(calls) == 1, "failure burns the day — no retry storm"


async def test_no_backend_means_no_sweep(monkeypatch) -> None:
    engine = _make_engine()
    calls: list[object] = []

    monkeypatch.setattr(worker_mod, "sweep_dead_vectors", lambda *a, **kw: calls.append(a))
    worker = _make_worker(engine, backend=None)

    await worker._maybe_sweep_dead_vectors()
    assert calls == []


async def test_worker_run_invokes_sweep_on_idle_cycle(monkeypatch) -> None:
    engine = _make_engine()
    backend = SQLiteBackend(engine)
    shutdown = asyncio.Event()
    calls: list[object] = []

    def fake_sweep(db, b, *, now=None, **kw):
        calls.append(now)
        shutdown.set()

        class _Counts:
            soft_deleted = 0
            superseded = 0

        return _Counts()

    monkeypatch.setattr(worker_mod, "sweep_dead_vectors", fake_sweep)
    worker = _make_worker(
        engine,
        backend=backend,
        poll_seconds=0.01,
        shutdown_event=shutdown,
    )
    await asyncio.wait_for(worker.run(), timeout=2.0)
    assert calls, "idle branch never invoked the dead-vector sweep"


async def test_sweep_end_to_end_through_worker() -> None:
    """Integration: the real sweep removes a long-dead node's vector when
    driven through the worker's idle hook."""
    engine = _make_engine()
    backend = SQLiteBackend(engine)
    now = datetime(2026, 6, 12, 9, 0, 0)

    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id="p1",
            user_id="self",
            type=NodeType.EVENT,
            description="worker sweep target",
            created_at=now - timedelta(days=60),
            deleted_at=now - timedelta(days=40),
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        backend.insert_vector(node.id, [0.1] * 384)
        nid = node.id

    worker = _make_worker(engine, backend=backend, now_fn=lambda: now)
    await worker._maybe_sweep_dead_vectors()

    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM concept_nodes_vec")).all()
    assert nid not in {r[0] for r in rows}

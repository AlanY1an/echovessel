"""ConsolidateWorker G phase tests (Spec 6 · plan §7.1).

Verifies that:
  1. When ``slow_cycle_fn`` is configured + enabled, the worker calls
     it after a successful consolidate and writes thoughts /
     expectations.
  2. ``slow_tick_enabled=False`` is a hard kill switch.
  3. Slow cycle failure does NOT unwind session close.
  4. Trivial sessions skip the G phase.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import ExtractedEvent, ExtractionResult
from echovessel.memory.models import ConceptNode
from echovessel.memory.slow_cycle import (
    SlowCycleExpectationInput,
    SlowCycleOutput,
    SlowCycleThoughtInput,
)
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="Luna"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _add_closing_session(engine, sid: str, message_contents: list[str]) -> None:
    with DbSession(engine) as db:
        db.add(
            Session(
                id=sid,
                persona_id="p",
                user_id="self",
                channel_id="t",
                status=SessionStatus.CLOSING,
                message_count=len(message_contents),
                total_tokens=sum(len(c) for c in message_contents),
            )
        )
        db.commit()
        for i, c in enumerate(message_contents):
            db.add(
                RecallMessage(
                    session_id=sid,
                    persona_id="p",
                    user_id="self",
                    channel_id="t",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=c,
                    day=date.today(),
                    token_count=len(c),
                )
            )
        db.commit()


async def _noop_reflect(_nodes, _reason):
    return []


def _small_extract_result(text: str = "user mentioned grad school progress"):
    async def _extract(_msgs):
        return ExtractionResult(
            events=[
                ExtractedEvent(
                    description=text,
                    emotional_impact=3,
                    emotion_tags=["hope"],
                )
            ]
        )

    return _extract


async def test_worker_runs_slow_cycle_after_consolidate():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    SQLiteBackend(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(
        engine,
        "s1",
        ["hi", "grad school is stressful", "I think I can finish it", "ok", "bye"],
    )

    slow_calls: list[dict] = []

    async def slow_fn(inp):
        slow_calls.append(dict(inp))
        first_event_id = (
            inp["recent_events"][-1]["id"] if inp.get("recent_events") else None
        )
        return SlowCycleOutput(
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="Alan carries grad school tension quietly",
                    filling_event_ids=[first_event_id] if first_event_id else [],
                    emotional_impact=-2,
                )
            ],
            new_expectations=[
                SlowCycleExpectationInput(
                    about_text="grad school progress",
                    prediction_text="Alan will share a milestone soon",
                    due_at=datetime(2026, 5, 1),
                    reasoning_event_ids=[first_event_id] if first_event_id else [],
                    emotional_impact=1,
                )
            ],
            input_tokens=300,
            output_tokens=50,
        )

    def _db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db_factory,
        backend=backend,
        extract_fn=_small_extract_result(),
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        slow_cycle_fn=slow_fn,
        slow_tick_enabled=True,
    )

    processed = await worker.drain_once()
    assert processed == 1
    assert len(slow_calls) == 1

    with DbSession(engine) as db:
        sess = db.get(Session, "s1")
        assert sess.status == SessionStatus.CLOSED

        thoughts = list(
            db.exec(
                select(ConceptNode).where(ConceptNode.type == NodeType.THOUGHT)
            )
        )
        expectations = list(
            db.exec(
                select(ConceptNode).where(ConceptNode.type == NodeType.EXPECTATION)
            )
        )
        assert len(thoughts) == 1
        assert thoughts[0].subject == "persona"
        assert len(expectations) == 1
        assert expectations[0].subject == "persona"

        persona = db.get(Persona, "p")
        assert persona.last_slow_tick_at is not None


async def test_worker_skips_slow_cycle_when_kill_switch_off():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(engine, "s2", ["a", "b", "c", "d", "e"])

    slow_calls: list[int] = []

    async def slow_fn(_inp):
        slow_calls.append(1)
        return SlowCycleOutput()

    def _db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db_factory,
        backend=backend,
        extract_fn=_small_extract_result(),
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        slow_cycle_fn=slow_fn,
        slow_tick_enabled=False,
    )
    await worker.drain_once()
    assert slow_calls == []


async def test_worker_slow_cycle_failure_leaves_session_closed():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(engine, "s3", ["x", "y", "z", "w", "v"])

    async def exploding_slow(_inp):
        raise RuntimeError("slow cycle blew up")

    def _db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db_factory,
        backend=backend,
        extract_fn=_small_extract_result(),
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        slow_cycle_fn=exploding_slow,
        slow_tick_enabled=True,
    )
    # Drain completes without raising — G phase failure is swallowed.
    await worker.drain_once()

    with DbSession(engine) as db:
        sess = db.get(Session, "s3")
        # Session is still CLOSED even though slow cycle blew up.
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True


async def test_worker_slow_cycle_noop_without_slow_fn():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    _add_closing_session(engine, "s4", ["a", "b", "c", "d", "e"])

    def _db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db_factory,
        backend=backend,
        extract_fn=_small_extract_result(),
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        slow_cycle_fn=None,
    )
    await worker.drain_once()

    with DbSession(engine) as db:
        sess = db.get(Session, "s4")
        assert sess.status == SessionStatus.CLOSED
        # No G-phase writes without a slow_cycle_fn.
        assert (
            list(
                db.exec(
                    select(ConceptNode).where(
                        ConceptNode.type == NodeType.THOUGHT
                    )
                )
            )
            == []
        )


async def test_worker_trivial_session_skips_slow_cycle():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    # Trivial session: only 1 short message.
    with DbSession(engine) as db:
        db.add(
            Session(
                id="s_trivial",
                persona_id="p",
                user_id="self",
                channel_id="t",
                status=SessionStatus.CLOSING,
                message_count=1,
                total_tokens=5,
            )
        )
        db.add(
            RecallMessage(
                session_id="s_trivial",
                persona_id="p",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="hi",
                day=date.today(),
                token_count=2,
            )
        )
        db.commit()

    slow_calls: list[int] = []

    async def slow_fn(_inp):
        slow_calls.append(1)
        return SlowCycleOutput()

    def _db_factory():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db_factory,
        backend=backend,
        extract_fn=_small_extract_result(),
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        slow_cycle_fn=slow_fn,
        slow_tick_enabled=True,
    )
    await worker.drain_once()
    assert slow_calls == []

    with DbSession(engine) as db:
        sess = db.get(Session, "s_trivial")
        assert sess.status == SessionStatus.CLOSED
        assert sess.trivial is True

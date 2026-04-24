"""Spec 4 · TurnTracer + NullTurnTracer behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date
from echovessel.runtime.turn_tracer import (
    NullTurnTracer,
    TurnTracer,
    make_turn_tracer,
)


def _engine():
    eng = create_engine(":memory:")
    ensure_schema_up_to_date(eng)
    create_all_tables(eng)
    ensure_schema_up_to_date(eng)
    return eng


def test_stage_start_end_pair_records_one_step() -> None:
    started = datetime.utcnow()
    t = TurnTracer(
        turn_id="t1",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
        started_at=started,
    )
    t.stage_start("ingest_user")
    t.stage_end("ingest_user", message_count=1)
    steps = t.steps()
    assert len(steps) == 1
    assert steps[0].stage == "ingest_user"
    assert steps[0].duration_ms >= 0
    assert steps[0].detail == {"message_count": 1}


def test_stage_end_without_start_silently_skips() -> None:
    t = TurnTracer(
        turn_id="t1",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
        started_at=datetime.utcnow(),
    )
    # No exception, no row added.
    t.stage_end("never_started")
    assert t.steps() == []


def test_synthetic_step_appends_with_overrides() -> None:
    t = TurnTracer(
        turn_id="t1",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
        started_at=datetime.utcnow(),
    )
    t.add_synthetic_step("debounce", t_ms=0, duration_ms=2000, message_count=2)
    steps = t.steps()
    assert len(steps) == 1
    assert steps[0].stage == "debounce"
    assert steps[0].t_ms == 0
    assert steps[0].duration_ms == 2000
    assert steps[0].detail == {"message_count": 2}


def test_persist_writes_row() -> None:
    eng = _engine()
    started = datetime.utcnow()
    t = TurnTracer(
        turn_id="t-abc",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
        started_at=started,
    )
    t.system_prompt = "# Persona\nYou are X"
    t.user_prompt = "# What they said\nHi"
    t.retrieval = [
        {
            "node_id": 1,
            "type": "event",
            "desc_snippet": "...",
            "recency": 0.5,
            "relevance": 0.6,
            "impact": 0.4,
            "relational": 0.0,
            "entity_anchor": 0.0,
            "total": 0.7,
            "anchored": False,
        }
    ]
    t.pinned_thoughts = {"user": [], "persona": []}
    t.entity_alias_hits = []
    t.episodic_state = {"mood": "calm", "energy": 6}
    t.llm_model = "claude-haiku-4-5"
    t.input_tokens = 1234
    t.output_tokens = 56
    t.first_token_ms = 320
    t.duration_ms = 3400
    t.finished_at = started + timedelta(milliseconds=3400)
    t.stage_start("ingest_user")
    t.stage_end("ingest_user")

    with DbSession(eng) as db:
        t.persist(db)

    with DbSession(eng) as db:
        row = db.execute(
            text("SELECT * FROM turn_traces WHERE turn_id=:tid"),
            {"tid": "t-abc"},
        ).fetchone()
    assert row is not None
    m = row._mapping
    assert m["persona_id"] == "p1"
    assert m["llm_model"] == "claude-haiku-4-5"
    assert m["duration_ms"] == 3400
    assert m["first_token_ms"] == 320
    assert m["input_tokens"] == 1234
    assert "ingest_user" in m["steps"]


def test_null_tracer_is_no_op() -> None:
    n = NullTurnTracer()
    # __setattr__ is dropped — assignments don't blow up but also don't
    # persist on the slotless instance.
    n.system_prompt = "anything"
    n.retrieval = [1, 2, 3]
    n.stage_start("x")
    n.stage_end("x", detail=1)
    n.add_synthetic_step("y", t_ms=0, duration_ms=0)
    assert n.steps() == []
    assert bool(n) is False


def test_make_turn_tracer_dispatch() -> None:
    a = make_turn_tracer(
        enabled=False,
        turn_id="t1",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
    )
    b = make_turn_tracer(
        enabled=True,
        turn_id="t2",
        persona_id="p1",
        user_id="u1",
        channel_id="web",
    )
    assert isinstance(a, NullTurnTracer)
    assert isinstance(b, TurnTracer)
    assert b.turn_id == "t2"


def test_null_tracer_persist_does_not_touch_db() -> None:
    eng = _engine()
    n = NullTurnTracer()
    with DbSession(eng) as db:
        n.persist(db)
        rows = db.execute(text("SELECT COUNT(*) FROM turn_traces")).scalar_one()
        assert rows == 0

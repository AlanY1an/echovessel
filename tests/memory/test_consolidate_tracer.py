"""Spec 4 · ConsolidateTracer + NullConsolidateTracer behaviour."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.consolidate_tracer import (
    ConsolidateTracer,
    NullConsolidateTracer,
    make_consolidate_tracer,
)
from echovessel.memory.migrations import ensure_schema_up_to_date


def _engine():
    eng = create_engine(":memory:")
    ensure_schema_up_to_date(eng)
    create_all_tables(eng)
    ensure_schema_up_to_date(eng)
    return eng


def test_phase_a_to_g_records_and_persists_full_row() -> None:
    eng = _engine()
    t = ConsolidateTracer(session_id="s1")
    t.record_phase_a(is_trivial=False, reason="above_threshold")
    t.record_phase_b(
        events_created=[{"id": 1, "description": "x", "impact": 4}],
        entities_resolved=[{"canonical_name": "Scott", "entity_id": 7}],
        junction_writes=[{"node_id": 1, "entity_id": 7}],
        junction_rejects=[
            {
                "node_id": 1,
                "entity_id": 7,
                "canonical_name": "Scott",
                "reason": "surface_form_not_in_description",
            }
        ],
        session_mood_signal={"mood": "calm", "energy": 6},
    )
    t.record_phase_c(shock_event_id=None)
    t.record_phase_d(timer_due=True, reflections_last_24h=1)
    t.record_phase_e(
        reflection_gate="timer",
        thoughts_created=[{"id": 21, "description": "ok"}],
    )
    t.record_phase_f(status="closed", extracted_at=None, close_trigger="extracted")
    t.record_phase_g(
        ran=True,
        cool_down_ok=True,
        budget_check="ok",
        nodes_written=[{"kind": "thought", "id": 99}],
    )

    with DbSession(eng) as db:
        t.persist(db)

    with DbSession(eng) as db:
        row = db.execute(
            text("SELECT * FROM session_traces WHERE session_id=:s"),
            {"s": "s1"},
        ).fetchone()
    assert row is not None
    m = row._mapping
    # Each phase JSON is non-null + carries the recorded payload.
    assert "above_threshold" in m["phase_a"]
    assert "junction_rejects" in m["phase_b"]
    assert "surface_form_not_in_description" in m["phase_b"]
    assert "shock_event_id" in m["phase_c"]
    assert "timer_due" in m["phase_d"]
    assert "thoughts_created" in m["phase_e"]
    assert "closed" in m["phase_f"]
    assert "nodes_written" in m["phase_g"]


def test_unrecorded_phases_persist_as_null() -> None:
    eng = _engine()
    t = ConsolidateTracer(session_id="s2")
    t.record_phase_a(is_trivial=True, reason="thresholds+no_strong_emotion")
    # Other phases skipped — their columns must stay NULL.

    with DbSession(eng) as db:
        t.persist(db)

    with DbSession(eng) as db:
        row = db.execute(
            text("SELECT * FROM session_traces WHERE session_id=:s"),
            {"s": "s2"},
        ).fetchone()
    m = row._mapping
    assert m["phase_a"] is not None
    assert m["phase_b"] is None
    assert m["phase_g"] is None


def test_null_consolidate_tracer_is_no_op() -> None:
    eng = _engine()
    n = NullConsolidateTracer()
    n.record_phase_a(is_trivial=False, reason="x")
    n.record_phase_b()
    n.record_phase_g(ran=False)
    with DbSession(eng) as db:
        n.persist(db)
        assert (
            db.execute(text("SELECT COUNT(*) FROM session_traces"))
            .scalar_one()
            == 0
        )
    assert bool(n) is False


def test_make_consolidate_tracer_dispatch() -> None:
    a = make_consolidate_tracer(enabled=False, session_id="s1")
    b = make_consolidate_tracer(enabled=True, session_id="s1")
    assert isinstance(a, NullConsolidateTracer)
    assert isinstance(b, ConsolidateTracer)


def test_phase_b_always_emits_junction_rejects_field_even_when_empty() -> None:
    eng = _engine()
    t = ConsolidateTracer(session_id="s_empty")
    t.record_phase_b()  # all defaults

    with DbSession(eng) as db:
        t.persist(db)

    with DbSession(eng) as db:
        row = db.execute(
            text("SELECT phase_b FROM session_traces WHERE session_id=:s"),
            {"s": "s_empty"},
        ).fetchone()
    # The JSON column carries an empty list for junction_rejects rather
    # than a missing key — the drawer relies on this to distinguish
    # "ran B, nothing rejected" from "never reached B".
    assert "junction_rejects" in row._mapping["phase_b"]

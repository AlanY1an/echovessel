"""Slow-tick reflection tests (Spec 6 · plan §7).

Covers the trigger logic, daily budget gate, typed writers (thoughts +
expectations), self-block edit-distance guard, and transcript
persistence. LLM callable is mocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import BlockLabel, NodeType, SessionStatus
from echovessel.memory import (
    CoreBlock,
    Persona,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    SlowCycleStats,
)
from echovessel.memory.slow_cycle import (
    SlowCycleBudgetExceeded,
    SlowCycleExpectationInput,
    SlowCycleOutput,
    SlowCycleThoughtInput,
    bulk_create_expectations,
    bulk_create_slow_thoughts,
    bump_slow_cycle_stats,
    get_daily_slow_cycle_stats,
    run_slow_cycle,
    session_has_shock_or_correction,
    should_run_slow_cycle,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="Luna"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _add_session(
    engine,
    sid: str,
    *,
    status: SessionStatus = SessionStatus.CLOSED,
    trivial: bool = False,
) -> None:
    with DbSession(engine) as db:
        db.add(
            Session(
                id=sid,
                persona_id="p",
                user_id="self",
                channel_id="t",
                status=status,
                trivial=trivial,
                message_count=5,
                total_tokens=100,
            )
        )
        db.commit()


def _add_event(
    engine,
    *,
    session_id: str | None = None,
    description: str,
    emotional_impact: int = 2,
    relational_tags: list[str] | None = None,
    created_at: datetime | None = None,
) -> int:
    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description=description,
            emotional_impact=emotional_impact,
            relational_tags=relational_tags or [],
            source_session_id=session_id,
            created_at=created_at or datetime.now(),
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node.id


# ---------------------------------------------------------------------------
# should_run_slow_cycle
# ---------------------------------------------------------------------------


def test_should_run_slow_cycle_first_time_fires():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1")
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        sess = db.get(Session, "s1")
        # last_slow_tick_at is None → main-path trigger fires.
        assert should_run_slow_cycle(
            db, persona=persona, session=sess, now=datetime.now()
        )


def test_should_run_slow_cycle_trivial_session_skips():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1", trivial=True)
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        sess = db.get(Session, "s1")
        assert should_run_slow_cycle(
            db, persona=persona, session=sess, now=datetime.now()
        ) is False


def test_should_run_slow_cycle_cool_down_blocks():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1")
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        persona.last_slow_tick_at = now - timedelta(minutes=5)
        db.add(persona)
        db.commit()
        sess = db.get(Session, "s1")
        assert should_run_slow_cycle(
            db, persona=persona, session=sess, now=now, cool_down_minutes=30
        ) is False


def test_should_run_slow_cycle_shock_bypasses_cool_down():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1")
    _add_event(
        engine,
        session_id="s1",
        description="user's cat died",
        emotional_impact=-10,  # shock
    )
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        persona.last_slow_tick_at = now - timedelta(minutes=5)
        db.add(persona)
        db.commit()
        sess = db.get(Session, "s1")
        assert should_run_slow_cycle(
            db, persona=persona, session=sess, now=now, cool_down_minutes=30
        )


def test_should_run_slow_cycle_correction_bypasses_cool_down():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1")
    _add_event(
        engine,
        session_id="s1",
        description="user pushed back on persona's assumption",
        emotional_impact=-1,
        relational_tags=["correction"],
    )
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        persona.last_slow_tick_at = now - timedelta(minutes=5)
        db.add(persona)
        db.commit()
        sess = db.get(Session, "s1")
        assert session_has_shock_or_correction(db, session=sess)
        assert should_run_slow_cycle(
            db, persona=persona, session=sess, now=now, cool_down_minutes=30
        )


def test_should_run_slow_cycle_kill_switch():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_session(engine, "s1")
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        sess = db.get(Session, "s1")
        assert should_run_slow_cycle(
            db,
            persona=persona,
            session=sess,
            now=datetime.now(),
            enabled=False,
        ) is False


# ---------------------------------------------------------------------------
# Daily budget
# ---------------------------------------------------------------------------


async def test_daily_cap_triggers_budget_exceeded():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        db.add(
            SlowCycleStats(
                date=now.date().isoformat(),
                persona_id="p",
                cycle_count=36,
                input_tokens=0,
                output_tokens=0,
            )
        )
        db.commit()

    async def slow_fn(_inp):
        pytest.fail("slow_cycle_fn must not be called once budget is hit")

    with DbSession(engine) as db, pytest.raises(SlowCycleBudgetExceeded):
        await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
            daily_cap=36,
        )


async def test_daily_input_token_budget_triggers_exceeded():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        db.add(
            SlowCycleStats(
                date=now.date().isoformat(),
                persona_id="p",
                cycle_count=0,
                input_tokens=200_000,
                output_tokens=0,
            )
        )
        db.commit()

    async def slow_fn(_inp):
        pytest.fail("must not call LLM")

    with DbSession(engine) as db, pytest.raises(SlowCycleBudgetExceeded):
        await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
            daily_input_token_budget=150_000,
        )


def test_bump_slow_cycle_stats_upserts_idempotently():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        row = bump_slow_cycle_stats(
            db, persona_id="p", now=now, input_tokens=100, output_tokens=20
        )
        assert row.cycle_count == 1
        assert row.input_tokens == 100
        row = bump_slow_cycle_stats(
            db, persona_id="p", now=now, input_tokens=50, output_tokens=5
        )
        assert row.cycle_count == 2
        assert row.input_tokens == 150
        assert row.output_tokens == 25
        rows = list(db.exec(select(SlowCycleStats)))
        assert len(rows) == 1


def test_get_daily_stats_returns_default_when_missing():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        stats = get_daily_slow_cycle_stats(db, persona_id="p", now=now)
        assert stats.cycle_count == 0
        assert stats.input_tokens == 0
        assert stats.output_tokens == 0
        # Default is an unsaved sentinel — no row in the DB.
        assert db.get(SlowCycleStats, (now.date().isoformat(), "p")) is None


# ---------------------------------------------------------------------------
# bulk_create_expectations + bulk_create_slow_thoughts
# ---------------------------------------------------------------------------


def test_bulk_create_expectations_writes_nodes_and_filling():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    evt_id = _add_event(engine, description="user mentioned grad school")
    with DbSession(engine) as db:
        ids = bulk_create_expectations(
            db,
            persona_id="p",
            user_id="self",
            expectations=[
                SlowCycleExpectationInput(
                    about_text="grad school applications",
                    prediction_text="Alan will share progress next week",
                    due_at=datetime(2026, 5, 2, 12, 0, 0),
                    reasoning_event_ids=[evt_id],
                    emotional_impact=2,
                )
            ],
        )
        assert len(ids) == 1
        node = db.get(ConceptNode, ids[0])
        assert node.type == NodeType.EXPECTATION
        assert node.subject == "persona"
        assert node.event_time_end is not None
        assert "grad school applications" in node.description
        # Filling row exists.
        filling = list(
            db.exec(
                select(ConceptNodeFilling).where(
                    ConceptNodeFilling.parent_id == ids[0]
                )
            )
        )
        assert len(filling) == 1
        assert filling[0].child_id == evt_id


def test_bulk_create_expectations_rejects_empty_reasoning():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db, pytest.raises(ValueError, match="reasoning_event_ids"):
        bulk_create_expectations(
            db,
            persona_id="p",
            user_id="self",
            expectations=[
                SlowCycleExpectationInput(
                    about_text="x",
                    prediction_text="y",
                    reasoning_event_ids=[],
                )
            ],
        )


def test_bulk_create_slow_thoughts_rejects_empty_filling():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db, pytest.raises(ValueError, match="filling_event_ids"):
        bulk_create_slow_thoughts(
            db,
            persona_id="p",
            user_id="self",
            thoughts=[
                SlowCycleThoughtInput(
                    description="orphan thought",
                    filling_event_ids=[],
                )
            ],
        )


# ---------------------------------------------------------------------------
# run_slow_cycle end-to-end
# ---------------------------------------------------------------------------


async def test_run_slow_cycle_writes_thoughts_and_expectations(tmp_path):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    e1 = _add_event(engine, description="user is applying to grad school")
    e2 = _add_event(engine, description="user is anxious about submissions")

    captured_input: dict = {}

    async def slow_fn(inp):
        captured_input.update(inp)
        return SlowCycleOutput(
            salient_questions=["is Alan regretting the choice?"],
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="Alan has been carrying the grad school stress alone",
                    filling_event_ids=[e1, e2],
                    emotional_impact=-3,
                )
            ],
            new_expectations=[
                SlowCycleExpectationInput(
                    about_text="grad school submissions",
                    prediction_text="Alan will mention a deadline next week",
                    due_at=datetime(2026, 5, 1),
                    reasoning_event_ids=[e1],
                    emotional_impact=2,
                )
            ],
            input_tokens=500,
            output_tokens=120,
        )

    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        result = await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
            transcript_dir=tmp_path,
        )

        assert result.ran
        assert len(result.thought_ids) == 1
        assert len(result.expectation_ids) == 1
        assert result.input_tokens == 500

        # Persona bookkeeping.
        persona = db.get(Persona, "p")
        assert persona.last_slow_tick_at == now

        # Stats upserted.
        row = db.get(SlowCycleStats, (now.date().isoformat(), "p"))
        assert row is not None
        assert row.cycle_count == 1
        assert row.input_tokens == 500
        assert row.output_tokens == 120

        # Transcript exists.
        transcripts = list(tmp_path.glob("*.json"))
        assert len(transcripts) == 1

    # Input payload shape — the LLM saw the recent events and the
    # bookkeeping metadata.
    assert "recent_events" in captured_input
    assert len(captured_input["recent_events"]) == 2
    assert captured_input["now_iso"] == now.isoformat()


async def test_run_slow_cycle_no_events_is_noop():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    async def slow_fn(_inp):
        pytest.fail("must not call LLM when there are no new events")

    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        persona = db.get(Persona, "p")
        # Set last tick so the "first cycle lookback" window does not
        # scoop up events older than 7 days. Fresh DB has none anyway.
        persona.last_slow_tick_at = now - timedelta(hours=1)
        db.add(persona)
        db.commit()

        result = await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
        )
        assert result.ran is False
        assert result.skipped_reason == "no_new_events"


async def test_run_slow_cycle_rejects_unknown_filling_event_id():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_event(engine, description="real event")

    async def slow_fn(_inp):
        return SlowCycleOutput(
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="fabricated",
                    filling_event_ids=[99999],  # not in input
                    emotional_impact=0,
                )
            ],
            input_tokens=1,
            output_tokens=1,
        )

    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db, pytest.raises(ValueError, match="unknown event ids"):
        await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
        )


async def test_run_slow_cycle_self_append_within_bound():
    """20% edit bound allows small appends to an existing self block."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    e1 = _add_event(engine, description="user shared a frank worry")

    # Seed an existing self block ~400 chars long so a 40-char append
    # is well within the 20% ratio.
    existing = "x" * 400
    with DbSession(engine) as db:
        db.add(
            CoreBlock(
                persona_id="p",
                user_id=None,
                label=BlockLabel.SELF.value,
                content=existing,
                char_count=len(existing),
            )
        )
        db.commit()

    async def slow_fn(_inp):
        return SlowCycleOutput(
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="Alan lets me in slowly",
                    filling_event_ids=[e1],
                    emotional_impact=1,
                )
            ],
            self_narrative_append="I learned to wait for him to open up.",
            input_tokens=200,
            output_tokens=50,
        )

    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        result = await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
        )
        assert result.self_appended is True
        block = db.exec(
            select(CoreBlock).where(
                CoreBlock.persona_id == "p",
                CoreBlock.label == BlockLabel.SELF.value,
            )
        ).one()
        assert "wait for him to open up" in block.content


async def test_run_slow_cycle_self_append_rejected_when_ratio_too_large():
    """A 50-char append on top of a 100-char self block exceeds 20%."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    e1 = _add_event(engine, description="user trusted me with a secret")

    existing = "y" * 100
    with DbSession(engine) as db:
        db.add(
            CoreBlock(
                persona_id="p",
                user_id=None,
                label=BlockLabel.SELF.value,
                content=existing,
                char_count=len(existing),
            )
        )
        db.commit()

    long_line = "z" * 80  # 80 chars on top of 100 → 80% ratio

    async def slow_fn(_inp):
        return SlowCycleOutput(
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="Alan trusted me",
                    filling_event_ids=[e1],
                    emotional_impact=3,
                )
            ],
            self_narrative_append=long_line,
            input_tokens=100,
            output_tokens=20,
        )

    now = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        result = await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
        )
        assert result.self_appended is False
        # Self block content is unchanged.
        block = db.exec(
            select(CoreBlock).where(
                CoreBlock.persona_id == "p",
                CoreBlock.label == BlockLabel.SELF.value,
            )
        ).one()
        assert block.content == existing


async def test_run_slow_cycle_truncates_events_over_input_budget():
    """With a tight input_token_limit, older events drop and newest survive."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    # Create events with increasing ids, using a large description so
    # each event is expensive enough that even two of them blow the
    # budget we set below.
    ids: list[int] = []
    now = datetime(2026, 4, 24, 12, 0, 0)
    for i in range(5):
        ids.append(
            _add_event(
                engine,
                description=(
                    f"event {i}: " + ("word " * 200)  # ~1000 chars each
                ),
                created_at=now - timedelta(hours=5 - i),
            )
        )

    observed_ids: list[int] = []

    async def slow_fn(inp):
        observed_ids.extend(
            int(e["id"]) for e in inp["recent_events"]
        )
        return SlowCycleOutput(
            new_thoughts=[
                SlowCycleThoughtInput(
                    description="noticed something",
                    filling_event_ids=[inp["recent_events"][-1]["id"]],
                    emotional_impact=1,
                )
            ],
            input_tokens=10,
            output_tokens=10,
        )

    with DbSession(engine) as db:
        await run_slow_cycle(
            db,
            persona_id="p",
            user_id="self",
            slow_cycle_fn=slow_fn,
            now=now,
            input_token_limit=600,  # very tight: only 1-2 events fit
        )

    assert len(observed_ids) >= 1
    assert len(observed_ids) <= 3
    # The most-recent event must always survive truncation.
    assert ids[-1] in observed_ids

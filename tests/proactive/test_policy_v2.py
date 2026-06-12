"""Policy · 5-gate evaluator tests over the v3 follow-up event shape.

Gates evaluated in order:

1. quiet_hours        (PersonaProfile.quiet_hours)
2. forbidden_topics   (matched against ConceptNode.follow_up_hint;
                       hit sets ``proactive_suppressed_at`` on the event)
3. in_flight_turn     (predicate)
4. rate_limit         (24h SQL count over proactive_decisions)
5. engagement_score   (soft, bypassed by high-confidence relational
                       tag / emotional_impact OR critical event)

v3 reads ``ConceptNode`` directly (memory event with ``follow_up_at``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from echovessel.core.types import NodeType
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import ConceptNode, Persona, User
from echovessel.memory.models import (
    ProactiveDecision as PersistedDecision,
)
from echovessel.proactive.core.base import (
    ActionType,
    EventType,
    ProactiveEvent,
    SkipReason,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.core.models import (
    PersonaProfile,
    ProactiveState,
)
from echovessel.proactive.engines.policy import PolicyEngine, quiet_window_end
from echovessel.proactive.execution.audit import SQLiteAuditSink
from tests.proactive.fakes import InMemoryMemoryApi

_NOW = datetime(2026, 5, 1, 14, 0, 0)


def _seed(engine, *, profile: PersonaProfile | None = None) -> None:
    with Session(engine) as db:
        db.add(Persona(id="p", display_name="P"))
        db.add(User(id="self", display_name="Owner"))
        if profile is not None:
            db.add(profile)
        db.commit()


def _profile(
    *,
    quiet_hours: list[int] | None = None,
    forbidden_topics: list[str] | None = None,
) -> PersonaProfile:
    return PersonaProfile(
        persona_id="p",
        style_summary="温柔关心，问候式开场。",
        quiet_hours=quiet_hours if quiet_hours is not None else [23, 7],
        forbidden_topics=forbidden_topics if forbidden_topics is not None else [],
        voice_id=None,
        profile_generated_at=_NOW,
        profile_source="llm_onboarding",
    )


def _state(*, score: float = 0.7) -> ProactiveState:
    return ProactiveState(
        persona_id="p",
        user_id="self",
        engagement_score=score,
        last_updated=_NOW - timedelta(days=1),
    )


def _event(
    *,
    event_id: int = 1,
    follow_up_hint: str = "提前关心面试",
    relational_tags: list[str] | None = None,
    emotional_impact: int = 4,
) -> ConceptNode:
    return ConceptNode(
        id=event_id,
        persona_id="p",
        user_id="self",
        type=NodeType.EVENT,
        description="面试 · 周一",
        emotional_impact=emotional_impact,
        relational_tags=relational_tags if relational_tags is not None else ["unresolved"],
        follow_up_at=_NOW - timedelta(minutes=1),
        follow_up_hint=follow_up_hint,
        advance_pre_hours=24,
        advance_post_hours=24,
        estimated_arc_days=14,
        event_time_start=_NOW + timedelta(days=1),
        event_time_end=_NOW + timedelta(days=1, hours=2),
        created_at=_NOW - timedelta(days=1),
    )


def _build_engine(
    engine,
    *,
    config: ProactiveConfig | None = None,
    is_turn_in_flight=None,
) -> PolicyEngine:
    cfg = config or ProactiveConfig(
        persona_id="p",
        user_id="self",
        max_per_24h=3,
    )
    audit = SQLiteAuditSink(db_factory=lambda: Session(engine))
    return PolicyEngine(
        config=cfg,
        audit=audit,
        memory=InMemoryMemoryApi(),
        is_turn_in_flight=is_turn_in_flight,
        db_factory=lambda: Session(engine),
    )


def _follow_up_event(
    event_id: int = 1, *, phase: str = "pre", critical: bool = False
) -> ProactiveEvent:
    return ProactiveEvent(
        event_type=EventType.FOLLOW_UP_DUE,
        persona_id="p",
        user_id="self",
        created_at=_NOW,
        payload={"event_id": event_id, "phase": phase, "decision_id": "d1"},
        critical=critical,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_follow_up_due_fires_when_all_gates_pass():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(relational_tags=["commitment"]))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SEND.value
    assert decision.skip_reason is None


def test_no_fireable_event_returns_no_trigger_match():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [],  # empty
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.NO_TRIGGER_MATCH.value


# ---------------------------------------------------------------------------
# Gate 1 · quiet_hours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now_hour,quiet,expect_block",
    [
        (3, [23, 7], True),  # wrap-midnight, inside
        (8, [23, 7], False),  # wrap-midnight, outside
        (12, [9, 17], True),  # same-day, inside
        (8, [9, 17], False),  # same-day, outside
        (23, [23, 7], True),  # wrap boundary start
        (7, [23, 7], False),  # wrap boundary end (exclusive)
    ],
)
def test_quiet_hours_gate(now_hour: int, quiet: list[int], expect_block: bool):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(quiet_hours=quiet))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(relational_tags=["commitment"]))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, now_hour, 0),
    )

    if expect_block:
        assert decision.action == ActionType.SKIP.value
        assert decision.skip_reason == SkipReason.QUIET_HOURS.value
    else:
        assert decision.action == ActionType.SEND.value


# ---------------------------------------------------------------------------
# Gate 2 · forbidden_topics
# ---------------------------------------------------------------------------


def test_forbidden_topics_blocks_event_on_hint_match():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(follow_up_hint="想跟你聊聊 politics"))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == "forbidden_topic"


def test_forbidden_topics_suppresses_event_when_blocked():
    """Forbidden is a hard veto that ALSO sets ``proactive_suppressed_at``
    on the source event so the same anchor doesn't re-arm on the next
    phase tick."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(follow_up_hint="想跟你聊聊 politics"))
        db.commit()

    pol = _build_engine(engine)
    pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    with Session(engine) as db:
        row = db.get(ConceptNode, 1)
    assert row.proactive_suppressed_at is not None


def test_forbidden_topics_inapplicable_when_event_missing():
    """No backing memory event (orphan FOLLOW_UP_DUE) → forbidden gate
    has nothing to match and falls through. Critical flag carries the
    decision past gate 5."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.2))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event(event_id=999, critical=True)],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


# ---------------------------------------------------------------------------
# Gate 3 · in_flight
# ---------------------------------------------------------------------------


def test_in_flight_predicate_blocks_decision():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event())
        db.commit()

    pol = _build_engine(engine, is_turn_in_flight=lambda: True)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.IN_FLIGHT_TURN.value


def test_in_flight_predicate_raises_treated_as_in_flight():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event())
        db.commit()

    def _boom() -> bool:
        raise RuntimeError("predicate exploded")

    pol = _build_engine(engine, is_turn_in_flight=_boom)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.IN_FLIGHT_TURN.value


# ---------------------------------------------------------------------------
# Gate 4 · rate_limit
# ---------------------------------------------------------------------------


def test_rate_limit_blocks_at_cap_via_sqlite_count():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event())
        # Three fires in the last 24h — at cap
        for i, hours in enumerate([1, 5, 12]):
            db.add(
                PersistedDecision(
                    decision_id=f"d-old-{i}",
                    timestamp=_NOW - timedelta(hours=hours),
                    persona_id="p",
                    user_id="self",
                    trigger_type="follow_up",
                    action="fire",
                    created_at=_NOW - timedelta(hours=hours),
                )
            )
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=_NOW,
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value


# ---------------------------------------------------------------------------
# Gate 5 · engagement_score
# ---------------------------------------------------------------------------


def test_engagement_low_blocks_low_confidence_event():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.3))
        db.add(_event(relational_tags=["unresolved"], emotional_impact=3))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == "low_engagement"


def test_engagement_low_bypassed_by_commitment_tag():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.3))
        db.add(_event(relational_tags=["commitment"], emotional_impact=2))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


def test_engagement_low_bypassed_by_high_emotional_impact():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.3))
        db.add(_event(relational_tags=["unresolved"], emotional_impact=8))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


def test_engagement_low_bypassed_by_critical_event_flag():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.2))
        db.add(_event(relational_tags=["unresolved"], emotional_impact=2))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event(critical=True)],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


def test_engagement_score_equal_to_threshold_suppresses():
    """``<= ENGAGEMENT_PASS`` (0.4) — at-or-below the threshold
    suppresses. CPython FP coincidentally lands ``0.7 - 0.15 - 0.15``
    on ``0.3999...`` so the old ``<`` happened to behave the same,
    but we make the semantics explicit so future reorderings can't
    flip the boundary."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(
            ProactiveState(
                persona_id="p",
                user_id="self",
                engagement_score=0.4,
                last_updated=_NOW - timedelta(days=1),
            )
        )
        db.add(_event(relational_tags=["unresolved"], emotional_impact=2))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == "low_engagement"


def test_engagement_initialised_when_state_row_missing():
    """First-touch: ProactiveState row absent → engine uses the
    default 0.7 baseline (above threshold) → fire."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_event(relational_tags=["unresolved"], emotional_impact=3))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


# ---------------------------------------------------------------------------
# Profile-missing graceful degradation
# ---------------------------------------------------------------------------


def test_missing_profile_does_not_crash_treats_as_no_quiet_hours():
    """If the persona has no PersonaProfile row yet (onboarding hasn't
    finished) the engine should not raise. quiet_hours / forbidden
    gates are inapplicable but other gates still run."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=None)
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(relational_tags=["commitment"]))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


# ---------------------------------------------------------------------------
# Skip provenance · trigger_payload on gate skips
# ---------------------------------------------------------------------------


def test_gate_skip_carries_event_provenance():
    """Every gate skip carries the event payload, so the persisted row
    gets source_event_id/phase — the FollowUpScheduler's cooldown,
    attempt-cap, and suppression queries filter on those columns and
    would otherwise never see suppressed attempts."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(quiet_hours=[23, 7]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_event(relational_tags=["commitment"]))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event(event_id=1, phase="pre")],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 2, 3, 0),  # inside quiet window
    )

    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.QUIET_HOURS.value
    assert decision.trigger_payload["event_id"] == 1
    assert decision.trigger_payload["phase"] == "pre"

    # And it survives the round-trip into the audit table.
    pol.audit.record(decision)
    with Session(engine) as db:
        row = db.exec(select(PersistedDecision)).first()
    assert row is not None
    assert row.action == "suppress"
    assert row.suppress_reason == "quiet_hours"
    assert row.source_event_id == 1
    assert row.phase == "pre"


@pytest.mark.parametrize(
    "reason_builder",
    [
        "forbidden_topic",
        "rate_limited",
        "low_engagement",
    ],
)
def test_every_gate_skip_persists_provenance(reason_builder: str):
    """Gates 2 / 4 / 5 also stamp provenance before short-circuiting."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    if reason_builder == "forbidden_topic":
        _seed(engine, profile=_profile(forbidden_topics=["politics"]))
        with Session(engine) as db:
            db.add(_state(score=0.7))
            db.add(_event(follow_up_hint="聊聊 politics"))
            db.commit()
    elif reason_builder == "rate_limited":
        _seed(engine, profile=_profile())
        with Session(engine) as db:
            db.add(_state(score=0.7))
            db.add(_event(relational_tags=["commitment"]))
            for i in range(3):
                db.add(
                    PersistedDecision(
                        decision_id=f"d-prior-{i}",
                        timestamp=datetime(2026, 5, 1, 8 + i, 0),
                        persona_id="p",
                        user_id="self",
                        trigger_type="follow_up",
                        action="fire",
                        created_at=datetime(2026, 5, 1, 8 + i, 0),
                    )
                )
            db.commit()
    else:  # low_engagement
        _seed(engine, profile=_profile())
        with Session(engine) as db:
            db.add(_state(score=0.2))
            db.add(_event(relational_tags=["unresolved"], emotional_impact=2))
            db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_follow_up_event(event_id=1, phase="on")],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == reason_builder
    assert decision.trigger_payload["event_id"] == 1
    assert decision.trigger_payload["phase"] == "on"

    pol.audit.record(decision)
    with Session(engine) as db:
        rows = db.exec(
            select(PersistedDecision).where(PersistedDecision.source_event_id == 1)
        ).all()
    assert len(rows) == 1
    assert rows[0].phase == "on"
    assert rows[0].action == "suppress"


# ---------------------------------------------------------------------------
# quiet_window_end helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now,quiet,expected",
    [
        # wrap-midnight window, before midnight → ends tomorrow 07:00
        (datetime(2026, 5, 1, 23, 30), [23, 7], datetime(2026, 5, 2, 7, 0)),
        # wrap-midnight window, after midnight → ends today 07:00
        (datetime(2026, 5, 2, 3, 0), [23, 7], datetime(2026, 5, 2, 7, 0)),
        # same-day window → ends today 17:00
        (datetime(2026, 5, 1, 12, 0), [9, 17], datetime(2026, 5, 1, 17, 0)),
        # outside the window → None
        (datetime(2026, 5, 1, 8, 0), [23, 7], None),
        (datetime(2026, 5, 1, 7, 0), [23, 7], None),  # end boundary exclusive
    ],
)
def test_quiet_window_end(now, quiet, expected):
    assert quiet_window_end(now, quiet) == expected

"""Policy v2 · 5-gate atomic rewrite tests · Stage 4.3.

Replaces the v1 trigger-matching cold_user / quiet_hours / rate_limit
flow. Gates evaluated in order:

1. quiet_hours        (profile-driven)
2. forbidden_topics   (only for THREAD_DUE events)
3. in_flight_turn     (predicate)
4. rate_limit         (24h SQL count over proactive_decisions)
5. engagement_score   (soft, bypassed by thread.confidence ≥ 0.8 OR critical event)

The v1 ``evaluate(events, ...)`` signature stays so the scheduler does
not change. The HIGH_EMOTIONAL_EVENT path is **atomic-replaced**,
not delete-then-add — the user's pre-flight pitfall A.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "FollowUpThread removed in v3 · policy refactored in Stage 3.1",
    allow_module_level=True,
)

from datetime import datetime, timedelta  # noqa: E402

from sqlmodel import Session  # noqa: E402

from echovessel.memory import create_all_tables, create_engine  # noqa: E402
from echovessel.memory.models import (  # noqa: E402
    FollowUpThread,
    Persona,
    PersonaProfile,
    ProactiveState,
    User,
)
from echovessel.memory.models import (  # noqa: E402
    ProactiveDecision as PersistedDecision,
)
from echovessel.proactive.core.base import (  # noqa: E402
    ActionType,
    EventType,
    ProactiveEvent,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig  # noqa: E402
from echovessel.proactive.engines.policy import PolicyEngine  # noqa: E402
from echovessel.proactive.execution.audit import SQLiteAuditSink  # noqa: E402
from tests.proactive.fakes import InMemoryMemoryApi  # noqa: E402

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


def _thread(
    *,
    confidence: float = 0.85,
    anchor_text: str = "提前关心面试",
    thread_id: int = 1,
) -> FollowUpThread:
    return FollowUpThread(
        id=thread_id,
        persona_id="p",
        user_id="self",
        source_event_id=1,
        anchor_text=anchor_text,
        kind="point_event_pre",
        due_at=_NOW - timedelta(minutes=1),
        confidence=confidence,
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


def _thread_due_event(thread_id: int = 1) -> ProactiveEvent:
    return ProactiveEvent(
        event_type=EventType.THREAD_DUE,
        persona_id="p",
        user_id="self",
        created_at=_NOW,
        payload={"thread_id": thread_id, "decision_id": "d1"},
    )


def _high_impact_event(critical: bool = True) -> ProactiveEvent:
    return ProactiveEvent(
        event_type=EventType.HIGH_EMOTIONAL_EVENT,
        persona_id="p",
        user_id="self",
        created_at=_NOW,
        payload={
            "source_event_id": 7,
            "emotional_impact": -9,
            "decision_id": "d-shock",
        },
        critical=critical,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_thread_due_fires_when_all_gates_pass():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_thread(confidence=0.85))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SEND.value
    assert decision.skip_reason is None


def test_high_emotional_event_fires_when_all_gates_pass():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_high_impact_event(critical=True)],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.HIGH_EMOTIONAL_EVENT.value


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
        db.add(_thread(confidence=0.85))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
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


def test_forbidden_topics_blocks_thread_on_anchor_match():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_thread(anchor_text="想跟你聊聊 politics"))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == "forbidden_topic"


def test_forbidden_topics_closes_thread_when_blocked():
    """spec 05: forbidden is a hard veto that ALSO closes the thread
    so the same anchor doesn't re-trigger every cooldown."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_thread(anchor_text="想跟你聊聊 politics"))
        db.commit()

    pol = _build_engine(engine)
    pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )

    with Session(engine) as db:
        thread = db.get(FollowUpThread, 1)
    assert thread.closed_at is not None


def test_forbidden_topics_inapplicable_to_high_emotional_event():
    """High-emotional events have no anchor_text — gate cannot
    suppress them on this rule."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile(forbidden_topics=["politics"]))
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_high_impact_event(critical=True)],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


# ---------------------------------------------------------------------------
# Gate 3 · in_flight (recover the test coverage Stage 3 lost)
# ---------------------------------------------------------------------------


def test_in_flight_predicate_blocks_decision():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.7))
        db.add(_thread())
        db.commit()

    pol = _build_engine(engine, is_turn_in_flight=lambda: True)
    decision = pol.evaluate(
        [_thread_due_event()],
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
        db.add(_thread())
        db.commit()

    def _boom() -> bool:
        raise RuntimeError("predicate exploded")

    pol = _build_engine(engine, is_turn_in_flight=_boom)
    decision = pol.evaluate(
        [_thread_due_event()],
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
        db.add(_thread())
        # Three fires in the last 24h — at cap
        for i, hours in enumerate([1, 5, 12]):
            db.add(
                PersistedDecision(
                    decision_id=f"d-old-{i}",
                    timestamp=_NOW - timedelta(hours=hours),
                    persona_id="p",
                    user_id="self",
                    trigger_type="thread_due",
                    action="fire",
                    created_at=_NOW - timedelta(hours=hours),
                )
            )
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=_NOW,
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value


# ---------------------------------------------------------------------------
# Gate 5 · engagement_score (replaces v1 cold_user)
# ---------------------------------------------------------------------------


def test_engagement_low_blocks_low_confidence_thread():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.3))
        db.add(_thread(confidence=0.5))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == "low_engagement"


def test_engagement_low_bypassed_by_high_confidence_thread():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.3))
        db.add(_thread(confidence=0.85))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


def test_engagement_low_bypassed_by_critical_event():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        db.add(_state(score=0.2))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_high_impact_event(critical=True)],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value


def test_engagement_score_equal_to_threshold_suppresses():
    """Stage 5 boundary fix: ``<= ENGAGEMENT_PASS`` (0.4) — at-or-below
    the threshold suppresses. CPython FP coincidentally lands
    ``0.7 - 0.15 - 0.15`` on ``0.3999...`` so the old ``<`` happened
    to behave the same, but we now make the semantics explicit so
    future reorderings can't flip the boundary."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine, profile=_profile())
    with Session(engine) as db:
        # Exactly at the threshold (uses Decimal-style explicit
        # construction to bypass any FP arithmetic surprise).
        db.add(
            ProactiveState(
                persona_id="p",
                user_id="self",
                engagement_score=0.4,
                last_updated=_NOW - timedelta(days=1),
            )
        )
        db.add(_thread(confidence=0.5))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
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
        db.add(_thread(confidence=0.6))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
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
        db.add(_thread(confidence=0.85))
        db.commit()

    pol = _build_engine(engine)
    decision = pol.evaluate(
        [_thread_due_event()],
        persona_id="p",
        user_id="self",
        now=datetime(2026, 5, 1, 12, 0),
    )
    assert decision.action == ActionType.SEND.value

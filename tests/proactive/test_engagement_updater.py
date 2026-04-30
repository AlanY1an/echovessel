"""engagement_updater tests · Stage 4.1.

The BA contingent reward loop has four update paths (spec 05):

* User reply within 24h of a fire → +0.10 (decision marked replied)
* User starts a turn with no pending fire → +0.05 (initiative)
* 24h passed with no reply → −0.15 (decision marked sentinel)
* 7+ silent days → −0.05 / day (idempotent per day)

Critical pitfall (user pre-flight #1): the same observer call
must NOT double-count by adding +0.10 *and* +0.05.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from echovessel.core.types import MessageRole
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import (
    Persona,
    RecallMessage,
    User,
)
from echovessel.proactive.core.models import (
    ProactiveDecision,
    ProactiveState,
)
from echovessel.proactive.execution.engagement_updater import (
    DELTA_DAILY_DECAY,
    DELTA_NOT_REPLIED,
    DELTA_REPLIED,
    DELTA_USER_INITIATIVE,
    ENGAGEMENT_INITIAL,
    ENGAGEMENT_MAX,
    ENGAGEMENT_MIN,
    NOT_REPLIED_SENTINEL,
    apply_silence_decay,
    handle_user_message_ingested,
    settle_unreplied_fires,
)

_NOW = datetime(2026, 5, 1, 14, 0, 0)


def _seed(db: Session) -> None:
    db.add(Persona(id="p1", display_name="P"))
    db.add(User(id="self", display_name="Owner"))
    db.commit()


def _seed_state(db: Session, *, score: float = ENGAGEMENT_INITIAL) -> None:
    db.add(
        ProactiveState(
            persona_id="p1",
            user_id="self",
            engagement_score=score,
            last_updated=_NOW - timedelta(days=1),
        )
    )
    db.commit()


def _seed_fire_decision(
    db: Session,
    *,
    timestamp: datetime,
    user_replied_at: datetime | None = None,
    decision_id: str = "d1",
) -> ProactiveDecision:
    d = ProactiveDecision(
        decision_id=decision_id,
        timestamp=timestamp,
        persona_id="p1",
        user_id="self",
        trigger_type="thread_due",
        action="fire",
        user_replied_at=user_replied_at,
        created_at=timestamp,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _user_message(*, content: str = "thanks!", created_at: datetime = _NOW) -> RecallMessage:
    return RecallMessage(
        session_id="s1",
        persona_id="p1",
        user_id="self",
        channel_id="t",
        role=MessageRole.USER,
        content=content,
        day=created_at.date(),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Path 1 · user reply within 24h → +0.10
# ---------------------------------------------------------------------------


def test_user_reply_in_24h_adds_010_and_marks_replied():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.5)
        # Fire 3 hours ago, no reply yet
        _seed_fire_decision(
            db,
            timestamp=_NOW - timedelta(hours=3),
            decision_id="d-pending",
        )

    msg = _user_message(content="answering you")

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=msg,
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        state = db.get(ProactiveState, ("p1", "self"))
        assert state.engagement_score == pytest.approx(0.6)  # 0.5 + 0.10
        assert state.last_updated == _NOW

        decisions = list(db.exec(select(ProactiveDecision)))
        replied = [d for d in decisions if d.user_replied_at == _NOW]
        assert len(replied) == 1
        assert replied[0].decision_id == "d-pending"


def test_user_reply_with_no_pending_fire_uses_initiative_path():
    """User starts a turn but there's nothing pending — that's a
    +0.05 initiative bump, NOT a +0.10 reply credit."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.5)

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=_user_message(),
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.55
        )  # 0.5 + 0.05


def test_user_reply_does_not_double_count():
    """The pending-fire path adds +0.10. It must NOT also add +0.05
    on the same call. Pitfall #1 from the user pre-flight."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.5)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=2))

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=_user_message(),
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        score = db.get(ProactiveState, ("p1", "self")).engagement_score
        # Must be exactly +0.10, not +0.15 (which would mean both paths fired)
        assert score == pytest.approx(0.6)


def test_persona_reply_skipped():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.5)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=1))

    persona_msg = RecallMessage(
        session_id="s1",
        persona_id="p1",
        user_id="self",
        channel_id="t",
        role=MessageRole.PERSONA,
        content="hi",
        day=_NOW.date(),
        created_at=_NOW,
    )
    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=persona_msg,
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        # No update at all
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(0.5)


def test_user_reply_outside_24h_window_no_credit():
    """A pending fire from 30 hours ago is past the reply window;
    the reply lands as initiative (+0.05), not as a credit
    (+0.10)."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.5)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=30))

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=_user_message(),
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.55
        )


def test_state_initialised_when_missing():
    """First-touch behaviour: no ProactiveState row yet → use the
    initial 0.7 baseline + apply the delta."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=2))

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=_user_message(),
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        state = db.get(ProactiveState, ("p1", "self"))
        assert state is not None
        assert state.engagement_score == pytest.approx(ENGAGEMENT_INITIAL + DELTA_REPLIED)


# ---------------------------------------------------------------------------
# Path 3 · 24h passes with no reply → −0.15 + sentinel
# ---------------------------------------------------------------------------


def test_settle_unreplied_marks_sentinel_and_decays():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.7)
        # Two pending fires, both > 24h ago
        _seed_fire_decision(
            db,
            timestamp=_NOW - timedelta(hours=30),
            decision_id="d-stale-1",
        )
        _seed_fire_decision(
            db,
            timestamp=_NOW - timedelta(hours=48),
            decision_id="d-stale-2",
        )
        # One still inside the 24h window
        _seed_fire_decision(
            db,
            timestamp=_NOW - timedelta(hours=10),
            decision_id="d-fresh",
        )

    with Session(engine) as db:
        settled = settle_unreplied_fires(
            db,
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    assert settled == 2

    with Session(engine) as db:
        score = db.get(ProactiveState, ("p1", "self")).engagement_score
        # 0.7 - 0.15 * 2 = 0.40
        assert score == pytest.approx(0.4)

        decisions = {d.decision_id: d for d in db.exec(select(ProactiveDecision))}
        assert decisions["d-stale-1"].user_replied_at == NOT_REPLIED_SENTINEL
        assert decisions["d-stale-2"].user_replied_at == NOT_REPLIED_SENTINEL
        assert decisions["d-fresh"].user_replied_at is None


def test_settle_unreplied_idempotent_does_not_double_decay():
    """Re-running settle_unreplied_fires on already-sentineled rows
    does not subtract the score again — the WHERE filter is
    user_replied_at IS NULL."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.7)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=30))

    with Session(engine) as db:
        first = settle_unreplied_fires(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )
        second = settle_unreplied_fires(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert first == 1
    assert second == 0
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.55
        )


def test_settle_unreplied_no_pending_returns_zero():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.7)

    with Session(engine) as db:
        settled = settle_unreplied_fires(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert settled == 0
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Path 4 · silence decay
# ---------------------------------------------------------------------------


def test_silence_decay_after_7_days_subtracts_005():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.6)
        # Last user message 9 days ago
        old = _NOW - timedelta(days=9)
        db.add(
            RecallMessage(
                session_id="s1",
                persona_id="p1",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="long ago",
                day=old.date(),
                created_at=old,
            )
        )
        db.commit()

    with Session(engine) as db:
        applied = apply_silence_decay(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert applied == 1
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.55
        )  # 0.6 - 0.05


def test_silence_decay_does_not_apply_under_7_days():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.6)
        recent = _NOW - timedelta(days=3)
        db.add(
            RecallMessage(
                session_id="s1",
                persona_id="p1",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="recent",
                day=recent.date(),
                created_at=recent,
            )
        )
        db.commit()

    with Session(engine) as db:
        applied = apply_silence_decay(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert applied == 0
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.6
        )


def test_silence_decay_idempotent_within_same_day():
    """Two calls on the same day apply the decay only once."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        # state.last_updated must be < today so first call goes through
        db.add(
            ProactiveState(
                persona_id="p1",
                user_id="self",
                engagement_score=0.6,
                last_updated=_NOW - timedelta(days=2),
            )
        )
        old = _NOW - timedelta(days=10)
        db.add(
            RecallMessage(
                session_id="s1",
                persona_id="p1",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="old",
                day=old.date(),
                created_at=old,
            )
        )
        db.commit()

    with Session(engine) as db:
        applied_a = apply_silence_decay(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )
    later_today = _NOW.replace(hour=20)
    with Session(engine) as db:
        applied_b = apply_silence_decay(
            db, persona_id="p1", user_id="self", now_fn=lambda: later_today
        )

    assert applied_a == 1
    assert applied_b == 0  # already updated today
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.55
        )


def test_silence_decay_no_user_history_returns_zero():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.6)

    with Session(engine) as db:
        applied = apply_silence_decay(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert applied == 0


# ---------------------------------------------------------------------------
# Score clamping
# ---------------------------------------------------------------------------


def test_engagement_clamps_at_min():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.1)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=30))

    with Session(engine) as db:
        settle_unreplied_fires(db, persona_id="p1", user_id="self", now_fn=lambda: _NOW)

    with Session(engine) as db:
        # 0.1 - 0.15 = -0.05 → clamps to ENGAGEMENT_MIN (0.05)
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            ENGAGEMENT_MIN
        )


def test_engagement_clamps_at_max():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        _seed_state(db, score=0.9)
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=2))

    with Session(engine) as db:
        handle_user_message_ingested(
            db,
            msg=_user_message(),
            persona_id="p1",
            user_id="self",
            now_fn=lambda: _NOW,
        )

    with Session(engine) as db:
        # 0.9 + 0.10 = 1.00 → clamps to ENGAGEMENT_MAX (0.95)
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            ENGAGEMENT_MAX
        )


def test_constants_match_spec():
    assert DELTA_REPLIED == 0.10
    assert DELTA_NOT_REPLIED == -0.15
    assert DELTA_USER_INITIATIVE == 0.05
    assert DELTA_DAILY_DECAY == -0.05
    assert ENGAGEMENT_INITIAL == 0.7
    assert ENGAGEMENT_MIN == 0.05
    assert ENGAGEMENT_MAX == 0.95


# ---------------------------------------------------------------------------
# maintain_engagement · the periodic worker-driven entry point
# ---------------------------------------------------------------------------


def test_maintain_engagement_processes_new_user_messages_once():
    """The maintain_engagement scan uses ``state.last_updated`` as a
    watermark so a re-run on the same data is a no-op (path 1+2
    are not idempotent on their own; the watermark gates them)."""
    from echovessel.proactive.execution.engagement_updater import (
        maintain_engagement,
    )

    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        # state.last_updated 4h ago — anything after that is "new"
        db.add(
            ProactiveState(
                persona_id="p1",
                user_id="self",
                engagement_score=0.5,
                last_updated=_NOW - timedelta(hours=4),
            )
        )
        # Two user messages after the watermark
        for hours in [3, 1]:
            ts = _NOW - timedelta(hours=hours)
            db.add(
                RecallMessage(
                    session_id="s1",
                    persona_id="p1",
                    user_id="self",
                    channel_id="t",
                    role=MessageRole.USER,
                    content=f"msg {hours}",
                    day=ts.date(),
                    created_at=ts,
                )
            )
        db.commit()

    with Session(engine) as db:
        first = maintain_engagement(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )
    # 0.5 + 0.05 + 0.05 = 0.60 (two initiative bumps)
    with Session(engine) as db:
        score = db.get(ProactiveState, ("p1", "self")).engagement_score
        assert score == pytest.approx(0.60)
    assert first.user_messages_processed == 2

    # Second run: watermark advanced; no new messages → no-op
    with Session(engine) as db:
        second = maintain_engagement(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )
    assert second.user_messages_processed == 0
    with Session(engine) as db:
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.60
        )


def test_maintain_engagement_runs_settle_and_decay():
    """The single maintainer invocation rolls path 3 (settle) and
    path 4 (decay) into the same call so the worker only needs one
    helper attached to the idle branch."""
    from echovessel.proactive.execution.engagement_updater import (
        maintain_engagement,
    )

    engine = create_engine(":memory:")
    create_all_tables(engine)
    with Session(engine) as db:
        _seed(db)
        # state.last_updated yesterday so silence-decay can fire
        db.add(
            ProactiveState(
                persona_id="p1",
                user_id="self",
                engagement_score=0.7,
                last_updated=_NOW - timedelta(days=1),
            )
        )
        # No recent user messages — last user message is 9 days ago
        old = _NOW - timedelta(days=9)
        db.add(
            RecallMessage(
                session_id="s1",
                persona_id="p1",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="long ago",
                day=old.date(),
                created_at=old,
            )
        )
        # One pending fire from 30h ago
        _seed_fire_decision(db, timestamp=_NOW - timedelta(hours=30))

    with Session(engine) as db:
        outcome = maintain_engagement(
            db, persona_id="p1", user_id="self", now_fn=lambda: _NOW
        )

    assert outcome.settled == 1  # path 3
    assert outcome.silence_decayed == 1  # path 4
    with Session(engine) as db:
        # 0.7 - 0.15 (settle) - 0.05 (decay) = 0.50
        assert db.get(ProactiveState, ("p1", "self")).engagement_score == pytest.approx(
            0.50
        )

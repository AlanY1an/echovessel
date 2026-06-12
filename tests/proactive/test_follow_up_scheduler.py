"""FollowUpScheduler · core dispatch tests · Stage 2.3.

Covers timer dispatch, wake→drain→reschedule ordering, and the
quiet-hours retry window.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import ConceptNode, NodeType, Persona, User
from echovessel.memory.models import (
    ProactiveDecision as PersistedDecision,
)
from echovessel.proactive.core.base import EventType
from echovessel.proactive.core.models import PersonaProfile
from echovessel.proactive.execution.follow_up_scheduler import (
    ATTEMPT_CAP,
    FollowUpScheduler,
)
from tests.proactive.fakes import FakeProactiveScheduler


@pytest.fixture
def db_factory():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with Session(engine) as db:
        db.add(Persona(id="p", display_name="Test"))
        db.add(User(id="u", display_name="Test"))
        db.commit()

    def _factory():
        return Session(engine)

    return _factory


@pytest.fixture
def fake_proactive_scheduler(db_factory):
    return FakeProactiveScheduler(db_factory=db_factory)


async def test_schedules_timer_and_fires(db_factory, fake_proactive_scheduler):
    """Reminder event (advance_pre=0, advance_post=0) due in 0.1s fires once."""
    fire_time = datetime.now() + timedelta(seconds=0.1)

    with db_factory() as db:
        event = ConceptNode(
            persona_id="p",
            user_id="u",
            type=NodeType.EVENT,
            description="reminder",
            follow_up_at=fire_time,
            event_time_start=fire_time,
            event_time_end=fire_time,
            advance_pre_hours=0,
            advance_post_hours=0,
            estimated_arc_days=1,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_id = event.id

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id="p",
        user_id="u",
    )

    with db_factory() as db:
        sched.on_event_created(db.get(ConceptNode, event_id))

    await asyncio.sleep(0.5)

    assert fake_proactive_scheduler.events
    proactive_event = fake_proactive_scheduler.events[-1]
    assert proactive_event.event_type == EventType.FOLLOW_UP_DUE
    assert proactive_event.payload["event_id"] == event_id
    assert proactive_event.payload["phase"] == "on"

    await sched.stop()


async def test_skips_event_without_follow_up_at(db_factory, fake_proactive_scheduler):
    """on_event_created with follow_up_at=None → no timer scheduled."""
    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id="p",
        user_id="u",
    )

    with db_factory() as db:
        event = ConceptNode(
            persona_id="p",
            user_id="u",
            type=NodeType.EVENT,
            description="ordinary",
            follow_up_at=None,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        sched.on_event_created(event)

    await asyncio.sleep(0.2)
    assert fake_proactive_scheduler.events == []

    await sched.stop()


async def test_start_initializes_pending_events(db_factory, fake_proactive_scheduler):
    """daemon start() finds DB-pending events and schedules timers."""
    fire_time = datetime.now() + timedelta(seconds=0.1)

    with db_factory() as db:
        db.add(
            ConceptNode(
                persona_id="p",
                user_id="u",
                type=NodeType.EVENT,
                description="pending",
                follow_up_at=fire_time,
                event_time_start=fire_time,
                event_time_end=fire_time,
                advance_pre_hours=0,
                advance_post_hours=0,
                estimated_arc_days=1,
            )
        )
        db.commit()

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id="p",
        user_id="u",
    )
    await sched.start()

    await asyncio.sleep(0.5)
    assert fake_proactive_scheduler.events

    await sched.stop()


async def test_start_after_stop_resets_shutdown(db_factory, fake_proactive_scheduler):
    """start() must reset _shutdown so the scheduler can run again after stop()."""
    fire_time = datetime.now() + timedelta(seconds=0.1)

    with db_factory() as db:
        event = ConceptNode(
            persona_id="p",
            user_id="u",
            type=NodeType.EVENT,
            description="reminder",
            follow_up_at=fire_time,
            event_time_start=fire_time,
            event_time_end=fire_time,
            advance_pre_hours=0,
            advance_post_hours=0,
            estimated_arc_days=1,
        )
        db.add(event)
        db.commit()

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id="p",
        user_id="u",
    )

    await sched.start()
    await sched.stop()
    fake_proactive_scheduler.events.clear()

    # Re-arm: bump fire_time and call start() again
    new_fire = datetime.now() + timedelta(seconds=0.1)
    with db_factory() as db:
        e = db.exec(select(ConceptNode)).first()
        e.follow_up_at = new_fire
        e.event_time_start = new_fire
        e.event_time_end = new_fire
        db.commit()

    await sched.start()
    await asyncio.sleep(0.5)

    assert fake_proactive_scheduler.events
    await sched.stop()


# ---------------------------------------------------------------------------
# Wake path · drain-before-reschedule
# ---------------------------------------------------------------------------


def _reminder_event(db_factory, *, fire_time: datetime) -> int:
    with db_factory() as db:
        event = ConceptNode(
            persona_id="p",
            user_id="u",
            type=NodeType.EVENT,
            description="reminder",
            follow_up_at=fire_time,
            event_time_start=fire_time,
            event_time_end=fire_time,
            advance_pre_hours=0,
            advance_post_hours=0,
            estimated_arc_days=1,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event.id


async def test_wake_drains_before_reschedule_no_duplicate_phase(
    db_factory, fake_proactive_scheduler
):
    """The wake must await the scheduler's drain so the decision row
    exists before the next attempt is computed. Rescheduling against
    pre-drain state would re-arm the SAME phase at delay 0 and emit a
    duplicate FOLLOW_UP_DUE for an already-fired phase."""
    event_id = _reminder_event(db_factory, fire_time=datetime.now() + timedelta(seconds=0.05))

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id="p",
        user_id="u",
    )
    with db_factory() as db:
        sched.on_event_created(db.get(ConceptNode, event_id))

    await asyncio.sleep(0.4)

    # Exactly one dispatch for the phase, and the drain ran between
    # the notify and any rescheduling.
    assert fake_proactive_scheduler.calls == ["notify:on", "tick"]

    # The fired phase stays retired — no delay-0 re-arm afterwards.
    await asyncio.sleep(0.2)
    assert len(fake_proactive_scheduler.events) == 1

    await sched.stop()


async def test_suppressed_attempts_count_toward_attempt_cap(db_factory):
    """Suppress rows carry source_event_id/phase, so the attempt cap
    sees them and stops re-arming once the cap is reached."""
    now = datetime(2026, 5, 1, 12, 0)
    event_id = _reminder_event(db_factory, fire_time=now - timedelta(hours=1))

    with db_factory() as db:
        for i in range(ATTEMPT_CAP):
            db.add(
                PersistedDecision(
                    decision_id=f"d-cap-{i}",
                    timestamp=now - timedelta(hours=ATTEMPT_CAP - i),
                    persona_id="p",
                    user_id="u",
                    trigger_type="follow_up",
                    source_event_id=event_id,
                    phase="on",
                    action="suppress",
                    suppress_reason="rate_limit",
                    created_at=now - timedelta(hours=ATTEMPT_CAP - i),
                )
            )
        db.commit()

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=FakeProactiveScheduler(db_factory=db_factory),
        persona_id="p",
        user_id="u",
        now_fn=lambda: now,
    )
    with db_factory() as db:
        event = db.get(ConceptNode, event_id)
    assert sched._compute_next_attempt(event) is None


# ---------------------------------------------------------------------------
# Quiet-hours retry window
# ---------------------------------------------------------------------------


def _seed_profile(db_factory, *, quiet_hours: list[int]) -> None:
    with db_factory() as db:
        db.add(
            PersonaProfile(
                persona_id="p",
                style_summary="温柔关心",
                quiet_hours=quiet_hours,
                forbidden_topics=[],
                voice_id=None,
                profile_generated_at=datetime(2026, 5, 1, 0, 0),
                profile_source="llm_onboarding",
            )
        )
        db.commit()


async def test_quiet_hours_suppress_retries_at_window_end(db_factory):
    """A quiet-hours-suppressed attempt re-arms at the end of the
    persona's quiet window, not immediately (which would just be
    suppressed again, spinning all night)."""
    _seed_profile(db_factory, quiet_hours=[23, 7])
    now = datetime(2026, 5, 1, 23, 30)
    event_id = _reminder_event(db_factory, fire_time=datetime(2026, 5, 1, 23, 0))

    with db_factory() as db:
        db.add(
            PersistedDecision(
                decision_id="d-quiet",
                timestamp=now,
                persona_id="p",
                user_id="u",
                trigger_type="follow_up",
                source_event_id=event_id,
                phase="on",
                action="suppress",
                suppress_reason="quiet_hours",
                created_at=now,
            )
        )
        db.commit()

    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=FakeProactiveScheduler(db_factory=db_factory),
        persona_id="p",
        user_id="u",
        now_fn=lambda: now,
    )
    with db_factory() as db:
        event = db.get(ConceptNode, event_id)

    result = sched._compute_next_attempt(event)
    assert result is not None
    phase, when = result
    assert phase == "on"
    assert when == datetime(2026, 5, 2, 7, 0)  # quiet window [23, 7] ends


async def test_quiet_hours_wake_suppress_parks_timer_until_window_end(db_factory):
    """End-to-end wake during quiet hours: notify → drain persists the
    suppress row with provenance → reschedule parks the timer at the
    window end instead of spinning at delay 0."""
    _seed_profile(db_factory, quiet_hours=[23, 7])
    now = datetime(2026, 5, 1, 23, 30)
    event_id = _reminder_event(db_factory, fire_time=datetime(2026, 5, 1, 23, 0))

    fake = FakeProactiveScheduler(
        db_factory=db_factory, action="suppress", suppress_reason="quiet_hours"
    )
    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake,
        persona_id="p",
        user_id="u",
        now_fn=lambda: now,
    )
    with db_factory() as db:
        sched.on_event_created(db.get(ConceptNode, event_id))

    await asyncio.sleep(0.3)

    assert fake.calls == ["notify:on", "tick"]
    # Timer re-armed for the window end, not fired again.
    timer = sched._active_timers.get(event_id)
    assert timer is not None and not timer.done()
    assert len(fake.events) == 1

    await sched.stop()

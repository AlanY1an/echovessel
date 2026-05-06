"""FollowUpScheduler · core dispatch tests · Stage 2.3.

Smart cooldown / attempt_cap / forbidden_topic tests come in Stage 2.3b.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, select

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import ConceptNode, NodeType, Persona, User
from echovessel.proactive.core.base import EventType
from echovessel.proactive.execution.follow_up_scheduler import FollowUpScheduler


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
def fake_proactive_scheduler():
    return MagicMock(notify=MagicMock())


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

    assert fake_proactive_scheduler.notify.called
    proactive_event = fake_proactive_scheduler.notify.call_args.args[0]
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
    fake_proactive_scheduler.notify.assert_not_called()

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
    assert fake_proactive_scheduler.notify.called

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
    fake_proactive_scheduler.notify.reset_mock()

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

    assert fake_proactive_scheduler.notify.called
    await sched.stop()

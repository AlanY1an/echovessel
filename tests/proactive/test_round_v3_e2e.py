"""v3 memory-driven proactive · e2e happy path · Stage 5.3.

User mentions an exam → consolidate's Phase B writes a ConceptNode with
``follow_up_at`` populated → ``MemoryFollowUpObserver`` forwards the new
event to ``FollowUpScheduler`` → the scheduler dispatches a
``FOLLOW_UP_DUE`` ``ProactiveEvent`` at the pre window. User then reports
the outcome → consolidate marks the original event ``superseded_by_id``
→ scheduler's eligibility filter blocks the post-phase fire.

The supersede-stops-post path is the v3-specific behaviour that v2
lacked (close_detection.py is gone — the chain is the new close
signal).

Real components: SQLite memory store, real consolidate pipeline, real
``FollowUpScheduler``, real observer wiring. Stubs: ``extract_fn``
returns scripted ``ExtractionResult``; ``embed_fn`` is deterministic;
the ``ProactiveScheduler`` is a ``FakeProactiveScheduler`` that records
dispatched events and persists a decision row per drain, so we exercise
the full notify → tick → reschedule loop without the delivery stack.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import Session as DbSession

from echovessel.core.types import EventTime, MessageRole, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.models import ConceptNode
from echovessel.proactive.core.base import EventType
from echovessel.proactive.execution.follow_up_scheduler import FollowUpScheduler
from echovessel.proactive.execution.observer import MemoryFollowUpObserver
from tests.proactive.fakes import FakeProactiveScheduler

PERSONA_ID = "p_e2e"
USER_ID = "u_e2e"

EXAM_TIME = datetime(2026, 5, 4, 10, 0)  # Mon 10:00 — interview
PRE_WINDOW = EXAM_TIME - timedelta(hours=24)  # Sun 10:00
POST_WINDOW = EXAM_TIME + timedelta(hours=24)  # Tue 10:00
USER_REPORTS_OUTCOME = datetime(2026, 5, 4, 18, 0)


def _seed(db: DbSession) -> None:
    db.add(Persona(id=PERSONA_ID, display_name="Mom"))
    db.add(User(id=USER_ID, display_name="Alan"))
    db.commit()


def _make_session(db: DbSession, *, sid: str) -> Session:
    sess = Session(
        id=sid,
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        channel_id="web",
        status=SessionStatus.CLOSING,
        message_count=3,
        total_tokens=300,
    )
    db.add(sess)
    db.commit()
    return sess


def _add_messages(db: DbSession, *, sid: str, contents: list[str]) -> None:
    for i, text in enumerate(contents):
        db.add(
            RecallMessage(
                session_id=sid,
                persona_id=PERSONA_ID,
                user_id=USER_ID,
                channel_id="web",
                role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                content=text,
                day=date.today(),
                token_count=len(text),
            )
        )
    db.commit()


def _deterministic_embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _make_extractor(events: list[ExtractedEvent]):
    async def _extract(_msgs):
        return ExtractionResult(events=list(events))

    return _extract


async def _empty_reflect(_nodes, _reason):
    return []


async def test_v3_happy_path_supersede_stops_post():
    """Full v3 dispatch loop · supersede prevents post-phase fire."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    def db_factory() -> DbSession:
        return DbSession(engine)

    with db_factory() as db:
        _seed(db)

    # ----- Phase 1 · user mentions exam → consolidate writes event ------
    fake_proactive_scheduler = FakeProactiveScheduler(db_factory=db_factory)
    follow_up_scheduler = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        # Pin "now" just inside the pre window so the timer fires immediately.
        now_fn=lambda: PRE_WINDOW + timedelta(seconds=0.05),
    )
    observer = MemoryFollowUpObserver(scheduler=follow_up_scheduler)

    initial_event = ExtractedEvent(
        description="user has an interview Monday 10am",
        emotional_impact=3,
        emotion_tags=["nervous"],
        relational_tags=["unresolved"],
        subject="user",
        event_time=EventTime(start=EXAM_TIME, end=EXAM_TIME),
        follow_up_at=EXAM_TIME,
        follow_up_hint="ask how the interview went",
        estimated_arc_days=14,
        advance_pre_hours=24,
        advance_post_hours=24,
    )

    with db_factory() as db:
        sess1 = _make_session(db, sid="s_intro")
        _add_messages(
            db,
            sid="s_intro",
            contents=[
                "下周一上午十点有面试 我有点紧张",
                "我会陪你的 准备得怎么样",
                "还行 但是怕表现不好",
            ],
        )

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess1,
            extract_fn=_make_extractor([initial_event]),
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
            observer=observer,
        )

        assert result.skipped is False
        assert len(result.events_created) == 1
        event_id = result.events_created[0].id
        assert event_id is not None

    # ----- Phase 2 · scheduler timer fires → notify dispatched ----------
    await asyncio.sleep(0.3)

    assert fake_proactive_scheduler.events, (
        "FollowUpScheduler should dispatch FOLLOW_UP_DUE at the pre window"
    )
    pre_event = fake_proactive_scheduler.events[-1]
    assert pre_event.event_type == EventType.FOLLOW_UP_DUE
    assert pre_event.payload["event_id"] == event_id
    assert pre_event.payload["phase"] == "pre"
    assert pre_event.payload["follow_up_hint"] == "ask how the interview went"

    await follow_up_scheduler.stop()

    # ----- Phase 3 · user reports outcome → supersede the original ------
    outcome_event = ExtractedEvent(
        description="user finished the interview, said it went ok",
        emotional_impact=2,
        emotion_tags=["relief"],
        relational_tags=[],
        subject="user",
        event_time=EventTime(start=USER_REPORTS_OUTCOME, end=USER_REPORTS_OUTCOME),
        superseded_event_ids=[event_id],
    )

    with db_factory() as db:
        sess2 = _make_session(db, sid="s_outcome")
        _add_messages(
            db,
            sid="s_outcome",
            contents=[
                "面试结束了 还行",
                "辛苦了 怎么样",
                "比想的顺利一点",
            ],
        )

        await consolidate_session(
            db=db,
            backend=backend,
            session=sess2,
            extract_fn=_make_extractor([outcome_event]),
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
            observer=observer,
        )

        # Phase B writes superseded_by_id back onto the original event.
        original = db.get(ConceptNode, event_id)
        assert original is not None
        assert original.superseded_by_id is not None, "supersede chain must mark the closed event"

    # ----- Phase 4 · post window arrives → scheduler does NOT fire ------
    fake_proactive_scheduler.events.clear()

    post_phase_scheduler = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_proactive_scheduler,
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        now_fn=lambda: POST_WINDOW + timedelta(seconds=0.05),
    )
    await post_phase_scheduler.start()
    await asyncio.sleep(0.3)

    assert fake_proactive_scheduler.events == []

    await post_phase_scheduler.stop()


@pytest.mark.parametrize("phase_under_test", ["pre", "on", "post"])
async def test_supersede_blocks_every_phase(phase_under_test: str):
    """Eligibility filter excludes superseded events for any phase window."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    def db_factory() -> DbSession:
        return DbSession(engine)

    with db_factory() as db:
        _seed(db)

    # Inline-create the superseded event pair so we can exercise each
    # phase window independently. (Phase 1 of the canonical happy-path
    # already covers consolidate→observer wiring; this parametric run
    # isolates the _eligible filter.)
    with db_factory() as db:
        original = ConceptNode(
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            type="event",
            description="exam Monday",
            subject="user",
            event_time_start=EXAM_TIME,
            event_time_end=EXAM_TIME,
            follow_up_at=EXAM_TIME,
            follow_up_hint="exam outcome",
            estimated_arc_days=14,
            advance_pre_hours=24,
            advance_post_hours=24,
            created_at=EXAM_TIME - timedelta(days=5),
        )
        db.add(original)
        db.commit()
        db.refresh(original)
        original_id = original.id

        replacement = ConceptNode(
            persona_id=PERSONA_ID,
            user_id=USER_ID,
            type="event",
            description="exam done",
            subject="user",
            event_time_start=USER_REPORTS_OUTCOME,
            event_time_end=USER_REPORTS_OUTCOME,
            created_at=USER_REPORTS_OUTCOME,
        )
        db.add(replacement)
        db.commit()
        db.refresh(replacement)

        original.superseded_by_id = replacement.id
        db.add(original)
        db.commit()

    phase_now = {
        "pre": PRE_WINDOW + timedelta(seconds=0.05),
        "on": EXAM_TIME + timedelta(seconds=0.05),
        "post": POST_WINDOW + timedelta(seconds=0.05),
    }[phase_under_test]

    fake_scheduler = FakeProactiveScheduler(db_factory=db_factory)
    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=fake_scheduler,
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        now_fn=lambda: phase_now,
    )
    await sched.start()
    await asyncio.sleep(0.3)

    assert fake_scheduler.events == []
    # _eligible also rejects on direct on_event_created.
    with db_factory() as db:
        sched.on_event_created(db.get(ConceptNode, original_id))
    await asyncio.sleep(0.1)
    assert fake_scheduler.events == []

    await sched.stop()

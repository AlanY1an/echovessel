"""Phase B captures follow_up_at + advance hours into ConceptNode (v0.7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from unittest.mock import AsyncMock

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


@dataclass
class _ConsolidateFixture:
    db: DbSession
    backend: SQLiteBackend
    session: Session
    embed_fn: object
    db_factory: object


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


@pytest.fixture
def consolidate_fixture():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    db = DbSession(engine)
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()

    sess = Session(
        id="s_test",
        persona_id="p_test",
        user_id="self",
        channel_id="test",
        status=SessionStatus.CLOSING,
        message_count=5,
        total_tokens=500,
    )
    db.add(sess)
    db.commit()

    for i, c in enumerate(["m1" * 20, "m2" * 20, "m3" * 20, "m4" * 20, "m5" * 20]):
        db.add(
            RecallMessage(
                session_id=sess.id,
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                content=c,
                day=date.today(),
                token_count=len(c),
            )
        )
    db.commit()

    def factory() -> DbSession:
        return DbSession(engine)

    try:
        yield _ConsolidateFixture(
            db=db, backend=backend, session=sess, embed_fn=_embed, db_factory=factory
        )
    finally:
        db.close()


def _wrap(events: list[ExtractedEvent]):
    return AsyncMock(return_value=ExtractionResult(events=events))


async def test_extracted_event_carries_follow_up_fields(consolidate_fixture):
    """LLM extraction output flows into concept_nodes."""

    extract_fn = _wrap(
        [
            ExtractedEvent(
                description="user has interview Monday 10am",
                subject="user",
                emotional_impact=4,
                emotion_tags=["anxiety"],
                relational_tags=["unresolved"],
                event_time=EventTime(
                    start=datetime(2026, 5, 4, 10, 0),
                    end=datetime(2026, 5, 4, 10, 0),
                ),
                superseded_event_ids=[],
                follow_up_at=datetime(2026, 5, 4, 10, 0),
                follow_up_hint="面试结果",
                estimated_arc_days=14,
                advance_pre_hours=24,
                advance_post_hours=24,
            )
        ]
    )

    result = await consolidate_session(
        db=consolidate_fixture.db,
        backend=consolidate_fixture.backend,
        session=consolidate_fixture.session,
        extract_fn=extract_fn,
        reflect_fn=AsyncMock(return_value=[]),
        embed_fn=consolidate_fixture.embed_fn,
        now=datetime(2026, 4, 29),
    )

    assert len(result.events_created) == 1
    event_id = result.events_created[0].id

    with consolidate_fixture.db_factory() as db:
        node = db.get(ConceptNode, event_id)
        assert node.follow_up_at == datetime(2026, 5, 4, 10, 0)
        assert node.follow_up_hint == "面试结果"
        assert node.estimated_arc_days == 14
        assert node.advance_pre_hours == 24
        assert node.advance_post_hours == 24
        assert node.proactive_suppressed_at is None


async def test_extracted_event_no_follow_up_when_null(consolidate_fixture):
    """Ordinary past event has all v0.7 fields NULL."""

    extract_fn = _wrap(
        [
            ExtractedEvent(
                description="user ate sandwich at noon",
                subject="user",
                emotional_impact=1,
                emotion_tags=[],
                relational_tags=[],
                event_time=None,
                superseded_event_ids=[],
                follow_up_at=None,
                follow_up_hint=None,
                estimated_arc_days=None,
                advance_pre_hours=None,
                advance_post_hours=None,
            )
        ]
    )

    result = await consolidate_session(
        db=consolidate_fixture.db,
        backend=consolidate_fixture.backend,
        session=consolidate_fixture.session,
        extract_fn=extract_fn,
        reflect_fn=AsyncMock(return_value=[]),
        embed_fn=consolidate_fixture.embed_fn,
        now=datetime(2026, 4, 29),
    )

    with consolidate_fixture.db_factory() as db:
        node = db.get(ConceptNode, result.events_created[0].id)
        assert node.follow_up_at is None
        assert node.follow_up_hint is None
        assert node.estimated_arc_days is None
        assert node.advance_pre_hours is None
        assert node.advance_post_hours is None


async def test_reminder_zero_advance_hours(consolidate_fixture):
    """'10分钟提醒我吃药' → advance_pre=0, advance_post=0."""

    extract_fn = _wrap(
        [
            ExtractedEvent(
                description="reminder: 提醒用户吃药",
                subject="persona",
                emotional_impact=0,
                emotion_tags=[],
                relational_tags=["commitment"],
                event_time=EventTime(
                    start=datetime(2026, 4, 29, 22, 40),
                    end=datetime(2026, 4, 29, 22, 40),
                ),
                superseded_event_ids=[],
                follow_up_at=datetime(2026, 4, 29, 22, 40),
                follow_up_hint="提醒吃药",
                estimated_arc_days=1,
                advance_pre_hours=0,
                advance_post_hours=0,
            )
        ]
    )

    result = await consolidate_session(
        db=consolidate_fixture.db,
        backend=consolidate_fixture.backend,
        session=consolidate_fixture.session,
        extract_fn=extract_fn,
        reflect_fn=AsyncMock(return_value=[]),
        embed_fn=consolidate_fixture.embed_fn,
        now=datetime(2026, 4, 29, 22, 30),
    )

    with consolidate_fixture.db_factory() as db:
        node = db.get(ConceptNode, result.events_created[0].id)
        assert node.advance_pre_hours == 0
        assert node.advance_post_hours == 0

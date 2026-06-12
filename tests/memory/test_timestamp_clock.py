"""Memory-layer timestamp convention — every row lands on naive local time.

The schema keeps a SQL server_default (SQLite CURRENT_TIMESTAMP = UTC)
only as a NOT NULL safety net for raw SQL; ORM writers must stamp local
``datetime.now()`` so recency scoring, the consolidation 24h windows,
and the slow-cycle event window all compare same-clock values.

TZ is pinned to a non-UTC zone (UTC+8) for these tests so a UTC
timestamp sneaking in fails on any host, including hosts that run UTC.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
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
    ExtractedThought,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.models import ConceptNode
from echovessel.memory.slow_cycle import _collect_recent_events


@pytest.fixture
def non_utc_tz():
    """Pin the process to UTC+8 so local time visibly differs from UTC."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="Luna"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _deterministic_embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def test_concept_node_default_created_at_is_local_now(non_utc_tz):
    """A ConceptNode written without an explicit created_at must land on
    the local clock (Python-side default), not the SQL server default."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        node = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="went hiking",
        )
        db.add(node)
        db.commit()
        db.refresh(node)

        assert node.created_at is not None
        age = abs((datetime.now() - node.created_at).total_seconds())
        assert age < 10, f"created_at is {age:.0f}s off local now — wrong clock"


async def test_consolidate_stamps_all_nodes_with_now():
    """Every node consolidate writes (events, session summary, reflection
    thoughts) carries the pipeline's ``now``, on the same clock as
    ``session.extracted_at``."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    fixed_now = datetime(2026, 3, 1, 12, 0, 0)

    async def _extract(_msgs):
        return ExtractionResult(
            events=[ExtractedEvent(description="dog passed away", emotional_impact=-9)],
            session_summary="a hard day",
        )

    async def _reflect(nodes, _reason):
        return [
            ExtractedThought(
                description="he is grieving",
                emotional_impact=-5,
                filling=[n.id for n in nodes],
            )
        ]

    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s1",
            persona_id="p",
            user_id="self",
            channel_id="t",
            status=SessionStatus.CLOSING,
            message_count=5,
            total_tokens=500,
        )
        db.add(sess)
        db.add(
            RecallMessage(
                session_id="s1",
                persona_id="p",
                user_id="self",
                channel_id="t",
                role=MessageRole.USER,
                content="my dog passed away today",
                day=date.today(),
                token_count=50,
            )
        )
        db.commit()

        result = await consolidate_session(
            db,
            backend,
            sess,
            extract_fn=_extract,
            reflect_fn=_reflect,
            embed_fn=_deterministic_embed,
            now=fixed_now,
        )

        assert result.events_created and result.thoughts_created
        nodes = list(db.exec(select(ConceptNode)))
        # event + session summary + reflection thought
        assert len(nodes) == 3
        for n in nodes:
            assert n.created_at == fixed_now, (
                f"node {n.id} ({n.type}) created_at={n.created_at} != pipeline now={fixed_now}"
            )
        assert result.session.extracted_at == fixed_now


def test_slow_cycle_window_matches_node_clock(non_utc_tz):
    """The slow-cycle event window (`created_at > since`, with ``since``
    derived from local ``datetime.now()``) must see a freshly written
    node that relied on the model's created_at default."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        node = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="started a new job",
        )
        db.add(node)
        db.commit()

        since = datetime.now() - timedelta(minutes=5)
        events = _collect_recent_events(db, persona_id="p", user_id="self", since=since)
        assert [e.description for e in events] == ["started a new job"]

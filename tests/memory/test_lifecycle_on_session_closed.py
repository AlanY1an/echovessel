"""Lifecycle hook `on_session_closed` fires after a session transitions
to CLOSED and commits.

Covers both paths in `consolidate_session`:
  - Trivial skip branch (§A)
  - Non-trivial path's final close (§F)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
    register_observer,
    unregister_observer,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import Session as SessionRow
from echovessel.memory.sessions import mark_session_closing


class _Spy:
    def __init__(self) -> None:
        self.new_sessions: list[tuple[str, str, str]] = []
        self.closed_sessions: list[tuple[str, str, str]] = []

    def on_new_session_started(
        self, session_id: str, persona_id: str, user_id: str
    ) -> None:
        self.new_sessions.append((session_id, persona_id, user_id))

    def on_session_closed(
        self, session_id: str, persona_id: str, user_id: str
    ) -> None:
        self.closed_sessions.append((session_id, persona_id, user_id))


@pytest.fixture
def spy():
    s = _Spy()
    register_observer(s)
    try:
        yield s
    finally:
        unregister_observer(s)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_close", display_name="Close"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _zero_embed(_text: str) -> list[float]:
    return [0.0] * 384


async def _noop_reflect(_nodes, _reason):
    return []


@pytest.mark.asyncio
async def test_trivial_skip_fires_session_closed(spy):
    """A session with 1 short message goes down the trivial-skip
    branch. The branch still transitions status → CLOSED, and the
    hook must fire."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 10, 0, 0)
        ingest_message(
            db,
            "p_close",
            "self",
            "web",
            MessageRole.USER,
            "hi",
            now=base,
        )
        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):
            return ExtractionResult()

        closed_before = len(spy.closed_sessions)
        await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )

        assert len(spy.closed_sessions) == closed_before + 1
        sid, pid, uid = spy.closed_sessions[-1]
        assert sid == session.id
        assert pid == "p_close"
        assert uid == "self"


@pytest.mark.asyncio
async def test_non_trivial_consolidate_fires_session_closed(spy):
    """A session with 3+ messages + extraction events takes the
    non-trivial path. The final `Mark session closed` step (§F) must
    fire `on_session_closed` too."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 10, 0, 0)
        for i in range(3):
            ingest_message(
                db,
                "p_close",
                "self",
                "web",
                MessageRole.USER,
                f"line {i}",
                now=base + timedelta(seconds=i),
            )
        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):
            return ExtractionResult(
                events=[
                ExtractedEvent(description="extracted", emotional_impact=2)
            ],
            )

        closed_before = len(spy.closed_sessions)
        await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )

        assert len(spy.closed_sessions) == closed_before + 1
        sid, pid, uid = spy.closed_sessions[-1]
        assert sid == session.id
        assert pid == "p_close"
        assert uid == "self"


@pytest.mark.asyncio
async def test_already_closed_session_does_not_fire(spy):
    """`consolidate_session` early-returns when status is already CLOSED.
    No hook should fire on the second attempt."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 10, 0, 0)
        ingest_message(
            db, "p_close", "self", "web", MessageRole.USER, "hi", now=base
        )
        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):
            return ExtractionResult()

        # First consolidate: trivial skip → fires once
        await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )
        first_count = len(spy.closed_sessions)
        assert first_count == 1

        # Second consolidate: already CLOSED → early return, no fire
        await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )
        assert len(spy.closed_sessions) == first_count

"""`source_turn_id` on ConceptNode — consolidate writes it correctly.

Review R2 rule: consolidate MUST remain per-session. `source_turn_id`
is a soft hint carried from the extraction prompt (or fallback from the
last user turn in the session) — it does NOT split extraction into
per-turn groups.
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


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _zero_embed(_text: str) -> list[float]:
    return [0.0] * 384


async def _noop_reflect(_nodes, _reason):
    return []


@pytest.mark.asyncio
async def test_source_turn_id_explicit_from_prompt():
    """If the extraction prompt emits `source_turn_id`, it is persisted
    verbatim to the ConceptNode."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)

        base_time = datetime(2026, 4, 16, 10, 0, 0)
        # Ingest 3 messages to avoid the trivial-skip branch
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "user line a",
            now=base_time,
            turn_id="turn-111",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "user line b",
            now=base_time + timedelta(seconds=5),
            turn_id="turn-222",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "user line c (third to skip trivial)",
            now=base_time + timedelta(seconds=10),
            turn_id="turn-222",
        )

        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base_time + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):  # noqa: F811
            return ExtractionResult(
                events=[
                ExtractedEvent(
                    description="event anchored in explicit turn",
                    emotional_impact=3,
                    source_turn_id="turn-explicit-from-prompt",
                )
            ],
            )

        result = await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )

        assert len(result.events_created) == 1
        assert (
            result.events_created[0].source_turn_id
            == "turn-explicit-from-prompt"
        )


@pytest.mark.asyncio
async def test_source_turn_id_falls_back_to_latest_user_turn():
    """If the extraction prompt omits source_turn_id, consolidate falls
    back to the last user-message turn_id in the session."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        base_time = datetime(2026, 4, 16, 10, 0, 0)
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "earlier user turn",
            now=base_time,
            turn_id="turn-001",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "later user turn",
            now=base_time + timedelta(seconds=30),
            turn_id="turn-002",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.PERSONA,
            "persona reply",
            now=base_time + timedelta(seconds=35),
            turn_id="turn-002",
        )

        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base_time + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):
            return ExtractionResult(
                events=[
                ExtractedEvent(
                    description="event without explicit source_turn_id",
                    emotional_impact=2,
                    # note: source_turn_id left unset → falls back
                )
            ],
            )

        result = await consolidate_session(
            db, backend, session, extract_fn, _noop_reflect, _zero_embed
        )

        assert len(result.events_created) == 1
        # Should be the latest user turn, not the persona's
        assert result.events_created[0].source_turn_id == "turn-002"


@pytest.mark.asyncio
async def test_per_session_extraction_not_per_turn():
    """Review R2 guarantee: a session with 2 user turns is still extracted
    with a single extract_fn call, not one per turn. Verified by counting
    calls on a spy."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    calls: list[int] = []

    async def spy_extract(messages):
        calls.append(len(messages))
        return ExtractionResult(events=[ExtractedEvent(description="grouped event", emotional_impact=1)])

    with DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 12, 0, 0)
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "burst a",
            now=base,
            turn_id="turn-A",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "burst b",
            now=base + timedelta(seconds=2),
            turn_id="turn-A",
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "different turn",
            now=base + timedelta(seconds=300),
            turn_id="turn-B",
        )

        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        await consolidate_session(
            db, backend, session, spy_extract, _noop_reflect, _zero_embed
        )

    # Review R2: ONE LLM call, not 2 (per-turn), not 3 (per-message)
    assert len(calls) == 1
    # And that single call saw all 3 messages at once
    assert calls[0] == 3

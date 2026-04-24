"""Observer exceptions from lifecycle hooks MUST NOT roll back memory
writes. The commit has already happened by the time the hook fires;
the dispatcher catches and logs the error and continues.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole
from echovessel.memory import (
    Persona,
    RecallMessage,
    User,
    create_all_tables,
    create_engine,
    register_observer,
    unregister_observer,
    update_episodic_state,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import Session as SessionRow
from echovessel.memory.sessions import mark_session_closing


class _Exploder:
    """Every lifecycle hook raises. Memory writes must still succeed."""

    def on_new_session_started(self, session_id, persona_id, user_id):
        raise RuntimeError(f"boom-new-{session_id}")

    def on_session_closed(self, session_id, persona_id, user_id):
        raise RuntimeError(f"boom-closed-{session_id}")

    def on_mood_updated(self, persona_id, user_id, new_mood_text):
        raise RuntimeError(f"boom-mood-{persona_id}")


@pytest.fixture
def exploder():
    e = _Exploder()
    register_observer(e)
    try:
        yield e
    finally:
        unregister_observer(e)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_boom", display_name="Boom"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _zero_embed(_text):
    return [0.0] * 384


async def _noop_reflect(_nodes, _reason):
    return []


def test_exploding_on_new_session_started_does_not_block_ingest(
    exploder, caplog
):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with caplog.at_level("WARNING"), DbSession(engine) as db:
        _seed(db)
        result = ingest_message(
            db,
            "p_boom",
            "self",
            "web",
            MessageRole.USER,
            "survives",
        )
        # Commit succeeded, message is persisted
        assert result.message.id is not None
        rows = list(db.exec(select(RecallMessage)))
        assert len(rows) == 1

    # Warning was logged
    assert any("on_new_session_started" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_exploding_on_session_closed_does_not_block_consolidate(
    exploder, caplog
):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with caplog.at_level("WARNING"), DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 10, 0, 0)
        ingest_message(
            db, "p_boom", "self", "web", MessageRole.USER, "hi", now=base
        )
        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        async def extract_fn(_messages):
            return ExtractionResult()

        # Consolidate runs through trivial-skip path. The close hook
        # will explode, but the session's status must still be CLOSED.
        await consolidate_session(
            db,
            backend,
            session,
            extract_fn,
            _noop_reflect,
            _zero_embed,
        )
        db.refresh(session)
        from echovessel.core.types import SessionStatus

        assert session.status == SessionStatus.CLOSED

    assert any("on_session_closed" in r.message for r in caplog.records)


def test_exploding_on_mood_updated_does_not_block_write(
    exploder, caplog
):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with caplog.at_level("WARNING"), DbSession(engine) as db:
        _seed(db)
        new_state = update_episodic_state(
            db,
            persona_id="p_boom",
            signal={
                "mood": "tired but steady",
                "energy": 4,
                "last_user_signal": None,
            },
        )
        # The episodic state was written successfully despite the
        # observer raising.
        assert new_state["mood"] == "tired but steady"
        persona = db.get(Persona, "p_boom")
        assert persona is not None
        assert persona.episodic_state["mood"] == "tired but steady"

    assert any("on_mood_updated" in r.message for r in caplog.records)

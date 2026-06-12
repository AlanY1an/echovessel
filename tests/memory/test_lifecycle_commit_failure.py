"""Lifecycle hook `on_new_session_started` fires only for sessions whose
creating transaction durably committed. A commit that raises must not
leave a queued event behind for a Session row that was never persisted.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
    register_observer,
    unregister_observer,
)
from echovessel.memory import sessions as sessions_mod
from echovessel.memory.ingest import ingest_message
from echovessel.memory.sessions import drain_and_fire_pending_lifecycle_events


class _Spy:
    def __init__(self) -> None:
        self.new_sessions: list[tuple[str, str, str]] = []
        self.closed_sessions: list[tuple[str, str, str]] = []

    def on_new_session_started(self, session_id: str, persona_id: str, user_id: str) -> None:
        self.new_sessions.append((session_id, persona_id, user_id))

    def on_session_closed(self, session_id: str, persona_id: str, user_id: str) -> None:
        self.closed_sessions.append((session_id, persona_id, user_id))

    def on_mood_updated(self, persona_id: str, user_id: str, new_mood_text: str) -> None:
        pass


@pytest.fixture
def spy():
    s = _Spy()
    register_observer(s)
    try:
        yield s
    finally:
        unregister_observer(s)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_life", display_name="Life"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def test_failed_commit_leaves_no_pending_event(spy, monkeypatch):
    """If `db.commit()` raises during ingest, the new-session event must
    not stay queued: the buffer holds nothing for that session, a drain
    right after fires nothing, and a later successful session open
    fires exactly one event for its own session."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)

        real_commit = db.commit

        def _failing_commit() -> None:
            raise OperationalError("COMMIT", None, Exception("database is locked"))

        monkeypatch.setattr(db, "commit", _failing_commit)
        with pytest.raises(OperationalError):
            ingest_message(db, "p_life", "self", "web", MessageRole.USER, "lost")
        monkeypatch.setattr(db, "commit", real_commit)
        db.rollback()

        # Nothing queued for the failed transaction's session, and a
        # drain right now fires nothing.
        assert sessions_mod._pending_new_sessions == []
        drain_and_fire_pending_lifecycle_events()
        assert spy.new_sessions == []

        # A fresh open on another channel (its own session lifecycle,
        # D6) fires exactly one event — the mechanism still works after
        # a failed commit.
        result = ingest_message(db, "p_life", "self", "discord:g1", MessageRole.USER, "real")

    assert len(spy.new_sessions) == 1
    assert spy.new_sessions[0][0] == result.session.id

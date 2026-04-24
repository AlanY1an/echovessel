"""Race-safety tests for ``get_or_create_open_session``.

Audit P1-5. The function previously did SELECT-then-INSERT with no
atomicity between them — two concurrent ingests for the same
(persona, user, channel) could both see no OPEN session and both
insert, landing the DB in a state with two OPEN sessions for the
same key.

The concurrent race is dormant under the current single-consumer
TurnDispatcher + SQLite's file-level write locking, but the
*invariant* — "at most one OPEN session per (persona, user, channel)
triple" — is not enforced by the schema. These tests pin that
invariant directly via a partial unique index, and verify the ORM
path (``get_or_create_open_session``) handles the constraint cleanly
when a concurrent insert races in.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import SessionStatus
from echovessel.memory import (
    Persona,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.sessions import get_or_create_open_session


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="p"))
    db.add(User(id="u", display_name="u"))
    db.commit()


def test_schema_rejects_two_open_sessions_for_same_triple():
    """Direct schema-level test: a partial unique index must prevent
    two OPEN sessions for the same (persona, user, channel) triple
    from coexisting. On main this test fails because no such index
    exists; after the fix, the second commit raises ``IntegrityError``.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    now = datetime(2026, 4, 18, 10, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        db.add(
            Session(
                id="s-first",
                persona_id="p",
                user_id="u",
                channel_id="web",
                status=SessionStatus.OPEN,
                started_at=now,
                last_message_at=now,
            )
        )
        db.commit()

    with DbSession(engine) as db:
        db.add(
            Session(
                id="s-second",
                persona_id="p",
                user_id="u",
                channel_id="web",
                status=SessionStatus.OPEN,
                started_at=now,
                last_message_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_two_closed_sessions_for_same_triple_are_allowed():
    """The partial index only covers rows with ``status='open'`` — the
    common case of many historical CLOSED sessions for one user must
    still be allowed (otherwise every consolidated session would need
    to be soft-deleted to free up the triple for the next turn).
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    now = datetime(2026, 4, 18, 10, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        for i in range(3):
            db.add(
                Session(
                    id=f"s-closed-{i}",
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    status=SessionStatus.CLOSED,
                    started_at=now,
                    last_message_at=now,
                )
            )
        db.commit()

    with DbSession(engine) as db:
        rows = list(
            db.exec(
                select(Session).where(Session.status == SessionStatus.CLOSED)
            )
        )
    assert len(rows) == 3


def test_get_or_create_returns_existing_open_without_creating_duplicate():
    """Happy path regression guard: if an OPEN session for the triple
    already exists, ``get_or_create_open_session`` must return it as-is
    — no new row, no attempted insert that would trip the unique index.

    Uses a fixed ``now`` window so the seeded session's
    ``last_message_at`` does not trip the idle-stale threshold as real
    wall-clock drifts past the hardcoded 2026-04-18 date.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)

    seeded_at = datetime(2026, 4, 18, 10, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        db.add(
            Session(
                id="s-existing",
                persona_id="p",
                user_id="u",
                channel_id="web",
                status=SessionStatus.OPEN,
                started_at=seeded_at,
                last_message_at=seeded_at,
            )
        )
        db.commit()

    with DbSession(engine) as db:
        sess = get_or_create_open_session(
            db, "p", "u", "web", now=seeded_at + timedelta(minutes=1)
        )
        db.commit()
        returned_id = sess.id

    assert returned_id == "s-existing"

    with DbSession(engine) as db:
        rows = list(
            db.exec(
                select(Session).where(
                    Session.status == SessionStatus.OPEN,
                    Session.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
    assert len(rows) == 1

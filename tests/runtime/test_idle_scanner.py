"""IdleScanner tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import SessionStatus
from echovessel.memory import (
    Persona,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.runtime.loops.idle_scanner import IdleScanner


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


async def test_idle_scanner_marks_stale_open_session_closing():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    base = datetime(2026, 4, 14, 12, 0, 0)
    stale_last = base - timedelta(minutes=31)

    with DbSession(engine) as db:
        db.add(
            Session(
                id="s_open",
                persona_id="p",
                user_id="self",
                channel_id="t",
                status=SessionStatus.OPEN,
                started_at=stale_last,
                last_message_at=stale_last,
            )
        )
        db.commit()

    def db_factory():
        return DbSession(engine)

    scanner = IdleScanner(
        db_factory=db_factory,
        interval_seconds=60.0,
        now_fn=lambda: base,
    )
    count = await scanner.tick_once()
    assert count == 1

    with DbSession(engine) as db:
        sess = db.get(Session, "s_open")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSING


async def test_idle_scanner_leaves_fresh_session_alone():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    base = datetime(2026, 4, 14, 12, 0, 0)
    with DbSession(engine) as db:
        db.add(
            Session(
                id="s_fresh",
                persona_id="p",
                user_id="self",
                channel_id="t",
                status=SessionStatus.OPEN,
                started_at=base - timedelta(minutes=1),
                last_message_at=base - timedelta(minutes=1),
            )
        )
        db.commit()

    def db_factory():
        return DbSession(engine)

    scanner = IdleScanner(db_factory=db_factory, now_fn=lambda: base)
    count = await scanner.tick_once()
    assert count == 0
    with DbSession(engine) as db:
        sess = db.get(Session, "s_fresh")
        assert sess and sess.status == SessionStatus.OPEN

"""`register_observer` / `unregister_observer` round-trip.

Registered observer sees lifecycle events; after unregister it does not.
"""

from __future__ import annotations

from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
    register_observer,
    unregister_observer,
    update_episodic_state,
)
from echovessel.memory.ingest import ingest_message


class _Counter:
    def __init__(self) -> None:
        self.fired = 0

    def on_new_session_started(self, session_id, persona_id, user_id):
        self.fired += 1

    def on_mood_updated(self, persona_id, user_id, new_mood_text):
        self.fired += 1


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_reg", display_name="Reg"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def test_register_then_unregister_stops_callbacks():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    counter = _Counter()
    register_observer(counter)

    try:
        with DbSession(engine) as db:
            _seed(db)
            ingest_message(
                db, "p_reg", "self", "web", MessageRole.USER, "hi"
            )
        assert counter.fired == 1

        unregister_observer(counter)

        with DbSession(engine) as db:
            ingest_message(
                db, "p_reg", "self", "imessage", MessageRole.USER, "hi"
            )
            update_episodic_state(
                db,
                persona_id="p_reg",
                signal={
                    "mood": "quiet",
                    "energy": 5,
                    "last_user_signal": None,
                },
            )
        # No additional fires after unregister
        assert counter.fired == 1
    finally:
        # Defensive cleanup in case the above raised before unregister
        unregister_observer(counter)


def test_unregister_unknown_observer_is_noop():
    """Unregistering something that was never registered must not raise."""
    stray = _Counter()
    unregister_observer(stray)  # should be silent


def test_multiple_observers_all_fired():
    """Two observers registered → both receive the callback."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    a = _Counter()
    b = _Counter()
    register_observer(a)
    register_observer(b)

    try:
        with DbSession(engine) as db:
            _seed(db)
            ingest_message(
                db, "p_reg", "self", "web", MessageRole.USER, "hello"
            )
        assert a.fired == 1
        assert b.fired == 1
    finally:
        unregister_observer(a)
        unregister_observer(b)

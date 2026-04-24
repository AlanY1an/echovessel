"""L6 · ``update_episodic_state`` unit tests (spec 2 · plan §5.3).

Covers the new ``memory/episodic.py`` entry point that replaces the
Phase 1 ``update_mood_block``. Contract:

- Writes to ``personas.episodic_state`` JSON column, not a core_blocks
  row (MOOD label was physically removed in the v0.4 migration).
- Rejects empty ``mood`` with ``ValueError``.
- Clamps ``energy`` to ``[0, 10]`` and coerces non-int-like input to 5.
- Collapses out-of-vocabulary ``last_user_signal`` to ``None``.
- Fires the module-level ``on_mood_updated`` lifecycle hook strictly
  after commit, same as the old mood-block path.
- Also invokes any ``observer=`` parameter passed in (per-call hook).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session as DbSession

from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
    register_observer,
    unregister_observer,
    update_episodic_state,
)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_e", display_name="E"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def on_mood_updated(
        self, persona_id: str, user_id: str, payload: str
    ) -> None:
        self.calls.append((persona_id, user_id, payload))


@pytest.fixture
def spy():
    s = _Spy()
    register_observer(s)
    try:
        yield s
    finally:
        unregister_observer(s)


def test_update_writes_mood_energy_and_signal(spy):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        state = update_episodic_state(
            db,
            persona_id="p_e",
            signal={
                "mood": "warm-curious",
                "energy": 7,
                "last_user_signal": "warm",
            },
            now=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        )
        assert state["mood"] == "warm-curious"
        assert state["energy"] == 7
        assert state["last_user_signal"] == "warm"
        assert state["updated_at"].startswith("2026-04-23T12:00:00")

        persona = db.get(Persona, "p_e")
        assert persona is not None
        assert persona.episodic_state == state

    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "p_e"
    assert spy.calls[0][1] == "self"
    assert "warm-curious" in spy.calls[0][2]


def test_empty_mood_raises():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(ValueError, match="non-empty"):
            update_episodic_state(
                db, persona_id="p_e", signal={"mood": "   "}
            )


def test_energy_clamped_to_0_10():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        above = update_episodic_state(
            db, persona_id="p_e", signal={"mood": "x", "energy": 99}
        )
        assert above["energy"] == 10
        below = update_episodic_state(
            db, persona_id="p_e", signal={"mood": "x", "energy": -5}
        )
        assert below["energy"] == 0


def test_bad_energy_defaults_to_five():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        out = update_episodic_state(
            db, persona_id="p_e", signal={"mood": "x", "energy": "nonsense"}
        )
        assert out["energy"] == 5


def test_unknown_last_user_signal_is_null():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        out = update_episodic_state(
            db,
            persona_id="p_e",
            signal={
                "mood": "x",
                "energy": 5,
                "last_user_signal": "ecstatic",
            },
        )
        assert out["last_user_signal"] is None


def test_unknown_persona_raises_lookup():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        with pytest.raises(LookupError):
            update_episodic_state(
                db,
                persona_id="missing",
                signal={"mood": "x", "energy": 5},
            )


def test_observer_param_also_fires(spy):
    """Per-call ``observer=`` fires in addition to registry spy."""

    class _CallObserver:
        calls: list[str] = []

        def on_mood_updated(
            self, persona_id: str, user_id: str, payload: str
        ) -> None:
            _CallObserver.calls.append(payload)

    obs = _CallObserver()
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        update_episodic_state(
            db,
            persona_id="p_e",
            signal={"mood": "tender", "energy": 4},
            observer=obs,
        )
    assert len(_CallObserver.calls) == 1
    assert "tender" in _CallObserver.calls[0]
    # Registry spy also fired.
    assert len(spy.calls) == 1

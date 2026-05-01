"""Smoke test that follow-up wiring composes cleanly."""

from __future__ import annotations

from unittest.mock import MagicMock

from echovessel.runtime.wiring.follow_up import (
    make_follow_up_scheduler,
    register_follow_up,
    unregister_follow_up,
)


def test_make_follow_up_scheduler_builds_pair() -> None:
    db_factory = MagicMock()
    proactive_scheduler = MagicMock()

    sched, obs = make_follow_up_scheduler(
        db_factory=db_factory,
        proactive_scheduler=proactive_scheduler,
        persona_id="p",
        user_id="u",
    )

    assert sched is not None
    assert obs is not None
    assert obs._scheduler is sched
    assert sched.persona_id == "p"
    assert sched.user_id == "u"
    assert sched.proactive_scheduler is proactive_scheduler


def test_register_unregister_uses_module_observers() -> None:
    """register/unregister go through memory.observers module-level registry."""
    from echovessel.memory.observers import _observers

    obs = MagicMock()
    initial_count = len(_observers)

    register_follow_up(obs)
    assert obs in _observers

    unregister_follow_up(obs)
    assert obs not in _observers
    assert len(_observers) == initial_count

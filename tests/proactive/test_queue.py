"""Tests for ProactiveEventQueue — bounded queue with critical overflow."""

from __future__ import annotations

from datetime import datetime, timedelta

from echovessel.proactive.core.base import EventType, ProactiveEvent
from echovessel.proactive.execution.queue import DEFAULT_MAX_EVENTS, ProactiveEventQueue


def _ev(i: int, *, critical: bool = False) -> ProactiveEvent:
    return ProactiveEvent(
        event_type=EventType.TICK,
        persona_id="p",
        user_id="u",
        created_at=datetime(2026, 4, 15, 12, 0, 0) + timedelta(seconds=i),
        payload={"i": i},
        critical=critical,
    )


def test_default_max_events_is_64():
    assert DEFAULT_MAX_EVENTS == 64


def test_push_below_cap():
    q = ProactiveEventQueue(max_events=5)
    for i in range(3):
        assert q.push(_ev(i))
    assert len(q) == 3
    drained = q.drain()
    assert [e.payload["i"] for e in drained] == [0, 1, 2]


def test_push_at_cap_drops_oldest_non_critical():
    q = ProactiveEventQueue(max_events=3)
    q.push(_ev(0))
    q.push(_ev(1))
    q.push(_ev(2))
    assert len(q) == 3
    assert q.overflow_count == 0

    # Adding a 4th event should drop event 0
    assert q.push(_ev(3))
    assert len(q) == 3
    assert q.overflow_count == 1
    drained = q.drain()
    assert [e.payload["i"] for e in drained] == [1, 2, 3]


def test_critical_events_survive_overflow():
    q = ProactiveEventQueue(max_events=3)
    q.push(_ev(0, critical=True))
    q.push(_ev(1))
    q.push(_ev(2))

    # Push a 4th: must drop event 1 (oldest non-critical), keep event 0 (critical)
    q.push(_ev(3))
    ids = [e.payload["i"] for e in q.peek()]
    assert 0 in ids  # critical preserved
    assert 1 not in ids  # oldest non-critical evicted
    assert 2 in ids
    assert 3 in ids


def test_all_critical_plus_non_critical_drops_new_non_critical():
    q = ProactiveEventQueue(max_events=3)
    q.push(_ev(0, critical=True))
    q.push(_ev(1, critical=True))
    q.push(_ev(2, critical=True))

    # Queue is full of criticals; incoming non-critical should be dropped.
    accepted = q.push(_ev(3, critical=False))
    assert accepted is False
    assert q.overflow_count == 1
    ids = [e.payload["i"] for e in q.peek()]
    assert set(ids) == {0, 1, 2}


def test_all_critical_plus_new_critical_drops_oldest_critical():
    q = ProactiveEventQueue(max_events=2)
    q.push(_ev(0, critical=True))
    q.push(_ev(1, critical=True))

    # Queue is full of criticals; a new critical drops the oldest critical.
    accepted = q.push(_ev(2, critical=True))
    assert accepted is True
    ids = [e.payload["i"] for e in q.peek()]
    assert ids == [1, 2]


def test_drain_clears():
    q = ProactiveEventQueue(max_events=5)
    q.push(_ev(0))
    q.push(_ev(1))
    assert q.drain()
    assert len(q) == 0


def test_max_events_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        ProactiveEventQueue(max_events=0)

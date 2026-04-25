"""Smoke tests for base.py dataclasses and enums."""

from __future__ import annotations

from datetime import datetime

from echovessel.proactive.core.base import (
    ActionType,
    EventType,
    ProactiveDecision,
    ProactiveEvent,
    ProactiveScheduler,
    SkipReason,
    TriggerReason,
)


def test_enum_values_stable():
    # These strings are persisted into JSONL audit files — changing any
    # of them is a breaking schema change.
    assert ActionType.SEND.value == "send"
    assert ActionType.SKIP.value == "skip"
    assert SkipReason.QUIET_HOURS.value == "quiet_hours"
    assert SkipReason.RATE_LIMITED.value == "rate_limited"
    assert SkipReason.LOW_PRESENCE_MODE.value == "low_presence_mode"
    assert TriggerReason.HIGH_EMOTIONAL_EVENT.value == "high_emotional_event"
    assert TriggerReason.LONG_SILENCE.value == "long_silence"
    assert EventType.TICK.value == "time.tick"
    assert EventType.EVENT_EXTRACTED.value == "memory.event_extracted"


def test_proactive_event_is_frozen():
    ev = ProactiveEvent(
        event_type=EventType.TICK,
        persona_id="p",
        user_id="u",
        created_at=datetime(2026, 4, 15, 12, 0, 0),
    )
    assert ev.event_type == EventType.TICK
    assert ev.critical is False
    try:
        ev.critical = True  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ProactiveEvent should be frozen")


def test_proactive_decision_update_outcome_partial():
    d = ProactiveDecision(
        decision_id="abc",
        persona_id="p",
        user_id="u",
        timestamp=datetime(2026, 4, 15, 12, 0),
        trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
        action=ActionType.SEND.value,
    )
    d.update_outcome(send_ok=True, ingest_message_id=42)
    assert d.send_ok is True
    assert d.ingest_message_id == 42
    # partial update: unrelated fields preserved
    assert d.voice_used is False
    d.update_outcome(voice_used=True, voice_error="foo")
    assert d.voice_used is True
    assert d.voice_error == "foo"
    assert d.send_ok is True  # still set


def test_proactive_scheduler_is_runtime_checkable():
    # The Protocol is marked @runtime_checkable so runtime can assert
    # subclasses. The concrete DefaultScheduler lives in scheduler.py
    # and must satisfy the Protocol.
    class FakeScheduler:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        def notify(self, event: ProactiveEvent) -> None: ...

    assert isinstance(FakeScheduler(), ProactiveScheduler)

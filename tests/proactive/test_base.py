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
    # These strings are persisted into JSONL / SQLite audit rows — changing
    # any of them is a breaking schema change. v3 trimmed the enum surface
    # but kept the legacy TriggerReason values so historical audit rows
    # still round-trip.
    assert ActionType.SEND.value == "send"
    assert ActionType.SKIP.value == "skip"
    assert SkipReason.QUIET_HOURS.value == "quiet_hours"
    assert SkipReason.RATE_LIMITED.value == "rate_limited"
    assert SkipReason.LOW_PRESENCE_MODE.value == "low_presence_mode"
    assert TriggerReason.LONG_SILENCE.value == "long_silence"
    # v3 fireable event types: FOLLOW_UP_DUE is the canonical lane;
    # EVENT_EXTRACTED + SESSION_CLOSED + TURN_COMPLETED are kept for
    # legacy producers / tests.
    assert EventType.FOLLOW_UP_DUE.value == "follow_up_due"
    assert EventType.THREAD_DUE.value == "thread_due"
    assert EventType.EVENT_EXTRACTED.value == "memory.event_extracted"


def test_proactive_event_is_frozen():
    ev = ProactiveEvent(
        event_type=EventType.THREAD_DUE,
        persona_id="p",
        user_id="u",
        created_at=datetime(2026, 4, 15, 12, 0, 0),
    )
    assert ev.event_type == EventType.THREAD_DUE
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
        trigger=TriggerReason.FOLLOW_UP.value,
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
        async def tick_once(self) -> None: ...

    assert isinstance(FakeScheduler(), ProactiveScheduler)

    # tick_once is part of the Protocol — the FollowUpScheduler awaits
    # it after notify so the decision row exists before rescheduling.
    class NoTickScheduler:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        def notify(self, event: ProactiveEvent) -> None: ...

    assert not isinstance(NoTickScheduler(), ProactiveScheduler)

"""PolicyEngine tests — three mandatory policies + priority order.

Every test constructs a fresh config + fake audit + fake memory so edge
cases can be dialed in independently. Uses the shared fakes in
``tests/proactive/fakes.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from echovessel.proactive.core.base import (
    ActionType,
    EventType,
    ProactiveEvent,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.engines.policy import PolicyEngine, _in_quiet_hours
from tests.proactive.fakes import (
    FakeAuditSink,
    FakeMessage,
    InMemoryMemoryApi,
)


def _make_engine(
    *,
    config: ProactiveConfig | None = None,
    audit: FakeAuditSink | None = None,
    memory: InMemoryMemoryApi | None = None,
) -> tuple[PolicyEngine, FakeAuditSink, InMemoryMemoryApi]:
    audit = audit or FakeAuditSink()
    memory = memory or InMemoryMemoryApi()
    cfg = config or ProactiveConfig(persona_id="p", user_id="u")
    engine = PolicyEngine(config=cfg, audit=audit, memory=memory)
    return engine, audit, memory


def _tick_events(now: datetime) -> list[ProactiveEvent]:
    return [
        ProactiveEvent(
            event_type=EventType.TICK,
            persona_id="p",
            user_id="u",
            created_at=now,
            payload={},
        )
    ]


# ---------------------------------------------------------------------------
# 1. Quiet hours (spec §3.1)
# ---------------------------------------------------------------------------


def test_quiet_hours_wraps_midnight():
    # Default 23-07 — midnight should be inside quiet hours
    assert _in_quiet_hours(datetime(2026, 4, 15, 2, 30), 23, 7) is True
    assert _in_quiet_hours(datetime(2026, 4, 15, 23, 30), 23, 7) is True
    # 08:00 should be outside
    assert _in_quiet_hours(datetime(2026, 4, 15, 8, 0), 23, 7) is False


def test_quiet_hours_same_day_range():
    # 12-14 = noon-2pm window
    assert _in_quiet_hours(datetime(2026, 4, 15, 13, 0), 12, 14) is True
    assert _in_quiet_hours(datetime(2026, 4, 15, 14, 0), 12, 14) is False
    assert _in_quiet_hours(datetime(2026, 4, 15, 11, 59), 12, 14) is False


def test_quiet_hours_gate_skips():
    engine, audit, _ = _make_engine()
    now = datetime(2026, 4, 15, 2, 0)  # 2am
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.QUIET_HOURS.value
    assert decision.trigger == TriggerReason.QUIET_HOURS_GATE.value


# ---------------------------------------------------------------------------
# 2. Rate limit (spec §3.2)
# ---------------------------------------------------------------------------


def test_rate_limit_gate_skips_when_cap_reached():
    audit = FakeAuditSink()
    audit.sends_count_24h = 3  # = cap
    engine, _, _ = _make_engine(audit=audit)
    now = datetime(2026, 4, 15, 12, 0)  # noon, outside quiet hours
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value


def test_rate_limit_does_not_fire_below_cap():
    audit = FakeAuditSink()
    audit.sends_count_24h = 2
    engine, _, _ = _make_engine(audit=audit)
    now = datetime(2026, 4, 15, 12, 0)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    # No trigger fires → no_trigger_match skip, but NOT rate_limited
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.NO_TRIGGER_MATCH.value


def test_rate_limit_read_error_is_safe_skip():
    audit = FakeAuditSink()

    def _boom(*, now):
        raise OSError("audit read broken")

    audit.count_sends_in_last_24h = _boom  # type: ignore[assignment]
    engine, _, _ = _make_engine(audit=audit)
    now = datetime(2026, 4, 15, 12, 0)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.RATE_LIMIT_READ_ERROR.value


# ---------------------------------------------------------------------------
# 3. Cold user (spec §3.3)
# ---------------------------------------------------------------------------


def _make_sent_decision(sent_at: datetime):
    from echovessel.proactive.core.base import ProactiveDecision

    return ProactiveDecision(
        decision_id=f"d_{sent_at.timestamp()}",
        persona_id="p",
        user_id="u",
        timestamp=sent_at,
        trigger=TriggerReason.LONG_SILENCE.value,
        action=ActionType.SEND.value,
    )


def test_cold_user_fires_after_two_unanswered_sends():
    audit = FakeAuditSink()
    memory = InMemoryMemoryApi()
    now = datetime(2026, 4, 15, 12, 0)

    # Two sends in the past 12h, no user replies
    audit.cold_user_sends = [
        _make_sent_decision(now - timedelta(hours=10)),
        _make_sent_decision(now - timedelta(hours=12)),
    ]

    engine, _, _ = _make_engine(audit=audit, memory=memory)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.LOW_PRESENCE_MODE.value


def test_cold_user_clears_when_user_replied():
    audit = FakeAuditSink()
    memory = InMemoryMemoryApi()
    now = datetime(2026, 4, 15, 12, 0)

    audit.cold_user_sends = [
        _make_sent_decision(now - timedelta(hours=10)),
        _make_sent_decision(now - timedelta(hours=12)),
    ]
    # User replied within the response window after the 10h-ago send
    memory.recent_messages = [
        FakeMessage(
            content="hi back",
            role="user",
            created_at=now - timedelta(hours=9),
        )
    ]

    engine, _, _ = _make_engine(audit=audit, memory=memory)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    # No cold user → falls through to no_trigger_match
    assert decision.skip_reason != SkipReason.LOW_PRESENCE_MODE.value


def test_cold_user_needs_minimum_sends():
    audit = FakeAuditSink()
    now = datetime(2026, 4, 15, 12, 0)
    audit.cold_user_sends = [_make_sent_decision(now - timedelta(hours=6))]
    engine, _, _ = _make_engine(audit=audit)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    # Only one send exists; threshold is 2 → cold_user doesn't fire
    assert decision.skip_reason != SkipReason.LOW_PRESENCE_MODE.value


# ---------------------------------------------------------------------------
# 4. Relationship-state triggers (spec §3.4)
# ---------------------------------------------------------------------------


def test_high_emotional_event_trigger_fires():
    engine, _, _ = _make_engine()
    now = datetime(2026, 4, 15, 12, 0)
    events = [
        ProactiveEvent(
            event_type=EventType.EVENT_EXTRACTED,
            persona_id="p",
            user_id="u",
            created_at=now,
            payload={
                "event_id": 7,
                "emotional_impact": -9,
                "emotion_tags": ["grief"],
            },
            critical=True,
        )
    ]
    decision = engine.evaluate(events, persona_id="p", user_id="u", now=now)
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.HIGH_EMOTIONAL_EVENT.value
    assert decision.trigger_payload == {
        "trigger_event_id": 7,
        "emotional_impact": -9,
        "emotion_tags": ["grief"],
    }


def test_high_emotional_event_ignored_below_shock():
    engine, _, _ = _make_engine()
    now = datetime(2026, 4, 15, 12, 0)
    events = [
        ProactiveEvent(
            event_type=EventType.EVENT_EXTRACTED,
            persona_id="p",
            user_id="u",
            created_at=now,
            payload={"event_id": 7, "emotional_impact": -7},
        )
    ]
    decision = engine.evaluate(events, persona_id="p", user_id="u", now=now)
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.NO_TRIGGER_MATCH.value


def test_long_silence_trigger_fires():
    memory = InMemoryMemoryApi()
    now = datetime(2026, 4, 15, 12, 0)
    memory.recent_messages = [
        FakeMessage(
            content="hello long ago",
            role="user",
            created_at=now - timedelta(hours=72),
        )
    ]
    engine, _, _ = _make_engine(memory=memory)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.LONG_SILENCE.value
    assert decision.trigger_payload
    assert decision.trigger_payload["silent_hours"] >= 48


def test_long_silence_does_not_fire_when_recent_user_msg():
    memory = InMemoryMemoryApi()
    now = datetime(2026, 4, 15, 12, 0)
    memory.recent_messages = [
        FakeMessage(
            content="just now",
            role="user",
            created_at=now - timedelta(hours=2),
        )
    ]
    engine, _, _ = _make_engine(memory=memory)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.NO_TRIGGER_MATCH.value


# ---------------------------------------------------------------------------
# 5. Priority ordering (spec §3.5)
# ---------------------------------------------------------------------------


def test_priority_quiet_hours_beats_everything_else():
    audit = FakeAuditSink()
    audit.sends_count_24h = 99  # would normally rate-limit
    audit.cold_user_sends = [
        _make_sent_decision(datetime(2026, 4, 15, 1, 0)),
        _make_sent_decision(datetime(2026, 4, 15, 0, 30)),
    ]
    engine, _, _ = _make_engine(audit=audit)
    now = datetime(2026, 4, 15, 2, 0)  # quiet hours
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    # Quiet hours wins even though cold_user and rate_limit would both fire
    assert decision.skip_reason == SkipReason.QUIET_HOURS.value


def test_priority_cold_user_beats_rate_limit():
    audit = FakeAuditSink()
    audit.sends_count_24h = 99  # rate_limit would fire
    now = datetime(2026, 4, 15, 12, 0)
    audit.cold_user_sends = [
        _make_sent_decision(now - timedelta(hours=8)),
        _make_sent_decision(now - timedelta(hours=10)),
    ]
    engine, _, _ = _make_engine(audit=audit)
    decision = engine.evaluate(
        _tick_events(now), persona_id="p", user_id="u", now=now
    )
    # Cold user wins
    assert decision.skip_reason == SkipReason.LOW_PRESENCE_MODE.value


def test_priority_rate_limit_beats_trigger_match():
    audit = FakeAuditSink()
    audit.sends_count_24h = 99
    memory = InMemoryMemoryApi()
    now = datetime(2026, 4, 15, 12, 0)
    # High emotional event would normally match
    events = [
        ProactiveEvent(
            event_type=EventType.EVENT_EXTRACTED,
            persona_id="p",
            user_id="u",
            created_at=now,
            payload={"emotional_impact": -9, "event_id": 1},
            critical=True,
        )
    ]
    engine, _, _ = _make_engine(audit=audit, memory=memory)
    decision = engine.evaluate(events, persona_id="p", user_id="u", now=now)
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value

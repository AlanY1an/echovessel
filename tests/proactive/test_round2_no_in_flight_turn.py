"""Round2 · review M5 · no_in_flight_turn gate.

Tests the v0.2 fourth policy gate: if runtime is currently processing
an in-flight turn, proactive skips with ``skip_reason='in_flight_turn'``
regardless of what the triggers say. The predicate is injected as a
callable on PolicyEngine; RT-round3 will wire the real closure later.
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
from echovessel.proactive.engines.policy import PolicyEngine
from tests.proactive.fakes import FakeAuditSink, FakeMessage, InMemoryMemoryApi


def _engine(
    *,
    is_turn_in_flight=None,
    memory: InMemoryMemoryApi | None = None,
    audit: FakeAuditSink | None = None,
) -> PolicyEngine:
    return PolicyEngine(
        config=ProactiveConfig(persona_id="p", user_id="u"),
        audit=audit or FakeAuditSink(),
        memory=memory or InMemoryMemoryApi(),
        is_turn_in_flight=is_turn_in_flight,
    )


def _long_silence_events(now: datetime) -> tuple[list[ProactiveEvent], InMemoryMemoryApi]:
    """Set up a state where long_silence would fire if no gate blocks it:
    one tick event + a 72h-old user message."""
    tick = ProactiveEvent(
        event_type=EventType.TICK,
        persona_id="p",
        user_id="u",
        created_at=now,
    )
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="hi from long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            ),
        ],
    )
    return [tick], memory


def test_policy_no_in_flight_turn_blocks():
    """Predicate returns True → skip with in_flight_turn reason, even
    though long_silence would otherwise fire."""
    now = datetime(2026, 4, 15, 12, 0)
    events, memory = _long_silence_events(now)
    engine = _engine(memory=memory, is_turn_in_flight=lambda: True)
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.IN_FLIGHT_TURN.value
    assert decision.trigger == TriggerReason.IN_FLIGHT_TURN_GATE.value
    # Ensure we didn't regress to another skip reason
    assert "in_flight" in (decision.skip_reason or "")


def test_policy_no_in_flight_turn_allows():
    """Predicate returns False → policy proceeds and (in this fixture)
    long_silence fires."""
    now = datetime(2026, 4, 15, 12, 0)
    events, memory = _long_silence_events(now)
    engine = _engine(memory=memory, is_turn_in_flight=lambda: False)
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.LONG_SILENCE.value


def test_policy_no_predicate_is_permissive():
    """When no is_turn_in_flight predicate is injected (factory default),
    the gate is a no-op — matches spec §3.5a's 'no channel readable →
    no in-flight turn' rule and keeps round1 construction paths working."""
    now = datetime(2026, 4, 15, 12, 0)
    events, memory = _long_silence_events(now)
    engine = _engine(memory=memory, is_turn_in_flight=None)
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.LONG_SILENCE.value


def test_policy_predicate_exception_treated_as_in_flight():
    """If the injected predicate raises, we defensively treat the state
    as 'in flight' and skip — never let a buggy predicate cause an
    unexpected send."""
    now = datetime(2026, 4, 15, 12, 0)
    events, memory = _long_silence_events(now)

    def _boom() -> bool:
        raise RuntimeError("predicate crashed")

    engine = _engine(memory=memory, is_turn_in_flight=_boom)
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.IN_FLIGHT_TURN.value


def test_policy_gate_order_quiet_hours_beats_in_flight():
    """quiet_hours is gate 1, in_flight_turn is gate 4. Quiet hours must
    win when both are active — the decision table in spec §3.5 is
    fixed."""
    now = datetime(2026, 4, 15, 2, 0)  # quiet hours
    events, memory = _long_silence_events(now)
    engine = _engine(memory=memory, is_turn_in_flight=lambda: True)
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.skip_reason == SkipReason.QUIET_HOURS.value


def test_policy_gate_order_rate_limit_beats_in_flight():
    """rate_limit is gate 3, in_flight_turn is gate 4. Rate-limit wins
    when both would trip — ensures the order matches spec §3.5."""
    now = datetime(2026, 4, 15, 12, 0)
    audit = FakeAuditSink()
    audit.sends_count_24h = 99
    events, memory = _long_silence_events(now)
    engine = _engine(
        memory=memory,
        audit=audit,
        is_turn_in_flight=lambda: True,
    )
    decision = engine.evaluate(
        events, persona_id="p", user_id="u", now=now
    )
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value

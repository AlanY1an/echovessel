"""Round2 · review M5 · PROOF that the 30 min / 10 s细粒度节流 are gone.

Per tracker §2.3 #3:
    > 构造两个间隔 1 秒的 fire 场景, policy 应该按 rate_limit 粗粒度判断,
    > **不会** 被旧的细粒度 min_interval 拒绝(除非 rate_limit 本身拒绝)

This test is the regression barrier: if anyone re-introduces a
``min_interval`` or ``window_seconds`` throttle in the future, a 1-second
burst test will fail because the second fire will be blocked, and this
test will tell us loud and clear.

We also add a static-grep assertion that the forbidden keywords do not
appear anywhere in the proactive source tree.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from echovessel.proactive.core.base import (
    ActionType,
    EventType,
    ProactiveEvent,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.engines.policy import PolicyEngine
from tests.proactive.fakes import FakeAuditSink, FakeMessage, InMemoryMemoryApi

PROACTIVE_SRC = Path(__file__).resolve().parents[2] / "src" / "echovessel" / "proactive"


def test_policy_removed_min_interval_allows_rapid_bursts():
    """Two evaluate calls 1 second apart, both with high_emotional_event,
    both below the max_per_24h rate cap. The old 30 min min_interval
    would have blocked the second one. v0.2 policy does not — it uses
    ONLY the 24h rolling rate_limit + the 4 other gates.

    Test shape: two back-to-back evaluates at t=12:00:00 and t=12:00:01.
    Both should produce action='send' (no intermediate audit state is
    mutated between them — the audit sink reports 0 sends in 24h for
    both calls via the fake).
    """
    t0 = datetime(2026, 4, 15, 12, 0, 0)
    t1 = t0 + timedelta(seconds=1)
    memory = InMemoryMemoryApi()
    audit = FakeAuditSink()
    audit.sends_count_24h = 0  # under cap throughout

    cfg = ProactiveConfig(persona_id="p", user_id="u", max_per_24h=3)
    engine = PolicyEngine(
        config=cfg,
        audit=audit,
        memory=memory,
        is_turn_in_flight=lambda: False,
    )

    def _fire(now: datetime):
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
        return engine.evaluate(events, persona_id="p", user_id="u", now=now)

    first = _fire(t0)
    second = _fire(t1)

    assert first.action == ActionType.SEND.value
    assert first.trigger == TriggerReason.HIGH_EMOTIONAL_EVENT.value
    # 1 second later — the PROOF. Old 30 min throttle would have blocked
    # this; v0.2 policy must let it through because rate_limit is the
    # only throttling layer that survived review M5.
    assert second.action == ActionType.SEND.value
    assert second.trigger == TriggerReason.HIGH_EMOTIONAL_EVENT.value


def test_policy_rate_limit_still_bites():
    """Complementary: rate_limit IS still enforced. If the audit says 3
    sends in 24h (== max), the second fire is blocked. This confirms
    the surviving rate_limit gate is still working."""
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="not cold, not silent",
                role="user",
                created_at=now - timedelta(minutes=5),
            )
        ],
    )
    audit = FakeAuditSink()
    audit.sends_count_24h = 3  # at cap

    engine = PolicyEngine(
        config=ProactiveConfig(persona_id="p", user_id="u", max_per_24h=3),
        audit=audit,
        memory=memory,
        is_turn_in_flight=lambda: False,
    )
    decision = engine.evaluate(
        [
            ProactiveEvent(
                event_type=EventType.EVENT_EXTRACTED,
                persona_id="p",
                user_id="u",
                created_at=now,
                payload={"emotional_impact": -9, "event_id": 1},
                critical=True,
            )
        ],
        persona_id="p",
        user_id="u",
        now=now,
    )
    assert decision.action == ActionType.SKIP.value
    # Proof that the survivor is rate_limit, not any regression.
    assert decision.skip_reason == "rate_limited"


def test_source_has_no_min_interval_or_window_keywords():
    """Static grep: the v0.2 source tree must not contain any of the
    round1/handoff names that could indicate a smuggled re-introduction
    of the throttles. This is a belt-and-braces check on top of the
    dynamic test above."""
    forbidden_patterns = (
        re.compile(r"\bmin_interval\b"),
        re.compile(r"\bwindow_seconds\b"),
        re.compile(r"\bMIN_INTERVAL\b"),
    )
    offending: list[tuple[Path, int, str]] = []
    for py_file in PROACTIVE_SRC.rglob("*.py"):
        lines = py_file.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            for pattern in forbidden_patterns:
                if pattern.search(line):
                    offending.append((py_file, lineno, line.strip()))
    assert not offending, (
        "review M5 violation: min_interval / window_seconds reappeared:\n"
        + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offending)
    )

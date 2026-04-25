"""Policy engine (spec §3).

Priority order (spec §3.5 decision table · v0.2):

    1. quiet_hours_gate      — time-of-day check, fastest, absolute veto
    2. cold_user_gate        — low-presence mode, anti-clinginess
    3. rate_limit_gate       — 24h rolling window
    4. no_in_flight_turn     — v0.2 · review M5 · don't interrupt a live turn
    5. trigger match         — relationship-state triggers (§3.4)
    6. default               — no_trigger_match → skip

Two MVP relationship-state triggers:

    - HIGH_EMOTIONAL_EVENT  — |impact| >= SHOCK_IMPACT in queued events
    - LONG_SILENCE          — last user message >= long_silence_hours old

v0.2 change (review M5): The two fine-grained throttles from the
original handoff §9.1 (a 30-minute minimum-interval check, and a
10-second user-silence window) are NOT in this engine. They were
evaluated and rejected by review M5 because:

    - The 30-minute minimum-interval check is redundant with
      ``max_per_24h=3`` (already a daily total cap).
    - The 10-second user-silence window overlaps with cold_user
      detection.

Only the no_in_flight_turn gate survives from handoff §9.1 because it
is the **only** semantic safety rule (avoiding "user asks question →
proactive interrupts → real reply lands after") in the three. It is
hardcoded with no config knob — there is no legitimate user scenario
for "let proactive interrupt an in-flight turn".

The engine is stateless: all mutable state lives in the audit sink + the
memory layer + the injected ``is_turn_in_flight`` predicate. This makes
it trivially unit-testable: give it a clock, a config, a fake audit
sink, a fake memory api, and a stubbed predicate — inspect the returned
Decision. No asyncio, no DB, no I/O.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from echovessel.proactive.core.base import (
    CONFIG_VERSION,
    ActionType,
    AuditSink,
    EventType,
    MemoryApi,
    ProactiveDecision,
    ProactiveEvent,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig

log = logging.getLogger(__name__)


# Spec §3.4.1: "high emotional event" = |impact| >= SHOCK_IMPACT. Mirrors
# memory.consolidate.SHOCK_IMPACT_THRESHOLD; duplicated here so proactive
# doesn't hard-depend on memory's constant (memory Thread owns that value
# independently).
SHOCK_IMPACT = 8


@dataclass(slots=True, frozen=True)
class TriggerMatch:
    """Result of trigger matching. ``reason`` is a TriggerReason enum,
    ``payload`` captures the event-specific fields that go into the audit
    trail (e.g. trigger_event_id, silent_hours)."""

    reason: TriggerReason
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


@dataclass
class PolicyEngine:
    """Stateless policy evaluator. Construct once per scheduler instance.

    v0.2 adds ``is_turn_in_flight``: an injected predicate the runtime
    wires up in RT-round3 by scanning its channel registry for any
    enabled channel with ``in_flight_turn_id is not None`` (spec §3.5a).
    When no predicate is injected (e.g. legacy tests, or before
    RT-round3 lands) the gate is permissive — it never blocks. That
    matches the spec §3.5a "no channel readable → no in-flight turn" rule.
    """

    config: ProactiveConfig
    audit: AuditSink
    memory: MemoryApi
    # v0.2 · review M5 · hardcoded UX safety gate. None = no injection yet,
    # treated as "no in-flight turn" (permissive). The predicate takes no
    # arguments because the semantics is "ANY enabled channel has an
    # in-flight turn" — the runtime closure captures the channel registry
    # it needs to scan. See spec §3.5a.
    is_turn_in_flight: Callable[[], bool] | None = field(default=None)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        events: list[ProactiveEvent],
        *,
        persona_id: str,
        user_id: str,
        now: datetime,
    ) -> ProactiveDecision:
        """Walk the priority gates, return exactly one ProactiveDecision.

        This method NEVER raises for policy reasons. Memory/audit I/O
        exceptions are caught and translated to defensive ``skip`` decisions
        with an appropriate skip_reason (spec §16.6).
        """
        decision = self._skeleton(persona_id, user_id, now)

        # 1. Quiet hours (spec §3.1)
        if _in_quiet_hours(
            now,
            self.config.quiet_hours_start,
            self.config.quiet_hours_end,
        ):
            return self._fill_skip(
                decision,
                trigger=TriggerReason.QUIET_HOURS_GATE,
                skip_reason=SkipReason.QUIET_HOURS,
            )

        # 2. Cold user (spec §3.3)
        try:
            if self._is_cold_user(persona_id, user_id, now):
                return self._fill_skip(
                    decision,
                    trigger=TriggerReason.COLD_USER_GATE,
                    skip_reason=SkipReason.LOW_PRESENCE_MODE,
                )
        except Exception as e:  # noqa: BLE001
            log.error("cold_user check failed: %s", e, exc_info=True)
            return self._fill_skip(
                decision,
                trigger=TriggerReason.COLD_USER_GATE,
                skip_reason=SkipReason.LOW_PRESENCE_MODE,
            )

        # 3. Rate limit (spec §3.2)
        try:
            sends_24h = self.audit.count_sends_in_last_24h(now=now)
        except Exception as e:  # noqa: BLE001
            log.error("rate_limit audit read failed: %s", e, exc_info=True)
            return self._fill_skip(
                decision,
                trigger=TriggerReason.RATE_LIMIT_GATE,
                skip_reason=SkipReason.RATE_LIMIT_READ_ERROR,
            )
        if sends_24h >= self.config.max_per_24h:
            return self._fill_skip(
                decision,
                trigger=TriggerReason.RATE_LIMIT_GATE,
                skip_reason=SkipReason.RATE_LIMITED,
            )

        # 4. No in-flight turn (spec §3.5 + §3.5a · v0.2 · review M5)
        #
        # This gate is NOT throttling — it is semantic UX safety. If
        # runtime is currently mid-turn (streaming an LLM reply to the
        # user), a proactive message slipping in would reorder visible
        # output as:
        #     [user's question] → [proactive interrupt] → [real reply]
        # which is a race-condition-grade UX bug. No config knob;
        # hardcoded behaviour. See spec §3.5a and review M5.
        #
        # The predicate can raise — that is treated like the cold_user
        # catch block: defensive skip with ``in_flight_turn`` reason, not
        # a hard failure. Production injection will not raise, but
        # stubs in tests sometimes simulate failure.
        if self.is_turn_in_flight is not None:
            try:
                in_flight = bool(self.is_turn_in_flight())
            except Exception as e:  # noqa: BLE001
                log.error(
                    "is_turn_in_flight predicate raised: %s; treating as in-flight",
                    e,
                    exc_info=True,
                )
                in_flight = True
            if in_flight:
                return self._fill_skip(
                    decision,
                    trigger=TriggerReason.IN_FLIGHT_TURN_GATE,
                    skip_reason=SkipReason.IN_FLIGHT_TURN,
                )

        # 5. Trigger match (spec §3.4 + §3.6)
        matched = self._match_trigger(events, persona_id, user_id, now)
        if matched is None:
            return self._fill_skip(
                decision,
                trigger=TriggerReason.NO_TRIGGER_MATCH,
                skip_reason=SkipReason.NO_TRIGGER_MATCH,
            )

        # Matched → action=send (the scheduler will do generation + delivery)
        decision.action = ActionType.SEND.value
        decision.skip_reason = None
        decision.trigger = matched.reason.value
        decision.trigger_payload = matched.payload
        return decision

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _is_cold_user(
        self, persona_id: str, user_id: str, now: datetime
    ) -> bool:
        """True when the user has been silent after ``cold_user_threshold``
        consecutive unanswered proactives.

        Algorithm (spec §3.3):
            1. Read the most recent N=threshold sends from audit.
            2. For each send, look for any user message in L2 whose
               created_at is within ``cold_user_response_window_hours``
               of that send.
            3. If every one of the N sends went unanswered → True.
            4. If fewer than N sends exist, the user has not yet been
               given enough chances to "stay cold" — return False.
        """
        threshold = self.config.cold_user_threshold
        window = timedelta(
            hours=self.config.cold_user_response_window_hours
        )

        sends = self.audit.recent_sends(last_n=threshold)
        if len(sends) < threshold:
            return False

        # For each send, check if user replied within the window. If ANY
        # send got a reply, we are not cold.
        for send in sends:
            if send.timestamp is None:
                continue
            deadline = send.timestamp + window
            if _user_replied_between(
                self.memory, persona_id, user_id, send.timestamp, deadline
            ):
                return False
        return True

    # ------------------------------------------------------------------
    # Trigger matching
    # ------------------------------------------------------------------

    def _match_trigger(
        self,
        events: list[ProactiveEvent],
        persona_id: str,
        user_id: str,
        now: datetime,
    ) -> TriggerMatch | None:
        # Priority 1: high emotional event
        for ev in events:
            if ev.event_type != EventType.EVENT_EXTRACTED:
                continue
            impact = int(ev.payload.get("emotional_impact", 0) or 0)
            if abs(impact) >= SHOCK_IMPACT:
                return TriggerMatch(
                    reason=TriggerReason.HIGH_EMOTIONAL_EVENT,
                    payload={
                        "trigger_event_id": ev.payload.get("event_id"),
                        "emotional_impact": impact,
                        "emotion_tags": list(
                            ev.payload.get("emotion_tags") or []
                        ),
                    },
                )

        # Priority 2: long silence (only needs tick/silence events)
        has_tick_like = any(
            ev.event_type
            in (EventType.TICK, EventType.LONG_SILENCE_DETECTED)
            for ev in events
        )
        if has_tick_like:
            silent_hours = _compute_silence_hours(
                self.memory, persona_id, user_id, now
            )
            if (
                silent_hours is not None
                and silent_hours >= self.config.long_silence_hours
            ):
                return TriggerMatch(
                    reason=TriggerReason.LONG_SILENCE,
                    payload={"silent_hours": round(silent_hours, 2)},
                )

        return None

    # ------------------------------------------------------------------
    # Decision skeletons
    # ------------------------------------------------------------------

    def _skeleton(
        self, persona_id: str, user_id: str, now: datetime
    ) -> ProactiveDecision:
        return ProactiveDecision(
            decision_id=str(uuid.uuid4()),
            persona_id=persona_id,
            user_id=user_id,
            timestamp=now,
            trigger=TriggerReason.NO_TRIGGER_MATCH.value,
            action=ActionType.SKIP.value,
            skip_reason=None,
            policy_snapshot={
                "quiet_hours_start": self.config.quiet_hours_start,
                "quiet_hours_end": self.config.quiet_hours_end,
                "max_per_24h": self.config.max_per_24h,
                "cold_user_threshold": self.config.cold_user_threshold,
                "cold_user_response_window_hours": (
                    self.config.cold_user_response_window_hours
                ),
                "long_silence_hours": self.config.long_silence_hours,
            },
            config_version=CONFIG_VERSION,
        )

    @staticmethod
    def _fill_skip(
        decision: ProactiveDecision,
        *,
        trigger: TriggerReason,
        skip_reason: SkipReason,
    ) -> ProactiveDecision:
        decision.action = ActionType.SKIP.value
        decision.trigger = trigger.value
        decision.skip_reason = skip_reason.value
        return decision


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    """True iff the local-hour of ``now`` falls within ``[start, end)``.

    When start > end the window wraps midnight (e.g. 23 → 7 means
    23:00-07:00 of the next day is quiet).
    """
    h = now.hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def _user_replied_between(
    memory: MemoryApi,
    persona_id: str,
    user_id: str,
    after: datetime,
    until: datetime,
) -> bool:
    """True iff any USER-role message exists in L2 with
    ``after <= created_at <= until``.

    Implementation note (D4): we call ``list_recall_messages`` without any
    channel_id filter — memory stays unified across channels (D4 guard).
    We scan the recent window (50 messages) in scheduler-side memory and
    filter by role + time locally since memory's list_recall_messages
    doesn't have a role_filter parameter yet (§17 open Q9).
    """
    recent = memory.list_recall_messages(
        persona_id,
        user_id,
        limit=50,
    )
    for msg in recent:
        role = getattr(msg, "role", None)
        role_value = getattr(role, "value", role)
        if role_value != "user":
            continue
        created_at = getattr(msg, "created_at", None)
        if created_at is None:
            continue
        if after <= created_at <= until:
            return True
    return False


def _compute_silence_hours(
    memory: MemoryApi,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> float | None:
    """Hours since the most recent USER message, or None if no history.

    Reads the 20 most recent messages (any role, any channel — D4) and
    picks the newest one tagged ``role == user``. If the newest message
    is already a persona reply (including a proactive one), the silence
    clock resets from the USER message before it.
    """
    recent = memory.list_recall_messages(
        persona_id,
        user_id,
        limit=20,
    )
    for msg in recent:  # newest-first (list_recall_messages DESC)
        role = getattr(msg, "role", None)
        role_value = getattr(role, "value", role)
        if role_value != "user":
            continue
        created_at = getattr(msg, "created_at", None)
        if created_at is None:
            continue
        delta = now - created_at
        return max(delta.total_seconds() / 3600.0, 0.0)
    return None


__all__ = [
    "PolicyEngine",
    "TriggerMatch",
    "SHOCK_IMPACT",
]

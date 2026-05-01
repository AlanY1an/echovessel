"""Policy engine · proactive v3 · 5-gate evaluator over follow-up events.

The engine takes a list of ``ProactiveEvent`` items (the ``evaluate``
signature is unchanged from v2 so the scheduler does not move) and
walks a fixed 5-gate sequence:

       1. quiet_hours        (PersonaProfile.quiet_hours)
       2. forbidden_topics   (PersonaProfile.forbidden_topics matched
                              against the ConceptNode's
                              ``follow_up_hint``; on hit, sets
                              ``proactive_suppressed_at`` on the event
                              so the same anchor can't re-trigger.)
       3. in_flight_turn     (injected predicate)
       4. rate_limit         (24h SQL count over proactive_decisions)
       5. engagement_score   (ProactiveState; soft, bypassed when the
                              backing memory event has a high-confidence
                              relational tag (``commitment`` /
                              ``vulnerability``) OR
                              ``|emotional_impact| >= 7``, OR the
                              ProactiveEvent itself is ``critical``.)

v3 reads ``ConceptNode`` directly (memory events with ``follow_up_at``
annotated by Phase B) and has a single fireable event type:
``FOLLOW_UP_DUE``.

The legacy ``cold_user`` gate is gone. Coverage proof: any scenario
that v1 caught (N consecutive unanswered fires) is now caught by
``engagement_score < 0.4`` because every unanswered fire decays
engagement by ``-0.15``. Two unanswered fires drop a fresh-baseline
``0.7`` score to ``0.4`` — the boundary the legacy default
``cold_user_threshold=2`` was set at.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from echovessel.memory.models import ConceptNode
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
from echovessel.proactive.core.models import (
    PersonaProfile,
    ProactiveState,
)
from echovessel.proactive.core.models import (
    ProactiveDecision as PersistedDecision,
)

log = logging.getLogger(__name__)


SHOCK_IMPACT = 8

# Engagement_score gate thresholds. ``ENGAGEMENT_PASS`` is the soft
# block boundary; the high-confidence escape hatch fires when the
# memory event carries a strong relational tag (``commitment`` /
# ``vulnerability``) or its absolute emotional impact reaches
# ``HIGH_CONFIDENCE_IMPACT`` — both signals say the LLM was confident
# enough about this anchor that recent silence shouldn't suppress it.
ENGAGEMENT_PASS = 0.4
HIGH_CONFIDENCE_BYPASS_TAGS = frozenset({"commitment", "vulnerability"})
HIGH_CONFIDENCE_IMPACT = 7

_FORBIDDEN_TOPIC_REASON = "forbidden_topic"
_LOW_ENGAGEMENT_REASON = "low_engagement"


@dataclass(slots=True, frozen=True)
class TriggerMatch:
    """Result of trigger matching (kept as a record for callers that
    still consume the v1 shape — currently nothing does)."""

    reason: TriggerReason
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


@dataclass
class PolicyEngine:
    """Stateless policy evaluator. Construct once per scheduler instance.

    ``db_factory`` is the v2 addition — gates 2 / 4 / 5 read PersonaProfile,
    proactive_decisions, ProactiveState directly. When ``None`` the engine
    degrades gracefully (forbidden / engagement gates are pass-through),
    matching the test fixtures that don't wire a real DB.
    """

    config: ProactiveConfig
    audit: AuditSink
    memory: MemoryApi
    is_turn_in_flight: Callable[[], bool] | None = field(default=None)
    db_factory: Callable[[], Session] | None = field(default=None)

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
        """Walk the 5-gate sequence, return exactly one ProactiveDecision.

        Memory / audit I/O exceptions are caught and translated to
        defensive ``skip`` decisions so the scheduler tick loop is
        never aborted from a policy run.
        """
        decision = self._skeleton(persona_id, user_id, now)

        target = _pick_fireable(events)
        if target is None:
            return self._fill_skip(
                decision,
                trigger=TriggerReason.NO_TRIGGER_MATCH,
                skip_reason=SkipReason.NO_TRIGGER_MATCH,
            )

        # Load shared state once. The DB-backed gates short-circuit
        # gracefully when ``db_factory`` is unwired (legacy tests).
        profile = self._load_profile(persona_id)
        memory_event = self._load_event(target)
        state = self._load_or_init_state(persona_id, user_id, now)

        # Gate 1 · quiet_hours
        if profile is not None and _in_quiet_hours(now, profile.quiet_hours):
            return self._fill_skip(
                decision,
                trigger=TriggerReason.QUIET_HOURS_GATE,
                skip_reason=SkipReason.QUIET_HOURS,
            )

        # Gate 2 · forbidden_topics (matched against the memory event's
        # ``follow_up_hint``; on hit, set ``proactive_suppressed_at``
        # so the same anchor doesn't re-arm on the next phase tick).
        if (
            profile is not None
            and memory_event is not None
            and _matches_forbidden(memory_event.follow_up_hint or "", profile.forbidden_topics)
        ):
            self._suppress_event(memory_event, now)
            return self._fill_skip_with_reason(
                decision,
                trigger=TriggerReason.NO_TRIGGER_MATCH,
                reason_str=_FORBIDDEN_TOPIC_REASON,
            )

        # Gate 3 · in_flight_turn (predicate)
        if self.is_turn_in_flight is not None:
            try:
                in_flight = bool(self.is_turn_in_flight())
            except Exception as e:  # noqa: BLE001
                log.error(
                    "is_turn_in_flight predicate raised: %s; treating as in-flight",
                    e,
                )
                in_flight = True
            if in_flight:
                return self._fill_skip(
                    decision,
                    trigger=TriggerReason.IN_FLIGHT_TURN_GATE,
                    skip_reason=SkipReason.IN_FLIGHT_TURN,
                )

        # Gate 4 · rate_limit (24h SQL count over proactive_decisions)
        sends_24h = self._count_recent_fires(persona_id, user_id, now)
        if sends_24h >= self.config.max_per_24h:
            return self._fill_skip(
                decision,
                trigger=TriggerReason.RATE_LIMIT_GATE,
                skip_reason=SkipReason.RATE_LIMITED,
            )

        # Gate 5 · engagement_score (soft, bypassed by high-confidence
        # memory event or critical ProactiveEvent).
        #
        # Boundary uses ``<=`` so the suppress side INCLUDES exact
        # equality with the threshold. The check guards against the
        # IEEE-754 fragility of ``0.7 - 0.15 - 0.15``: that arithmetic
        # gives ``0.3999...`` on CPython today (so ``<`` would also
        # suppress), but a future re-ordering of update paths or a
        # delta tweak could land on exactly ``0.4``. With ``<=`` the
        # "at-or-below threshold ⇒ suppress" semantics is explicit
        # rather than dependent on FP coincidence.
        if state.engagement_score <= ENGAGEMENT_PASS:
            high_confidence = _is_high_confidence(memory_event)
            is_critical = bool(target.critical)
            if not (high_confidence or is_critical):
                return self._fill_skip_with_reason(
                    decision,
                    trigger=TriggerReason.NO_TRIGGER_MATCH,
                    reason_str=_LOW_ENGAGEMENT_REASON,
                )

        # All gates pass — fire.
        decision.action = ActionType.SEND.value
        decision.skip_reason = None
        decision.trigger = _trigger_for(target).value
        decision.trigger_payload = dict(target.payload or {})
        return decision

    # ------------------------------------------------------------------
    # DB readers
    # ------------------------------------------------------------------

    def _load_profile(self, persona_id: str) -> PersonaProfile | None:
        if self.db_factory is None:
            return None
        try:
            with self.db_factory() as db:
                return db.get(PersonaProfile, persona_id)
        except Exception as e:  # noqa: BLE001
            log.warning("policy: PersonaProfile load failed: %s", e)
            return None

    def _load_event(self, event: ProactiveEvent) -> ConceptNode | None:
        """Load the backing ``ConceptNode`` for a ``FOLLOW_UP_DUE`` event.

        Other event types carry no memory backing — the caller treats
        the gate inputs as ``None`` and lets the event-agnostic gates
        (quiet_hours / in_flight / rate_limit / engagement) decide.
        """
        if self.db_factory is None:
            return None
        if event.event_type != EventType.FOLLOW_UP_DUE:
            return None
        event_id = event.payload.get("event_id")
        if event_id is None:
            return None
        try:
            with self.db_factory() as db:
                return db.get(ConceptNode, event_id)
        except Exception as e:  # noqa: BLE001
            log.warning("policy: ConceptNode load failed: %s", e)
            return None

    def _load_or_init_state(
        self,
        persona_id: str,
        user_id: str,
        now: datetime,
    ) -> ProactiveState:
        """Return the row or a fresh in-memory baseline. The fresh
        baseline is NOT persisted from inside policy — that's a
        side-effect ``handle_user_message_ingested`` /
        ``settle_unreplied_fires`` is responsible for. Policy reads
        must be pure."""
        if self.db_factory is None:
            return ProactiveState(
                persona_id=persona_id,
                user_id=user_id,
                engagement_score=0.7,
                last_updated=now,
            )
        try:
            with self.db_factory() as db:
                state = db.exec(
                    select(ProactiveState).where(
                        ProactiveState.persona_id == persona_id,
                        ProactiveState.user_id == user_id,
                    )
                ).first()
                if state is not None:
                    return state
        except Exception as e:  # noqa: BLE001
            log.warning("policy: ProactiveState load failed: %s", e)
        return ProactiveState(
            persona_id=persona_id,
            user_id=user_id,
            engagement_score=0.7,
            last_updated=now,
        )

    def _count_recent_fires(
        self,
        persona_id: str,
        user_id: str,
        now: datetime,
    ) -> int:
        """24h SQL count of ``action='fire'`` rows. Falls back to the
        audit sink's ``count_sends_in_last_24h`` when ``db_factory`` is
        unwired (legacy tests)."""
        if self.db_factory is None:
            try:
                return int(self.audit.count_sends_in_last_24h(now=now))
            except Exception as e:  # noqa: BLE001
                log.warning("audit count fallback failed: %s", e)
                return 0
        cutoff = now - timedelta(hours=24)
        try:
            with self.db_factory() as db:
                n = db.exec(
                    select(func.count(PersistedDecision.id)).where(
                        PersistedDecision.persona_id == persona_id,
                        PersistedDecision.user_id == user_id,
                        PersistedDecision.action == "fire",
                        PersistedDecision.timestamp >= cutoff,
                        PersistedDecision.timestamp <= now,
                    )
                ).one()
            return int(n or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("policy: rate_limit count failed: %s", e)
            return 0

    def _suppress_event(self, memory_event: ConceptNode, now: datetime) -> None:
        """Set ``proactive_suppressed_at`` on a forbidden-topic event.

        v3 closes a follow-up by marking the source ``ConceptNode``
        suppressed (the FollowUpScheduler ``_eligible`` filter excludes
        suppressed nodes from re-arming). No retry, no cooldown — the
        anchor is permanently disallowed for proactive follow-up.
        """
        if self.db_factory is None or memory_event.id is None:
            return
        try:
            with self.db_factory() as db:
                row = db.get(ConceptNode, memory_event.id)
                if row is None:
                    return
                row.proactive_suppressed_at = now
                db.add(row)
                db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("policy: forbidden_topic suppress failed: %s", e)

    # ------------------------------------------------------------------
    # Decision skeleton helpers
    # ------------------------------------------------------------------

    def _skeleton(self, persona_id: str, user_id: str, now: datetime) -> ProactiveDecision:
        return ProactiveDecision(
            decision_id=str(uuid.uuid4()),
            persona_id=persona_id,
            user_id=user_id,
            timestamp=now,
            trigger=TriggerReason.NO_TRIGGER_MATCH.value,
            action=ActionType.SKIP.value,
            skip_reason=None,
            policy_snapshot={
                "max_per_24h": self.config.max_per_24h,
                "engagement_pass": ENGAGEMENT_PASS,
                "high_confidence_bypass_tags": sorted(HIGH_CONFIDENCE_BYPASS_TAGS),
                "high_confidence_impact": HIGH_CONFIDENCE_IMPACT,
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

    @staticmethod
    def _fill_skip_with_reason(
        decision: ProactiveDecision,
        *,
        trigger: TriggerReason,
        reason_str: str,
    ) -> ProactiveDecision:
        """Skip variant that takes a free-form ``reason_str`` —
        ``forbidden_topic`` and ``low_engagement`` aren't in the v1
        ``SkipReason`` enum but the audit table accepts any string."""
        decision.action = ActionType.SKIP.value
        decision.trigger = trigger.value
        decision.skip_reason = reason_str
        return decision


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _pick_fireable(events: list[ProactiveEvent]) -> ProactiveEvent | None:
    """Return the first event the v3 policy is willing to fire on.

    v3 has a single fireable lane: ``FOLLOW_UP_DUE`` (emitted by the
    FollowUpScheduler when a memory event's phase wakes). A
    transitional ``EVENT_EXTRACTED`` branch stays for legacy tests
    that pre-load ``emotional_impact`` directly into the payload.
    """
    for ev in events:
        if ev.event_type == EventType.FOLLOW_UP_DUE:
            return ev
        if ev.event_type == EventType.EVENT_EXTRACTED:
            impact = int(ev.payload.get("emotional_impact", 0) or 0)
            if abs(impact) >= SHOCK_IMPACT:
                return ev
    return None


def _trigger_for(event: ProactiveEvent) -> TriggerReason:
    """Map the fireable event back to a ``TriggerReason`` value for
    the audit row.

    The ``trigger`` field on the dataclass is a legacy free-form
    string; the canonical routing column on the persisted row is
    ``trigger_type``. v3 collapses both the ``FOLLOW_UP_DUE`` lane
    and the transitional ``EVENT_EXTRACTED`` shock path to
    ``FOLLOW_UP``.
    """
    return TriggerReason.FOLLOW_UP


def _in_quiet_hours(now: datetime, quiet_hours: list[int]) -> bool:
    """True iff ``now.hour`` falls inside ``[start, end)`` of
    ``quiet_hours = [start_hour, end_hour]``. Wrap-midnight aware:
    ``[23, 7]`` covers ``23:00–07:00``."""
    if not quiet_hours or len(quiet_hours) != 2:
        return False
    start, end = quiet_hours
    if start == end:
        return False
    h = now.hour
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _matches_forbidden(hint: str, forbidden: list[str]) -> bool:
    """Case-insensitive substring match. Keyword filter, not a
    semantic gate — the LLM's ``follow_up_hint`` writer is responsible
    for not creating obvious bypasses."""
    if not forbidden:
        return False
    text = (hint or "").lower()
    return any(t.lower() in text for t in forbidden if t)


def _is_high_confidence(memory_event: ConceptNode | None) -> bool:
    """High-confidence bypass for the engagement gate. v3 uses two
    proxies on the backing ``ConceptNode``: a strong relational tag
    (``commitment`` / ``vulnerability``) or a high absolute emotional
    impact (``|impact| >= 7``)."""
    if memory_event is None:
        return False
    if any(t in HIGH_CONFIDENCE_BYPASS_TAGS for t in memory_event.relational_tags):
        return True
    return abs(memory_event.emotional_impact) >= HIGH_CONFIDENCE_IMPACT


__all__ = [
    "ENGAGEMENT_PASS",
    "HIGH_CONFIDENCE_BYPASS_TAGS",
    "HIGH_CONFIDENCE_IMPACT",
    "PolicyEngine",
    "SHOCK_IMPACT",
    "TriggerMatch",
]

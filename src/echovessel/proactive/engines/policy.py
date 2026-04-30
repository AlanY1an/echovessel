"""Policy engine · proactive v2 · Stage 4 (atomic 5-gate rewrite).

Replaces the v1 ``cold_user_gate`` / trigger-matching flow. The
``evaluate(events, ...)`` signature is preserved on purpose so the
v1 scheduler can keep calling it through the Stage 3 transition
without having to land scheduler changes in the same commit.

Internally the engine now:

1. Picks the first **fireable** event from the queue — currently
   ``THREAD_DUE`` or ``HIGH_EMOTIONAL_EVENT`` (the v2 lanes), with a
   transitional fallback for ``EVENT_EXTRACTED`` events whose payload
   carries ``|emotional_impact| >= SHOCK_IMPACT`` (this is what the
   ``test_round2_delivery_inheritance`` suite still drives through).
   No fireable event ⇒ ``NO_TRIGGER_MATCH`` skip.
2. Walks the new 5-gate sequence:

       1. quiet_hours        (PersonaProfile.quiet_hours)
       2. forbidden_topics   (PersonaProfile.forbidden_topics, only
                              meaningful for THREAD_DUE; closes the
                              thread on hit so the same anchor doesn't
                              re-trigger every cooldown)
       3. in_flight_turn     (injected predicate)
       4. rate_limit         (24h SQL count over proactive_decisions)
       5. engagement_score   (ProactiveState; soft, bypassed by
                              ``thread.confidence ≥ 0.8`` OR
                              ``event.critical``).

The HIGH_EMOTIONAL_EVENT path is **atomic-replaced** (pre-flight
A): the v1 ``_match_trigger`` payload-driven branch is gone, but the
new flow still fires the same event via the same gate sequence — no
delete-then-add window where high-impact is dead.

The v1 ``cold_user`` gate is removed. Coverage proof: any scenario
that v1 caught (N consecutive unanswered fires) is now caught by
``engagement_score < 0.4`` because every unanswered fire decays
engagement by ``-0.15``. Two unanswered fires drop a fresh-baseline
``0.7`` score to ``0.4`` — the same boundary the v1 default
``cold_user_threshold=2`` was set at. See Stage 4 tracker §
"cold_user → engagement coverage".
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

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
# block boundary; ``HIGH_CONFIDENCE_BYPASS`` is the per-thread escape
# hatch — a thread with ``confidence ≥ 0.8`` overrides the engagement
# block (the LLM was confident enough about the future-branch that we
# should not be deterred by recent silence).
ENGAGEMENT_PASS = 0.4
HIGH_CONFIDENCE_BYPASS = 0.8

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
        thread = self._load_thread(target)
        state = self._load_or_init_state(persona_id, user_id, now)

        # Gate 1 · quiet_hours
        if profile is not None and _in_quiet_hours(now, profile.quiet_hours):
            return self._fill_skip(
                decision,
                trigger=TriggerReason.QUIET_HOURS_GATE,
                skip_reason=SkipReason.QUIET_HOURS,
            )

        # Gate 2 · forbidden_topics (only meaningful for THREAD_DUE)
        if (
            profile is not None
            and thread is not None
            and _matches_forbidden(thread.anchor_text, profile.forbidden_topics)
        ):
            self._close_thread(thread, now)
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
        # thread or critical event).
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
            high_confidence = (
                thread is not None and thread.confidence >= HIGH_CONFIDENCE_BYPASS
            )
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

    def _load_thread(self, event: ProactiveEvent) -> FollowUpThread | None:  # noqa: F821
        # FollowUpThread removed in v3 · this path is rewritten in Stage 3.1.
        if self.db_factory is None:
            return None
        if event.event_type != EventType.THREAD_DUE:
            return None
        thread_id = event.payload.get("thread_id")
        if thread_id is None:
            return None
        try:
            with self.db_factory() as db:
                return db.get(FollowUpThread, thread_id)  # noqa: F821
        except Exception as e:  # noqa: BLE001
            log.warning("policy: FollowUpThread load failed: %s", e)
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

    def _close_thread(self, thread: FollowUpThread, now: datetime) -> None:  # noqa: F821
        """Close a thread when the forbidden gate hits — the anchor is
        permanently disallowed, no point cooling-down-and-retrying.

        FollowUpThread removed in v3 · this path is rewritten in Stage 3.1.
        """
        if self.db_factory is None or thread.id is None:
            return
        try:
            with self.db_factory() as db:
                row = db.get(FollowUpThread, thread.id)  # noqa: F821
                if row is None:
                    return
                row.closed_at = now
                db.add(row)
                db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("policy: forbidden_topic thread close failed: %s", e)

    # ------------------------------------------------------------------
    # Decision skeleton helpers
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
                "max_per_24h": self.config.max_per_24h,
                "engagement_pass": ENGAGEMENT_PASS,
                "high_confidence_bypass": HIGH_CONFIDENCE_BYPASS,
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
    """Return the first event the v2 policy is willing to fire on."""
    for ev in events:
        if ev.event_type == EventType.THREAD_DUE:
            return ev
        # Transitional: legacy ``EVENT_EXTRACTED`` payload-driven path.
        # Stage 3 left tests + integration calling ``scheduler.notify``
        # with EVENT_EXTRACTED+impact pre-set; this branch keeps them
        # firing through the new gates without a separate code path.
        if ev.event_type == EventType.EVENT_EXTRACTED:
            impact = int(ev.payload.get("emotional_impact", 0) or 0)
            if abs(impact) >= SHOCK_IMPACT:
                return ev
    return None


def _trigger_for(event: ProactiveEvent) -> TriggerReason:
    """Map the fireable event back to a ``TriggerReason`` value for
    the audit row."""
    if event.event_type == EventType.EVENT_EXTRACTED:
        return TriggerReason.HIGH_EMOTIONAL_EVENT  # transitional
    # THREAD_DUE: spec 05 routes via ``trigger_type`` column on the
    # SQLite audit row; the legacy ``trigger`` field on the
    # dataclass keeps HIGH_EMOTIONAL_EVENT as its closest match.
    return TriggerReason.HIGH_EMOTIONAL_EVENT


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


def _matches_forbidden(anchor_text: str, forbidden: list[str]) -> bool:
    """Case-insensitive substring match. Keyword filter, not a
    semantic gate — the LLM's anchor_text writer is responsible for
    not creating obvious bypasses."""
    if not forbidden:
        return False
    text = (anchor_text or "").lower()
    return any(t.lower() in text for t in forbidden if t)


__all__ = [
    "ENGAGEMENT_PASS",
    "HIGH_CONFIDENCE_BYPASS",
    "PolicyEngine",
    "SHOCK_IMPACT",
    "TriggerMatch",
]

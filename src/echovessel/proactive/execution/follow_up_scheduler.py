"""Event-driven proactive follow-up scheduler.

Listens to memory ``on_event_created`` hook; schedules an asyncio timer for
each ``ConceptNode`` with a ``follow_up_at``. The timer wakes at the
computed next-attempt time, re-validates the event, and emits a
``FOLLOW_UP_DUE`` ``ProactiveEvent`` into the proactive scheduler. After
each attempt the next phase / retry is rescheduled.

No polling, no timer pumps. On daemon restart ``start()`` re-initialises
timers from DB state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import select

from echovessel.memory.models import ConceptNode, ProactiveDecision
from echovessel.proactive.core.base import (
    EventType,
    ProactiveEvent,
    ProactiveScheduler,
)

log = logging.getLogger(__name__)

DEFAULT_ADVANCE_PRE_HOURS = 24
DEFAULT_ADVANCE_POST_HOURS = 24
DEFAULT_COOLDOWN_HOURS = 4
ATTEMPT_CAP = 5
ON_PHASE_TOLERANCE_MIN = 5  # reminder semantics: pre=post=0


@dataclass
class FollowUpScheduler:
    db_factory: Callable[[], Any]
    proactive_scheduler: ProactiveScheduler
    persona_id: str
    user_id: str
    now_fn: Callable[[], datetime] = datetime.now
    _active_timers: dict[int, asyncio.Task] = field(default_factory=dict, init=False)
    _shutdown: bool = field(default=False, init=False)

    async def start(self) -> None:
        """Daemon start: re-schedule all DB-pending events."""
        with self.db_factory() as db:
            pending = db.exec(
                select(ConceptNode).where(
                    ConceptNode.persona_id == self.persona_id,
                    ConceptNode.user_id == self.user_id,
                    ConceptNode.follow_up_at.is_not(None),
                    ConceptNode.deleted_at.is_(None),
                    ConceptNode.superseded_by_id.is_(None),
                    ConceptNode.proactive_suppressed_at.is_(None),
                )
            ).all()
            for event in pending:
                self._schedule_next(event)
        log.info(
            "FollowUpScheduler.start: scheduled %d pending events",
            len(self._active_timers),
        )

    async def stop(self) -> None:
        """Cancel all active timers."""
        self._shutdown = True
        for task in self._active_timers.values():
            task.cancel()
        await asyncio.gather(*self._active_timers.values(), return_exceptions=True)
        self._active_timers.clear()

    def on_event_created(self, event: ConceptNode) -> None:
        """Memory observer hook · sync · non-blocking."""
        if event.persona_id != self.persona_id or event.user_id != self.user_id:
            return
        if event.follow_up_at is None:
            return
        if not self._eligible(event):
            return
        self._schedule_next(event)

    def _schedule_next(self, event: ConceptNode) -> None:
        """Compute next attempt time and create the timer task."""
        if self._shutdown:
            return
        if event.id is None:
            return
        existing = self._active_timers.get(event.id)
        if existing is not None and not existing.done():
            existing.cancel()

        result = self._compute_next_attempt(event)
        if result is None:
            return
        phase, when = result
        delay = max(0.0, (when - self.now_fn()).total_seconds())

        self._active_timers[event.id] = asyncio.create_task(
            self._wake_and_attempt(event.id, phase, delay)
        )

    async def _wake_and_attempt(self, event_id: int, phase: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        if self._shutdown:
            return

        with self.db_factory() as db:
            event = db.get(ConceptNode, event_id)
            if event is None or not self._eligible(event):
                return

            decision_id = str(uuid.uuid4())
            self.proactive_scheduler.notify(
                ProactiveEvent(
                    event_type=EventType.FOLLOW_UP_DUE,
                    persona_id=event.persona_id,
                    user_id=event.user_id,
                    created_at=self.now_fn(),
                    payload={
                        "event_id": event_id,
                        "phase": phase,
                        "follow_up_hint": event.follow_up_hint,
                        "decision_id": decision_id,
                    },
                    critical=False,
                )
            )
            # notify is sync; policy.evaluate runs inline in v3. After fire/
            # suppress lands an audit row, reload and reschedule next phase.
            db.refresh(event)
            self._schedule_next(event)

    def _compute_next_attempt(self, event: ConceptNode) -> tuple[str, datetime] | None:
        """Returns (phase, when) for the next attempt, or None if exhausted."""
        with self.db_factory() as db:
            now = self.now_fn()
            for phase in self._candidate_phases(event, db, now):
                if self._has_fired(db, event.id, phase):
                    continue
                if self._attempt_cap_reached(db, event.id, phase):
                    continue

                last = self._last_decision(db, event.id, phase)
                next_time = self._compute_when(event, phase, last, now)
                if next_time is None:
                    continue
                return phase, next_time
        return None

    def _candidate_phases(self, event: ConceptNode, db: Any, now: datetime) -> list[str]:
        """Phases applicable to this event in natural order."""
        if event.advance_pre_hours == 0 and event.advance_post_hours == 0:
            return ["on"]  # reminder semantics

        if event.event_time_end is not None:
            phases = ["pre", "on"]
            if (event.advance_post_hours or DEFAULT_ADVANCE_POST_HOURS) > 0:
                phases.append("post")
            return phases

        # ongoing/unresolved · indexed check_N
        n = self._next_check_number(db, event.id)
        return [f"check_{n}"]

    def _compute_when(
        self,
        event: ConceptNode,
        phase: str,
        last: ProactiveDecision | None,
        now: datetime,
    ) -> datetime | None:
        """Compute when to attempt this phase, applying smart cooldown."""
        if event.advance_pre_hours == 0 and event.advance_post_hours == 0:
            base = event.follow_up_at  # reminder fires AT follow_up_at
        elif event.event_time_end is not None:
            target = event.follow_up_at
            pre = event.advance_pre_hours or DEFAULT_ADVANCE_PRE_HOURS
            post = event.advance_post_hours or DEFAULT_ADVANCE_POST_HOURS
            base = {
                "pre": target - timedelta(hours=pre),
                "on": target - timedelta(hours=1),
                "post": target + timedelta(hours=post),
            }[phase]
        else:
            base = event.follow_up_at  # ongoing/unresolved · at follow_up_at

        if last is None or last.action == "fire":
            return max(base, now)

        if last.suppress_reason == "forbidden_topic":
            with self.db_factory() as db:
                e = db.get(ConceptNode, event.id)
                if e is not None:
                    e.proactive_suppressed_at = now
                    db.add(e)
                    db.commit()
            return None

        if last.suppress_reason == "quiet_hours":
            return max(base, now)

        if last.suppress_reason == "rate_limit":
            return max(base, last.timestamp + timedelta(hours=DEFAULT_COOLDOWN_HOURS))

        return max(base, last.timestamp + timedelta(hours=DEFAULT_COOLDOWN_HOURS))

    def _eligible(self, event: ConceptNode | None) -> bool:
        if event is None:
            return False
        if event.deleted_at is not None:
            return False
        if event.superseded_by_id is not None:
            return False
        if event.proactive_suppressed_at is not None:
            return False
        if event.follow_up_at is None:
            return False
        if event.estimated_arc_days is not None:
            stale_threshold = event.created_at + timedelta(days=3 * event.estimated_arc_days)
            if self.now_fn() > stale_threshold:
                return False
        return True

    def _has_fired(self, db: Any, event_id: int | None, phase: str) -> bool:
        if event_id is None:
            return False
        return (
            db.exec(
                select(ProactiveDecision)
                .where(
                    ProactiveDecision.source_event_id == event_id,
                    ProactiveDecision.phase == phase,
                    ProactiveDecision.action == "fire",
                )
                .limit(1)
            ).first()
            is not None
        )

    def _attempt_cap_reached(self, db: Any, event_id: int | None, phase: str) -> bool:
        if event_id is None:
            return False
        count = db.exec(
            select(func.count(ProactiveDecision.id)).where(
                ProactiveDecision.source_event_id == event_id,
                ProactiveDecision.phase == phase,
            )
        ).one()
        if isinstance(count, tuple):
            count = count[0]
        return count >= ATTEMPT_CAP

    def _last_decision(self, db: Any, event_id: int | None, phase: str) -> ProactiveDecision | None:
        if event_id is None:
            return None
        return db.exec(
            select(ProactiveDecision)
            .where(
                ProactiveDecision.source_event_id == event_id,
                ProactiveDecision.phase == phase,
            )
            .order_by(ProactiveDecision.timestamp.desc())
            .limit(1)
        ).first()

    def _next_check_number(self, db: Any, event_id: int | None) -> int:
        if event_id is None:
            return 1
        count = db.exec(
            select(func.count(ProactiveDecision.id)).where(
                ProactiveDecision.source_event_id == event_id
            )
        ).one()
        if isinstance(count, tuple):
            count = count[0]
        return count + 1


__all__ = ["FollowUpScheduler"]

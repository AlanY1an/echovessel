"""BA contingent reward loop · proactive v2 · Stage 4.

Four update paths feed ``proactive_state.engagement_score``:

1. User reply within 24h of a fire        → ``+0.10`` (single bump,
   regardless of how many pending fires landed in the same window
   — they all get marked ``user_replied_at = now``).
2. User starts a turn with no pending fire → ``+0.05`` (initiative).
3. 24h passes with no reply                → ``-0.15`` per pending
   decision; each settled row is marked with ``NOT_REPLIED_SENTINEL``
   (``datetime.min``) so a second pass never double-decays.
4. 7+ silent days                          → ``-0.05/day``,
   idempotent per local-day via ``state.last_updated``.

``decay > reward`` is the literature-driven hard constraint
(``develop-docs/research/2026-04-18-proactive-frameworks.md`` ·
Kanter et al. 2010). Adding new paths must not soften this.

The single ``handle_user_message_ingested`` entry point is what the
runtime memory observer calls. It branches between path 1 (with
pending fire) and path 2 (no pending fire) so the same observer
call **never** double-counts. Verified by
``test_user_reply_does_not_double_count``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlmodel import Session, select

from echovessel.core.types import MessageRole
from echovessel.memory.models import (
    NOT_REPLIED_SENTINEL,
    ProactiveDecision,
    RecallMessage,
)
from echovessel.proactive.core.models import (
    ProactiveState,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable constants — matched to spec 05 §"Engagement_score 更新规则"
# ---------------------------------------------------------------------------

DELTA_REPLIED: float = 0.10
DELTA_NOT_REPLIED: float = -0.15
DELTA_USER_INITIATIVE: float = 0.05
DELTA_DAILY_DECAY: float = -0.05

REPLY_WINDOW_HOURS: int = 24
MIN_DAYS_FOR_DECAY: int = 7

ENGAGEMENT_INITIAL: float = 0.7
ENGAGEMENT_MIN: float = 0.05
ENGAGEMENT_MAX: float = 0.95


# ---------------------------------------------------------------------------
# Score helpers
# ---------------------------------------------------------------------------


def _clamp(score: float) -> float:
    return max(ENGAGEMENT_MIN, min(ENGAGEMENT_MAX, score))


def _load_or_init_state(
    db: Session,
    *,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> ProactiveState:
    """Lookup the row by composite PK or insert a fresh baseline."""
    state = db.exec(
        select(ProactiveState).where(
            ProactiveState.persona_id == persona_id,
            ProactiveState.user_id == user_id,
        )
    ).first()
    if state is None:
        state = ProactiveState(
            persona_id=persona_id,
            user_id=user_id,
            engagement_score=ENGAGEMENT_INITIAL,
            last_updated=now,
        )
        db.add(state)
        db.flush()
    return state


def _apply_delta(state: ProactiveState, delta: float, *, now: datetime) -> None:
    state.engagement_score = _clamp(state.engagement_score + delta)
    state.last_updated = now


# ---------------------------------------------------------------------------
# Path 1 + 2 · user message ingested
# ---------------------------------------------------------------------------


def handle_user_message_ingested(
    db: Session,
    *,
    msg: RecallMessage,
    persona_id: str,
    user_id: str,
    now_fn: Callable[[], datetime] = datetime.now,
) -> None:
    """Memory observer hook for a user message.

    Branch:

    * pending fire decision in the last 24h → ``+0.10``, mark all
      pending decisions ``user_replied_at = now``
    * otherwise → ``+0.05`` (user-initiated turn)

    The branches are mutually exclusive — the function never adds
    both deltas on the same call.
    """
    role = getattr(msg.role, "value", msg.role)
    if role != MessageRole.USER.value:
        return
    if msg.persona_id != persona_id or msg.user_id != user_id:
        return

    now = now_fn()
    cutoff = now - timedelta(hours=REPLY_WINDOW_HOURS)

    pending = list(
        db.exec(
            select(ProactiveDecision).where(
                ProactiveDecision.persona_id == persona_id,
                ProactiveDecision.user_id == user_id,
                ProactiveDecision.action == "fire",
                ProactiveDecision.timestamp >= cutoff,
                ProactiveDecision.user_replied_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )

    state = _load_or_init_state(
        db, persona_id=persona_id, user_id=user_id, now=now
    )

    if pending:
        for d in pending:
            d.user_replied_at = now
            db.add(d)
        _apply_delta(state, DELTA_REPLIED, now=now)
    else:
        _apply_delta(state, DELTA_USER_INITIATIVE, now=now)

    db.add(state)
    db.commit()


# ---------------------------------------------------------------------------
# Path 3 · 24h passed, no reply
# ---------------------------------------------------------------------------


def settle_unreplied_fires(
    db: Session,
    *,
    persona_id: str,
    user_id: str,
    now_fn: Callable[[], datetime] = datetime.now,
) -> int:
    """Sweep fires older than 24h with no reply, apply ``-0.15`` per
    decision, mark each row with the sentinel so a second pass is a
    no-op.

    Returns the number of decisions newly settled.
    """
    now = now_fn()
    cutoff = now - timedelta(hours=REPLY_WINDOW_HOURS)

    over_window = list(
        db.exec(
            select(ProactiveDecision).where(
                ProactiveDecision.persona_id == persona_id,
                ProactiveDecision.user_id == user_id,
                ProactiveDecision.action == "fire",
                ProactiveDecision.timestamp < cutoff,
                ProactiveDecision.user_replied_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    if not over_window:
        return 0

    state = _load_or_init_state(
        db, persona_id=persona_id, user_id=user_id, now=now
    )

    settled = 0
    for d in over_window:
        d.user_replied_at = NOT_REPLIED_SENTINEL
        db.add(d)
        _apply_delta(state, DELTA_NOT_REPLIED, now=now)
        settled += 1

    db.add(state)
    db.commit()
    return settled


# ---------------------------------------------------------------------------
# Path 4 · silence decay
# ---------------------------------------------------------------------------


def apply_silence_decay(
    db: Session,
    *,
    persona_id: str,
    user_id: str,
    now_fn: Callable[[], datetime] = datetime.now,
) -> int:
    """Apply ``-0.05`` per day after 7+ days of no user message.

    Idempotent within a calendar day: ``state.last_updated.date()``
    is compared against ``now.date()`` so multiple calls on the same
    day apply the decay at most once.

    Returns 1 if the decay was applied, 0 otherwise.
    """
    now = now_fn()

    last_user_msg = db.exec(
        select(RecallMessage)
        .where(
            RecallMessage.persona_id == persona_id,
            RecallMessage.user_id == user_id,
            RecallMessage.role == MessageRole.USER.value,
        )
        .order_by(RecallMessage.created_at.desc())
        .limit(1)
    ).first()

    if last_user_msg is None or last_user_msg.created_at is None:
        return 0

    days_silent = (now - last_user_msg.created_at).days
    if days_silent < MIN_DAYS_FOR_DECAY:
        return 0

    state = _load_or_init_state(
        db, persona_id=persona_id, user_id=user_id, now=now
    )

    # Already updated today → skip (idempotent per calendar day)
    if state.last_updated.date() >= now.date():
        return 0

    _apply_delta(state, DELTA_DAILY_DECAY, now=now)
    db.add(state)
    db.commit()
    return 1


__all__ = [
    "DELTA_DAILY_DECAY",
    "DELTA_NOT_REPLIED",
    "DELTA_REPLIED",
    "DELTA_USER_INITIATIVE",
    "ENGAGEMENT_INITIAL",
    "ENGAGEMENT_MAX",
    "ENGAGEMENT_MIN",
    "MIN_DAYS_FOR_DECAY",
    "NOT_REPLIED_SENTINEL",
    "REPLY_WINDOW_HOURS",
    "MaintainOutcome",
    "apply_silence_decay",
    "handle_user_message_ingested",
    "maintain_engagement",
    "settle_unreplied_fires",
]


# ---------------------------------------------------------------------------
# Periodic maintainer · the consolidate worker idle branch entry point
# ---------------------------------------------------------------------------


from dataclasses import dataclass as _dataclass  # noqa: E402


@_dataclass
class MaintainOutcome:
    """Counters returned by :func:`maintain_engagement`."""

    user_messages_processed: int = 0
    settled: int = 0
    silence_decayed: int = 0


def maintain_engagement(
    db: Session,
    *,
    persona_id: str,
    user_id: str,
    now_fn: Callable[[], datetime] = datetime.now,
) -> MaintainOutcome:
    """Roll the four engagement paths into a single periodic call.

    The worker idle branch invokes this every cycle. Idempotency
    relies on ``ProactiveState.last_updated`` as a watermark for the
    user-message scan: only messages with ``created_at >
    state.last_updated`` are processed by paths 1+2. Paths 3 (settle)
    and 4 (decay) are independently idempotent (sentinel /
    once-per-day).
    """
    now = now_fn()
    outcome = MaintainOutcome()

    # Path 1+2 · scan user messages that arrived after the watermark.
    state = _load_or_init_state(
        db, persona_id=persona_id, user_id=user_id, now=now
    )
    cutoff = state.last_updated
    new_msgs = list(
        db.exec(
            select(RecallMessage)
            .where(
                RecallMessage.persona_id == persona_id,
                RecallMessage.user_id == user_id,
                RecallMessage.role == MessageRole.USER.value,
                RecallMessage.created_at > cutoff,
            )
            .order_by(RecallMessage.created_at.asc())
        )
    )
    for msg in new_msgs:
        handle_user_message_ingested(
            db,
            msg=msg,
            persona_id=persona_id,
            user_id=user_id,
            now_fn=now_fn,
        )
    outcome.user_messages_processed = len(new_msgs)

    # Path 4 · silence decay (once per local day).
    #
    # Runs BEFORE settle: settle bumps ``state.last_updated`` to
    # ``now`` and apply_silence_decay's idempotency check would then
    # short-circuit ("already updated today"). Decay's own
    # idempotency only blocks a second decay on the same calendar
    # day — running it before settle preserves that semantic.
    outcome.silence_decayed = apply_silence_decay(
        db,
        persona_id=persona_id,
        user_id=user_id,
        now_fn=now_fn,
    )

    # Path 3 · settle 24h-old unreplied fires.
    outcome.settled = settle_unreplied_fires(
        db,
        persona_id=persona_id,
        user_id=user_id,
        now_fn=now_fn,
    )

    return outcome

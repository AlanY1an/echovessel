"""Admin /api/admin/proactive/* routes — decision history + active follow-up events.

Surfaces the v3 ``proactive_decisions`` audit log and the v0.7 follow-up
annotation on ``concept_nodes`` to the admin UI so the user can:

1. See "why she said X today" (history tab) — every fire and every
   suppress with its gate state, so they can debug "she's been quiet,
   why?".
2. See "what she's still planning to follow up on" (events tab) — and
   manually suppress any event whose follow-up they don't want her to
   bring up.

Two boundary translations live here on purpose:

* ``user_replied_at == datetime.min`` is the engagement_updater's
  in-DB sentinel for "settled, no reply within 24h". It MUST NOT cross
  the API boundary as ``year=1`` — the wire format scrubs it to
  ``null`` plus an explicit ``settled_no_reply: True`` boolean.
* ``state=active`` filter is strict: ``follow_up_at IS NOT NULL AND
  superseded_by_id IS NULL AND proactive_suppressed_at IS NULL AND
  deleted_at IS NULL``. A superseded or user-suppressed event is no
  longer eligible for follow-up, so it does not appear in the active
  list.

The cursor encodes ``(timestamp_iso, id)`` so same-millisecond ties
across page boundaries are resolved by the secondary id key.

v3 note · ``follow_up_threads`` is gone; the per-event follow-up state
now lives on ``concept_nodes`` (memory is single source of truth). The
``thread_id`` column on ``proactive_decisions`` is preserved for v2
audit-history compat but never written by v3 code, so the response
serializer does not attempt to dereference it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import func, select

from echovessel.channels.web.routes.admin.helpers import _open_db, _persona_id
from echovessel.core.types import NodeType
from echovessel.memory.models import NOT_REPLIED_SENTINEL, ConceptNode, ProactiveDecision

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire formats
# ---------------------------------------------------------------------------


class DecisionItem(BaseModel):
    """One row in the proactive history list.

    ``settled_no_reply`` is the explicit boolean derived from the in-DB
    sentinel — see module docstring. ``user_replied_at`` is therefore
    a real datetime (user did reply) or ``null`` (waiting OR settled);
    the boolean disambiguates.

    ``phase`` is v3 only — ``pre`` | ``on`` | ``post`` | ``check_N`` |
    ``None`` (legacy). The frontend uses this to colour the row so
    the user can see "this was the pre-event reminder" vs "this was a
    follow-up ping".
    """

    id: int
    decision_id: str
    timestamp: datetime
    trigger_type: str
    thread_id: int | None
    source_event_id: int | None
    phase: str | None
    action: Literal["fire", "suppress"]
    suppress_reason: str | None
    message_text: str | None
    sent_message_id: int | None
    user_replied_at: datetime | None
    settled_no_reply: bool
    voice_used: bool | None
    voice_error: str | None
    gate_state_snapshot: dict[str, Any] | None

    source_event_description: str | None = None


class DecisionsResponse(BaseModel):
    items: list[DecisionItem]
    total: int
    has_more: bool
    cursor: str | None


class EventItem(BaseModel):
    """One row in the proactive follow-up events list.

    Surfaces the v0.7 follow-up annotation columns on ``concept_nodes``
    plus a derived ``fired_at`` timestamp (earliest matching fire in
    ``proactive_decisions``) so the user can see whether the scheduler
    has already pinged this event.
    """

    id: int
    description: str
    follow_up_at: datetime | None
    follow_up_hint: str | None
    estimated_arc_days: int | None
    advance_pre_hours: int | None
    advance_post_hours: int | None
    proactive_suppressed_at: datetime | None
    superseded_by_id: int | None
    fired_at: datetime | None
    closed: bool
    created_at: datetime


class EventsResponse(BaseModel):
    items: list[EventItem]
    total: int


# ---------------------------------------------------------------------------
# Cursor helpers · pitfall #3
# ---------------------------------------------------------------------------


def _encode_cursor(timestamp: datetime, row_id: int) -> str:
    return f"{timestamp.isoformat()}|{row_id}"


def _decode_cursor(raw: str) -> tuple[datetime, int]:
    """Parse a cursor produced by ``_encode_cursor``.

    Raises ``HTTPException(400)`` on malformed input — the cursor is a
    user-supplied string from the previous page response, so we treat
    it as untrusted boundary data.
    """

    try:
        ts_str, id_str = raw.rsplit("|", 1)
        return datetime.fromisoformat(ts_str), int(id_str)
    except (ValueError, AttributeError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {e}",
        ) from e


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _serialize_decision(db: Any, row: ProactiveDecision) -> DecisionItem:
    raw_replied = row.user_replied_at
    if raw_replied is not None and raw_replied <= NOT_REPLIED_SENTINEL:
        user_replied_at: datetime | None = None
        settled_no_reply = True
    else:
        user_replied_at = raw_replied
        settled_no_reply = False

    snapshot: dict[str, Any] | None = None
    if row.gate_state_snapshot:
        try:
            decoded = json.loads(row.gate_state_snapshot)
            if isinstance(decoded, dict):
                snapshot = decoded
        except (TypeError, ValueError) as e:
            log.warning(
                "gate_state_snapshot for decision %s is not valid JSON: %s",
                row.id,
                e,
            )

    source_event_description: str | None = None
    if row.source_event_id is not None:
        event = db.get(ConceptNode, row.source_event_id)
        if event is not None:
            source_event_description = event.description

    assert row.id is not None
    action: Literal["fire", "suppress"] = "fire" if row.action == "fire" else "suppress"

    return DecisionItem(
        id=row.id,
        decision_id=row.decision_id,
        timestamp=row.timestamp,
        trigger_type=row.trigger_type,
        thread_id=row.thread_id,
        source_event_id=row.source_event_id,
        phase=row.phase,
        action=action,
        suppress_reason=row.suppress_reason,
        message_text=row.message_text,
        sent_message_id=row.sent_message_id,
        user_replied_at=user_replied_at,
        settled_no_reply=settled_no_reply,
        voice_used=row.voice_used,
        voice_error=row.voice_error,
        gate_state_snapshot=snapshot,
        source_event_description=source_event_description,
    )


def _serialize_event(db: Any, row: ConceptNode) -> EventItem:
    """Convert a ConceptNode with follow-up annotation into wire-format.

    Derives ``fired_at`` from the earliest ``fire``-action decision row
    pointing at this event, and ``closed`` from supersede / suppress
    state. The DB row already carries ``follow_up_at`` etc. directly.
    """

    fired_at: datetime | None = db.exec(
        select(func.min(ProactiveDecision.timestamp)).where(
            ProactiveDecision.source_event_id == row.id,
            ProactiveDecision.action == "fire",
        )
    ).first()

    closed = row.superseded_by_id is not None or row.proactive_suppressed_at is not None

    assert row.id is not None
    return EventItem(
        id=row.id,
        description=row.description,
        follow_up_at=row.follow_up_at,
        follow_up_hint=row.follow_up_hint,
        estimated_arc_days=row.estimated_arc_days,
        advance_pre_hours=row.advance_pre_hours,
        advance_post_hours=row.advance_post_hours,
        proactive_suppressed_at=row.proactive_suppressed_at,
        superseded_by_id=row.superseded_by_id,
        fired_at=fired_at,
        closed=closed,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_proactive_routes(
    router: APIRouter,
    *,
    runtime: Any,
    user_id: str,
) -> None:
    @router.get(
        "/api/admin/proactive/decisions",
        response_model=DecisionsResponse,
    )
    async def list_proactive_decisions(
        action: Literal["fire", "suppress", "all"] = "all",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ) -> DecisionsResponse:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            stmt = (
                select(ProactiveDecision)
                .where(
                    ProactiveDecision.persona_id == persona_id,
                    ProactiveDecision.user_id == user_id,
                )
                .order_by(
                    ProactiveDecision.timestamp.desc(),  # type: ignore[attr-defined]
                    ProactiveDecision.id.desc(),  # type: ignore[attr-defined]
                )
            )
            if action != "all":
                stmt = stmt.where(ProactiveDecision.action == action)
            if since is not None:
                stmt = stmt.where(ProactiveDecision.timestamp >= since)
            if until is not None:
                stmt = stmt.where(ProactiveDecision.timestamp <= until)
            if cursor is not None:
                cursor_ts, cursor_id = _decode_cursor(cursor)
                stmt = stmt.where(
                    (ProactiveDecision.timestamp < cursor_ts)
                    | (
                        (ProactiveDecision.timestamp == cursor_ts)
                        & (ProactiveDecision.id < cursor_id)
                    )
                )

            rows = list(db.exec(stmt.limit(limit + 1)).all())
            has_more = len(rows) > limit
            rows = rows[:limit]

            items = [_serialize_decision(db, row) for row in rows]

            next_cursor: str | None = None
            if has_more and rows:
                last = rows[-1]
                assert last.id is not None
                next_cursor = _encode_cursor(last.timestamp, last.id)

            total_stmt = select(func.count(ProactiveDecision.id)).where(
                ProactiveDecision.persona_id == persona_id,
                ProactiveDecision.user_id == user_id,
            )
            if action != "all":
                total_stmt = total_stmt.where(ProactiveDecision.action == action)
            if since is not None:
                total_stmt = total_stmt.where(ProactiveDecision.timestamp >= since)
            if until is not None:
                total_stmt = total_stmt.where(ProactiveDecision.timestamp <= until)
            total = int(db.exec(total_stmt).one() or 0)

            return DecisionsResponse(
                items=items,
                total=total,
                has_more=has_more,
                cursor=next_cursor,
            )

    @router.get(
        "/api/admin/proactive/events",
        response_model=EventsResponse,
    )
    async def list_proactive_events(
        state: Literal["active", "fired", "closed", "all"] = "active",
        limit: int = Query(50, ge=1, le=200),
    ) -> EventsResponse:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            stmt = (
                select(ConceptNode)
                .where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.user_id == user_id,
                    ConceptNode.type == NodeType.EVENT,
                    ConceptNode.follow_up_at.is_not(None),  # type: ignore[union-attr]
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(ConceptNode.follow_up_at.asc())  # type: ignore[attr-defined]
            )

            if state == "active":
                stmt = stmt.where(
                    ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                    ConceptNode.proactive_suppressed_at.is_(None),  # type: ignore[union-attr]
                )
            elif state == "closed":
                stmt = stmt.where(
                    or_(
                        ConceptNode.superseded_by_id.is_not(None),  # type: ignore[union-attr]
                        ConceptNode.proactive_suppressed_at.is_not(None),  # type: ignore[union-attr]
                    )
                )
            elif state == "fired":
                # Surface only events the scheduler has already pinged at
                # least once; computed via the EXISTS subquery to avoid a
                # duplicate join row per fire.
                fired_subquery = (
                    select(ProactiveDecision.source_event_id)
                    .where(
                        ProactiveDecision.action == "fire",
                        ProactiveDecision.source_event_id.is_not(None),  # type: ignore[union-attr]
                    )
                    .distinct()
                )
                stmt = stmt.where(ConceptNode.id.in_(fired_subquery))  # type: ignore[union-attr]
            # state == "all": no extra filter beyond persona + follow_up_at

            rows = list(db.exec(stmt.limit(limit)).all())
            items = [_serialize_event(db, row) for row in rows]

            return EventsResponse(items=items, total=len(items))

    @router.delete(
        "/api/admin/memory/events/{event_id}/proactive-follow-up",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def suppress_proactive_follow_up(event_id: int) -> None:
        """User-initiated "don't bring this up" — sets ``proactive_suppressed_at``.

        Distinct from natural close (``superseded_by_id`` set by Phase B
        when the user reports an outcome). Suppressing does NOT delete
        the event itself; the event row stays visible in memory and the
        admin events list under ``state=closed``.
        """

        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            event = db.get(ConceptNode, event_id)
            if event is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="event not found",
                )
            if event.persona_id != persona_id or event.user_id != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="event not owned by current persona/user",
                )
            if event.deleted_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="event already deleted",
                )
            if event.proactive_suppressed_at is not None:
                # Idempotent — a second suppress is a no-op, not an error
                return

            event.proactive_suppressed_at = datetime.now()
            db.add(event)
            db.commit()


__all__ = ["register_proactive_routes"]

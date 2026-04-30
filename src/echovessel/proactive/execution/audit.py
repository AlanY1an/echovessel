"""Audit trail · proactive v2.

Every call to :meth:`PolicyEngine.evaluate` produces exactly one
:class:`ProactiveDecision`, whether the action is ``send`` or
``skip``. The :class:`SQLiteAuditSink` defined here persists those
rows to the ``proactive_decisions`` table created by the v0.6 schema
migration; the admin "主动消息历史" tab reads back through the same
table.

The runtime tick loop cannot tolerate audit raising — ``record`` and
``update_latest`` swallow exceptions at ERROR level rather than
propagate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.proactive.core.base import (
    ActionType,
    ProactiveDecision,
)
from echovessel.proactive.core.models import (
    ProactiveDecision as PersistedDecision,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers · v1 dataclass ↔ v0.6 ``proactive_decisions`` row
# ---------------------------------------------------------------------------


def _isoformat_or_none(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _walk_jsonify(obj: Any) -> Any:
    """Recursively prepare a value for ``json.dumps`` — ``datetime``
    becomes ISO-8601, lists / tuples / dicts are walked."""
    if isinstance(obj, dict):
        return {k: _walk_jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk_jsonify(v) for v in obj]
    return _isoformat_or_none(obj)


def _action_to_row(action: str) -> str:
    """Map v1 ``ActionType`` value to the v0.6 ``action`` column vocab.

    The dataclass uses ``send`` / ``skip``; the SQLite audit table uses
    ``fire`` / ``suppress`` because they read more naturally in the
    admin UI.
    """
    if action == ActionType.SEND.value:
        return "fire"
    return "suppress"


def _row_to_action(action: str) -> str:
    """Inverse of :func:`_action_to_row`."""
    if action == "fire":
        return ActionType.SEND.value
    return ActionType.SKIP.value


def _trigger_to_type(trigger: str | None) -> str:
    """Map the v1 ``TriggerReason`` value onto the v0.6 ``trigger_type``
    vocab. v0.6 only knows ``thread_due`` and ``high_emotional_event``;
    older trigger reasons collapse onto ``thread_due`` so the audit row
    still records "this attempt fell out of proactive's natural inbox".
    The real differentiation lives on ``suppress_reason``.
    """
    if trigger == "high_emotional_event":
        return "high_emotional_event"
    return "thread_due"


def _serialise_gate_snapshot(payload: object) -> str | None:
    """Serialise the policy snapshot to a JSON string for storage in
    the ``gate_state_snapshot`` TEXT column. Returns ``None`` when the
    payload is empty / falsy."""
    if not payload:
        return None
    return json.dumps(_walk_jsonify(payload), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# SQLiteAuditSink
# ---------------------------------------------------------------------------


@dataclass
class SQLiteAuditSink:
    """Persist proactive decisions into the ``proactive_decisions`` table.

    Implements the duck-typed :class:`AuditSink` Protocol used by the
    scheduler: ``record`` / ``update_latest`` / ``recent_sends`` /
    ``count_sends_in_last_24h``.

    The mapping from the v1 ``ProactiveDecision`` dataclass to the v0.6
    SQLModel row drops the diagnostic fields the v0.6 schema does not
    carry (``send_ok`` / ``send_error`` / ``rationale`` /
    ``llm_latency_ms`` / token counts). The admin UI never surfaced
    them, so the audit table stays lean on purpose; anything the admin
    "主动消息历史" tab needs that isn't a column lives in
    ``gate_state_snapshot`` (a JSON blob).
    """

    db_factory: Callable[[], DbSession]

    def record(self, decision: ProactiveDecision) -> None:
        """Write one row. Failures are logged at ERROR and swallowed
        — the scheduler tick loop never raises through audit."""
        try:
            with self.db_factory() as db:
                db.add(self._to_row(decision))
                db.commit()
        except Exception as e:  # noqa: BLE001 — boundary
            log.error(
                "SQLiteAuditSink record failed for %s: %s",
                decision.decision_id,
                e,
            )

    def update_latest(
        self,
        decision_id: str,
        *,
        send_ok: bool | None = None,
        send_error: str | None = None,
        ingest_message_id: int | None = None,
        delivery: str | None = None,
        voice_used: bool | None = None,
        voice_error: str | None = None,
        llm_latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Update the audit row identified by ``decision_id``.

        Diagnostic fields the v0.6 schema doesn't carry
        (``send_ok`` / ``send_error`` / ``llm_latency_ms`` / token
        counts / ``delivery``) are silently ignored; the schema records
        only what the admin UI surfaces.
        """
        try:
            with self.db_factory() as db:
                row = db.exec(
                    select(PersistedDecision)
                    .where(PersistedDecision.decision_id == decision_id)
                    .order_by(PersistedDecision.timestamp.desc())  # type: ignore[attr-defined]
                    .limit(1)
                ).first()
                if row is None:
                    return
                if ingest_message_id is not None:
                    row.sent_message_id = ingest_message_id
                if voice_used is not None:
                    row.voice_used = voice_used
                if voice_error is not None:
                    row.voice_error = voice_error
                db.add(row)
                db.commit()
        except Exception as e:  # noqa: BLE001 — boundary
            log.error(
                "SQLiteAuditSink update_latest failed for %s: %s",
                decision_id,
                e,
            )

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        if last_n <= 0:
            return []
        with self.db_factory() as db:
            rows = list(
                db.exec(
                    select(PersistedDecision)
                    .where(PersistedDecision.action == "fire")
                    .order_by(PersistedDecision.timestamp.desc())  # type: ignore[attr-defined]
                    .limit(last_n)
                )
            )
        return [self._from_row(r) for r in rows]

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        cutoff = now - timedelta(hours=24)
        with self.db_factory() as db:
            n = db.exec(
                select(func.count(PersistedDecision.id)).where(
                    PersistedDecision.action == "fire",
                    PersistedDecision.timestamp >= cutoff,
                    PersistedDecision.timestamp <= now,
                )
            ).one()
        return int(n or 0)

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _to_row(self, decision: ProactiveDecision) -> PersistedDecision:
        return PersistedDecision(
            decision_id=decision.decision_id,
            timestamp=decision.timestamp,
            persona_id=decision.persona_id,
            user_id=decision.user_id,
            trigger_type=_trigger_to_type(decision.trigger),
            thread_id=decision.trigger_payload.get("thread_id")
            if decision.trigger_payload
            else None,
            source_event_id=decision.trigger_payload.get("source_event_id")
            if decision.trigger_payload
            else None,
            action=_action_to_row(decision.action),
            suppress_reason=decision.skip_reason,
            message_text=decision.message_text,
            sent_message_id=decision.ingest_message_id,
            voice_used=decision.voice_used,
            voice_error=decision.voice_error,
            gate_state_snapshot=_serialise_gate_snapshot(
                decision.policy_snapshot
            ),
            created_at=decision.timestamp,
        )

    def _from_row(self, row: PersistedDecision) -> ProactiveDecision:
        return ProactiveDecision(
            decision_id=row.decision_id,
            persona_id=row.persona_id,
            user_id=row.user_id,
            timestamp=row.timestamp,
            trigger=row.trigger_type,
            trigger_payload={
                "thread_id": row.thread_id,
                "source_event_id": row.source_event_id,
            },
            action=_row_to_action(row.action),
            skip_reason=row.suppress_reason,
            message_text=row.message_text,
            ingest_message_id=row.sent_message_id,
            voice_used=bool(row.voice_used) if row.voice_used is not None else False,
            voice_error=row.voice_error,
        )


__all__ = ["SQLiteAuditSink"]

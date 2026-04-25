"""Per-session dev-mode trace recorder for the consolidate pipeline (Spec 4).

Mirrors :class:`echovessel.runtime.turn.tracer.TurnTracer` but the shape
is a set of six phase JSON blobs (A–G) rather than a timeline. The
consolidate pipeline is a strictly sequential state machine so a
timeline would add no information; the phase-keyed layout matches the
drawer UI which renders each phase as a standalone card.

Hot-path invariants:

- :class:`NullConsolidateTracer` is a complete no-op. When dev_trace is
  disabled every record/capture call is a bytecode-level NOP.

- :meth:`ConsolidateTracer.persist` is best-effort. The consolidate
  worker wraps it in try/except so a failing trace write never unwinds
  the session-close transition.

- Phase_b always records ``junction_rejects``, even when empty. Commit
  aaeb9f9 added a defensive junction drop (entity surface-form not in
  event description) — this is exactly the class of silent behaviour
  the trace makes visible again. An empty array means "nothing was
  rejected"; a missing field means "this run never reached phase B".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session as DbSession

log = logging.getLogger(__name__)


__all__ = [
    "ConsolidateTracer",
    "NullConsolidateTracer",
    "make_consolidate_tracer",
]


@dataclass
class ConsolidateTracer:
    """Record the six consolidate phases for one session."""

    session_id: str

    phase_a: dict[str, Any] | None = None
    phase_b: dict[str, Any] | None = None
    phase_c: dict[str, Any] | None = None
    phase_d: dict[str, Any] | None = None
    phase_e: dict[str, Any] | None = None
    phase_f: dict[str, Any] | None = None
    phase_g: dict[str, Any] | None = None

    finished_at: datetime | None = field(default=None)

    def __bool__(self) -> bool:  # pragma: no cover — trivial
        return True

    def record_phase_a(self, *, is_trivial: bool, reason: str = "") -> None:
        """Phase A · trivial-skip decision."""
        self.phase_a = {"is_trivial": bool(is_trivial), "reason": reason}

    def record_phase_b(
        self,
        *,
        extract_prompt: str | None = None,
        extract_response_raw: str | None = None,
        events_created: list[dict[str, Any]] | None = None,
        entities_resolved: list[dict[str, Any]] | None = None,
        junction_writes: list[dict[str, Any]] | None = None,
        junction_rejects: list[dict[str, Any]] | None = None,
        session_mood_signal: dict[str, Any] | None = None,
        commitments_extracted: list[dict[str, Any]] | None = None,
    ) -> None:
        """Phase B · extraction. ``junction_rejects`` is mandatory even
        if empty — this is the defensive-drop path from commit aaeb9f9
        that we want to make visible whenever it fires.
        """
        self.phase_b = {
            "extract_prompt": extract_prompt,
            "extract_response_raw": extract_response_raw,
            "events_created": list(events_created or []),
            "entities_resolved": list(entities_resolved or []),
            "junction_writes": list(junction_writes or []),
            "junction_rejects": list(junction_rejects or []),
            "session_mood_signal": session_mood_signal,
            "commitments_extracted": list(commitments_extracted or []),
        }

    def record_phase_c(self, *, shock_event_id: int | None) -> None:
        """Phase C · SHOCK detection result."""
        self.phase_c = {"shock_event_id": shock_event_id}

    def record_phase_d(
        self, *, timer_due: bool, reflections_last_24h: int
    ) -> None:
        """Phase D · TIMER/rate gate."""
        self.phase_d = {
            "timer_due": bool(timer_due),
            "reflections_last_24h": int(reflections_last_24h),
        }

    def record_phase_e(
        self,
        *,
        reflection_gate: str,
        reflect_prompt: str | None = None,
        reflect_response_raw: str | None = None,
        thoughts_created: list[dict[str, Any]] | None = None,
    ) -> None:
        """Phase E · reflection execution. ``reflection_gate`` is one of
        ``'shock'``, ``'timer'``, ``'hard_gate_hit'``, or ``'none'``.
        """
        self.phase_e = {
            "reflection_gate": reflection_gate,
            "reflect_prompt": reflect_prompt,
            "reflect_response_raw": reflect_response_raw,
            "thoughts_created": list(thoughts_created or []),
        }

    def record_phase_f(
        self,
        *,
        status: str,
        extracted_at: datetime | None,
        close_trigger: str,
    ) -> None:
        """Phase F · session-status flip."""
        self.phase_f = {
            "status": status,
            "extracted_at": extracted_at.isoformat() if extracted_at else None,
            "close_trigger": close_trigger,
        }

    def record_phase_g(
        self,
        *,
        ran: bool,
        cool_down_ok: bool | None = None,
        budget_check: str | None = None,
        slow_cycle_prompt: str | None = None,
        slow_cycle_response_raw: str | None = None,
        nodes_written: list[dict[str, Any]] | None = None,
        self_append_attempted: bool = False,
    ) -> None:
        """Phase G · slow_cycle. ``ran=False`` with a filled
        ``budget_check`` covers the throttled path."""
        self.phase_g = {
            "ran": bool(ran),
            "cool_down_ok": cool_down_ok,
            "budget_check": budget_check,
            "slow_cycle_prompt": slow_cycle_prompt,
            "slow_cycle_response_raw": slow_cycle_response_raw,
            "nodes_written": list(nodes_written or []),
            "self_append_attempted": bool(self_append_attempted),
        }

    def persist(self, db: DbSession) -> None:
        """Write the single ``session_traces`` row. Uses INSERT OR REPLACE
        so a retry after a partial write is harmless.
        """

        def _j(v: dict[str, Any] | None) -> str | None:
            return None if v is None else json.dumps(v, ensure_ascii=False, default=str)

        finished_at = self.finished_at or datetime.utcnow()
        db.execute(
            text(
                "INSERT OR REPLACE INTO session_traces ("
                "session_id, finished_at, "
                "phase_a, phase_b, phase_c, phase_d, phase_e, phase_f, phase_g"
                ") VALUES ("
                ":session_id, :finished_at, "
                ":phase_a, :phase_b, :phase_c, :phase_d, :phase_e, :phase_f, :phase_g)"
            ),
            {
                "session_id": self.session_id,
                "finished_at": finished_at,
                "phase_a": _j(self.phase_a),
                "phase_b": _j(self.phase_b),
                "phase_c": _j(self.phase_c),
                "phase_d": _j(self.phase_d),
                "phase_e": _j(self.phase_e),
                "phase_f": _j(self.phase_f),
                "phase_g": _j(self.phase_g),
            },
        )
        db.commit()


class NullConsolidateTracer:
    """No-op tracer — zero hot-path overhead when dev_trace is off."""

    __slots__ = ()

    def __bool__(self) -> bool:  # pragma: no cover — trivial
        return False

    def __setattr__(self, name: str, value: Any) -> None:
        return

    def __getattr__(self, name: str) -> Any:
        return None

    def record_phase_a(self, **kwargs: Any) -> None:
        return

    def record_phase_b(self, **kwargs: Any) -> None:
        return

    def record_phase_c(self, **kwargs: Any) -> None:
        return

    def record_phase_d(self, **kwargs: Any) -> None:
        return

    def record_phase_e(self, **kwargs: Any) -> None:
        return

    def record_phase_f(self, **kwargs: Any) -> None:
        return

    def record_phase_g(self, **kwargs: Any) -> None:
        return

    def persist(self, db: DbSession) -> None:
        return


def make_consolidate_tracer(
    *, enabled: bool, session_id: str
) -> ConsolidateTracer | NullConsolidateTracer:
    if not enabled:
        return NullConsolidateTracer()
    return ConsolidateTracer(session_id=session_id)

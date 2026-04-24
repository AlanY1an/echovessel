"""Per-turn dev-mode trace recorder (Spec 4 · plan §4).

Captures a 12-stage waterfall for one invocation of
``runtime.interaction.assemble_turn`` — each stage carries its wall-
clock duration, its offset from turn start, and a detail dict the
instrumentation site controls. The drawer on the chat page renders the
list top-to-bottom and lets the user (developer) drill into any stage's
payload, view the verbatim system/user prompts, and inspect the
retrieval candidate table with its per-candidate score components.

Hot-path invariants:

- :class:`NullTurnTracer` is a complete no-op. When ``cfg.dev_trace``
  is disabled the assemble_turn flow hits this class for every
  ``stage_start`` / ``stage_end`` / attribute assignment, so the
  overhead MUST be a single bytecode-level NOP. The Null class uses
  ``__slots__=()`` and overrides ``__setattr__`` to drop assignments
  on the floor.

- :meth:`TurnTracer.stage_end` tolerates a missing ``stage_start``
  (pending dict pop returns ``None`` → silently skip). This means a
  misbehaving instrumentation site degrades to "missing row in the
  timeline" rather than raising mid-turn.

- :meth:`TurnTracer.persist` is best-effort: the caller wraps it in a
  ``try``/``except`` so a trace-write failure never breaks the user-
  visible reply. A broken write leaves no row; the next turn is fine.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session as DbSession

log = logging.getLogger(__name__)


__all__ = [
    "TurnStep",
    "TurnTracer",
    "NullTurnTracer",
    "make_turn_tracer",
]


@dataclass(slots=True)
class TurnStep:
    """One row on the per-turn waterfall timeline."""

    stage: str
    t_ms: int
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "t_ms": int(self.t_ms),
            "duration_ms": int(self.duration_ms),
            "detail": dict(self.detail),
        }


@dataclass
class TurnTracer:
    """Recording tracer — captures stages + header fields for one turn.

    All header fields default to ``None`` so the caller can fill them
    in as it learns them. :meth:`persist` writes the row at turn end.
    """

    turn_id: str
    persona_id: str
    user_id: str
    channel_id: str
    started_at: datetime

    system_prompt: str | None = None
    user_prompt: str | None = None
    retrieval: list[dict[str, Any]] | None = None
    pinned_thoughts: dict[str, list[dict[str, Any]]] | None = None
    entity_alias_hits: list[dict[str, Any]] | None = None
    episodic_state: dict[str, Any] | None = None
    llm_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    first_token_ms: int | None = None
    finished_at: datetime | None = None

    _steps: list[TurnStep] = field(default_factory=list)
    _pending: dict[str, datetime] = field(default_factory=dict)

    # The class is truthy in `if tracer:` checks so callers can use the
    # same variable for both recording and null variants.
    def __bool__(self) -> bool:  # pragma: no cover — trivial
        return True

    def stage_start(self, name: str) -> None:
        """Mark the start of a stage. Idempotent on repeat names —
        the second start overwrites the first; the paired stage_end
        still closes exactly one step.
        """
        self._pending[name] = datetime.utcnow()

    def stage_end(self, name: str, **detail: Any) -> None:
        """Close the matching ``stage_start`` and emit a :class:`TurnStep`.

        If no matching start is registered we silently skip — the spec
        requires `stage_start`/`stage_end` mismatches to degrade
        gracefully rather than raise mid-turn.
        """
        start = self._pending.pop(name, None)
        if start is None:
            return
        ended = datetime.utcnow()
        duration_ms = max(0, int((ended - start).total_seconds() * 1000))
        t_ms = max(0, int((start - self.started_at).total_seconds() * 1000))
        self._steps.append(
            TurnStep(stage=name, t_ms=t_ms, duration_ms=duration_ms, detail=dict(detail))
        )

    def steps(self) -> list[TurnStep]:
        """Return a copy of the current step list (tests read this)."""
        return list(self._steps)

    def add_synthetic_step(
        self,
        stage: str,
        *,
        t_ms: int,
        duration_ms: int,
        **detail: Any,
    ) -> None:
        """Record a stage that wasn't bracketed by ``stage_start``/``stage_end``.

        Used by the caller for the "debounce" stage, which happens in
        the channel adapter before assemble_turn gets control — the
        waterfall still wants to show the reconstructed window.
        """
        self._steps.append(
            TurnStep(
                stage=stage,
                t_ms=max(0, int(t_ms)),
                duration_ms=max(0, int(duration_ms)),
                detail=dict(detail),
            )
        )

    def persist(self, db: DbSession) -> None:
        """Write the single ``turn_traces`` row for this turn.

        Best-effort: the caller wraps in try/except. We use
        ``INSERT OR REPLACE`` so a rare turn_id collision (or a
        re-entry after a partial write) is harmless.
        """
        steps_json = json.dumps([s.to_json() for s in self._steps], ensure_ascii=False)
        retrieval_json = (
            json.dumps(self.retrieval, ensure_ascii=False, default=str)
            if self.retrieval is not None
            else None
        )
        pinned_json = (
            json.dumps(self.pinned_thoughts, ensure_ascii=False, default=str)
            if self.pinned_thoughts is not None
            else None
        )
        alias_json = (
            json.dumps(self.entity_alias_hits, ensure_ascii=False, default=str)
            if self.entity_alias_hits is not None
            else None
        )
        episodic_json = (
            json.dumps(self.episodic_state, ensure_ascii=False, default=str)
            if self.episodic_state is not None
            else None
        )

        db.execute(
            text(
                "INSERT OR REPLACE INTO turn_traces ("
                "turn_id, persona_id, user_id, channel_id, "
                "started_at, finished_at, "
                "system_prompt, user_prompt, "
                "retrieval, pinned_thoughts, entity_alias_hits, episodic_state, "
                "llm_model, input_tokens, output_tokens, "
                "duration_ms, first_token_ms, steps"
                ") VALUES ("
                ":turn_id, :persona_id, :user_id, :channel_id, "
                ":started_at, :finished_at, "
                ":system_prompt, :user_prompt, "
                ":retrieval, :pinned_thoughts, :entity_alias_hits, :episodic_state, "
                ":llm_model, :input_tokens, :output_tokens, "
                ":duration_ms, :first_token_ms, :steps)"
            ),
            {
                "turn_id": self.turn_id,
                "persona_id": self.persona_id,
                "user_id": self.user_id,
                "channel_id": self.channel_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "system_prompt": self.system_prompt,
                "user_prompt": self.user_prompt,
                "retrieval": retrieval_json,
                "pinned_thoughts": pinned_json,
                "entity_alias_hits": alias_json,
                "episodic_state": episodic_json,
                "llm_model": self.llm_model,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "duration_ms": self.duration_ms,
                "first_token_ms": self.first_token_ms,
                "steps": steps_json,
            },
        )
        db.commit()


class NullTurnTracer:
    """No-op tracer — zero hot-path overhead when dev_trace is off.

    Every attribute assignment is dropped and every method is a noop.
    The class uses ``__slots__=()`` so accidental state cannot leak
    across turns (there is no dict to write into).
    """

    __slots__ = ()

    def __bool__(self) -> bool:  # pragma: no cover — trivial
        return False

    # No attribute storage — both reads and writes are inert so callers
    # can unconditionally do ``tracer.system_prompt = x`` or
    # ``tracer.retrieval = [...]`` without paying for the write.
    def __setattr__(self, name: str, value: Any) -> None:
        return

    def __getattr__(self, name: str) -> Any:
        # We only land here for names not in the zero-slot class body,
        # which is everything the caller might reach for. Return None
        # so ``if tracer.finished_at:`` style checks behave sanely.
        return None

    def stage_start(self, name: str) -> None:  # noqa: D401 — intentional no-op
        return

    def stage_end(self, name: str, **detail: Any) -> None:  # noqa: D401
        return

    def add_synthetic_step(
        self,
        stage: str,
        *,
        t_ms: int,
        duration_ms: int,
        **detail: Any,
    ) -> None:
        return

    def steps(self) -> list[TurnStep]:
        return []

    def persist(self, db: DbSession) -> None:  # noqa: D401
        return


def make_turn_tracer(
    *,
    enabled: bool,
    turn_id: str | None,
    persona_id: str,
    user_id: str,
    channel_id: str,
    started_at: datetime | None = None,
) -> TurnTracer | NullTurnTracer:
    """Construct either a recording or a null tracer based on ``enabled``.

    ``turn_id=None`` auto-mints a UUID4 — convenient when the runtime
    has a turn envelope without an externally-supplied id (test seams
    use this path). Production always passes ``turn.turn_id``.
    """
    if not enabled:
        return NullTurnTracer()
    return TurnTracer(
        turn_id=turn_id or str(uuid.uuid4()),
        persona_id=persona_id,
        user_id=user_id,
        channel_id=channel_id,
        started_at=started_at or datetime.utcnow(),
    )

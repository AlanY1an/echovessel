"""Slow-tick reflection phase · memory-layer writer (plan §7 · Spec 6).

Runs at the tail of ``consolidate_worker._process_one`` (phase G) when
the session has been successfully marked CLOSED. Aggregates recent
events across sessions and produces typed thoughts + expectations.

v0.5 note: slow_cycle MUST NOT touch L1 core_blocks. Any "persona is
thinking about itself" output lives on L4.thought[subject='persona'],
surfaced via ``retrieve.force_load_persona_thoughts`` + the
``# How you see yourself lately`` prompt section. L1 is the
human-authored identity layer from v0.5 onward.

Airi anti-pattern guards enforced here (plan §7.5):
  - No free-form narrative: output must match the typed schema.
  - No new goal / external action / web call: the writer cannot
    produce anything beyond typed ConceptNode rows.
  - No L1 write path: slow_cycle never appends / edits core_blocks.
  - No recursion: a failed cycle does NOT reschedule itself; the next
    session close (or operator SQL-flip) is the only retrigger.
  - No self-scheduling: ``run_slow_cycle`` never calls itself.
  - Token walls: input truncated head-first to fit; output that
    exceeds the cap is parsed as-is or dropped — never retried.
  - ``reasoning_event_ids`` MUST be non-empty (raised at schema gate
    + again at write time in ``bulk_create_expectations``).

The cycle is best-effort. Any exception raised here is caught by the
consolidate worker's G phase, logged at WARNING, and the session stays
CLOSED. Slow cycle failure MUST NOT unwind extraction or reflection.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.consolidate_tracer import (
    ConsolidateTracer,
    NullConsolidateTracer,
)
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    Persona,
    Session,
    SlowCycleStats,
)
from echovessel.memory.observers import MemoryEventObserver, _fire_lifecycle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — cfg.slow_tick.* overrides at the runtime layer
# ---------------------------------------------------------------------------

DEFAULT_COOL_DOWN_MINUTES: int = 30
DEFAULT_DAILY_CAP: int = 36
DEFAULT_DAILY_INPUT_TOKEN_BUDGET: int = 150_000
DEFAULT_DAILY_OUTPUT_TOKEN_BUDGET: int = 30_000
DEFAULT_INPUT_TOKEN_LIMIT: int = 8_000
DEFAULT_OUTPUT_TOKEN_LIMIT: int = 1_000

# Cheap-but-sensible token estimate — not tiktoken-perfect, but the
# purpose here is budgeting not billing. 1 token ≈ 4 bytes covers most
# mixed-language text (CJK lands a bit high, ASCII a bit low, both
# correlate with ground truth well enough for the budget gate).
_AVG_BYTES_PER_TOKEN: int = 4


# SHOCK threshold — mirrors ``memory.consolidate.SHOCK_IMPACT_THRESHOLD``.
# Imported indirectly via ``|emotional_impact| >= 8`` here instead of
# pulling it in to avoid a circular dependency.
_SHOCK_IMPACT_THRESHOLD: int = 8


# Recent events lookback fallback when ``last_slow_tick_at`` is None —
# the first cycle for a persona needs a starting point. 7 days covers
# a reasonable warm-up corpus for a freshly-deployed daemon.
_FIRST_CYCLE_LOOKBACK_DAYS: int = 7


# Default transcript directory — overridable by the runtime caller so
# the write target matches ``<data_dir>/slow_tick_transcripts/``.
DEFAULT_TRANSCRIPT_DIR: Path = Path("develop-docs/slow_tick_transcripts")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SlowCycleBudgetExceeded(Exception):  # noqa: N818 — not an error suffix, this is a throttle signal
    """Raised when the per-day cap or token budget would be breached.

    The consolidate worker's G phase catches this and logs a WARNING —
    the session stays CLOSED. No retry. The next cycle that actually
    fits the budget will run when the budget naturally resets at
    midnight local time.
    """


# ---------------------------------------------------------------------------
# Public API types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SlowCycleThoughtInput:
    """A thought produced by slow_cycle that is about to be written.

    Mirrors the prompt-layer ``RawSlowThought`` but lives here so memory
    doesn't import prompts. Runtime bridges the two via
    ``runtime.prompts_wiring.make_slow_cycle_fn``.
    """

    description: str
    filling_event_ids: list[int]
    emotional_impact: int = 0


@dataclass(slots=True)
class SlowCycleExpectationInput:
    """Forward-looking prediction (L4 expectation, subject=persona)."""

    about_text: str
    prediction_text: str
    due_at: datetime | None = None
    reasoning_event_ids: list[int] = field(default_factory=list)
    emotional_impact: int = 0


@dataclass(slots=True)
class SlowCycleOutput:
    """Structured output the LLM callable promises to return.

    Shape matches the prompt parser. Memory keeps its own copy of the
    dataclass so the callable signature doesn't cross the layering
    contract. The prompts-layer parser hands runtime a parsed
    `SlowCycleParseResult`; runtime maps it into this shape.
    """

    salient_questions: list[str] = field(default_factory=list)
    new_thoughts: list[SlowCycleThoughtInput] = field(default_factory=list)
    new_expectations: list[SlowCycleExpectationInput] = field(default_factory=list)
    # Usage bookkeeping the callable reports so ``run_slow_cycle`` can
    # update the daily stats row without caring about provider specifics.
    input_tokens: int = 0
    output_tokens: int = 0


# A slow_cycle callable consumes the packed ``input_dict`` runtime
# passed to the prompt and returns the typed output. Async because the
# LLM call itself is async on the runtime layer.
SlowCycleFn = Callable[[dict[str, Any]], Awaitable[SlowCycleOutput]]


# ---------------------------------------------------------------------------
# Trigger logic (plan §7.2)
# ---------------------------------------------------------------------------


def session_has_shock_or_correction(
    db: DbSession, *, session: Session
) -> bool:
    """Does this session contain a SHOCK event OR a correction-tagged event?

    SHOCK is ``|emotional_impact| >= 8`` on any extracted event from this
    session. Correction is an extracted event with ``'correction'`` in
    ``relational_tags``. Both bypass the cool-down window (plan §7.2
    旁路 path).
    """
    rows = list(
        db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == session.id,
                ConceptNode.type == NodeType.EVENT.value,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    for node in rows:
        if abs(node.emotional_impact or 0) >= _SHOCK_IMPACT_THRESHOLD:
            return True
        tags = node.relational_tags or []
        if "correction" in tags:
            return True
    return False


def should_run_slow_cycle(
    db: DbSession,
    *,
    persona: Persona,
    session: Session,
    now: datetime,
    enabled: bool = True,
    cool_down_minutes: int = DEFAULT_COOL_DOWN_MINUTES,
) -> bool:
    """Decide whether phase G runs for ``session``.

    Two trigger paths (plan §7.2):
      1. **Main** — ``now - last_slow_tick_at >= cool_down_minutes``.
         First ever cycle (``last_slow_tick_at is None``) counts.
      2. **Bypass** — SHOCK impact event OR correction-tagged event
         in the just-extracted events; runs regardless of cool-down.

    Trivial sessions never trigger: slow cycle only meaningfully runs
    when there is new material on the stream.
    """
    if not enabled:
        return False
    if session.trivial:
        return False

    # Main path
    last = persona.last_slow_tick_at
    if last is None:
        return True
    elapsed_min = (now - last).total_seconds() / 60.0
    if elapsed_min >= cool_down_minutes:
        return True

    # Bypass path
    return session_has_shock_or_correction(db, session=session)


# ---------------------------------------------------------------------------
# Daily budget bookkeeping (plan §7.4)
# ---------------------------------------------------------------------------


def _today_str(now: datetime) -> str:
    """Render ``now`` as YYYY-MM-DD. Uses the naive date; callers that
    want per-persona local-time buckets must pass a timezone-aware
    ``now`` with the persona's tz applied."""
    return (
        now.date().isoformat()
        if isinstance(now.date(), date_cls)
        else str(now.date())
    )


def get_daily_slow_cycle_stats(
    db: DbSession, *, persona_id: str, now: datetime
) -> SlowCycleStats:
    """Return today's stats row, or an unsaved default if none exists yet.

    The returned object is ALWAYS a ``SlowCycleStats`` instance;
    callers can read counters without branching on None. Callers MUST
    re-query + UPSERT via :func:`bump_slow_cycle_stats` when actually
    recording a new cycle — the default row is an in-memory sentinel,
    not a persistent row.
    """
    today = _today_str(now)
    row = db.get(SlowCycleStats, (today, persona_id))
    if row is not None:
        return row
    return SlowCycleStats(
        date=today,
        persona_id=persona_id,
        cycle_count=0,
        input_tokens=0,
        output_tokens=0,
        last_cycle_at=None,
    )


def bump_slow_cycle_stats(
    db: DbSession,
    *,
    persona_id: str,
    now: datetime,
    input_tokens: int,
    output_tokens: int,
) -> SlowCycleStats:
    """UPSERT today's stats row by (date, persona_id).

    Called at the end of a successful cycle. A budget-exceeded cycle
    does NOT call this — the budget check itself runs first and the
    gate raises before any stats are recorded.
    """
    today = _today_str(now)
    row = db.get(SlowCycleStats, (today, persona_id))
    if row is None:
        row = SlowCycleStats(
            date=today,
            persona_id=persona_id,
            cycle_count=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            last_cycle_at=now,
        )
        db.add(row)
    else:
        row.cycle_count = (row.cycle_count or 0) + 1
        row.input_tokens = (row.input_tokens or 0) + input_tokens
        row.output_tokens = (row.output_tokens or 0) + output_tokens
        row.last_cycle_at = now
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _check_daily_budget(
    stats: SlowCycleStats,
    *,
    daily_cap: int,
    daily_input_token_budget: int,
    daily_output_token_budget: int,
) -> None:
    """Raise :class:`SlowCycleBudgetExceeded` if any wall is at/over."""
    if stats.cycle_count >= daily_cap:
        raise SlowCycleBudgetExceeded(
            f"daily cycle cap reached ({stats.cycle_count}/{daily_cap})"
        )
    if stats.input_tokens >= daily_input_token_budget:
        raise SlowCycleBudgetExceeded(
            f"daily input token budget reached "
            f"({stats.input_tokens}/{daily_input_token_budget})"
        )
    if stats.output_tokens >= daily_output_token_budget:
        raise SlowCycleBudgetExceeded(
            f"daily output token budget reached "
            f"({stats.output_tokens}/{daily_output_token_budget})"
        )


# ---------------------------------------------------------------------------
# Expectation writer (plan §7 · Spec 6 T5)
# ---------------------------------------------------------------------------


def bulk_create_expectations(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    expectations: list[SlowCycleExpectationInput],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Insert L4 expectation rows (``type='expectation'``) + filling chain.

    Each expectation is written as a :class:`ConceptNode` with:
      - ``type = expectation``
      - ``subject = 'persona'`` (forward-looking from the persona's POV)
      - ``event_time_end = due_at`` (so the prompt renderer can render
        a "by ..." phrase consistently with L3 events)
      - ``description = "<about_text> — <prediction_text>"`` so retrieve's
        vector search has a single descriptive surface to hash.
      - A ``ConceptNodeFilling`` row per ``reasoning_event_id`` so the
        "why does the persona believe this?" chain is inspectable.

    ``reasoning_event_ids`` MUST be non-empty per plan §7.5 Airi guard —
    an expectation without grounding events is the exact pattern that
    turns slow cycle into a confabulation engine.
    """
    if not expectations:
        return []
    now = now or datetime.now()
    created: list[ConceptNode] = []

    for i, exp in enumerate(expectations):
        if not exp.reasoning_event_ids:
            raise ValueError(
                f"bulk_create_expectations[{i}]: reasoning_event_ids must be "
                "non-empty (plan §7.5 Airi guard)"
            )
        if not exp.about_text.strip() or not exp.prediction_text.strip():
            raise ValueError(
                f"bulk_create_expectations[{i}]: about_text and "
                "prediction_text must both be non-empty"
            )
        description = f"{exp.about_text.strip()} — {exp.prediction_text.strip()}"
        node = ConceptNode(
            persona_id=persona_id,
            user_id=user_id,
            type=NodeType.EXPECTATION,
            subject="persona",
            description=description,
            emotional_impact=exp.emotional_impact,
            event_time_end=exp.due_at,
            created_at=now,
        )
        db.add(node)
        created.append(node)
    db.flush()
    # Capture ids before filling rows reference them.
    for n in created:
        if n.id is None:
            # Defensive: flush should have populated id. If not, bail
            # before writing bad filling rows.
            raise RuntimeError("bulk_create_expectations: node id not populated after flush")

    # Filling: one row per (expectation_id, reasoning_event_id). We drop
    # duplicates per expectation.
    for node, exp in zip(created, expectations, strict=True):
        for child_id in dict.fromkeys(exp.reasoning_event_ids):
            db.add(ConceptNodeFilling(parent_id=node.id, child_id=child_id))

    db.commit()
    ids = [n.id for n in created if n.id is not None]
    for n in created:
        db.refresh(n)

    for n in created:
        if observer is not None:
            try:
                observer.on_thought_created(n, "slow_tick")
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "observer.on_thought_created raised (expectation id=%s): %s",
                    n.id,
                    e,
                )
        _fire_lifecycle("on_thought_created", n, "slow_tick")
    return ids


# ---------------------------------------------------------------------------
# Thought writer (for slow_cycle thought nodes with filling chain)
# ---------------------------------------------------------------------------


def bulk_create_slow_thoughts(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    thoughts: list[SlowCycleThoughtInput],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Insert L4 thought rows with filling chain.

    Unlike the import-pipeline ``bulk_create_thoughts`` (which takes
    ``ThoughtInput`` with ``imported_from``), this writer is for
    slow-cycle output: the thoughts come from an LLM call rather than
    a file import, and each MUST cite at least one event id in its
    ``filling_event_ids``. Empty filling is a schema violation here —
    parser raised, but we re-check at write time.
    """
    if not thoughts:
        return []
    now = now or datetime.now()
    created: list[ConceptNode] = []

    for i, th in enumerate(thoughts):
        if not th.filling_event_ids:
            raise ValueError(
                f"bulk_create_slow_thoughts[{i}]: filling_event_ids must "
                "be non-empty (plan §7.5 Airi guard)"
            )
        node = ConceptNode(
            persona_id=persona_id,
            user_id=user_id,
            type=NodeType.THOUGHT,
            subject="persona",
            description=th.description,
            emotional_impact=th.emotional_impact,
            created_at=now,
        )
        db.add(node)
        created.append(node)
    db.flush()

    for node, th in zip(created, thoughts, strict=True):
        if node.id is None:
            raise RuntimeError(
                "bulk_create_slow_thoughts: node id not populated after flush"
            )
        for child_id in dict.fromkeys(th.filling_event_ids):
            db.add(ConceptNodeFilling(parent_id=node.id, child_id=child_id))

    db.commit()
    ids = [n.id for n in created if n.id is not None]
    for n in created:
        db.refresh(n)

    for n in created:
        if observer is not None:
            try:
                observer.on_thought_created(n, "slow_tick")
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "observer.on_thought_created raised (thought id=%s): %s",
                    n.id,
                    e,
                )
        _fire_lifecycle("on_thought_created", n, "slow_tick")
    return ids


# ---------------------------------------------------------------------------
# Input packing
# ---------------------------------------------------------------------------


def _estimate_tokens(obj: Any) -> int:
    """Cheap character-based token estimate. JSON-encodes the object and
    divides by ``_AVG_BYTES_PER_TOKEN``.
    """
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return max(1, len(text.encode("utf-8")) // _AVG_BYTES_PER_TOKEN)


def _event_to_dict(e: ConceptNode) -> dict[str, Any]:
    return {
        "id": e.id,
        "description": e.description,
        "emotional_impact": e.emotional_impact,
        "emotion_tags": list(e.emotion_tags or []),
        "relational_tags": list(e.relational_tags or []),
        "subject": getattr(e, "subject", "user"),
        "created_at_iso": e.created_at.isoformat() if e.created_at else "",
    }


def _truncate_events_to_budget(
    events: list[dict[str, Any]],
    *,
    base_input: dict[str, Any],
    budget_tokens: int,
) -> list[dict[str, Any]]:
    """Drop oldest events until the packed input fits.

    ``base_input`` is the rest of the payload (recent thoughts +
    bookkeeping). We prepend ``events`` in reverse-chron order so the
    newest ones survive truncation.
    """
    # Always keep at least the single most-recent event; if that alone
    # blows the budget, we return [it] and let the cycle raise later.
    sorted_events = sorted(events, key=lambda e: e.get("id") or 0, reverse=True)
    kept: list[dict[str, Any]] = []
    for ev in sorted_events:
        candidate = [*kept, ev]
        trial = {**base_input, "recent_events": list(reversed(candidate))}
        if _estimate_tokens(trial) > budget_tokens and kept:
            break
        kept.append(ev)
    # Restore chronological order for the LLM.
    return list(reversed(kept))


# ---------------------------------------------------------------------------
# Transcript persistence
# ---------------------------------------------------------------------------


def save_slow_cycle_transcript(
    *,
    transcript_dir: Path | str,
    persona_id: str,
    now: datetime,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
) -> Path | None:
    """Write a JSON transcript for admin debugging.

    Returns the written path, or ``None`` if transcripts were disabled
    (``transcript_dir`` is the empty string) or if the write fails for
    any reason (we never let a disk issue sink the cycle).
    """
    if not transcript_dir:
        return None
    dir_path = Path(transcript_dir)
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        cycle_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{persona_id}"
        path = dir_path / f"{cycle_id}.json"
        payload = {
            "persona_id": persona_id,
            "now_iso": now.isoformat(),
            "input": input_payload,
            "output": output_payload,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return path
    except Exception as e:  # noqa: BLE001
        log.warning("save_slow_cycle_transcript failed: %s", e)
        return None


def prune_slow_cycle_transcripts(
    *, transcript_dir: Path | str, retention_days: int, now: datetime
) -> int:
    """Delete transcripts older than ``retention_days``.

    Returns the number of files removed. ``retention_days=0`` disables
    retention (everything older than "now" is removed). Fails silently
    on individual file errors — the rest of the directory is still
    pruned.
    """
    if not transcript_dir:
        return 0
    dir_path = Path(transcript_dir)
    if not dir_path.exists():
        return 0
    cutoff = now - timedelta(days=max(0, retention_days))
    removed = 0
    for path in dir_path.glob("*.json"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("prune_slow_cycle_transcripts failed for %s: %s", path, e)
    return removed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _collect_recent_events(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    since: datetime,
) -> list[ConceptNode]:
    """Return non-deleted, non-superseded event nodes since ``since``."""
    stmt = (
        select(ConceptNode)
        .where(
            ConceptNode.persona_id == persona_id,
            ConceptNode.user_id == user_id,
            ConceptNode.type == NodeType.EVENT.value,
            ConceptNode.created_at > since,
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ConceptNode.created_at)  # type: ignore[union-attr]
    )
    return list(db.exec(stmt))


def _collect_recent_thought_descs(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    limit: int,
) -> list[str]:
    stmt = (
        select(ConceptNode)
        .where(
            ConceptNode.persona_id == persona_id,
            ConceptNode.user_id == user_id,
            ConceptNode.type == NodeType.THOUGHT.value,
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ConceptNode.created_at.desc())  # type: ignore[union-attr]
        .limit(limit)
    )
    return [n.description for n in db.exec(stmt)]


@dataclass(slots=True)
class SlowCycleRunResult:
    """Summary of a single cycle for logging and tests."""

    ran: bool
    skipped_reason: str | None = None
    thought_ids: list[int] = field(default_factory=list)
    expectation_ids: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    transcript_path: str | None = None


async def run_slow_cycle(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    slow_cycle_fn: SlowCycleFn,
    now: datetime,
    daily_cap: int = DEFAULT_DAILY_CAP,
    daily_input_token_budget: int = DEFAULT_DAILY_INPUT_TOKEN_BUDGET,
    daily_output_token_budget: int = DEFAULT_DAILY_OUTPUT_TOKEN_BUDGET,
    input_token_limit: int = DEFAULT_INPUT_TOKEN_LIMIT,
    recent_thoughts_limit: int = 5,
    transcript_dir: Path | str | None = None,
    observer: MemoryEventObserver | None = None,
    tracer: ConsolidateTracer | NullConsolidateTracer | None = None,
) -> SlowCycleRunResult:
    """Run a single slow cycle for ``(persona_id, user_id)``.

    Caller MUST have already decided to run via
    :func:`should_run_slow_cycle`. This function does the actual work:

      1. Enforce the daily budget — raise SlowCycleBudgetExceeded early
         if we're already past the cap or any token wall.
      2. Collect recent events + thought descriptions since the
         persona's last tick. (v0.5 · no L1 read: slow_cycle is
         event+thought only.)
      3. Truncate events head-first to fit the per-cycle input token
         budget. Never retry; truncation is the fallback.
      4. Call ``slow_cycle_fn`` with the packed input dict.
      5. Write thoughts + expectations via the typed bulk writers
         (enforcing reasoning/filling non-empty).
      6. UPSERT today's stats row + update ``personas.last_slow_tick_at``.
      7. Best-effort: save a transcript for admin debugging.
    """
    persona = db.exec(select(Persona).where(Persona.id == persona_id)).one()
    tracer = tracer if tracer is not None else NullConsolidateTracer()

    # 1. Budget gate.
    stats = get_daily_slow_cycle_stats(db, persona_id=persona_id, now=now)
    try:
        _check_daily_budget(
            stats,
            daily_cap=daily_cap,
            daily_input_token_budget=daily_input_token_budget,
            daily_output_token_budget=daily_output_token_budget,
        )
    except SlowCycleBudgetExceeded as e:
        tracer.record_phase_g(
            ran=False,
            cool_down_ok=True,
            budget_check=str(e),
        )
        raise

    # 2. Collect inputs.
    since = persona.last_slow_tick_at or (
        now - timedelta(days=_FIRST_CYCLE_LOOKBACK_DAYS)
    )
    recent_events = _collect_recent_events(
        db, persona_id=persona_id, user_id=user_id, since=since
    )
    if not recent_events:
        tracer.record_phase_g(
            ran=False, cool_down_ok=True, budget_check="ok", slow_cycle_prompt=None
        )
        return SlowCycleRunResult(ran=False, skipped_reason="no_new_events")

    recent_thought_descs = _collect_recent_thought_descs(
        db, persona_id=persona_id, user_id=user_id, limit=recent_thoughts_limit
    )
    elapsed_hours = (now - since).total_seconds() / 3600.0

    event_dicts = [_event_to_dict(e) for e in recent_events]
    base_input = {
        "recent_thoughts": recent_thought_descs,
        "elapsed_hours": round(elapsed_hours, 2),
        "now_iso": now.isoformat(),
    }

    # 3. Truncate events to fit the per-cycle input token wall.
    if _estimate_tokens({**base_input, "recent_events": event_dicts}) > input_token_limit:
        event_dicts = _truncate_events_to_budget(
            event_dicts, base_input=base_input, budget_tokens=input_token_limit
        )
    input_payload = {**base_input, "recent_events": event_dicts}
    input_event_ids = {
        int(e["id"]) for e in event_dicts if isinstance(e.get("id"), int)
    }

    # 4. LLM call.
    output: SlowCycleOutput = await slow_cycle_fn(input_payload)

    # Defensive: validate the callable honoured the input event id set.
    # (Also done at parse time; keeping this is belt-and-braces.)
    for i, th in enumerate(output.new_thoughts):
        bad = [fid for fid in th.filling_event_ids if fid not in input_event_ids]
        if bad:
            raise ValueError(
                f"slow_cycle_fn returned thought[{i}] citing unknown event "
                f"ids {bad}"
            )
    for i, exp in enumerate(output.new_expectations):
        bad = [fid for fid in exp.reasoning_event_ids if fid not in input_event_ids]
        if bad:
            raise ValueError(
                f"slow_cycle_fn returned expectation[{i}] citing unknown event "
                f"ids {bad}"
            )

    # 5. Writers. v0.5 · L4 only. L1 is human-authored from v0.5 onward;
    # any "self-narrative" output from an older LLM is silently
    # dropped — slow_cycle has no L1 write path.
    thought_ids: list[int] = []
    expectation_ids: list[int] = []
    if output.new_thoughts:
        thought_ids = bulk_create_slow_thoughts(
            db,
            persona_id=persona_id,
            user_id=user_id,
            thoughts=output.new_thoughts,
            observer=observer,
            now=now,
        )
    if output.new_expectations:
        expectation_ids = bulk_create_expectations(
            db,
            persona_id=persona_id,
            user_id=user_id,
            expectations=output.new_expectations,
            observer=observer,
            now=now,
        )

    # 6. Bookkeeping.
    persona.last_slow_tick_at = now
    db.add(persona)
    db.commit()
    db.refresh(persona)

    bump_slow_cycle_stats(
        db,
        persona_id=persona_id,
        now=now,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
    )

    # 7. Transcript (optional, best-effort).
    output_payload = {
        "salient_questions": list(output.salient_questions),
        "new_thoughts": [
            {
                "description": t.description,
                "filling_event_ids": list(t.filling_event_ids),
                "emotional_impact": t.emotional_impact,
            }
            for t in output.new_thoughts
        ],
        "new_expectations": [
            {
                "about_text": e.about_text,
                "prediction_text": e.prediction_text,
                "due_at": e.due_at.isoformat() if e.due_at else None,
                "reasoning_event_ids": list(e.reasoning_event_ids),
                "emotional_impact": e.emotional_impact,
            }
            for e in output.new_expectations
        ],
        "thought_ids": thought_ids,
        "expectation_ids": expectation_ids,
    }
    path: Path | None = None
    if transcript_dir is not None:
        path = save_slow_cycle_transcript(
            transcript_dir=transcript_dir,
            persona_id=persona_id,
            now=now,
            input_payload=input_payload,
            output_payload=output_payload,
        )

    nodes_written: list[dict] = [
        {"kind": "thought", "id": tid} for tid in thought_ids
    ] + [
        {"kind": "expectation", "id": eid} for eid in expectation_ids
    ]
    tracer.record_phase_g(
        ran=True,
        cool_down_ok=True,
        budget_check="ok",
        slow_cycle_prompt=None,
        slow_cycle_response_raw=None,
        nodes_written=nodes_written,
        self_append_attempted=False,
    )

    return SlowCycleRunResult(
        ran=True,
        thought_ids=thought_ids,
        expectation_ids=expectation_ids,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
        transcript_path=str(path) if path else None,
    )


__all__ = [
    "DEFAULT_COOL_DOWN_MINUTES",
    "DEFAULT_DAILY_CAP",
    "DEFAULT_DAILY_INPUT_TOKEN_BUDGET",
    "DEFAULT_DAILY_OUTPUT_TOKEN_BUDGET",
    "DEFAULT_INPUT_TOKEN_LIMIT",
    "DEFAULT_OUTPUT_TOKEN_LIMIT",
    "DEFAULT_TRANSCRIPT_DIR",
    "SlowCycleBudgetExceeded",
    "SlowCycleExpectationInput",
    "SlowCycleFn",
    "SlowCycleOutput",
    "SlowCycleRunResult",
    "SlowCycleThoughtInput",
    "bulk_create_expectations",
    "bulk_create_slow_thoughts",
    "bump_slow_cycle_stats",
    "get_daily_slow_cycle_stats",
    "prune_slow_cycle_transcripts",
    "run_slow_cycle",
    "save_slow_cycle_transcript",
    "session_has_shock_or_correction",
    "should_run_slow_cycle",
]



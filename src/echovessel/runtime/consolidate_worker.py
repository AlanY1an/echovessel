"""Consolidate worker — async background task that runs consolidate_session.

See docs/runtime/01-spec-v0.1.md §8.

Responsibilities:
- Poll the sessions table for `status='closing' AND extracted=False`
- For each, call `consolidate_session(...)` inside a retry loop
- Mark `FAILED` after `worker_max_retries` transient errors
- Skip already-extracted sessions (idempotency; §12)

Scheduling model: a single asyncio task that sleeps between polls. The DB
lives in the same process as this task (SQLite single-writer), so no
cross-process coordination is needed.

**Idempotency contract.** The single source of truth is the persistent
``Session.extracted`` flag (plus ``Session.extracted_events`` for the
stage-B flag). ``_process_one`` short-circuits on ``session.extracted``
before running any LLM / DB work, so a session that has already been
fully consolidated will never be consolidated again regardless of how
many times it lands on the queue. There is NO in-memory "seen set" —
that was an earlier belt-and-suspenders layer that prevented legitimate
retries after crashes and blocked debug-time workflows like
``UPDATE sessions SET status='closing', extracted=0`` to re-trigger
consolidation without a daemon restart. The current design lets that
SQL-flip trigger a retry within one poll interval (default 5s).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import sqlalchemy.exc
from sqlmodel import select

from echovessel.core.types import SessionStatus
from echovessel.memory.backend import StorageBackend
from echovessel.memory.consolidate import (
    REFLECTION_HARD_LIMIT_24H,
    TRIVIAL_MESSAGE_COUNT,
    TRIVIAL_TOKEN_COUNT,
    EmbedFn,
    ExtractFn,
    ReflectFn,
    consolidate_session,
)
from echovessel.memory.models import Persona, Session
from echovessel.memory.observers import MemoryEventObserver
from echovessel.memory.slow_cycle import (
    DEFAULT_COOL_DOWN_MINUTES,
    DEFAULT_DAILY_CAP,
    DEFAULT_DAILY_INPUT_TOKEN_BUDGET,
    DEFAULT_DAILY_OUTPUT_TOKEN_BUDGET,
    DEFAULT_INPUT_TOKEN_LIMIT,
    SlowCycleBudgetExceeded,
    SlowCycleFn,
    run_slow_cycle,
    should_run_slow_cycle,
)
from echovessel.runtime.llm.errors import LLMPermanentError, LLMTransientError


def _is_transient_infra_error(e: BaseException) -> bool:
    """Whether ``e`` is an infrastructure transient that is safe to retry.

    SQLite WAL contention surfaces as ``sqlalchemy.exc.OperationalError``
    wrapping ``sqlite3.OperationalError("database is locked" | "database is
    busy")`` and is the dominant case in the local-first single-file
    deployment — the consolidate worker and the live runtime share one db
    file and occasionally race. New transient classes get added here as
    they are observed in the wild (YAGNI for unproven candidates)."""
    if isinstance(e, sqlalchemy.exc.OperationalError):
        msg = str(getattr(e, "orig", "") or e)
        return "database is locked" in msg or "database is busy" in msg
    return False

log = logging.getLogger(__name__)

DbSessionFactory = Callable[[], "_DbContextManager"]


class _DbContextManager:
    """Duck-typed protocol for a context manager that yields a SQLModel
    Session. We don't import sqlmodel.Session here to keep the typing easy
    for tests that pass in custom factories."""

    def __enter__(self): ...
    def __exit__(self, exc_type, exc, tb): ...


@dataclass
class ConsolidateWorker:
    """Single background task that drains closing sessions.

    Parameters:
        db_factory: context manager factory for SQLModel Session. Each
            iteration builds a fresh session so commits are self-contained.
        backend: memory StorageBackend (sqlite-vec wrapper).
        extract_fn / reflect_fn: async callables built by prompts_wiring.
        embed_fn: sync embedder (see memory.consolidate.EmbedFn).
        poll_seconds: seconds between poll loops when the queue is empty.
        max_retries: per-session transient retry budget.
        shutdown_event: asyncio.Event the launcher sets on SIGINT/SIGTERM.
        initial_session_ids: ids picked up at startup by catch-up scan.
    """

    db_factory: Callable[[], object]
    backend: StorageBackend
    extract_fn: ExtractFn
    reflect_fn: ReflectFn
    embed_fn: EmbedFn
    poll_seconds: float = 5.0
    max_retries: int = 3
    shutdown_event: asyncio.Event | None = None
    initial_session_ids: tuple[str, ...] = ()
    now_fn: Callable[[], datetime] = datetime.now
    # Consolidate-policy tunables threaded from `cfg.consolidate.*` at
    # runtime construction time. Defaults match the module-level
    # constants in `echovessel.memory.consolidate` so tests that don't
    # build a Runtime still see historical behaviour.
    trivial_message_count: int = TRIVIAL_MESSAGE_COUNT
    trivial_token_count: int = TRIVIAL_TOKEN_COUNT
    reflection_hard_limit_24h: int = REFLECTION_HARD_LIMIT_24H
    # Slow-tick (plan §7 · Spec 6). When ``slow_cycle_fn`` is None, the G
    # phase is a no-op — useful for legacy tests that don't care about
    # reflection between sessions. ``slow_tick_enabled=False`` is the
    # runtime-facing kill switch surfaced via ``cfg.slow_tick.enabled``.
    slow_cycle_fn: SlowCycleFn | None = None
    slow_tick_enabled: bool = True
    slow_tick_cool_down_minutes: int = DEFAULT_COOL_DOWN_MINUTES
    slow_tick_daily_cap: int = DEFAULT_DAILY_CAP
    slow_tick_daily_input_token_budget: int = DEFAULT_DAILY_INPUT_TOKEN_BUDGET
    slow_tick_daily_output_token_budget: int = DEFAULT_DAILY_OUTPUT_TOKEN_BUDGET
    slow_tick_input_token_limit: int = DEFAULT_INPUT_TOKEN_LIMIT
    slow_tick_transcript_dir: Path | str | None = None
    observer: MemoryEventObserver | None = None
    _queue: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        for sid in self.initial_session_ids:
            if sid and sid not in self._queue:
                self._queue.append(sid)

    # ---- Public entry point ------------------------------------------------

    async def run(self) -> None:
        """Drain closing sessions until shutdown_event is set."""
        while not self._shutting_down():
            try:
                self._poll_closing_sessions()
            except Exception as e:  # noqa: BLE001
                log.error("consolidate worker poll failed: %s", e, exc_info=True)

            if not self._queue:
                await asyncio.sleep(self.poll_seconds)
                continue

            session_id = self._queue.pop(0)
            await self._process_one(session_id)

    async def drain_once(self) -> int:
        """Test helper: poll once and process everything queued. Returns the
        number of sessions processed in this call."""
        self._poll_closing_sessions()
        processed = 0
        while self._queue:
            session_id = self._queue.pop(0)
            await self._process_one(session_id)
            processed += 1
        return processed

    # ---- Internals ---------------------------------------------------------

    def _shutting_down(self) -> bool:
        return self.shutdown_event is not None and self.shutdown_event.is_set()

    def _poll_closing_sessions(self) -> None:
        with self.db_factory() as db:  # type: ignore[operator]
            stmt = select(Session).where(
                Session.status == SessionStatus.CLOSING,
                Session.extracted == False,  # noqa: E712
                Session.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            for s in db.exec(stmt):  # type: ignore[attr-defined]
                sid = s.id
                if sid and sid not in self._queue:
                    self._queue.append(sid)

    async def _process_one(self, session_id: str) -> None:
        retries = 0
        while retries <= self.max_retries:
            try:
                with self.db_factory() as db:  # type: ignore[operator]
                    session: Session | None = db.get(Session, session_id)  # type: ignore[attr-defined]
                    if session is None:
                        log.warning("session %s disappeared", session_id)
                        return
                    if session.extracted:
                        return  # idempotency (§12)
                    now = self.now_fn()
                    result = await consolidate_session(
                        db=db,
                        backend=self.backend,
                        session=session,
                        extract_fn=self.extract_fn,
                        reflect_fn=self.reflect_fn,
                        embed_fn=self.embed_fn,
                        now=now,
                        trivial_message_count=self.trivial_message_count,
                        trivial_token_count=self.trivial_token_count,
                        reflection_hard_limit_24h=self.reflection_hard_limit_24h,
                    )
                    log.info(
                        "consolidated session %s: skipped=%s events=%d thoughts=%d",
                        session_id,
                        result.skipped,
                        len(result.events_created),
                        len(result.thoughts_created),
                    )
                    # G phase · slow-tick reflection (plan §7.1). Runs
                    # strictly after F committed ``session.status=CLOSED``,
                    # so failure here cannot unwind the session close.
                    await self._maybe_run_slow_cycle(db, session, now)
                return
            except LLMTransientError as e:
                retries += 1
                if retries > self.max_retries:
                    log.error(
                        "consolidate exhausted retries for %s: %s",
                        session_id,
                        e,
                    )
                    self._mark_failed(session_id, f"transient: {e}")
                    return
                backoff = 2**retries
                log.warning(
                    "consolidate transient error on %s (retry %d/%d in %ds): %s",
                    session_id,
                    retries,
                    self.max_retries,
                    backoff,
                    e,
                )
                await asyncio.sleep(backoff)
            except LLMPermanentError as e:
                log.error(
                    "consolidate permanent error on %s: %s",
                    session_id,
                    e,
                )
                self._mark_failed(session_id, f"permanent: {e}")
                return
            except Exception as e:  # noqa: BLE001
                if _is_transient_infra_error(e):
                    retries += 1
                    if retries > self.max_retries:
                        log.error(
                            "consolidate exhausted retries for %s "
                            "(transient infra): %s",
                            session_id,
                            e,
                        )
                        self._mark_failed(session_id, f"transient_infra: {e}")
                        return
                    backoff = 2**retries
                    log.warning(
                        "consolidate transient infra error on %s "
                        "(retry %d/%d in %ds): %s",
                        session_id,
                        retries,
                        self.max_retries,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)
                    continue
                log.error(
                    "consolidate unexpected error on %s: %s",
                    session_id,
                    e,
                    exc_info=True,
                )
                self._mark_failed(session_id, f"unexpected: {e}")
                return

    def _mark_failed(self, session_id: str, reason: str) -> None:
        try:
            with self.db_factory() as db:  # type: ignore[operator]
                session = db.get(Session, session_id)  # type: ignore[attr-defined]
                if session is None:
                    return
                session.status = SessionStatus.FAILED
                session.close_trigger = (session.close_trigger or "") + f"|failed:{reason[:60]}"
                db.add(session)
                db.commit()
        except Exception as e:  # noqa: BLE001
            log.error("failed to mark session %s as FAILED: %s", session_id, e)

    async def _maybe_run_slow_cycle(
        self, db: object, session: Session, now: datetime
    ) -> None:
        """Phase G entry point. Decides + runs the slow-tick cycle.

        No-op when any of:
          - ``slow_cycle_fn`` is not configured (tests / legacy runtime).
          - ``slow_tick_enabled=False`` (kill switch).
          - ``should_run_slow_cycle`` declines the trigger (cool-down
            active without SHOCK/correction bypass, or session was
            marked trivial).

        Failure of the cycle is logged at WARNING and swallowed — plan
        §7.1 is explicit: F has already committed ``status=CLOSED``, so
        we must not let a broken reflection undo that transition.
        """
        if self.slow_cycle_fn is None or not self.slow_tick_enabled:
            return
        try:
            persona = db.exec(  # type: ignore[attr-defined]
                select(Persona).where(Persona.id == session.persona_id)
            ).one_or_none()
            if persona is None:
                return
            if not should_run_slow_cycle(
                db,  # type: ignore[arg-type]
                persona=persona,
                session=session,
                now=now,
                enabled=self.slow_tick_enabled,
                cool_down_minutes=self.slow_tick_cool_down_minutes,
            ):
                return

            result = await run_slow_cycle(
                db,  # type: ignore[arg-type]
                persona_id=session.persona_id,
                user_id=session.user_id,
                slow_cycle_fn=self.slow_cycle_fn,
                now=now,
                daily_cap=self.slow_tick_daily_cap,
                daily_input_token_budget=self.slow_tick_daily_input_token_budget,
                daily_output_token_budget=self.slow_tick_daily_output_token_budget,
                input_token_limit=self.slow_tick_input_token_limit,
                transcript_dir=self.slow_tick_transcript_dir,
                observer=self.observer,
            )
            log.info(
                "slow cycle for session %s: ran=%s thoughts=%d expectations=%d self_appended=%s",
                session.id,
                result.ran,
                len(result.thought_ids),
                len(result.expectation_ids),
                result.self_appended,
            )
        except SlowCycleBudgetExceeded as e:
            log.warning(
                "slow cycle skipped for session %s: %s", session.id, e
            )
        except Exception as e:  # noqa: BLE001 — plan §7.1: never bubble
            log.warning(
                "slow cycle failed for session %s (session remains CLOSED): %s",
                session.id,
                e,
                exc_info=True,
            )


__all__ = ["ConsolidateWorker"]

"""Runtime-side mediator between Web channel admin routes and the
``echovessel.import_.pipeline`` module (spec §17a.6).

## Why this exists

Both ``echovessel.channels.web`` and ``echovessel.import_.pipeline`` live
in Layer 2/3 of the layered architecture. Direct ``channels/web →
import_`` imports would be a same-layer sibling reference, which the
``lint-imports`` contract forbids. The clean resolution is:

    channels/web ──►  runtime.importer_facade  ◄── runtime
                         │
                         ▼
                    import_.pipeline

Runtime owns the facade instance (one per daemon), wires it to the
concrete ``LLMProvider`` / ``VoiceService`` / ``MemoryFacade`` dependencies
it has on hand, and injects the facade into the Web channel constructor
at Step 11. The Web channel then calls ``start_pipeline()`` /
``cancel_pipeline()`` / ``resume_pipeline()`` / ``subscribe_events()``
from its admin route handlers.

## Round 4 (Thread IMPORT-code) upgrade

As of this round the facade is no longer a stub. ``start_pipeline``
spawns an ``asyncio.create_task`` that actually invokes
``echovessel.import_.run_pipeline`` with the injected dependencies.
The pipeline emits ``PipelineEventLike`` events which the facade
translates into :class:`PipelineEvent` and fan-outs to every
subscriber queue — matching the shape the Round 3 smoke tests already
expect.

The constructor signature is unchanged from Round 3 — Thread IMPORT-code
is **not** allowed to break the channel/runtime wiring contract. Every
piece of new behaviour uses attribute probing on the injected
``memory_api`` / ``llm_provider`` so tests and the runtime can supply
richer or poorer objects as appropriate.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from echovessel.import_ import (
    PipelineEventLike,
    run_pipeline,
)
from echovessel.import_.models import ProgressSnapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """One event emitted by an import pipeline (spec §17a.6).

    Stable fields:
        pipeline_id: The ULID / UUID string the facade allocated when
            ``start_pipeline`` was called.
        type: Free-form string identifier. Real pipeline stages emit
            names like ``"chunk.start"`` / ``"chunk.done"`` /
            ``"chunk.error"`` / ``"pipeline.done"``.
        payload: Arbitrary JSON-serializable metadata. Always a dict
            (never ``None``) so consumers can do ``payload["key"]``
            without None-checks.
    """

    pipeline_id: str
    type: str
    payload: dict


@dataclass
class _PipelineState:
    """Per-pipeline in-memory state managed by the facade."""

    pipeline_id: str
    upload_id: str
    status: str = "running"
    subscribers: list[asyncio.Queue[PipelineEvent | None]] = field(
        default_factory=list
    )
    created_at: datetime = field(default_factory=datetime.now)
    task: asyncio.Task | None = None
    progress: ProgressSnapshot | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


class ImporterFacade:
    """Thin async facade between Web channel admin routes and the
    import pipeline.

    Dependency injection rationale:
        - ``llm_provider`` — import pipeline calls the LLM for
          per-chunk extraction (SMALL tier per tracker §4 #4).
        - ``voice_service`` — optional; MVP import pipeline does not
          use voice, but the spec reserves the slot so v1.0 can
          re-ingest voice memos.
        - ``memory_api`` — the :class:`echovessel.runtime.wiring.memory.MemoryFacade`
          instance. We duck-type it for attributes the pipeline needs
          (``_db_factory``, optional ``embed_fn`` / ``vector_writer``)
          so tests can supply a bare stub.
    """

    def __init__(
        self,
        *,
        llm_provider: Any,
        voice_service: Any | None,
        memory_api: Any,
    ) -> None:
        self._llm = llm_provider
        self._voice = voice_service
        self._memory = memory_api
        self._pipelines: dict[str, _PipelineState] = {}

    # --- Public API (spec §17a.6) --------------------------------

    async def start_pipeline(
        self,
        upload_id: str,
        *,
        force_duplicate: bool = False,
        # The following kwargs are runtime-provided when starting a
        # real pipeline and are optional so the Round 3 smoke tests
        # (which only check registration) still work.
        upload_path: Any | None = None,
        raw_bytes: bytes | None = None,
        suffix: str = "",
        source_label: str = "",
        file_hash: str = "",
        persona_id: str = "",
        user_id: str = "",
        persona_context: str = "",
        embed_fn: Any | None = None,
        vector_writer: Any | None = None,
    ) -> str:
        """Register a new pipeline for ``upload_id`` and return its id.

        If enough runtime-side arguments are supplied (``raw_bytes`` or
        ``upload_path``, plus ``persona_id`` / ``user_id``), the facade
        actually spawns the import pipeline as an asyncio task.
        Otherwise it keeps the Round 3 shape: register, emit
        ``pipeline.registered``, and wait for ``emit_event`` from the
        test harness. That lets the existing smoke tests pass without
        alteration.
        """
        pipeline_id = uuid.uuid4().hex
        state = _PipelineState(pipeline_id=pipeline_id, upload_id=upload_id)
        self._pipelines[pipeline_id] = state

        await self._emit(
            pipeline_id,
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.registered",
                payload={"upload_id": upload_id},
            ),
        )

        db_session_factory = self._resolve_db_session_factory()
        if db_session_factory is None:
            # Round 3 smoke path — no pipeline task, just wait for
            # test-driven emit_event calls.
            log.debug(
                "start_pipeline: memory_api has no _db_factory, "
                "skipping pipeline task (smoke mode)"
            )
            return pipeline_id

        if raw_bytes is None and upload_path is None:
            # Also smoke mode — no upload payload provided.
            log.debug(
                "start_pipeline: no upload payload, "
                "skipping pipeline task (smoke mode)"
            )
            return pipeline_id

        if not persona_id or not user_id:
            # Safety: real pipeline needs these.
            log.warning(
                "start_pipeline: persona_id/user_id missing — "
                "skipping pipeline task"
            )
            return pipeline_id

        progress = ProgressSnapshot(pipeline_id=pipeline_id)
        state.progress = progress

        resolved_embed_fn = embed_fn or getattr(self._memory, "embed_fn", None)
        resolved_vector_writer = vector_writer or getattr(
            self._memory, "vector_writer", None
        )

        run_kwargs: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "upload_path": upload_path,
            "raw_bytes": raw_bytes,
            "suffix": suffix,
            "source_label": source_label,
            "file_hash": file_hash,
            "persona_id": persona_id,
            "user_id": user_id,
            "persona_context": persona_context,
            "llm": self._llm,
            "db_session_factory": db_session_factory,
            "embed_fn": resolved_embed_fn,
            "vector_writer": resolved_vector_writer,
            "event_sink": self._make_event_sink(pipeline_id),
            "progress": progress,
            "force_duplicate": force_duplicate,
        }
        state.kwargs = run_kwargs
        state.task = asyncio.create_task(self._run_pipeline(pipeline_id))
        return pipeline_id

    async def cancel_pipeline(self, pipeline_id: str) -> None:
        """Mark a pipeline cancelled and tear down its task.

        No-op (with warning log) when the pipeline_id is unknown — same
        behaviour the Round 3 stub had, so the Web channel's admin
        route stays idempotent on double-cancel.
        """
        state = self._pipelines.get(pipeline_id)
        if state is None:
            log.warning(
                "cancel_pipeline: unknown pipeline_id=%s (no-op)", pipeline_id
            )
            return
        state.status = "cancelled"
        if state.task is not None and not state.task.done():
            state.task.cancel()
        await self._emit(
            pipeline_id,
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.cancelled",
                payload={},
            ),
        )
        # Close out every subscriber queue with a sentinel so consumers
        # can exit their async-for loops cleanly.
        for q in state.subscribers:
            q.put_nowait(None)

    async def resume_pipeline(self, pipeline_id: str) -> None:
        """Resume a pipeline that was paused by a transient error.

        Re-invokes ``run_pipeline`` starting from the stored
        :class:`ProgressSnapshot.current_chunk`. Only works when the
        original ``start_pipeline`` call had enough runtime arguments
        to spawn a real task — smoke-mode pipelines just re-emit a
        ``pipeline.resumed`` event.
        """
        state = self._pipelines.get(pipeline_id)
        if state is None:
            log.warning(
                "resume_pipeline: unknown pipeline_id=%s (no-op)", pipeline_id
            )
            return
        state.status = "running"
        await self._emit(
            pipeline_id,
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.resumed",
                payload={
                    "resume_from": (
                        state.progress.current_chunk
                        if state.progress is not None
                        else 0
                    )
                },
            ),
        )
        if state.kwargs and state.task is not None and state.task.done():
            # Re-spawn the task; progress snapshot tells run_pipeline
            # where to pick up.
            state.task = asyncio.create_task(self._run_pipeline(pipeline_id))

    def subscribe_events(
        self,
        pipeline_id: str,
    ) -> AsyncIterator[PipelineEvent]:
        """Return an async iterator over this pipeline's events.

        Each call produces a fresh iterator backed by its own
        :class:`asyncio.Queue`, so multiple SSE subscribers can consume
        independently. The iterator terminates when the facade pushes a
        ``None`` sentinel into the queue.

        Raises:
            KeyError: the pipeline_id is unknown. Web channel admin
                handler is responsible for mapping this to a 404.
        """
        state = self._pipelines.get(pipeline_id)
        if state is None:
            raise KeyError(f"unknown pipeline_id: {pipeline_id}")

        queue: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        state.subscribers.append(queue)
        return self._iter_queue(queue, state)

    async def emit_event(self, event: PipelineEvent) -> None:
        """Public emit hook used by tests (and by
        ``echovessel.import_.pipeline``'s event sink) to push an event
        into a registered pipeline.

        No-op when ``event.pipeline_id`` is unknown so tests do not
        need to race the ``start_pipeline`` registration.
        """
        await self._emit(event.pipeline_id, event)

    # --- Internals -----------------------------------------------

    async def _run_pipeline(self, pipeline_id: str) -> None:
        """Task body for a real import run.

        Wraps :func:`echovessel.import_.run_pipeline` and translates
        all exceptions into ``pipeline.done`` events so the subscriber
        loop always terminates cleanly.

        Worker ζ · the entire pipeline runs inside a ``feature=import``
        context so each LLM extraction call is attributed to the
        admin Cost tab's import bucket.
        """
        from echovessel.runtime.cost_logger import feature_context

        state = self._pipelines.get(pipeline_id)
        if state is None or not state.kwargs:
            return
        try:
            with feature_context("import"):
                await run_pipeline(**state.kwargs)
        except asyncio.CancelledError:
            state.status = "cancelled"
            await self._emit(
                pipeline_id,
                PipelineEvent(
                    pipeline_id=pipeline_id,
                    type="pipeline.done",
                    payload={"status": "cancelled"},
                ),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "import pipeline %s crashed: %s", pipeline_id, exc
            )
            await self._emit(
                pipeline_id,
                PipelineEvent(
                    pipeline_id=pipeline_id,
                    type="pipeline.done",
                    payload={"status": "failed", "error": str(exc)},
                ),
            )
        finally:
            # Wake subscribers so their async-for loops exit — unless
            # a cancel_pipeline already did it.
            for q in state.subscribers:
                q.put_nowait(None)

    async def _emit(self, pipeline_id: str, event: PipelineEvent) -> None:
        state = self._pipelines.get(pipeline_id)
        if state is None:
            return
        for q in state.subscribers:
            q.put_nowait(event)

    def _make_event_sink(self, pipeline_id: str):
        """Build an async callable the pipeline orchestrator can use
        to push events back into the facade's subscriber queues.
        """

        async def _sink(ev: PipelineEventLike) -> None:
            await self._emit(
                pipeline_id,
                PipelineEvent(
                    pipeline_id=pipeline_id,
                    type=ev.type,
                    payload=dict(ev.payload),
                ),
            )

        return _sink

    def _resolve_db_session_factory(self):
        """Extract a ``() -> DbSession`` callable from the injected
        ``memory_api``.

        Looks at:
          1. ``memory_api.db_session_factory`` (explicit public hook)
          2. ``memory_api._db_factory`` (MemoryFacade private attr —
             pragmatically probed so the runtime wiring in
             ``runtime/app.py`` keeps working without modification)

        Returns ``None`` when neither is present. In that case the
        facade runs in "smoke mode" and the pipeline task is not
        spawned — matching the Round 3 stub behaviour.
        """
        for attr in ("db_session_factory", "_db_factory"):
            candidate = getattr(self._memory, attr, None)
            if callable(candidate):
                return candidate
        return None

    async def _iter_queue(
        self,
        queue: asyncio.Queue[PipelineEvent | None],
        state: _PipelineState,
    ) -> AsyncIterator[PipelineEvent]:
        """Async iterator driver used by ``subscribe_events``."""
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item
        _ = state  # pragma: no cover


__all__ = ["ImporterFacade", "PipelineEvent"]

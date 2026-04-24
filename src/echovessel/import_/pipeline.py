"""Universal importer pipeline orchestrator.

Glue code that wires normalization → chunking → extraction → routing
→ dispatch → embed. Everything below is async because the LLM call is
async; DB work still runs synchronously inside a session (SQLModel has
no real async driver and memory writes are fast enough that offloading
to a thread pool is not worth the complexity for MVP).

## Dependency injection

The orchestrator never imports from ``echovessel.runtime``,
``channels``, or ``proactive`` (tracker §4 #11). It accepts every
collaborator as a parameter:

- ``llm``: an object exposing ``async complete(system, user, *, tier,
  max_tokens, temperature) -> str``. Duck-typed; matches
  ``echovessel.runtime.llm.base.LLMProvider``.
- ``db_session_factory``: a callable ``() -> DbSession``. Each chunk
  opens a fresh session for its own transaction.
- ``embed_fn`` + ``vector_writer``: see :mod:`echovessel.import_.embed`.
- ``event_sink``: async callable taking a ``PipelineEventLike`` (any
  dataclass with ``type`` + ``payload``). Used to surface progress to
  ``ImporterFacade``'s subscriber queues.
- ``progress``: optional :class:`ProgressSnapshot` for resume support.

The `ImporterFacade.start_pipeline` entry point handles all of the
above from the runtime side so callers of the facade never touch this
module directly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session as DbSession

from echovessel.import_.chunking import chunk_text
from echovessel.import_.embed import EmbedFn, VectorWriter, run_embed_pass
from echovessel.import_.errors import (
    ExtractionError,
    NormalizationError,
    PipelineError,
)
from echovessel.import_.extraction import extract_chunk
from echovessel.import_.models import (
    ContentItem,
    DroppedItem,
    PipelineReport,
    ProgressSnapshot,
)
from echovessel.import_.normalization import normalize_bytes, normalize_file
from echovessel.import_.routing import dispatch_item

log = logging.getLogger(__name__)


DbSessionFactory = Callable[[], DbSession]
EventSink = Callable[["PipelineEventLike"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PipelineEventLike:
    """Minimal structural event used by the orchestrator.

    Matches the shape of
    ``echovessel.runtime.importer_facade.PipelineEvent`` so the
    facade can translate back-and-forth without a dependency from this
    module up to runtime.
    """

    pipeline_id: str
    type: str
    payload: dict[str, Any]


async def run_pipeline(
    *,
    pipeline_id: str,
    upload_path: Path | str | None = None,
    raw_bytes: bytes | None = None,
    suffix: str = "",
    source_label: str = "",
    file_hash: str = "",
    persona_id: str,
    user_id: str,
    persona_context: str = "",
    llm: Any,
    db_session_factory: DbSessionFactory,
    embed_fn: EmbedFn | None,
    vector_writer: VectorWriter | None,
    event_sink: EventSink | None = None,
    progress: ProgressSnapshot | None = None,
    force_duplicate: bool = False,
) -> PipelineReport:
    """Run one import pipeline end-to-end.

    Exactly one of ``upload_path`` / ``raw_bytes`` must be provided.
    ``force_duplicate`` is currently a no-op placeholder — duplicate
    detection lives at the runtime layer (``start`` endpoint), not
    inside the pipeline itself.

    The function never raises for per-chunk failures; it records them
    in the returned :class:`PipelineReport` and continues. Fatal
    errors (permanent normalization failure, unrecoverable LLM
    schema violation) still propagate — the caller's
    ``ImporterFacade._run_pipeline`` wraps this function in a try /
    except and translates exceptions into the ``pipeline.done``
    event with ``status="failed"``.
    """
    _ = force_duplicate  # reserved for v1.x

    report = PipelineReport(
        pipeline_id=pipeline_id,
        source_label=source_label,
        file_hash=file_hash,
    )

    # --- Step 1. Normalization -----------------------------------
    try:
        text = _load_text(
            upload_path=upload_path,
            raw_bytes=raw_bytes,
            suffix=suffix,
        )
    except NormalizationError as exc:
        report.status = "failed"
        report.error_message = f"normalization: {exc}"
        await _emit(
            event_sink,
            pipeline_id,
            "chunk.error",
            {"fatal": True, "error": str(exc), "stage": "normalization"},
        )
        await _emit(
            event_sink,
            pipeline_id,
            "pipeline.done",
            {"status": "failed", "error": str(exc)},
        )
        return report

    # --- Step 2. Chunking ----------------------------------------
    chunks = chunk_text(text, source_label=source_label)
    report.total_chunks = len(chunks)

    if progress is not None:
        progress.total_chunks = len(chunks)

    start_chunk = 0 if progress is None else max(0, progress.current_chunk)
    resume_chunks = chunks[start_chunk:]

    await _emit(
        event_sink,
        pipeline_id,
        "pipeline.start",
        {
            "total_chunks": len(chunks),
            "source_label": source_label,
            "resume_from": start_chunk,
        },
    )

    all_new_concept_ids: list[int] = []
    any_chunk_succeeded = False
    any_chunk_failed = False
    fatal_error: str | None = None

    # --- Step 3-5. Per-chunk loop --------------------------------
    for offset, chunk in enumerate(resume_chunks):
        chunk_index_global = start_chunk + offset
        await _emit(
            event_sink,
            pipeline_id,
            "chunk.start",
            {
                "chunk_index": chunk_index_global,
                "total_chunks": report.total_chunks,
                "chars_in_chunk": len(chunk.content),
            },
        )
        try:
            items, dropped, summary = await extract_chunk(
                chunk,
                llm=llm,
                persona_id=persona_id,
                user_id=user_id,
                persona_context=persona_context,
                source_label=source_label,
                imported_from=file_hash,
            )
        except ExtractionError as exc:
            any_chunk_failed = True
            report.dropped_items.append(
                DroppedItem(
                    chunk_index=chunk_index_global,
                    reason=f"extraction: {exc}",
                )
            )
            await _emit(
                event_sink,
                pipeline_id,
                "chunk.error",
                {
                    "chunk_index": chunk_index_global,
                    "fatal": bool(exc.fatal),
                    "error": str(exc),
                    "stage": "extraction",
                },
            )
            # Audit P1-8: advance progress past a failed chunk so a
            # subsequent resume does not re-extract the same chunk and
            # risk duplicating any items that may have committed
            # before the failure. Failed chunks are not auto-retried;
            # the operator can re-import the file to replay them.
            if progress is not None and not exc.fatal:
                progress.current_chunk = chunk_index_global + 1
            if exc.fatal:
                fatal_error = str(exc)
                break
            continue
        except asyncio.CancelledError:
            # Preserve partial progress into the snapshot and re-raise
            # so the task-level handler in ImporterFacade can observe.
            if progress is not None:
                progress.current_chunk = chunk_index_global
                progress.state = "cancelled"
                progress.written_concept_node_ids.extend(all_new_concept_ids)
            report.status = "cancelled"
            raise

        report.dropped_items.extend(dropped)

        # --- Step 6. Dispatch + bookkeeping in a fresh DB session.
        try:
            chunk_new_ids = _dispatch_chunk_items(
                items,
                db_session_factory=db_session_factory,
                source=file_hash,
                report=report,
            )
        except Exception as exc:  # noqa: BLE001 — record and continue
            any_chunk_failed = True
            report.dropped_items.append(
                DroppedItem(
                    chunk_index=chunk_index_global,
                    reason=f"dispatch: {exc}",
                )
            )
            await _emit(
                event_sink,
                pipeline_id,
                "chunk.error",
                {
                    "chunk_index": chunk_index_global,
                    "fatal": False,
                    "error": str(exc),
                    "stage": "dispatch",
                },
            )
            # Audit P1-8: advance progress past a failed dispatch.
            # See the matching comment on the extraction branch above
            # for the rationale.
            if progress is not None:
                progress.current_chunk = chunk_index_global + 1
            continue

        all_new_concept_ids.extend(chunk_new_ids)
        any_chunk_succeeded = True
        report.processed_chunks += 1
        if progress is not None:
            progress.current_chunk = chunk_index_global + 1
            progress.written_concept_node_ids.extend(chunk_new_ids)

        await _emit(
            event_sink,
            pipeline_id,
            "chunk.done",
            {
                "chunk_index": chunk_index_global,
                "writes_count": len(items),
                "dropped_in_chunk": len(dropped),
                "summary": summary,
            },
        )

    # --- Step 7. Embed pass --------------------------------------
    # Run the embed pass in its own DB session so we can read back
    # descriptions via SQLModel.
    try:
        with db_session_factory() as db:
            written = run_embed_pass(
                db=db,
                concept_node_ids=all_new_concept_ids,
                embed_fn=embed_fn,
                vector_writer=vector_writer,
            )
        report.embedded_vector_count = written
        report.new_concept_node_ids = list(all_new_concept_ids)
    except PipelineError as exc:
        fatal_error = fatal_error or str(exc)
        any_chunk_failed = True
        report.dropped_items.append(
            DroppedItem(
                chunk_index=-1,
                reason=f"embed: {exc}",
            )
        )
        await _emit(
            event_sink,
            pipeline_id,
            "chunk.error",
            {"fatal": True, "error": str(exc), "stage": "embed"},
        )

    # --- Step 8. Status + done event -----------------------------
    if fatal_error is not None:
        report.status = "failed"
        report.error_message = fatal_error
    elif any_chunk_failed and any_chunk_succeeded:
        report.status = "partial_success"
    elif any_chunk_succeeded or report.total_chunks == 0:
        report.status = "success"
    else:
        report.status = "failed"
        report.error_message = report.error_message or "no chunks processed"

    await _emit(
        event_sink,
        pipeline_id,
        "pipeline.done",
        {
            "status": report.status,
            "processed_chunks": report.processed_chunks,
            "total_chunks": report.total_chunks,
            "writes_by_target": dict(report.writes_by_target),
            "dropped_count": len(report.dropped_items),
            "embedded_vector_count": report.embedded_vector_count,
            "error": report.error_message,
        },
    )
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_text(
    *,
    upload_path: Path | str | None,
    raw_bytes: bytes | None,
    suffix: str,
) -> str:
    if upload_path is not None and raw_bytes is not None:
        raise NormalizationError(
            "run_pipeline: pass exactly one of upload_path / raw_bytes"
        )
    if upload_path is not None:
        return normalize_file(upload_path)
    if raw_bytes is not None:
        return normalize_bytes(raw_bytes, suffix=suffix)
    raise NormalizationError(
        "run_pipeline: either upload_path or raw_bytes must be set"
    )


def _dispatch_chunk_items(
    items: list[ContentItem],
    *,
    db_session_factory: DbSessionFactory,
    source: str,
    report: PipelineReport,
) -> list[int]:
    """Write every item in ``items`` to memory under one chunk.

    NOTE: the memory import API commits per-call, so "one transaction
    per chunk" is not strictly atomic today — M-round3 chose commit-
    inside-helper semantics. If any item in the chunk fails mid-way,
    earlier writes remain on disk and we record the failure as a
    dropped item. This matches the tracker §2.6 "partial" semantics.
    """
    new_ids: list[int] = []
    with db_session_factory() as db:
        for item in items:
            result, ids = dispatch_item(item, db=db, source=source)
            report.record_write(result.content_type)
            # Track L1 append provenance so the report surfaces every
            # core_block write touched by the pipeline (Memory Timeline
            # / admin audit consume this list downstream).
            for append_id in result.core_block_append_ids:
                report.new_core_block_append_ids.append(append_id)
            new_ids.extend(ids)
    return new_ids


async def _emit(
    event_sink: EventSink | None,
    pipeline_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if event_sink is None:
        return
    await event_sink(
        PipelineEventLike(
            pipeline_id=pipeline_id,
            type=event_type,
            payload=payload,
        )
    )


__all__ = [
    "DbSessionFactory",
    "EventSink",
    "PipelineEventLike",
    "run_pipeline",
]

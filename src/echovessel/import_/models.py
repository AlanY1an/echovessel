"""Dataclasses shared across the import pipeline modules.

Nothing in this file touches I/O, memory, or the LLM — it's strictly
pure value types. The orchestrator (`pipeline.py`) wires these into the
rest of the stack.

Spec references:
- `docs/import/01-import-spec-v0.1.md` §5 (LLM processing stage)
- `docs/import/03-code-tracker.md` §2.1 (content_type whitelist)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The four legal content_types accepted by `memory.import_content`
#: (v0.5 · plan §1 dropped ``relationship_facts``).
#: Authoritative list: `src/echovessel/memory/imports.py::_ALLOWED_CONTENT_TYPES`.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    [
        "persona_traits",
        "user_identity_facts",
        "user_events",
        "user_reflections",
    ]
)


@dataclass(frozen=True, slots=True)
class Chunk:
    """A single semantic slice of the normalized source text.

    The pipeline feeds one Chunk per LLM call.
    """

    chunk_index: int
    total_chunks: int
    content: str
    offset: int = 0
    source_label: str = ""


@dataclass(frozen=True, slots=True)
class ContentItem:
    """One memory write decision coming out of LLM extraction.

    The LLM prompt emits a richer 6-target union (§5.3 of the spec);
    `routing.translate_llm_write` collapses that union into this shape
    before the pipeline dispatches to memory.

    Fields:
        content_type: One of the five whitelist strings. Validated at
            construction time.
        payload: Dict that must satisfy `memory.import_content`'s
            per-branch requirements — e.g. L1 appends need `"content"`,
            L3 event writes need `"events": [{description, ...}]`, etc.
        chunk_index: Origin chunk index for provenance / drop-tracking.
        evidence_quote: Verbatim substring from the chunk that justifies
            the write. Stored but not used by memory writes (informational).
        raw_target: The original LLM-side target string
            (e.g. ``"L1.persona_block"``) — kept for audit logging.
    """

    content_type: str
    payload: dict[str, Any]
    chunk_index: int = -1
    evidence_quote: str = ""
    raw_target: str = ""

    def __post_init__(self) -> None:
        if self.content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(
                f"ContentItem: unknown content_type {self.content_type!r}. "
                f"Allowed: {sorted(ALLOWED_CONTENT_TYPES)}"
            )


@dataclass(frozen=True, slots=True)
class DroppedItem:
    """Record of a single dropped LLM write.

    Used in `PipelineReport.dropped_items` to surface schema / validation
    failures back up to the caller (SSE event + admin UI drawer).
    """

    chunk_index: int
    reason: str
    raw_target: str = ""
    payload_excerpt: str = ""


@dataclass(slots=True)
class PipelineReport:
    """Aggregate result of a single `run_pipeline` invocation."""

    pipeline_id: str
    source_label: str
    file_hash: str
    total_chunks: int = 0
    processed_chunks: int = 0
    writes_by_target: dict[str, int] = field(default_factory=dict)
    new_concept_node_ids: list[int] = field(default_factory=list)
    new_core_block_append_ids: list[int] = field(default_factory=list)
    dropped_items: list[DroppedItem] = field(default_factory=list)
    embedded_vector_count: int = 0
    status: str = "running"  # "running" / "success" / "partial_success" / "failed" / "cancelled"
    error_message: str = ""

    def record_write(self, content_type: str) -> None:
        self.writes_by_target[content_type] = (
            self.writes_by_target.get(content_type, 0) + 1
        )


@dataclass(slots=True)
class ProgressSnapshot:
    """In-memory progress pointer for resume support (tracker §2.4).

    MVP: never persisted. Lives inside `ImporterFacade._pipelines`.
    """

    pipeline_id: str
    current_chunk: int = 0
    total_chunks: int = 0
    written_concept_node_ids: list[int] = field(default_factory=list)
    state: str = "running"  # "running" / "paused" / "cancelled" / "done" / "failed"


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "Chunk",
    "ContentItem",
    "DroppedItem",
    "PipelineReport",
    "ProgressSnapshot",
]

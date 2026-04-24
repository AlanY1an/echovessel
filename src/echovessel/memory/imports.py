"""Memory-layer Import API.

The import pipeline (higher up, in `echovessel.import_.pipeline` — not yet
implemented as of round 3) produces structured write instructions from
external content (diaries, chat logs, books, transcripts, …) and feeds
them into this module. Everything here is **pure memory writes** — no
LLM calls, no chunking, no embeddings (those happen in the pipeline).

Public entry points (spec: `docs/memory/02-architecture-v0.3.md#5.1.4`):

    import_content(...)                        — high-level dispatcher
    append_to_core_block(...)                  — L1 append with audit
    bulk_create_events(...)                    — L3 batch insert
    bulk_create_thoughts(...)                  — L4 batch insert
    count_events_by_imported_from(...)         — duplicate-detection
    count_thoughts_by_imported_from(...)       — duplicate-detection

The `import_content` dispatcher is what Thread RT-round3's `ImporterFacade`
will call per-chunk; the lower-level helpers are available for tests and
for any caller that already has a validated payload.

Invariants (schema spec §3.6 CHECK + review R2 / M4):

- `imported_from` and `source_session_id` are **mutually exclusive** on
  `concept_nodes`. The SQLite CHECK constraint is the last line of
  defence; application code here sets only one of the two.
- Every `append_to_core_block` call records a row in `core_block_appends`
  **within the same transaction** as the update to `core_blocks.content`.
- No embeddings are computed inside this module. Callers that want their
  L3/L4 imports to be retrievable later must push vectors into the
  `StorageBackend` separately. (The tracker keeps Round 3 out of that
  path; embeddings will land in a later round when import pipeline
  code ships.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory.models import (
    ConceptNode,
    CoreBlock,
    CoreBlockAppend,
)
from echovessel.memory.observers import MemoryEventObserver, _fire_lifecycle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content type routing
# ---------------------------------------------------------------------------


#: Content types the high-level `import_content` dispatcher accepts.
#: The list is deliberately small and stable — v0.3 covers exactly the
#: five buckets called out in the round3 tracker §2.2. Adding a new
#: bucket is additive but requires a tracker patch.
ImportContentType = Literal[
    "persona_traits",       # L1.persona_block append
    "user_identity_facts",  # L1.user_block append
    "user_events",          # L3.event bulk insert
    "user_reflections",     # L4.thought bulk insert
    "relationship_facts",   # L1.relationship_block append
]

_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    [
        "persona_traits",
        "user_identity_facts",
        "user_events",
        "user_reflections",
        "relationship_facts",
    ]
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary returned by `import_content` for one call."""

    content_type: str
    core_block_append_ids: tuple[int, ...] = ()
    event_ids: tuple[int, ...] = ()
    thought_ids: tuple[int, ...] = ()

    @property
    def total_writes(self) -> int:
        return (
            len(self.core_block_append_ids)
            + len(self.event_ids)
            + len(self.thought_ids)
        )


# ---------------------------------------------------------------------------
# Payload dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventInput:
    """Pure input shape for `bulk_create_events`.

    The caller has already validated `imported_from` (file hash or
    similar stable identifier). This dataclass intentionally omits
    `source_session_id` — the schema CHECK constraint forbids both
    being set, and this is the import path.
    """

    persona_id: str
    user_id: str
    description: str
    emotional_impact: int = 0
    emotion_tags: tuple[str, ...] = ()
    relational_tags: tuple[str, ...] = ()
    imported_from: str = ""


@dataclass(frozen=True, slots=True)
class ThoughtInput:
    """Pure input shape for `bulk_create_thoughts`."""

    persona_id: str
    user_id: str
    description: str
    emotional_impact: int = 0
    emotion_tags: tuple[str, ...] = ()
    relational_tags: tuple[str, ...] = ()
    imported_from: str = ""


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def import_content(
    db: DbSession,
    *,
    source: str,
    content_type: ImportContentType,
    payload: dict[str, Any],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> ImportResult:
    """Route an import write to the right memory primitive.

    Arguments:
        db: Active SQLModel session. The function commits before
            returning.
        source: Stable identifier for the originating file / chunk batch
            (typically a file hash). Stored on `concept_nodes.imported_from`
            and in the `core_block_appends.provenance_json.imported_from`
            field.
        content_type: One of the five strings listed in
            `ImportContentType`. Any other value raises `ValueError`.
        payload: Dict with shape depending on `content_type`. See the
            per-branch comments below for the expected keys.
        observer: Optional post-commit notification hook.
        now: Override for the commit timestamp (tests).

    Returns:
        `ImportResult` with the IDs of everything that was written.

    Raises:
        ValueError: `content_type` is not in the whitelist, or the
            payload is missing required keys for that branch.
    """
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ValueError(
            f"import_content: unknown content_type {content_type!r}. "
            f"Allowed: {sorted(_ALLOWED_CONTENT_TYPES)}"
        )

    now = now or datetime.now()
    persona_id = payload.get("persona_id")
    user_id = payload.get("user_id")
    if not persona_id:
        raise ValueError(
            f"import_content[{content_type}]: payload missing 'persona_id'"
        )
    if not user_id:
        raise ValueError(
            f"import_content[{content_type}]: payload missing 'user_id'"
        )

    # --- L1 core-block append branches --------------------------------

    if content_type == "persona_traits":
        content = _require_content(payload)
        append = append_to_core_block(
            db,
            persona_id=persona_id,
            user_id=None,  # persona_block is a shared block (user_id NULL)
            label=BlockLabel.PERSONA.value,
            content=content,
            provenance={
                "imported_from": source,
                "source_label": payload.get("source_label", ""),
                "chunk_index": payload.get("chunk_index"),
                "prompt_round": payload.get("prompt_round", ""),
                "notes": payload.get("notes", ""),
            },
            observer=observer,
            now=now,
        )
        return ImportResult(
            content_type=content_type,
            core_block_append_ids=(append.id,) if append.id is not None else (),
        )

    if content_type == "user_identity_facts":
        content = _require_content(payload)
        append = append_to_core_block(
            db,
            persona_id=persona_id,
            user_id=user_id,
            label=BlockLabel.USER.value,
            content=content,
            provenance={
                "imported_from": source,
                "source_label": payload.get("source_label", ""),
                "chunk_index": payload.get("chunk_index"),
                "prompt_round": payload.get("prompt_round", ""),
                "notes": payload.get("notes", ""),
                "category": payload.get("category", "other"),
            },
            observer=observer,
            now=now,
        )
        return ImportResult(
            content_type=content_type,
            core_block_append_ids=(append.id,) if append.id is not None else (),
        )

    if content_type == "relationship_facts":
        content = _require_content(payload)
        append = append_to_core_block(
            db,
            persona_id=persona_id,
            user_id=user_id,
            label=BlockLabel.RELATIONSHIP.value,
            content=content,
            provenance={
                "imported_from": source,
                "source_label": payload.get("source_label", ""),
                "chunk_index": payload.get("chunk_index"),
                "prompt_round": payload.get("prompt_round", ""),
                "notes": payload.get("notes", ""),
                "person_label": payload.get("person_label", ""),
            },
            observer=observer,
            now=now,
        )
        return ImportResult(
            content_type=content_type,
            core_block_append_ids=(append.id,) if append.id is not None else (),
        )

    # --- L3 event bulk insert branch ----------------------------------

    if content_type == "user_events":
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise ValueError(
                "import_content[user_events]: payload['events'] must be a list"
            )
        event_inputs = [
            EventInput(
                persona_id=persona_id,
                user_id=user_id,
                description=_require_description(e, where="user_events"),
                emotional_impact=int(e.get("emotional_impact", 0)),
                emotion_tags=tuple(e.get("emotion_tags", [])),
                relational_tags=tuple(e.get("relational_tags", [])),
                imported_from=source,
            )
            for e in raw_events
        ]
        event_ids = bulk_create_events(
            db,
            events=event_inputs,
            observer=observer,
            now=now,
        )
        return ImportResult(
            content_type=content_type,
            event_ids=tuple(event_ids),
        )

    # --- L4 thought bulk insert branch --------------------------------

    if content_type == "user_reflections":
        raw_thoughts = payload.get("thoughts")
        if not isinstance(raw_thoughts, list):
            raise ValueError(
                "import_content[user_reflections]: "
                "payload['thoughts'] must be a list"
            )
        thought_inputs = [
            ThoughtInput(
                persona_id=persona_id,
                user_id=user_id,
                description=_require_description(t, where="user_reflections"),
                emotional_impact=int(t.get("emotional_impact", 0)),
                emotion_tags=tuple(t.get("emotion_tags", [])),
                relational_tags=tuple(t.get("relational_tags", [])),
                imported_from=source,
            )
            for t in raw_thoughts
        ]
        thought_ids = bulk_create_thoughts(
            db,
            thoughts=thought_inputs,
            observer=observer,
            now=now,
        )
        return ImportResult(
            content_type=content_type,
            thought_ids=tuple(thought_ids),
        )

    # Unreachable given the whitelist check above, but keeps mypy happy.
    raise ValueError(f"import_content: unreachable content_type={content_type}")


# ---------------------------------------------------------------------------
# Core block append (L1)
# ---------------------------------------------------------------------------


def append_to_core_block(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str | None,
    label: str,
    content: str,
    provenance: dict[str, Any],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> CoreBlockAppend:
    """Append content to an existing L1 core block + log the provenance.

    Behavior:
      1. Look up (or create, if missing) the `core_blocks` row identified
         by (persona_id, user_id, label).
      2. Append the new text to `core_blocks.content` (separated by a
         newline if existing content is non-empty).
      3. Insert a `core_block_appends` row with the provenance JSON.
      4. Commit the transaction as a single unit.
      5. Fire `observer.on_core_block_appended` (best-effort).

    The `label` argument is a plain string matching one of the
    `BlockLabel` enum values. Unknown labels are rejected to protect
    the L1 vocabulary.
    """
    if label not in {bl.value for bl in BlockLabel}:
        raise ValueError(
            f"append_to_core_block: unknown label {label!r}. "
            f"Allowed: {sorted(bl.value for bl in BlockLabel)}"
        )
    if not content or not content.strip():
        raise ValueError("append_to_core_block: content must be non-empty")

    now = now or datetime.now()

    # Find or create the target core_blocks row. The UniqueConstraint is
    # on (persona_id, user_id, label) — note user_id can be NULL for
    # shared blocks, and IS NULL requires explicit handling in SQL.
    stmt = select(CoreBlock).where(
        CoreBlock.persona_id == persona_id,
        CoreBlock.label == label,
        CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    if user_id is None:
        stmt = stmt.where(CoreBlock.user_id.is_(None))  # type: ignore[union-attr]
    else:
        stmt = stmt.where(CoreBlock.user_id == user_id)

    block = db.exec(stmt).one_or_none()
    if block is None:
        block = CoreBlock(
            persona_id=persona_id,
            user_id=user_id,
            label=BlockLabel(label),
            content="",
            last_edited_by="import",
        )
        db.add(block)
        db.flush()

    if block.content:
        block.content = f"{block.content}\n{content}"
    else:
        block.content = content
    block.char_count = len(block.content)
    block.last_edited_by = "import"
    db.add(block)

    append = CoreBlockAppend(
        persona_id=persona_id,
        user_id=user_id,
        label=label,
        content=content,
        provenance_json=provenance,
        created_at=now,
    )
    db.add(append)

    db.commit()
    db.refresh(block)
    db.refresh(append)

    if observer is not None:
        try:
            observer.on_core_block_appended(append)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "observer.on_core_block_appended raised (append id=%s): %s",
                append.id,
                e,
            )

    return append


# ---------------------------------------------------------------------------
# L3 / L4 bulk inserts
# ---------------------------------------------------------------------------


def bulk_create_events(
    db: DbSession,
    *,
    events: list[EventInput],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Transactional bulk-insert of L3 events tagged as imports.

    All-or-nothing semantics: any failure rolls back the entire batch.
    Returns the newly-assigned primary keys in input order.

    The `imported_from` field is set from each EventInput; the schema
    CHECK constraint forbids both `imported_from` and `source_session_id`
    being non-NULL, so we deliberately do NOT set the latter.

    Embeddings are NOT computed here — the caller (import pipeline) is
    responsible for pushing vectors into the StorageBackend separately.
    """
    if not events:
        return []

    now = now or datetime.now()
    created: list[ConceptNode] = []
    for ev in events:
        if not ev.imported_from:
            raise ValueError(
                "bulk_create_events: EventInput.imported_from is required"
            )
        node = ConceptNode(
            persona_id=ev.persona_id,
            user_id=ev.user_id,
            type=NodeType.EVENT,
            description=ev.description,
            emotional_impact=ev.emotional_impact,
            emotion_tags=list(ev.emotion_tags),
            relational_tags=list(ev.relational_tags),
            imported_from=ev.imported_from,
            created_at=now,
        )
        db.add(node)
        created.append(node)
    db.flush()
    ids = [n.id for n in created if n.id is not None]
    db.commit()
    for n in created:
        db.refresh(n)

    for n in created:
        if observer is not None:
            try:
                observer.on_event_created(n)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "observer.on_event_created raised (event id=%s): %s",
                    n.id,
                    e,
                )
        _fire_lifecycle("on_event_created", n)

    return ids


def bulk_create_thoughts(
    db: DbSession,
    *,
    thoughts: list[ThoughtInput],
    observer: MemoryEventObserver | None = None,
    now: datetime | None = None,
) -> list[int]:
    """Transactional bulk-insert of L4 thoughts tagged as imports.

    Same contract as `bulk_create_events`, but inserts `type='thought'`
    rows. Unlike consolidate's reflection path, import thoughts do not
    carry `concept_node_filling` links — there are no existing events to
    reference. v1.0 may extend this to accept soft-chain hints.
    """
    if not thoughts:
        return []

    now = now or datetime.now()
    created: list[ConceptNode] = []
    for th in thoughts:
        if not th.imported_from:
            raise ValueError(
                "bulk_create_thoughts: ThoughtInput.imported_from is required"
            )
        node = ConceptNode(
            persona_id=th.persona_id,
            user_id=th.user_id,
            type=NodeType.THOUGHT,
            description=th.description,
            emotional_impact=th.emotional_impact,
            emotion_tags=list(th.emotion_tags),
            relational_tags=list(th.relational_tags),
            imported_from=th.imported_from,
            created_at=now,
        )
        db.add(node)
        created.append(node)
    db.flush()
    ids = [n.id for n in created if n.id is not None]
    db.commit()
    for n in created:
        db.refresh(n)

    for n in created:
        if observer is not None:
            try:
                observer.on_thought_created(n, "import")
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "observer.on_thought_created raised (thought id=%s): %s",
                    n.id,
                    e,
                )
        _fire_lifecycle("on_thought_created", n, "import")

    return ids


# ---------------------------------------------------------------------------
# Duplicate-detection helpers
# ---------------------------------------------------------------------------


def count_events_by_imported_from(
    db: DbSession, *, imported_from: str
) -> int:
    """Return how many L3 events have `imported_from == <file_hash>`.

    Used by import pipeline to detect "you already imported this file"
    and offer the user a choice (re-import / skip).
    """
    stmt = select(func.count(ConceptNode.id)).where(
        ConceptNode.imported_from == imported_from,
        ConceptNode.type == NodeType.EVENT.value,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    result = db.exec(stmt).one()
    # SQLModel sometimes returns a tuple, sometimes the scalar.
    return int(result[0] if isinstance(result, tuple) else result)


def count_thoughts_by_imported_from(
    db: DbSession, *, imported_from: str
) -> int:
    """Return how many L4 thoughts have `imported_from == <file_hash>`."""
    stmt = select(func.count(ConceptNode.id)).where(
        ConceptNode.imported_from == imported_from,
        ConceptNode.type == NodeType.THOUGHT.value,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    result = db.exec(stmt).one()
    return int(result[0] if isinstance(result, tuple) else result)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _require_content(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            "import_content: payload['content'] must be a non-empty string"
        )
    return content


def _require_description(item: Any, *, where: str) -> str:
    if not isinstance(item, dict):
        raise ValueError(
            f"import_content[{where}]: each item must be a dict, "
            f"got {type(item).__name__}"
        )
    description = item.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            f"import_content[{where}]: each item needs a non-empty 'description'"
        )
    return description


# Expose a json encoder passthrough for tests that want to round-trip
# provenance payloads. No functional role — just a convenience handle.
_provenance_json_dumps = json.dumps


__all__ = [
    "import_content",
    "append_to_core_block",
    "bulk_create_events",
    "bulk_create_thoughts",
    "count_events_by_imported_from",
    "count_thoughts_by_imported_from",
    "EventInput",
    "ThoughtInput",
    "ImportResult",
    "ImportContentType",
]

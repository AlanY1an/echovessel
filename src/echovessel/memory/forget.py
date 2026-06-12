"""Forgetting rights — the user's right to remove memories from the system.

Implements the three deletion paths from architecture v0.3 §4.12:

    4.12.1 Delete L2 message → mark L3 source_deleted (no re-extraction)
    4.12.2 Delete L3 event → interactive cascade:
              - Case A: no L4 dependents → simple soft delete
              - Case B: has dependents → return a DeletionPreview for user
                choice (cascade / orphan / cancel)
    4.12.3 "Forget event, keep lesson" → handled automatically by orphan path

All deletes are SOFT (set deleted_at). `sweep_dead_vectors` later removes
the search-index rows (concept_nodes_vec + concept_nodes_fts) for nodes
that have been dead past the retention threshold; the concept_nodes rows
themselves are never physically removed — supersede chains and the
deletion record stay intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import aliased
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    CoreBlockAppend,
    RecallMessage,
)

log = logging.getLogger(__name__)


class DeletionChoice(StrEnum):
    """User's choice when deletion has L4 dependents."""

    CASCADE = "cascade"  # delete the node + all dependent thoughts
    ORPHAN = "orphan"  # delete the node, keep thoughts but mark filling orphaned
    CANCEL = "cancel"  # abort


@dataclass(slots=True)
class DeletionPreview:
    """Summary returned when a delete has cascading consequences.

    The caller (UI layer) uses this to ask the user how to proceed. The user's
    choice feeds back into `delete_concept_node(..., choice=...)`.
    """

    target_id: int
    dependent_thought_ids: list[int]
    dependent_thought_descriptions: list[str]


# ---------------------------------------------------------------------------
# L2 · Delete recall message(s)
# ---------------------------------------------------------------------------


def delete_recall_message(
    db: DbSession,
    message_id: int,
    now: datetime | None = None,
) -> None:
    """Soft-delete a single L2 message. Marks any L3 event extracted from
    its session as `source_deleted = True` (architecture §4.12.1).

    We intentionally do NOT re-run extraction. Source is gone; any rewrite
    would be a fabrication.
    """
    now = now or datetime.now()
    msg = db.exec(
        select(RecallMessage).where(RecallMessage.id == message_id)
    ).one_or_none()
    if msg is None or msg.deleted_at is not None:
        return

    msg.deleted_at = now
    db.add(msg)

    # Mark every L3 event whose source_session_id matches the deleted message's
    # session as source_deleted. This does NOT cascade — events remain usable
    # but flagged.
    stmt = select(ConceptNode).where(
        ConceptNode.source_session_id == msg.session_id,
        ConceptNode.type == NodeType.EVENT.value,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    for event in db.exec(stmt):
        event.source_deleted = True
        db.add(event)

    db.commit()


def delete_recall_session(
    db: DbSession,
    session_id: str,
    now: datetime | None = None,
) -> None:
    """Soft-delete all L2 messages in a session and mark derived L3 events
    as source_deleted."""
    now = now or datetime.now()

    stmt = select(RecallMessage).where(
        RecallMessage.session_id == session_id,
        RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    for msg in db.exec(stmt):
        msg.deleted_at = now
        db.add(msg)

    stmt = select(ConceptNode).where(
        ConceptNode.source_session_id == session_id,
        ConceptNode.type == NodeType.EVENT.value,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    for event in db.exec(stmt):
        event.source_deleted = True
        db.add(event)

    db.commit()


# ---------------------------------------------------------------------------
# L3 / L4 · Delete concept node with cascade preview
# ---------------------------------------------------------------------------


def preview_concept_node_deletion(
    db: DbSession,
    node_id: int,
) -> DeletionPreview:
    """Inspect cascade consequences before committing a delete.

    Returns a DeletionPreview. If `dependent_thought_ids` is empty, the
    deletion can proceed directly (no user choice needed).
    """
    stmt = (
        select(ConceptNode)
        .join(ConceptNodeFilling, ConceptNodeFilling.parent_id == ConceptNode.id)
        .where(
            ConceptNodeFilling.child_id == node_id,
            ConceptNodeFilling.orphaned == False,  # noqa: E712
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    )
    dependents = list(db.exec(stmt))
    return DeletionPreview(
        target_id=node_id,
        dependent_thought_ids=[d.id for d in dependents],
        dependent_thought_descriptions=[d.description for d in dependents],
    )


def delete_concept_node(
    db: DbSession,
    node_id: int,
    choice: DeletionChoice = DeletionChoice.ORPHAN,
    now: datetime | None = None,
) -> None:
    """Perform a soft delete, handling L4 dependencies per the user's choice.

    Args:
        db: Database session.
        node_id: The ConceptNode to delete.
        choice: What to do with dependent thoughts (default ORPHAN — preserve
            insights but strip the deleted event from their filling chains).
        now: Override current time for tests.

    The vector and FTS index rows are left in place — the concept_nodes
    join filters them by deleted_at, and `sweep_dead_vectors` removes
    them once retention expires.

    Raises:
        ValueError: If choice == CANCEL. The caller should never pass CANCEL
            to this function; it should intercept at the UI layer.
    """
    if choice == DeletionChoice.CANCEL:
        raise ValueError("delete_concept_node called with CANCEL")

    now = now or datetime.now()

    node = db.exec(select(ConceptNode).where(ConceptNode.id == node_id)).one_or_none()
    if node is None or node.deleted_at is not None:
        return

    # Gather dependent thoughts (parents in the filling graph)
    dependent_thought_ids: list[int] = []
    filling_stmt = select(ConceptNodeFilling).where(
        ConceptNodeFilling.child_id == node_id,
        ConceptNodeFilling.orphaned == False,  # noqa: E712
    )
    filling_rows = list(db.exec(filling_stmt))
    dependent_thought_ids = [row.parent_id for row in filling_rows]

    if choice == DeletionChoice.CASCADE:
        # Soft-delete the node and every thought that depended on it
        node.deleted_at = now
        db.add(node)

        if dependent_thought_ids:
            stmt = select(ConceptNode).where(
                ConceptNode.id.in_(dependent_thought_ids),  # type: ignore[union-attr]
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            for thought in db.exec(stmt):
                thought.deleted_at = now
                db.add(thought)

    elif choice == DeletionChoice.ORPHAN:
        # Soft-delete just the node; mark its filling links as orphaned so
        # thoughts survive but can't retrace to a deleted event.
        node.deleted_at = now
        db.add(node)

        for row in filling_rows:
            row.orphaned = True
            row.orphaned_at = now
            db.add(row)

    db.commit()


# ---------------------------------------------------------------------------
# Retention sweep · physical search-index cleanup for long-dead nodes
# ---------------------------------------------------------------------------

DEAD_VECTOR_RETENTION_DAYS = 30


@dataclass(slots=True)
class SweepCounts:
    """How many nodes `sweep_dead_vectors` cleaned, per category."""

    soft_deleted: int = 0
    superseded: int = 0


def sweep_dead_vectors(
    db: DbSession,
    backend: StorageBackend,
    *,
    older_than_days: int = DEAD_VECTOR_RETENTION_DAYS,
    now: datetime | None = None,
) -> SweepCounts:
    """Remove search-index rows for nodes retrieval can never surface.

    Soft delete and supersede both leave the node's vector in
    `concept_nodes_vec` (and, for soft delete, its `concept_nodes_fts`
    entry): retrieval filters dead nodes out AFTER the KNN/MATCH step,
    so the scan space grows monotonically and stale near-duplicate
    vectors keep matching first (mention-dedup then has to chain-resolve
    to the live head). This sweep cleans nodes dead longer than
    `older_than_days`:

    - Soft-deleted (`deleted_at` past the threshold): vector + FTS entry
      removed. The `concept_fts_update` trigger is scoped to
      `UPDATE OF description`, so a soft delete never touches the FTS
      index — the explicit external-content 'delete' here is the only
      cleanup path.
    - Superseded (`superseded_by_id` set; the successor's `created_at`
      is the supersede time — both writes land in the same commit):
      vector removed only. The FTS entry stays because admin search
      filters on `deleted_at` alone and superseded history remains
      searchable.

    The `concept_nodes` rows are kept in both cases.

    Idempotency gate: a candidate must still have a vec row, and the vec
    + FTS deletes commit together. This matters — an external-content
    FTS 'delete' for a rowid that is no longer in the index raises
    "database disk image is malformed", so the FTS delete must fire at
    most once per row. Chain-resolve in mention-dedup keeps covering the
    window where a superseded node's vector is still present (younger
    than the threshold).
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=older_than_days)
    conn = db.connection()
    counts = SweepCounts()

    soft_deleted_rows = db.exec(
        select(ConceptNode.id, ConceptNode.description).where(  # type: ignore[call-overload]
            ConceptNode.deleted_at.is_not(None),  # type: ignore[union-attr]
            ConceptNode.deleted_at < cutoff,  # type: ignore[operator]
        )
    ).all()
    for node_id, description in soft_deleted_rows:
        if not _has_vec_row(conn, node_id):
            continue
        backend.delete_vector(node_id, conn=conn)
        conn.execute(
            text(
                "INSERT INTO concept_nodes_fts(concept_nodes_fts, rowid, description) "
                "VALUES('delete', :id, :description)"
            ),
            {"id": node_id, "description": description},
        )
        counts.soft_deleted += 1

    successor = aliased(ConceptNode)
    superseded_ids = db.exec(
        select(ConceptNode.id)  # type: ignore[call-overload]
        .join(successor, successor.id == ConceptNode.superseded_by_id)
        .where(
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            successor.created_at < cutoff,
        )
    ).all()
    for node_id in superseded_ids:
        if not _has_vec_row(conn, node_id):
            continue
        backend.delete_vector(node_id, conn=conn)
        counts.superseded += 1

    db.commit()
    return counts


def _has_vec_row(conn, node_id: int) -> bool:
    row = conn.execute(
        text("SELECT id FROM concept_nodes_vec WHERE id = :id"),
        {"id": node_id},
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# L1 · Delete a single core-block append audit row (physical delete)
# ---------------------------------------------------------------------------


def delete_core_block_append(
    db: DbSession,
    append_id: int,
) -> bool:
    """Physically delete a single `CoreBlockAppend` audit row.

    `CoreBlockAppend` is append-only (no `deleted_at` column — see
    `models.CoreBlockAppend` docstring), so the "forget" operation here
    is a real `DELETE`. The row is an audit pointer; removing it does
    NOT rewrite the live `core_blocks.content`. Reversing the append's
    textual effect is a separate concern that the admin UI handles by
    also rewriting the surviving audit trail into `core_blocks.content`
    — out of scope for this helper.

    Returns True when a row was found and deleted, False when the id
    did not exist. The caller (admin route) maps False → 404.
    """

    append = db.get(CoreBlockAppend, append_id)
    if append is None:
        return False

    db.delete(append)
    db.commit()
    return True

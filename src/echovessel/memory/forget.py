"""Forgetting rights — the user's right to remove memories from the system.

Implements the three deletion paths from architecture v0.3 §4.12:

    4.12.1 Delete L2 message → mark L3 source_deleted (no re-extraction)
    4.12.2 Delete L3 event → interactive cascade:
              - Case A: no L4 dependents → simple soft delete
              - Case B: has dependents → return a DeletionPreview for user
                choice (cascade / orphan / cancel)
    4.12.3 "Forget event, keep lesson" → handled automatically by orphan path

All deletes are SOFT (set deleted_at). Physical cleanup happens via a
separate cron job 30 days later (not implemented yet — v1.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
    backend: StorageBackend | None = None,
    now: datetime | None = None,
) -> None:
    """Perform a soft delete, handling L4 dependencies per the user's choice.

    Args:
        db: Database session.
        node_id: The ConceptNode to delete.
        choice: What to do with dependent thoughts (default ORPHAN — preserve
            insights but strip the deleted event from their filling chains).
        backend: Optional StorageBackend; if given, we also remove the row from
            the vector table. If None, the vector row is left (it's safe — the
            concept_nodes join will filter it out by deleted_at).
        now: Override current time for tests.

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
        if backend is not None:
            backend.delete_vector(node.id)

        if dependent_thought_ids:
            stmt = select(ConceptNode).where(
                ConceptNode.id.in_(dependent_thought_ids),  # type: ignore[union-attr]
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            for thought in db.exec(stmt):
                thought.deleted_at = now
                db.add(thought)
                if backend is not None:
                    backend.delete_vector(thought.id)

    elif choice == DeletionChoice.ORPHAN:
        # Soft-delete just the node; mark its filling links as orphaned so
        # thoughts survive but can't retrace to a deleted event.
        node.deleted_at = now
        db.add(node)
        if backend is not None:
            backend.delete_vector(node.id)

        for row in filling_rows:
            row.orphaned = True
            row.orphaned_at = now
            db.add(row)

    db.commit()


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

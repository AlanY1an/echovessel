"""Soft-delete specific ConceptNode rows by id, cascading to dependent thoughts.

Use case: cleanup of laundered events (Issue 5 of the 2026-04-20 dogfood
audit) — concept_nodes the consolidate pipeline produced from persona-led
content the user never confirmed. The 2026-04-20 prompt fix prevents new
laundering, but pre-existing rows must be removed by hand.

Wraps ``echovessel.memory.forget.delete_concept_node`` with CASCADE choice,
which:
  - soft-deletes the target node (sets deleted_at)
  - soft-deletes every thought whose filling chain references the target
  - scrubs the corresponding rows from the vector index

Idempotent: re-running on an already-soft-deleted id is a no-op.

Usage:
    uv run python scripts/delete_concept_nodes.py --ids 4,6            # dry-run
    uv run python scripts/delete_concept_nodes.py --ids 4,6 --commit   # apply
    uv run python scripts/delete_concept_nodes.py --ids 4,6 --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session as DbSession

from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.db import create_engine
from echovessel.memory.forget import (
    DeletionChoice,
    delete_concept_node,
    preview_concept_node_deletion,
)
from echovessel.memory.models import ConceptNode

_DEFAULT_DB = Path.home() / ".echovessel" / "memory.db"


def _parse_ids(arg: str) -> list[int]:
    out: list[int] = []
    for part in arg.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError as e:
            raise SystemExit(f"invalid id {s!r}: not an integer") from e
    return out


def _print_target(db: DbSession, node_id: int) -> bool:
    node = db.get(ConceptNode, node_id)
    if node is None:
        print(f"  id={node_id}  NOT FOUND")
        return False
    if node.deleted_at is not None:
        print(f"  id={node_id}  ALREADY DELETED at {node.deleted_at.isoformat()}")
        return False

    type_str = getattr(node.type, "value", node.type)
    print(
        f"  id={node_id}  type={type_str:<7} user={node.user_id:<24} "
        f"persona={node.persona_id}",
    )
    print(f"    description: {node.description[:160]}")

    preview = preview_concept_node_deletion(db, node_id)
    if preview.dependent_thought_ids:
        print(
            f"    cascade → {len(preview.dependent_thought_ids)} dependent thought(s): "
            f"{preview.dependent_thought_ids}",
        )
    else:
        print("    cascade → no dependents")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument(
        "--ids",
        required=True,
        help="comma-separated ConceptNode ids to delete, e.g. '4,6'",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write the soft-delete (default: dry-run)",
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    ids = _parse_ids(args.ids)
    if not ids:
        print("no ids provided", file=sys.stderr)
        return 2

    engine = create_engine(args.db)
    backend = SQLiteBackend(engine)
    mode = "COMMIT" if args.commit else "DRY-RUN"

    with DbSession(engine) as db:
        print(f"[{mode}] {len(ids)} target(s) in {args.db}:")
        deletable: list[int] = []
        for nid in ids:
            if _print_target(db, nid):
                deletable.append(nid)

        if not args.commit:
            print(f"\nRe-run with --commit to soft-delete {len(deletable)} node(s).")
            return 0

        if not deletable:
            print("\nnothing to do — every id already gone or missing.")
            return 0

        for nid in deletable:
            delete_concept_node(db, nid, choice=DeletionChoice.CASCADE, backend=backend)
        db.commit()
        print(f"\nsoft-deleted {len(deletable)} node(s) (cascade); vector rows scrubbed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

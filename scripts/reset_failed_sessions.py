"""Reset sessions stuck in status=failed back to closing for re-consolidation.

The consolidate worker marks a session FAILED when it hits an exception it
cannot retry. Some of those exceptions (sqlite WAL contention before
2026-04-21) were actually transient infra errors that should have been
retried; the sessions are recoverable now that the classifier is fixed.

This script lists every failed session, optionally writes the reset, and
strips the `|failed:<reason>` suffix from `close_trigger` so the worker
re-picks the row on its next poll. Idempotent: re-running on a clean db
finds zero failed sessions and exits 0.

Usage:
    uv run python scripts/reset_failed_sessions.py            # dry-run
    uv run python scripts/reset_failed_sessions.py --commit   # apply
    uv run python scripts/reset_failed_sessions.py --db /tmp/other.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import SessionStatus
from echovessel.memory.db import create_engine
from echovessel.memory.models import Session

_DEFAULT_DB = Path.home() / ".echovessel" / "memory.db"


def _strip_failed_suffix(close_trigger: str | None) -> str | None:
    """Drop the ``|failed:<reason>`` suffix appended by ``_mark_failed``.

    The mark-failed code path appends one segment per failure, so we split
    on the first occurrence and keep everything before it. Multiline
    ``reason`` content (e.g. SQLAlchemy errors with embedded newlines and
    SQL fragments) is fully discarded with the segment."""
    if not close_trigger:
        return close_trigger
    cleaned = close_trigger.split("|failed:", 1)[0]
    return cleaned or None


def _list_failed(db: DbSession) -> list[Session]:
    stmt = select(Session).where(
        Session.status == SessionStatus.FAILED,
        Session.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    return list(db.exec(stmt))


def _print_row(s: Session) -> None:
    new_trigger = _strip_failed_suffix(s.close_trigger)
    print(
        f"  {s.id}  channel={s.channel_id:<8} user={s.user_id:<24} "
        f"messages={s.message_count:>3}  extracted_events={int(s.extracted_events)}",
    )
    print(f"    close_trigger before: {s.close_trigger!r}")
    print(f"    close_trigger after : {new_trigger!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write the reset (default: dry-run)",
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    engine = create_engine(args.db)
    with DbSession(engine) as db:
        failed = _list_failed(db)
        if not failed:
            print(f"no failed sessions in {args.db}")
            return 0

        mode = "COMMIT" if args.commit else "DRY-RUN"
        print(f"[{mode}] {len(failed)} failed session(s) in {args.db}:")
        for s in failed:
            _print_row(s)

        if not args.commit:
            print("\nRe-run with --commit to apply.")
            return 0

        for s in failed:
            s.status = SessionStatus.CLOSING
            s.close_trigger = _strip_failed_suffix(s.close_trigger)
            db.add(s)
        db.commit()
        print(f"\nreset {len(failed)} session(s) → status=closing")
        print("the consolidate worker will re-pick them on its next poll.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

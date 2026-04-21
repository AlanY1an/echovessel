"""Migrate transport-native ``user_id`` values to the internal ``"self"``.

Companion to the 2026-04-20 ``external_identities`` rollout. Pre-existing
db rows hold the raw transport id channels used to mint:

    discord  → snowflake (e.g. ``"753654474022584361"``)
    imessage → handle (phone, e.g. ``"+1747..."``)
    web      → ``"self"`` (already correct, no change needed)

After this script runs, every persisted ``user_id`` is ``"self"``, and
``external_identities`` carries one row per ``(channel_id, raw_id)``
mapping back to ``"self"`` so the historical transport identity is
preserved (and rebindable when multi-user / group-chat lands).

Affected tables (every column named ``user_id`` other than the
``users.id`` PK and ``external_identities.internal_user_id``):

    sessions, recall_messages, concept_nodes, core_blocks

``core_blocks.user_id`` is nullable (shared blocks have NULL); rows with
NULL are left alone.

Idempotent: re-running on a clean db finds zero pairs and exits 0. Uses
``INSERT OR IGNORE`` for the mapping rows so a partial earlier run
doesn't double-insert.

Usage:
    uv run python scripts/backfill_internal_user_ids.py            # dry-run
    uv run python scripts/backfill_internal_user_ids.py --commit
    uv run python scripts/backfill_internal_user_ids.py --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.memory.db import create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date

_DEFAULT_DB = Path.home() / ".echovessel" / "memory.db"
_INTERNAL_DEFAULT = "self"

# Tables to scan + rewrite. ``where_clause`` excludes already-migrated
# rows so the dry-run preview is accurate after a partial run.
_SCAN_TABLES: tuple[tuple[str, bool], ...] = (
    # (table_name, has_channel_id)
    ("sessions", True),
    ("recall_messages", True),
    ("concept_nodes", False),
    ("core_blocks", False),
)


def _discover_pairs(db: DbSession) -> dict[tuple[str, str], dict[str, int]]:
    """Walk every table; return ``{(channel_id, raw_user_id): {table: count}}``.

    For tables without a ``channel_id`` column (concept_nodes, core_blocks)
    we look up the most-common channel for the same ``user_id`` from
    ``recall_messages`` so the mapping row carries useful provenance.
    Falls back to ``"unknown"`` if the user_id has never appeared with
    a channel context.
    """
    counts: dict[tuple[str, str], dict[str, int]] = {}

    # Step A: tables with channel_id — direct GROUP BY
    for table, has_channel in _SCAN_TABLES:
        if not has_channel:
            continue
        rows = db.exec(
            text(
                f"SELECT channel_id, user_id, COUNT(*) "  # noqa: S608
                f"FROM {table} "
                f"WHERE user_id != :self AND user_id IS NOT NULL "
                f"GROUP BY channel_id, user_id"
            ),
            params={"self": _INTERNAL_DEFAULT},  # type: ignore[call-arg]
        ).all()
        for ch, uid, n in rows:
            key = (ch, uid)
            counts.setdefault(key, {})[table] = int(n)

    # Step B: build a user_id → channel_id hint map from what we already
    # found (covers the dogfood scenario where every non-self user_id
    # exists somewhere in recall_messages with a channel).
    uid_to_channel: dict[str, str] = {uid: ch for (ch, uid) in counts}

    # Step C: tables without channel_id — fall back to the hint map
    for table, has_channel in _SCAN_TABLES:
        if has_channel:
            continue
        rows = db.exec(
            text(
                f"SELECT user_id, COUNT(*) "  # noqa: S608
                f"FROM {table} "
                f"WHERE user_id != :self AND user_id IS NOT NULL "
                f"GROUP BY user_id"
            ),
            params={"self": _INTERNAL_DEFAULT},  # type: ignore[call-arg]
        ).all()
        for uid, n in rows:
            ch = uid_to_channel.get(uid, "unknown")
            key = (ch, uid)
            counts.setdefault(key, {})[table] = int(n)

    return counts


def _print_preview(counts: dict[tuple[str, str], dict[str, int]]) -> None:
    if not counts:
        print("no transport-native user_ids found — db already migrated.")
        return
    total_rows = sum(sum(t.values()) for t in counts.values())
    print(f"{len(counts)} mapping(s), {total_rows} row(s) will be rewritten:\n")
    for (ch, uid), per_table in sorted(counts.items()):
        rows_summary = ", ".join(f"{t}={n}" for t, n in sorted(per_table.items()))
        print(
            f"  ({ch:<8} {uid}) → "
            f"internal_user_id={_INTERNAL_DEFAULT!r}   [{rows_summary}]"
        )


def _apply(db: DbSession, counts: dict[tuple[str, str], dict[str, int]]) -> None:
    # Insert mapping rows first so the FK constraint stays satisfied
    # for any future reader; INSERT OR IGNORE keeps re-runs safe.
    for ch, uid in counts:
        if ch == "unknown":
            # Don't fabricate a channel — the row mapping needs a real
            # channel to be useful. Skip the insert, still rewrite the
            # downstream rows so they stop dangling.
            continue
        db.exec(
            text(
                "INSERT OR IGNORE INTO external_identities "
                "(channel_id, external_id, internal_user_id) "
                "VALUES (:ch, :uid, :self)"
            ),
            params={"ch": ch, "uid": uid, "self": _INTERNAL_DEFAULT},  # type: ignore[call-arg]
        )

    # Rewrite each table's user_id column.
    for table, _ in _SCAN_TABLES:
        db.exec(
            text(
                f"UPDATE {table} SET user_id = :self "  # noqa: S608
                f"WHERE user_id != :self AND user_id IS NOT NULL"
            ),
            params={"self": _INTERNAL_DEFAULT},  # type: ignore[call-arg]
        )

    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write the migration (default: dry-run)",
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    engine = create_engine(args.db)
    # The script may run on a db that predates the external_identities
    # table (the migration only fires on daemon startup). Apply the
    # idempotent schema bring-up here so the INSERT below has a target.
    ensure_schema_up_to_date(engine)
    mode = "COMMIT" if args.commit else "DRY-RUN"

    with DbSession(engine) as db:
        counts = _discover_pairs(db)
        print(f"[{mode}] {args.db}")
        _print_preview(counts)

        if not counts:
            return 0

        if not args.commit:
            print("\nRe-run with --commit to apply.")
            return 0

        _apply(db, counts)
        print(f"\nrewrote {sum(sum(t.values()) for t in counts.values())} row(s); "
              f"inserted up to {len(counts)} external_identities mapping(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

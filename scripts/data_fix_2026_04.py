"""One-shot data fix companion to the v0.4 6-layer-memory migration.

The migration (``ensure_schema_up_to_date``) already runs the destructive
pieces that are safe to do automatically:

- DELETE ``core_blocks`` WHERE label='mood' (physical wipe, plan §4.7)
- Backfill ``concept_nodes.source_turn_ids`` from the legacy scalar

Everything else in the plan's §4.7 list is either owner-scoped
(``users.<owner>.timezone`` depends on who you are) or LLM-assisted
(rewriting the user block into third person). Both are handled here as
guided steps rather than baked into migration so the owner can eyeball
the result before committing.

Dry-run is the default — nothing is written. Pass ``--commit`` to
actually apply. Re-running on an already-fixed DB is a no-op.

Usage
-----

    uv run python scripts/data_fix_2026_04.py                     # dry-run
    uv run python scripts/data_fix_2026_04.py --commit
    uv run python scripts/data_fix_2026_04.py --db /tmp/x.db \\
        --owner-user-id self --owner-timezone America/New_York --commit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

from echovessel.memory.db import create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date

_DEFAULT_DB = Path.home() / ".echovessel" / "memory.db"


def _count(conn, sql: str, params: dict | None = None) -> int:
    return int(conn.execute(text(sql), params or {}).scalar() or 0)


def _report(conn) -> dict[str, int]:
    """Describe what state the DB currently holds."""
    out: dict[str, int] = {}
    out["mood_blocks_remaining"] = _count(
        conn, "SELECT COUNT(*) FROM core_blocks WHERE label='mood'"
    )
    out["concept_nodes_needing_turn_ids_backfill"] = _count(
        conn,
        "SELECT COUNT(*) FROM concept_nodes "
        "WHERE source_turn_id IS NOT NULL AND source_turn_ids = '[]'",
    )
    out["users_without_timezone"] = _count(
        conn, "SELECT COUNT(*) FROM users WHERE timezone IS NULL"
    )
    out["user_block_rows"] = _count(conn, "SELECT COUNT(*) FROM core_blocks WHERE label='user'")
    return out


def _apply_timezone(conn, user_id: str, tz: str) -> bool:
    """Set ``users.<user_id>.timezone``. No-op if already set to same value."""
    current = conn.execute(text("SELECT timezone FROM users WHERE id=:u"), {"u": user_id}).scalar()
    if current == tz:
        return False
    conn.execute(
        text("UPDATE users SET timezone=:t WHERE id=:u"),
        {"u": user_id, "t": tz},
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="apply changes (default is a dry-run preview)",
    )
    parser.add_argument(
        "--owner-user-id",
        type=str,
        default="self",
        help="which users.id row to populate the timezone for (default 'self')",
    )
    parser.add_argument(
        "--owner-timezone",
        type=str,
        default=None,
        help=(
            "IANA timezone string (e.g. 'America/New_York') to set on the "
            "owner user row. Skip to leave timezone untouched — the web "
            "channel will fill it in on first connect (plan decision 5)."
        ),
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    engine = create_engine(args.db)
    # Bring the schema up first — this script's checks assume v0.4 shape.
    ensure_schema_up_to_date(engine)

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"[{mode}] {args.db}")

    with engine.begin() as conn:
        before = _report(conn)
        print("\nBefore:")
        for k, v in before.items():
            print(f"  {k}: {v}")

        if before["mood_blocks_remaining"]:
            # Migration already ran above — if any remain, something
            # else is creating them (mood.py still writes until Phase 2).
            # Sweep again defensively.
            if args.commit:
                conn.execute(text("DELETE FROM core_blocks WHERE label='mood'"))
            print("\n- mood core_blocks remained after migration; sweep re-run.")

        if before["concept_nodes_needing_turn_ids_backfill"]:
            if args.commit:
                conn.execute(
                    text(
                        "UPDATE concept_nodes "
                        "SET source_turn_ids = json_array(source_turn_id) "
                        "WHERE source_turn_id IS NOT NULL "
                        "AND source_turn_ids = '[]'"
                    )
                )
            print("\n- concept_nodes: backfilled source_turn_ids JSON from legacy scalar.")

        if args.owner_timezone:
            changed = (
                _apply_timezone(conn, args.owner_user_id, args.owner_timezone)
                if args.commit
                else None
            )
            if args.commit:
                if changed:
                    print(
                        f"\n- users[{args.owner_user_id}].timezone set to {args.owner_timezone!r}."
                    )
                else:
                    print(
                        f"\n- users[{args.owner_user_id}].timezone already "
                        f"{args.owner_timezone!r}; no change."
                    )
            else:
                print(
                    f"\n- would set users[{args.owner_user_id}].timezone = {args.owner_timezone!r}."
                )
        else:
            print(
                "\n- users.timezone not touched. Pass --owner-timezone to "
                "set it from the CLI; otherwise the web channel will fill "
                "it on first connect (plan decision 5)."
            )

        # The user-block third-person rewrite (plan §4.7) needs an LLM
        # round-trip against the current owner's preferred provider.
        # That's out of scope for a stdlib script — surface it as a todo.
        print(
            "\n- REMINDER: core_blocks.user may still contain first-person "
            "prose (plan §4.7 quality-analysis Q3). Rewrite to third person "
            "manually via the admin UI or a follow-up LLM script."
        )

        if args.commit:
            after = _report(conn)
            print("\nAfter:")
            for k, v in after.items():
                print(f"  {k}: {v}")
        else:
            print("\nRe-run with --commit to apply the changes above.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Purge old rows from turn_traces + session_traces (Spec 4 · TTL).

Dev-mode traces are unbounded by default — every turn writes one
``turn_traces`` row and every consolidate run writes one
``session_traces`` row. Left alone this grows forever, so we sweep
anything older than ``--days`` (default 14). The owner can wire this
into cron; no daemon-side scheduler is involved.

Usage:
    python scripts/purge_old_traces.py                 # 14-day retention
    python scripts/purge_old_traces.py --days 30       # 30-day retention
    python scripts/purge_old_traces.py --db /path.db   # non-default DB
    python scripts/purge_old_traces.py --dry-run       # count only

Returns nonzero exit only on DB access failure; a clean run with zero
matching rows prints "deleted: turn_traces=0 session_traces=0" and
exits 0 so cron doesn't flap.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--days",
        type=int,
        default=14,
        help="Retention window in days (default: 14)",
    )
    p.add_argument(
        "--db",
        default=str(Path.home() / ".echovessel" / "memory.db"),
        help="Path to the EchoVessel memory DB (default: ~/.echovessel/memory.db)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without actually deleting",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}", file=sys.stderr)
        return 1

    cutoff = datetime.utcnow() - timedelta(days=max(0, int(args.days)))
    conn = sqlite3.connect(db_path)
    try:
        if args.dry_run:
            t_n = conn.execute(
                "SELECT COUNT(*) FROM turn_traces WHERE started_at < ?",
                (cutoff.isoformat(),),
            ).fetchone()[0]
            s_n = conn.execute(
                "SELECT COUNT(*) FROM session_traces "
                "WHERE finished_at IS NOT NULL AND finished_at < ?",
                (cutoff.isoformat(),),
            ).fetchone()[0]
            print(f"dry-run: would delete turn_traces={t_n} session_traces={s_n}")
            return 0
        t_cur = conn.execute(
            "DELETE FROM turn_traces WHERE started_at < ?",
            (cutoff.isoformat(),),
        )
        # session_traces has nullable finished_at (trivial sessions may
        # land here mid-process); only sweep rows that have finished.
        s_cur = conn.execute(
            "DELETE FROM session_traces "
            "WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff.isoformat(),),
        )
        conn.commit()
        print(
            f"deleted: turn_traces={t_cur.rowcount} "
            f"session_traces={s_cur.rowcount}"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Dogfood helper for the 9-case validation · local-dev only.

Skips the 30-min idle wait by directly manipulating ~/.echovessel/memory.db:

  --close-current           Flip the current OPEN session to status='closing'
                            so consolidate_worker picks it up on next 5s poll.
  --force-reextract SID     Reset one session so worker re-runs extraction
                            + reflection + G phase (slow_tick).
  --seed-case-7             Create a past-week event "interview next week" so
                            case 7 can be tested without waiting a week.
  --seed-case-8             Create two events, one for "Scott" and one for
                            "黄逸扬", so case 8 can be tested after the next
                            consolidate alias-dedup pass.
  --seed-case-9             Inject a slow_cycle-style L4 thought so case 9 has
                            something retrievable without waiting for G phase.
  --state                   Dump current persona + recent sessions + last
                            5 slow_tick_runs + count of thoughts with
                            subject='persona'.

All ops are idempotent-safe: they read first, write only when needed,
print what they changed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB = Path.home() / ".echovessel" / "memory.db"


def _conn() -> sqlite3.Connection:
    if not DB.exists():
        print(f"ERROR: {DB} not found", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def close_current() -> None:
    conn = _conn()
    cur = conn.execute(
        "SELECT id, persona_id, user_id, channel_id, message_count "
        "FROM sessions WHERE status='open' "
        "ORDER BY last_message_at DESC LIMIT 5"
    )
    rows = cur.fetchall()
    if not rows:
        print("no OPEN sessions to close")
        return
    for row in rows:
        conn.execute(
            "UPDATE sessions SET status='closing', close_trigger='manual_dogfood' "
            "WHERE id=?",
            (row["id"],),
        )
        print(
            f"closed session {row['id']} "
            f"({row['channel_id']} · {row['message_count']} msgs)"
        )
    conn.commit()
    print("\nconsolidate_worker polls every 5s · extraction + reflection + G phase "
          "should run within ~10s. Check with --state or the admin transcripts endpoint.")


def force_reextract(session_id: str) -> None:
    conn = _conn()
    cur = conn.execute("SELECT id, status, extracted FROM sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    if not row:
        print(f"session {session_id} not found")
        return
    conn.execute(
        "UPDATE sessions SET status='closing', extracted=0, extracted_events=0, "
        "extracted_at=NULL, close_trigger='manual_reextract' WHERE id=?",
        (session_id,),
    )
    conn.commit()
    print(f"reset session {session_id} · worker will re-run extraction + reflection + slow_tick")


def seed_case_7(persona_id: str = "default", user_id: str = "self") -> None:
    """Insert an event dated a week ago with an explicit event_time window."""
    conn = _conn()
    now = datetime.utcnow()
    one_week_ago = now - timedelta(days=7)
    event_day = now - timedelta(days=2)

    # Insert into concept_nodes
    cur = conn.execute(
        """INSERT INTO concept_nodes (
            persona_id, user_id, type, subject, description,
            emotional_impact, emotion_tags, relational_tags,
            source_deleted, source_session_id, source_turn_ids,
            event_time_start, event_time_end,
            created_at, access_count, last_accessed_at,
            mention_count
        ) VALUES (?, ?, 'event', 'user', ?,
                  ?, ?, ?,
                  0, NULL, '[]',
                  ?, ?,
                  ?, 0, NULL, 1)""",
        (
            persona_id, user_id,
            "用户说下周要做一个重要面试 · 有点紧张",
            6,
            json.dumps(["anticipation", "nervous"], ensure_ascii=False),
            json.dumps(["identity-bearing"], ensure_ascii=False),
            event_day.date().isoformat(),  # event_time_start (the interview day)
            event_day.date().isoformat(),  # event_time_end
            one_week_ago.isoformat(),       # created_at (a week ago — so it's "old")
        ),
    )
    new_id = cur.lastrowid
    conn.commit()
    print(f"seeded L3 event id={new_id} · interview on {event_day.date()} · "
          f"created a week ago")
    print("now ask the persona: '你还记得那个面试吗？感觉怎么样'")
    print("→ it should say '前几天' / '上周' / '几天前' · NOT '下周'")


def seed_case_8(persona_id: str = "default", user_id: str = "self") -> None:
    """Two events, one per alias ('Scott' and '黄逸扬'), + the entity + 2 alias rows."""
    conn = _conn()
    now = datetime.utcnow()

    # Insert entity
    cur = conn.execute(
        """INSERT INTO entities (persona_id, user_id, canonical_name, kind, merge_status,
                                 created_at, updated_at)
           VALUES (?, ?, ?, 'person', 'confirmed', ?, ?)""",
        (persona_id, user_id, "黄逸扬", now.isoformat(), now.isoformat()),
    )
    entity_id = cur.lastrowid

    for alias in ("Scott", "黄逸扬"):
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
            (alias, entity_id),
        )

    # Two events, one per alias
    events = [
        ("用户和 Scott 吃了饭 · Scott 最近压力挺大", "Scott"),
        ("黄逸扬跟用户说他想换工作 · 还在找", "黄逸扬"),
    ]
    for desc, _alias in events:
        cur = conn.execute(
            """INSERT INTO concept_nodes (
                persona_id, user_id, type, subject, description,
                emotional_impact, emotion_tags, relational_tags,
                source_deleted, source_session_id, source_turn_ids,
                created_at, access_count, mention_count
            ) VALUES (?, ?, 'event', 'user', ?,
                      2, '["concern"]', '[]',
                      0, NULL, '[]',
                      ?, 0, 1)""",
            (persona_id, user_id, desc, now.isoformat()),
        )
        event_id = cur.lastrowid
        conn.execute(
            "INSERT INTO concept_node_entities (node_id, entity_id) VALUES (?, ?)",
            (event_id, entity_id),
        )
    conn.commit()
    print(f"seeded entity '黄逸扬' (id={entity_id}) with aliases Scott + 黄逸扬 + 2 L3 events")
    print("now ask: 'Scott 最近怎么样了' → should mention '换工作'; or")
    print("         '黄逸扬最近怎么样' → should mention '吃饭 / 压力大'")


def seed_case_9(persona_id: str = "default", user_id: str = "self") -> None:
    """Inject a slow_cycle-style L4 thought so case 9 has something retrievable."""
    conn = _conn()
    now = datetime.utcnow()
    cur = conn.execute(
        """INSERT INTO concept_nodes (
            persona_id, user_id, type, subject, description,
            emotional_impact, emotion_tags, relational_tags,
            source_deleted, source_session_id, source_turn_ids,
            created_at, access_count, mention_count
        ) VALUES (?, ?, 'thought', 'persona', ?,
                  3, '["reflective"]', '[]',
                  0, NULL, '[]',
                  ?, 0, 0)""",
        (persona_id, user_id,
         "我这几天总在想你那个 grad school 申请的事 · 不知道你决定好了没",
         now.isoformat()),
    )
    thought_id = cur.lastrowid
    conn.commit()
    print(f"seeded L4 thought id={thought_id} · subject='persona' · "
          f"description references 'grad school 申请'")
    print("now ask: '你最近想我吗' → should reference 'grad school 申请'")
    print("NOTE: this is a manual seed · real slow_cycle product is identical shape.")


def state() -> None:
    conn = _conn()
    print("## personas ##")
    for r in conn.execute("SELECT id, timezone, episodic_state, last_slow_tick_at "
                          "FROM personas"):
        print(f"  {r['id']} · tz={r['timezone']} · last_slow_tick={r['last_slow_tick_at']} · "
              f"episodic={r['episodic_state']}")

    print("\n## sessions (recent 5) ##")
    for r in conn.execute("SELECT id, channel_id, status, message_count, "
                          "last_message_at, close_trigger, extracted "
                          "FROM sessions ORDER BY last_message_at DESC LIMIT 5"):
        print(f"  {r['id'][:12]}.. · {r['channel_id']} · {r['status']} · "
              f"{r['message_count']} msgs · extracted={bool(r['extracted'])} · "
              f"trigger={r['close_trigger']}")

    print("\n## thoughts with subject='persona' (slow_cycle candidates) ##")
    for r in conn.execute("SELECT id, description, created_at FROM concept_nodes "
                          "WHERE type='thought' AND subject='persona' "
                          "ORDER BY created_at DESC LIMIT 5"):
        print(f"  id={r['id']} · {r['created_at']} · {r['description'][:80]}")

    print("\n## entities ##")
    for r in conn.execute("SELECT id, canonical_name, kind, merge_status FROM entities "
                          "LIMIT 10"):
        aliases = [a["alias"] for a in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id=?", (r["id"],))]
        print(f"  id={r['id']} · {r['canonical_name']} ({r['kind']}) · "
              f"{r['merge_status']} · aliases={aliases}")

    # slow_tick_runs may or may not exist depending on Spec 6 migration state
    try:
        print("\n## slow_tick_runs (last 5) ##")
        for r in conn.execute("SELECT * FROM slow_tick_runs ORDER BY created_at DESC LIMIT 5"):
            print(f"  {dict(r)}")
    except sqlite3.OperationalError:
        print("\n## slow_tick_runs ## (table not present yet)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dogfood helper for 9-case validation")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--close-current", action="store_true",
                   help="Flip current OPEN sessions to closing")
    g.add_argument("--force-reextract", metavar="SID",
                   help="Reset one session so worker re-extracts + re-reflects + re-slow_ticks")
    g.add_argument("--seed-case-7", action="store_true",
                   help="Seed a week-old event for time-anchor testing")
    g.add_argument("--seed-case-8", action="store_true",
                   help="Seed two events + entity with aliases for cross-alias recall testing")
    g.add_argument("--seed-case-9", action="store_true",
                   help="Seed a slow_cycle-style L4 thought for parallel-existing testing")
    g.add_argument("--state", action="store_true", help="Dump relevant state")
    args = ap.parse_args()

    if args.close_current:
        close_current()
    elif args.force_reextract:
        force_reextract(args.force_reextract)
    elif args.seed_case_7:
        seed_case_7()
    elif args.seed_case_8:
        seed_case_8()
    elif args.seed_case_9:
        seed_case_9()
    elif args.state:
        state()


if __name__ == "__main__":
    main()

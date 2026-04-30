"""scripts/migrate_proactive_jsonl.py tests · Stage 4.5.

The migration is a one-shot manual tool: ``ensure_schema_up_to_date``
must NOT run it (every daemon boot would dupe-insert), so the test
just confirms the round-trip integrity of the JSONL → SQLite
mapping when the script is invoked directly.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import (
    Persona,
    User,
)
from echovessel.proactive.core.models import (
    ProactiveDecision as PersistedDecision,
)

_NOW = datetime(2026, 5, 1, 14, 0, 0)


def _seed(engine) -> None:
    with Session(engine) as db:
        db.add(Persona(id="p", display_name="P"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _import_migrate_module():
    """Load the script as a module so the test can call its
    ``migrate`` function directly."""
    repo = Path(__file__).resolve().parents[2]
    scripts_dir = repo / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import migrate_proactive_jsonl as m

    return m


def test_migrate_inserts_one_row_per_jsonl_line(tmp_path: Path):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    log_dir = tmp_path / "logs"
    today = date.today().isoformat()
    rows = [
        {
            "decision_id": "d-1",
            "timestamp": (_NOW - timedelta(hours=2)).isoformat(),
            "persona_id": "p",
            "user_id": "self",
            "trigger": "high_emotional_event",
            "trigger_payload": {"event_id": 1, "emotional_impact": -9},
            "action": "send",
            "skip_reason": None,
            "message_text": "thinking of you",
            "ingest_message_id": 42,
            "voice_used": False,
        },
        {
            "decision_id": "d-2",
            "timestamp": (_NOW - timedelta(hours=5)).isoformat(),
            "persona_id": "p",
            "user_id": "self",
            "trigger": "quiet_hours_gate",
            "action": "skip",
            "skip_reason": "quiet_hours",
            "policy_snapshot": {"quiet_hours_start": 23, "quiet_hours_end": 7},
        },
    ]
    _write_jsonl(log_dir / f"proactive-{today}.jsonl", rows)

    m = _import_migrate_module()

    inserted = m.migrate(
        log_dir=log_dir,
        engine=engine,
        dry_run=False,
    )

    assert inserted == 2
    with Session(engine) as db:
        persisted = list(
            db.exec(
                select(PersistedDecision).order_by(
                    PersistedDecision.timestamp.asc()
                )
            )
        )
    assert len(persisted) == 2
    fire = next(d for d in persisted if d.decision_id == "d-1")
    skip = next(d for d in persisted if d.decision_id == "d-2")
    assert fire.action == "fire"
    assert fire.message_text == "thinking of you"
    assert fire.sent_message_id == 42
    assert fire.trigger_type == "high_emotional_event"
    assert skip.action == "suppress"
    assert skip.suppress_reason == "quiet_hours"
    assert skip.gate_state_snapshot is not None  # JSON-serialised


def test_migrate_dry_run_does_not_persist(tmp_path: Path):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    log_dir = tmp_path / "logs"
    today = date.today().isoformat()
    _write_jsonl(
        log_dir / f"proactive-{today}.jsonl",
        [
            {
                "decision_id": "d-x",
                "timestamp": _NOW.isoformat(),
                "persona_id": "p",
                "user_id": "self",
                "trigger": "long_silence",
                "action": "skip",
                "skip_reason": "no_trigger_match",
            }
        ],
    )

    m = _import_migrate_module()
    inserted = m.migrate(log_dir=log_dir, engine=engine, dry_run=True)
    assert inserted == 1  # the would-be count
    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert rows == []


def test_migrate_skips_blank_and_malformed_lines(tmp_path: Path):
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    log_dir = tmp_path / "logs"
    today = date.today().isoformat()
    file = log_dir / f"proactive-{today}.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        "\n"
        + json.dumps(
            {
                "decision_id": "d-real",
                "timestamp": _NOW.isoformat(),
                "persona_id": "p",
                "user_id": "self",
                "trigger": "long_silence",
                "action": "skip",
                "skip_reason": "no_trigger_match",
            }
        )
        + "\n"
        + "this is not JSON\n"
        + "\n",
        encoding="utf-8",
    )

    m = _import_migrate_module()
    inserted = m.migrate(log_dir=log_dir, engine=engine, dry_run=False)

    assert inserted == 1
    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert len(rows) == 1
    assert rows[0].decision_id == "d-real"


def test_migrate_warns_about_manual_one_shot_in_module_docstring():
    """The script docstring must call out that it's a manual one-shot
    — schema migrations do not (and must not) run it
    automatically. Pinned by docstring grep so a future edit can't
    silently soften the contract."""
    m = _import_migrate_module()
    doc = m.__doc__ or ""
    assert "manual" in doc.lower()
    assert "one-shot" in doc.lower() or "once" in doc.lower()

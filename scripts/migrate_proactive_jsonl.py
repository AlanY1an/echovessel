"""One-shot migration: legacy JSONL audit logs → ``proactive_decisions`` table.

**Manual** invocation only. ``ensure_schema_up_to_date`` does NOT run
this — every daemon boot would otherwise dupe-insert the same audit
rows. Run **once** after Stage 4 lands::

    uv run python -m scripts.migrate_proactive_jsonl \\
        --log-dir ~/.echovessel/logs \\
        --db ~/.echovessel/memory.db

``--dry-run`` reports the would-be insert count without writing
anything.

Mapping rules:

* ``decision_id``      → ``decision_id`` (kept verbatim, or fresh
                         UUID for legacy lines that lack the field)
* ``timestamp``        → ``timestamp`` + ``created_at`` (parsed as
                         ISO-8601)
* ``persona_id``       → ``persona_id``
* ``user_id``          → ``user_id`` (defaults to ``"self"`` if
                         missing)
* ``trigger``          → ``trigger_type``: ``high_emotional_event``
                         keeps its name, every other v1 trigger
                         (``long_silence`` / ``cold_user_gate`` /
                         ``rate_limit_gate`` / etc.) maps to
                         ``thread_due`` (lossy, but the suppress_reason
                         column carries the actual blocking reason)
* ``trigger_payload``  → ``thread_id`` / ``source_event_id`` (when
                         present in payload), rest discarded
* ``action``           → ``fire`` for ``send`` / ``suppress`` for
                         ``skip``
* ``skip_reason``      → ``suppress_reason`` (raw string, including
                         v1 values like ``low_presence_mode`` that
                         have no v2 equivalent)
* ``message_text``     → ``message_text``
* ``ingest_message_id``→ ``sent_message_id``
* ``voice_used``       → ``voice_used``
* ``voice_error``      → ``voice_error``
* ``policy_snapshot``  → ``gate_state_snapshot`` (JSON-stringified)

Anything else on the v1 row (``send_ok`` / ``send_error`` /
``llm_latency_ms`` / ``rationale`` / ``target_channel_id`` /
``memory_snapshot_hash``) is dropped — the v0.6 schema is
deliberately leaner.

Idempotency: NOT idempotent. Running this twice double-inserts every
row. Operator is responsible for running it exactly once.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from echovessel.memory import create_engine
from echovessel.proactive.core.models import ProactiveDecision

log = logging.getLogger(__name__)


_TRIGGER_HIGH = "high_emotional_event"
_TRIGGER_DEFAULT = "thread_due"


def _map_trigger(legacy: str | None) -> str:
    if legacy in (_TRIGGER_HIGH,):
        return _TRIGGER_HIGH
    return _TRIGGER_DEFAULT


def _map_action(legacy: str | None) -> str:
    if legacy == "send":
        return "fire"
    return "suppress"


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError(f"unparseable timestamp: {value!r}")


def _entry_to_row(entry: dict[str, Any]) -> ProactiveDecision:
    timestamp = _parse_timestamp(entry["timestamp"])
    payload = entry.get("trigger_payload") or {}
    snapshot = entry.get("policy_snapshot")
    snapshot_str: str | None
    if snapshot:
        snapshot_str = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    else:
        snapshot_str = None

    return ProactiveDecision(
        decision_id=entry.get("decision_id") or str(uuid.uuid4()),
        timestamp=timestamp,
        persona_id=entry["persona_id"],
        user_id=entry.get("user_id", "self"),
        trigger_type=_map_trigger(entry.get("trigger")),
        thread_id=payload.get("thread_id") if isinstance(payload, dict) else None,
        source_event_id=(
            payload.get("source_event_id") or payload.get("trigger_event_id")
            if isinstance(payload, dict)
            else None
        ),
        action=_map_action(entry.get("action")),
        suppress_reason=entry.get("skip_reason"),
        message_text=entry.get("message_text"),
        sent_message_id=entry.get("ingest_message_id") or entry.get("sent_message_id"),
        voice_used=entry.get("voice_used"),
        voice_error=entry.get("voice_error"),
        gate_state_snapshot=snapshot_str,
        created_at=timestamp,
    )


def _iter_jsonl_files(log_dir: Path):
    """Yield ``proactive-*.jsonl`` files under ``log_dir`` in lexical
    order — the per-day naming makes lexical = chronological."""
    if not log_dir.exists():
        return
    yield from sorted(log_dir.glob("proactive-*.jsonl"))


def migrate(
    *,
    log_dir: Path,
    engine: Engine,
    dry_run: bool = False,
) -> int:
    """Walk every JSONL row in ``log_dir`` and insert one
    ``ProactiveDecision`` per row. Returns the count.

    Malformed lines (blank or non-JSON) are skipped with a WARNING
    log entry — they don't abort the migration.
    """
    inserted = 0
    for path in _iter_jsonl_files(log_dir):
        log.info("migrating %s", path)
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("skipping malformed line in %s: %s", path, e)
                    continue
                if not isinstance(entry, dict):
                    log.warning(
                        "skipping non-object line in %s: %r", path, entry
                    )
                    continue
                try:
                    row = _entry_to_row(entry)
                except (KeyError, ValueError) as e:
                    log.warning("skipping malformed entry in %s: %s", path, e)
                    continue

                if not dry_run:
                    with Session(engine) as db:
                        db.add(row)
                        db.commit()
                inserted += 1
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        type=Path,
        required=True,
        help="Directory containing proactive-YYYY-MM-DD.jsonl files",
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the EchoVessel memory.db file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the would-be insert count without writing anything",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.log_dir.exists():
        log.error("log_dir does not exist: %s", args.log_dir)
        return 1
    if not args.db.exists() and not args.dry_run:
        log.error(
            "db file does not exist: %s "
            "(rerun the daemon once to bootstrap the schema, or use --dry-run)",
            args.db,
        )
        return 1

    engine = create_engine(str(args.db))
    inserted = migrate(log_dir=args.log_dir, engine=engine, dry_run=args.dry_run)
    verb = "would insert" if args.dry_run else "inserted"
    log.info("%s %d row(s)", verb, inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())

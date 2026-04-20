"""Idempotent schema migrations for memory.db.

MVP strategy (review M4 · `docs/web/99-main-thread-review.md#M4`):

    No Alembic. No migration history table. No version strings.

Instead we run a single entry point, `ensure_schema_up_to_date(engine)`,
during daemon startup **before** `create_all_tables`. The function walks
a hardcoded list of "add column if not exists" and "create table if not
exists" steps. Each step is a no-op when the target state is already
reached, so calling the function repeatedly is safe.

Scope (MVP only, review M4):

- Adds a new column to an existing table     ✅
- Creates a new table that didn't exist      ✅
- Renames a column                            ❌ (not supported)
- Drops a column                              ❌ (not supported)
- Changes a column type                       ❌ (not supported)
- Backfills data                              ❌ (new columns are nullable or
                                                  carry a SQL default)

For anything outside MVP scope (renaming, type changes, complex backfills)
a proper migration framework is needed — that's a v1.0 task.

---

## Why this is safe (review M4 rationale)

- Every new column is **nullable** or has a SQL-level DEFAULT. Existing
  rows don't need touching after the ALTER.
- `CREATE TABLE IF NOT EXISTS` handles the "new table" case the same way
  `SQLModel.metadata.create_all` does, so this module doesn't compete
  with `db.py::create_all_tables` — both paths converge on the same
  final schema.
- Running this on a fresh database (the common case in tests) is a
  no-op because every check fails-fast and skips the ALTER.

Tests cover both directions: idempotent reruns on a current DB, and
upgrade of a hand-crafted legacy schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column additions (v0.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ColumnSpec:
    """A single 'add column if not exists' migration step."""

    table: str
    column: str
    sql_type: str  # e.g. "TEXT"


# Order matters only in the sense that each column must appear after its
# parent table exists. All of these tables are created by
# `SQLModel.metadata.create_all` in db.py, so on a fresh DB the ALTERs
# are skipped (column already present on the freshly-created table),
# and on a legacy DB the ALTERs run once.
_V0_3_COLUMNS: tuple[_ColumnSpec, ...] = (
    _ColumnSpec(table="recall_messages", column="turn_id", sql_type="TEXT"),
    _ColumnSpec(table="concept_nodes", column="source_turn_id", sql_type="TEXT"),
    _ColumnSpec(table="concept_nodes", column="imported_from", sql_type="TEXT"),
    # Consolidate retry-safety (2026-04 P0 fix): resume point flag so a
    # transient reflection failure doesn't re-run extraction.
    _ColumnSpec(
        table="sessions",
        column="extracted_events",
        sql_type="BOOLEAN NOT NULL DEFAULT 0",
    ),
    _ColumnSpec(
        table="sessions", column="extracted_events_at", sql_type="DATETIME"
    ),
)


# llm_calls cache token breakdown (2026-04 · issue #1 Stage 3).
# Two new columns track prompt-cache savings per call so the admin
# Cost tab can show "(of which N cached)" breakdowns.
_LLM_CALLS_COLUMNS: tuple[_ColumnSpec, ...] = (
    _ColumnSpec(
        table="llm_calls",
        column="cache_read_input_tokens",
        sql_type="INTEGER NOT NULL DEFAULT 0",
    ),
    _ColumnSpec(
        table="llm_calls",
        column="cache_creation_input_tokens",
        sql_type="INTEGER NOT NULL DEFAULT 0",
    ),
)


# Persona biographic facts (2026-04 · `2026-04-persona-facts` initiative).
# 15 additive columns on personas. All nullable — LLM extraction fills what
# it can during onboarding; the user reviews/edits from the Web admin page.
_PERSONA_FACTS_COLUMNS: tuple[_ColumnSpec, ...] = (
    _ColumnSpec(table="personas", column="full_name", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="gender", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="birth_date", sql_type="DATE"),
    _ColumnSpec(table="personas", column="ethnicity", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="nationality", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="native_language", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="locale_region", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="education_level", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="occupation", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="occupation_field", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="location", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="timezone", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="relationship_status", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="life_stage", sql_type="TEXT"),
    _ColumnSpec(table="personas", column="health_status", sql_type="TEXT"),
)


# ---------------------------------------------------------------------------
# New tables (v0.3)
# ---------------------------------------------------------------------------
#
# Listed here for the legacy-upgrade path only. New databases will get
# the canonical version via SQLModel.metadata.create_all in db.py. When
# both run, the IF NOT EXISTS guard makes this a no-op.
#
# The schema mirrors `models.CoreBlockAppend.__table__` exactly. Keep in
# sync when that model changes.
_V0_3_NEW_TABLES: tuple[tuple[str, str], ...] = (
    (
        "core_block_appends",
        """
        CREATE TABLE IF NOT EXISTS core_block_appends (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            persona_id TEXT NOT NULL,
            user_id TEXT,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            provenance_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(persona_id) REFERENCES personas (id),
            FOREIGN KEY(user_id) REFERENCES users (id)
        )
        """,
    ),
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ensure_schema_up_to_date(engine: Engine) -> None:
    """Bring the memory DB up to the v0.3 schema via idempotent steps.

    MUST be called **before** `create_all_tables(engine)` in daemon
    startup. Fresh databases treat this as a no-op (every check short-
    circuits), so it's cheap to always run.

    Failure is fatal: if any ALTER raises, the daemon should refuse to
    start rather than run with a half-migrated schema that will explode
    on the next insert.
    """
    with engine.begin() as conn:
        for table_name, ddl in _V0_3_NEW_TABLES:
            if not _table_exists(conn, table_name):
                conn.execute(text(ddl))
                log.info("schema migration: created table %s", table_name)

        for spec in (*_V0_3_COLUMNS, *_PERSONA_FACTS_COLUMNS, *_LLM_CALLS_COLUMNS):
            if not _table_exists(conn, spec.table):
                # Legacy DB that predates the parent table entirely.
                # Skip; create_all_tables will build it later with the
                # full v0.3 schema (including this column).
                continue
            if _column_exists(conn, spec.table, spec.column):
                continue
            conn.execute(
                text(
                    f"ALTER TABLE {spec.table} ADD COLUMN {spec.column} {spec.sql_type}"
                )
            )
            log.info(
                "schema migration: added %s.%s %s",
                spec.table,
                spec.column,
                spec.sql_type,
            )

        # Audit P1-5: partial unique index preventing two OPEN sessions
        # for the same (persona, user, channel) triple. On fresh DBs
        # create_all_tables also creates this via the SQLModel
        # declaration; the IF NOT EXISTS guard makes the two paths
        # converge. Legacy DBs may already have a duplicate pair — in
        # that case the CREATE fails. Log a warning and continue rather
        # than crash: `get_or_create_open_session`'s savepoint path still
        # handles the absence of the constraint gracefully, and a DB
        # with a pre-existing conflict is safer to leave visible than
        # to refuse startup over.
        if _table_exists(conn, "sessions") and not _index_exists(
            conn, "uq_sessions_one_open_per_channel"
        ):
            try:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_sessions_one_open_per_channel "
                        "ON sessions (persona_id, user_id, channel_id) "
                        "WHERE status = 'open' AND deleted_at IS NULL"
                    )
                )
                log.info(
                    "schema migration: added unique index "
                    "uq_sessions_one_open_per_channel"
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "schema migration: could not create "
                    "uq_sessions_one_open_per_channel "
                    "(likely a pre-existing duplicate OPEN pair): %s",
                    e,
                )


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------


def _table_exists(conn, table_name: str) -> bool:
    """Check `sqlite_master` for a table by name."""
    row = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
        ),
        {"name": table_name},
    ).first()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    """Check `PRAGMA table_info(<table>)` for a column by name."""
    # PRAGMA doesn't accept bound params in SQLite; validate the table
    # name as an identifier before interpolating to avoid injection.
    if not table_name.replace("_", "").isalnum():
        raise ValueError(f"invalid table name for PRAGMA: {table_name!r}")
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    return any(row[1] == column_name for row in rows)


def _index_exists(conn, index_name: str) -> bool:
    """Check `sqlite_master` for an index by name."""
    row = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name=:name"
        ),
        {"name": index_name},
    ).first()
    return row is not None


__all__ = ["ensure_schema_up_to_date"]

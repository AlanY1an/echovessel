"""Idempotent-migration guarantee for `ensure_schema_up_to_date`.

Running the migration twice on the same database must be a no-op (same
table/column set, no exceptions, no duplicate columns). This is the
review M4 safety net — `ensure_schema_up_to_date` is called on every
daemon start, so it MUST be safe to call repeatedly.
"""

from __future__ import annotations

from sqlalchemy import text

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date


def _column_names(engine, table: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return [r[1] for r in rows]


def _table_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
            ),
            {"n": name},
        ).first()
    return row is not None


def test_migrations_rerun_is_noop_on_fresh_db():
    engine = create_engine(":memory:")
    create_all_tables(engine)  # this already runs migrations once

    cols_before = _column_names(engine, "recall_messages")
    concept_before = _column_names(engine, "concept_nodes")
    personas_before = _column_names(engine, "personas")

    # Run the migration a second time — should complete silently
    ensure_schema_up_to_date(engine)
    ensure_schema_up_to_date(engine)  # and a third for good measure

    cols_after = _column_names(engine, "recall_messages")
    concept_after = _column_names(engine, "concept_nodes")
    personas_after = _column_names(engine, "personas")

    assert cols_before == cols_after
    assert concept_before == concept_after
    assert personas_before == personas_after


def test_migrations_rerun_preserves_new_table():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    assert _table_exists(engine, "core_block_appends")

    ensure_schema_up_to_date(engine)
    assert _table_exists(engine, "core_block_appends")


def test_migrations_v03_columns_present_after_create_all():
    """After create_all_tables on a fresh DB, the v0.3 additive columns
    must all appear. This proves the ORM model declarations include
    them (if they didn't, the migrations alone wouldn't add them on
    fresh DBs)."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    recall_cols = _column_names(engine, "recall_messages")
    concept_cols = _column_names(engine, "concept_nodes")
    sessions_cols = _column_names(engine, "sessions")

    assert "turn_id" in recall_cols
    assert "source_turn_id" in concept_cols
    assert "imported_from" in concept_cols
    # Retry-safety columns (2026-04 P0 fix)
    assert "extracted_events" in sessions_cols
    assert "extracted_events_at" in sessions_cols

    # Persona biographic facts (2026-04 · persona-facts initiative)
    personas_cols = _column_names(engine, "personas")
    for expected in (
        "full_name",
        "gender",
        "birth_date",
        "ethnicity",
        "nationality",
        "native_language",
        "locale_region",
        "education_level",
        "occupation",
        "occupation_field",
        "location",
        "timezone",
        "relationship_status",
        "life_stage",
        "health_status",
    ):
        assert expected in personas_cols, f"missing personas.{expected}"


def test_migrations_upgrade_legacy_db_without_retry_safety_columns():
    """Simulate a pre-retry-safety DB: drop the two new sessions columns,
    then re-run ``ensure_schema_up_to_date`` and assert they're added.

    Proves the ``ALTER TABLE`` path (not just ``CREATE TABLE``) is in
    play for existing deployments upgrading to this build.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)

    # SQLite can't drop columns portably pre-3.35; simulate a legacy
    # schema by re-creating the sessions table without the new columns.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sessions RENAME TO sessions_new"))
        conn.execute(
            text(
                """
                CREATE TABLE sessions (
                    id TEXT NOT NULL PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_message_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at DATETIME,
                    extracted BOOLEAN NOT NULL DEFAULT 0,
                    extracted_at DATETIME,
                    trivial BOOLEAN NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    close_trigger TEXT,
                    deleted_at DATETIME,
                    FOREIGN KEY(persona_id) REFERENCES personas (id),
                    FOREIGN KEY(user_id) REFERENCES users (id)
                )
                """
            )
        )
        conn.execute(text("DROP TABLE sessions_new"))

    legacy_cols = _column_names(engine, "sessions")
    assert "extracted_events" not in legacy_cols
    assert "extracted_events_at" not in legacy_cols

    ensure_schema_up_to_date(engine)

    upgraded_cols = _column_names(engine, "sessions")
    assert "extracted_events" in upgraded_cols
    assert "extracted_events_at" in upgraded_cols

    # Re-running must be a no-op.
    ensure_schema_up_to_date(engine)
    assert _column_names(engine, "sessions") == upgraded_cols

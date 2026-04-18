"""Upgrade path: idempotent ALTER applied to a hand-crafted v0.2 DB.

Builds a database that has the v0.2 shape (no `turn_id`,
`source_turn_id`, `imported_from`, `core_block_appends`), then runs
`ensure_schema_up_to_date` and verifies that:

1. The new columns / table appear
2. Existing rows are preserved exactly
3. New columns default to NULL on legacy rows
"""

from __future__ import annotations

from sqlalchemy import text

from echovessel.memory import create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date

_LEGACY_V0_2_SCHEMA = [
    """
    CREATE TABLE personas (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        description TEXT,
        avatar_path TEXT,
        voice_config TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    )
    """,
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    )
    """,
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        persona_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_message_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        closed_at DATETIME,
        extracted INTEGER NOT NULL DEFAULT 0,
        extracted_at DATETIME,
        trivial INTEGER NOT NULL DEFAULT 0,
        message_count INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        close_trigger TEXT,
        deleted_at DATETIME
    )
    """,
    """
    CREATE TABLE recall_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        persona_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        token_count INTEGER,
        day DATE NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    )
    """,
    """
    CREATE TABLE concept_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        type TEXT NOT NULL,
        description TEXT NOT NULL,
        emotional_impact INTEGER NOT NULL DEFAULT 0,
        emotion_tags TEXT NOT NULL DEFAULT '[]',
        relational_tags TEXT NOT NULL DEFAULT '[]',
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed_at DATETIME,
        source_session_id TEXT,
        source_deleted INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    )
    """,
]


def _cols(engine, table: str) -> list[str]:
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


def test_legacy_db_gets_new_columns_and_table():
    engine = create_engine(":memory:")

    # Build the v0.2 shape by hand (no SQLModel metadata.create_all)
    with engine.begin() as conn:
        for stmt in _LEGACY_V0_2_SCHEMA:
            conn.execute(text(stmt))

        # Seed a handful of rows so we can assert they survive migration
        conn.execute(
            text(
                "INSERT INTO personas (id, display_name) VALUES ('p1', 'Ann')"
            )
        )

        # Legacy persona rows should keep their data and get NULL for the
        # new biographic fact columns after migration.
        conn.execute(
            text("INSERT INTO users (id, display_name) VALUES ('self', 'Alan')")
        )
        conn.execute(
            text(
                "INSERT INTO sessions "
                "(id, persona_id, user_id, channel_id, status, started_at, last_message_at) "
                "VALUES ('s1', 'p1', 'self', 'web', 'closed', "
                "'2026-01-01 10:00:00', '2026-01-01 10:05:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO recall_messages "
                "(session_id, persona_id, user_id, channel_id, role, content, day) "
                "VALUES ('s1', 'p1', 'self', 'web', 'user', 'hello legacy', '2026-01-01')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO concept_nodes "
                "(persona_id, user_id, type, description, source_session_id) "
                "VALUES ('p1', 'self', 'event', 'legacy event', 's1')"
            )
        )

    # Pre-migration assertions: no new columns yet
    assert "turn_id" not in _cols(engine, "recall_messages")
    assert "source_turn_id" not in _cols(engine, "concept_nodes")
    assert "imported_from" not in _cols(engine, "concept_nodes")
    assert not _table_exists(engine, "core_block_appends")
    assert "full_name" not in _cols(engine, "personas")
    assert "timezone" not in _cols(engine, "personas")

    # Run the idempotent migration
    ensure_schema_up_to_date(engine)

    # New columns are present
    assert "turn_id" in _cols(engine, "recall_messages")
    assert "source_turn_id" in _cols(engine, "concept_nodes")
    assert "imported_from" in _cols(engine, "concept_nodes")
    # New table is present
    assert _table_exists(engine, "core_block_appends")
    # Persona biographic facts (2026-04 · persona-facts initiative)
    personas_cols_after = _cols(engine, "personas")
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
        assert expected in personas_cols_after, f"missing personas.{expected}"

    # Existing legacy data still intact; new columns default to NULL
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT content, turn_id FROM recall_messages WHERE id=1")
        ).one()
        assert row[0] == "hello legacy"
        assert row[1] is None

        row = conn.execute(
            text(
                "SELECT description, source_session_id, source_turn_id, "
                "imported_from FROM concept_nodes WHERE id=1"
            )
        ).one()
        assert row[0] == "legacy event"
        assert row[1] == "s1"
        assert row[2] is None
        assert row[3] is None

        # Legacy persona row is preserved; facts columns default to NULL.
        row = conn.execute(
            text(
                "SELECT display_name, full_name, timezone FROM personas "
                "WHERE id='p1'"
            )
        ).one()
        assert row[0] == "Ann"
        assert row[1] is None
        assert row[2] is None

    # And running it again on the upgraded DB is still a no-op
    ensure_schema_up_to_date(engine)
    assert "turn_id" in _cols(engine, "recall_messages")
    assert "full_name" in _cols(engine, "personas")

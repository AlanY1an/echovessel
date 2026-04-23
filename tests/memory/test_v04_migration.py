"""v0.4 · 6-layer-memory schema baseline.

Proves that ``ensure_schema_up_to_date`` brings a DB to the v0.4 shape:

- ``concept_nodes`` gains event_time_{start,end}, subject, superseded_by_id,
  mention_count, source_turn_ids.
- ``personas`` gains episodic_state (JSON NOT NULL with neutral default) and
  last_slow_tick_at.
- ``users`` gains timezone.
- Three new tables are created: entities, entity_aliases, concept_node_entities.
- ``entities_vec`` virtual table is created by ``create_all_tables``.
- A partial unique index enforces the single-self invariant on entities.
- MOOD core_blocks rows are physically deleted on migration.
- ``concept_nodes.source_turn_ids`` backfill lifts the legacy scalar column
  into a JSON array.
- Re-running the migration is a no-op (idempotent).
"""

from __future__ import annotations

from sqlalchemy import text

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date


def _cols(engine, table: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return [r[1] for r in rows]


def _table_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": name},
        ).first()
    return row is not None


def _index_exists(engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name=:n"),
            {"n": name},
        ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Case 1 · fresh DB
# ---------------------------------------------------------------------------


def test_fresh_db_carries_all_v04_shape():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    # concept_nodes columns
    concept_cols = _cols(engine, "concept_nodes")
    for expected in (
        "event_time_start",
        "event_time_end",
        "subject",
        "superseded_by_id",
        "mention_count",
        "source_turn_ids",
    ):
        assert expected in concept_cols, f"missing concept_nodes.{expected}"

    # personas columns
    persona_cols = _cols(engine, "personas")
    assert "episodic_state" in persona_cols
    assert "last_slow_tick_at" in persona_cols

    # users columns
    user_cols = _cols(engine, "users")
    assert "timezone" in user_cols

    # New tables
    for tbl in ("entities", "entity_aliases", "concept_node_entities"):
        assert _table_exists(engine, tbl), f"missing table {tbl}"

    # Partial unique index for single-self invariant
    assert _index_exists(engine, "uq_entities_single_self")


# ---------------------------------------------------------------------------
# Case 2 · legacy v0.3 DB (no v0.4 columns) upgrades cleanly
# ---------------------------------------------------------------------------


_LEGACY_V0_3_SUBSET = [
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
    CREATE TABLE core_blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        user_id TEXT,
        label TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        char_count INTEGER NOT NULL DEFAULT 0,
        char_limit INTEGER NOT NULL DEFAULT 5000,
        version INTEGER NOT NULL DEFAULT 1,
        last_edited_by TEXT NOT NULL DEFAULT 'system',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
        source_turn_id TEXT,
        imported_from TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_at DATETIME
    )
    """,
]


def test_legacy_db_gains_v04_columns_and_tables():
    engine = create_engine(":memory:")
    with engine.begin() as conn:
        for ddl in _LEGACY_V0_3_SUBSET:
            conn.execute(text(ddl))
        # Seed a persona + a legacy concept_node with the old scalar
        # source_turn_id so we can verify backfill.
        conn.execute(text("INSERT INTO personas (id, display_name) VALUES ('p1', 'P')"))
        conn.execute(text("INSERT INTO users (id, display_name) VALUES ('self', 'A')"))
        conn.execute(
            text(
                "INSERT INTO concept_nodes "
                "(persona_id, user_id, type, description, source_turn_id) "
                "VALUES ('p1', 'self', 'event', 'e', 'turn-abc')"
            )
        )
        # Seed a legacy mood core_block and a non-mood one so we can
        # prove the migration only deletes the mood row.
        conn.execute(
            text(
                "INSERT INTO core_blocks (persona_id, label, content) VALUES ('p1', 'mood', 'calm')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO core_blocks (persona_id, label, content) "
                "VALUES ('p1', 'persona', 'warm listener')"
            )
        )

    # Pre-migration: new columns absent
    assert "event_time_start" not in _cols(engine, "concept_nodes")
    assert "episodic_state" not in _cols(engine, "personas")
    assert "timezone" not in _cols(engine, "users")
    assert not _table_exists(engine, "entities")

    ensure_schema_up_to_date(engine)

    # Post-migration: new columns and tables present
    concept_cols = _cols(engine, "concept_nodes")
    for col in (
        "event_time_start",
        "event_time_end",
        "subject",
        "superseded_by_id",
        "mention_count",
        "source_turn_ids",
    ):
        assert col in concept_cols

    assert "episodic_state" in _cols(engine, "personas")
    assert "last_slow_tick_at" in _cols(engine, "personas")
    assert "timezone" in _cols(engine, "users")

    assert _table_exists(engine, "entities")
    assert _table_exists(engine, "entity_aliases")
    assert _table_exists(engine, "concept_node_entities")
    assert _index_exists(engine, "uq_entities_single_self")

    # Existing data preserved; new defaults applied.
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT description, source_turn_id, source_turn_ids, "
                "subject, mention_count "
                "FROM concept_nodes LIMIT 1"
            )
        ).one()
        assert row[0] == "e"
        assert row[1] == "turn-abc"
        # Backfilled to ['turn-abc'].
        assert row[2] == '["turn-abc"]'
        # Default subject='user', mention_count=1.
        assert row[3] == "user"
        assert row[4] == 1

        # Personas legacy row gets the neutral JSON default.
        row = conn.execute(
            text("SELECT episodic_state, last_slow_tick_at FROM personas WHERE id='p1'")
        ).one()
        assert '"mood":"neutral"' in row[0]
        assert row[1] is None


# ---------------------------------------------------------------------------
# Case 3 · migration idempotent — running it twice is a no-op
# ---------------------------------------------------------------------------


def test_rerun_is_noop():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    before_concept = _cols(engine, "concept_nodes")
    before_personas = _cols(engine, "personas")
    before_users = _cols(engine, "users")

    ensure_schema_up_to_date(engine)
    ensure_schema_up_to_date(engine)

    assert _cols(engine, "concept_nodes") == before_concept
    assert _cols(engine, "personas") == before_personas
    assert _cols(engine, "users") == before_users


# ---------------------------------------------------------------------------
# Case 4 · MOOD rows are physically deleted
# ---------------------------------------------------------------------------


def test_mood_rows_deleted_on_migration():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO personas (id, display_name) VALUES ('p', 'P')"))
        for label, content in (
            ("mood", "calm"),
            ("persona", "warm listener"),
            ("style", "never start with haha"),
        ):
            conn.execute(
                text(
                    "INSERT INTO core_blocks "
                    "(persona_id, label, content, char_count, char_limit, "
                    "version, last_edited_by) "
                    "VALUES (:p, :l, :c, :n, 5000, 1, 'system')"
                ),
                {"p": "p", "l": label, "c": content, "n": len(content)},
            )

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM core_blocks WHERE label='mood'")).scalar()
        assert n == 1

    ensure_schema_up_to_date(engine)

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM core_blocks WHERE label='mood'")).scalar()
        assert n == 0
        # Other labels untouched.
        labels = {r[0] for r in conn.execute(text("SELECT label FROM core_blocks")).all()}
        assert labels == {"persona", "style"}


# ---------------------------------------------------------------------------
# Case 5 · source_turn_ids backfill wraps legacy scalar into a JSON list
# ---------------------------------------------------------------------------


def test_source_turn_ids_backfill():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO personas (id, display_name) VALUES ('p', 'P')"))
        conn.execute(text("INSERT INTO users (id, display_name) VALUES ('self', 'A')"))
        # Fresh-DB concept_nodes has many NOT NULL columns without SQL
        # defaults (Python-side only). Supply them explicitly here.
        _base_cols = (
            "persona_id, user_id, type, description, "
            "emotional_impact, emotion_tags, relational_tags, "
            "access_count, source_deleted, "
            "subject, mention_count, source_turn_ids, source_turn_id"
        )
        conn.execute(
            text(
                f"INSERT INTO concept_nodes ({_base_cols}) "
                "VALUES ('p', 'self', 'event', 'with-turn', "
                "0, '[]', '[]', 0, 0, "
                "'user', 1, '[]', 'T1')"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO concept_nodes ({_base_cols}) "
                "VALUES ('p', 'self', 'event', 'without-turn', "
                "0, '[]', '[]', 0, 0, "
                "'user', 1, '[]', NULL)"
            )
        )

    # create_all_tables already ran ensure_schema_up_to_date once — the
    # column exists and the backfill already fired. Re-running must be
    # a no-op.
    ensure_schema_up_to_date(engine)

    with engine.connect() as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                text("SELECT description, source_turn_id, source_turn_ids FROM concept_nodes")
            ).all()
        }
    assert rows["with-turn"] == ("T1", '["T1"]')
    # Rows with NULL scalar keep the empty list default.
    assert rows["without-turn"] == (None, "[]")


# ---------------------------------------------------------------------------
# Case 6 · entities_vec virtual table exists after create_all_tables
# ---------------------------------------------------------------------------


def test_entities_vec_virtual_table_exists():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    names = {r[0] for r in rows}
    assert "entities_vec" in names


# ---------------------------------------------------------------------------
# Case 7 · single-self partial unique index actually fires
# ---------------------------------------------------------------------------


def test_single_self_invariant_enforced():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO personas (id, display_name) VALUES ('p', 'P')"))
        conn.execute(text("INSERT INTO users (id, display_name) VALUES ('self', 'A')"))
        conn.execute(
            text(
                "INSERT INTO entities (persona_id, user_id, canonical_name, kind) "
                "VALUES ('p', 'self', 'me', 'self')"
            )
        )

    # A second self-kind row for the same (persona, user) triple must
    # trip the partial unique index.
    import sqlalchemy

    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO entities (persona_id, user_id, canonical_name, kind) "
                    "VALUES ('p', 'self', 'me-again', 'self')"
                )
            )
    except sqlalchemy.exc.IntegrityError:
        raised = True
    assert raised, "partial unique index should have prevented second self entity"

    # Non-self kinds are unrestricted.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO entities (persona_id, user_id, canonical_name, kind) "
                "VALUES ('p', 'self', 'Scott', 'person')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO entities (persona_id, user_id, canonical_name, kind) "
                "VALUES ('p', 'self', 'Mochi', 'pet')"
            )
        )

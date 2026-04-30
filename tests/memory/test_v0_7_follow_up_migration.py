"""v0.7 migration: idempotent ALTER for new follow-up columns."""

from sqlalchemy import inspect, text

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date


def test_walker_adds_columns_to_legacy_db(tmp_path):
    """Legacy DB without these columns gets them added by the walker."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")

    # Build a legacy concept_nodes (without v0.7 columns) using raw SQL
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE concept_nodes (
                    id INTEGER PRIMARY KEY,
                    persona_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    emotional_impact INTEGER NOT NULL DEFAULT 0,
                    emotion_tags TEXT NOT NULL DEFAULT '[]',
                    relational_tags TEXT NOT NULL DEFAULT '[]',
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at TEXT,
                    source_session_id TEXT,
                    source_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT,
                    subject TEXT NOT NULL DEFAULT 'user'
                )
                """
            )
        )

    ensure_schema_up_to_date(engine)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("concept_nodes")}
    assert "follow_up_at" in cols
    assert "follow_up_hint" in cols
    assert "estimated_arc_days" in cols
    assert "advance_pre_hours" in cols
    assert "advance_post_hours" in cols
    assert "proactive_suppressed_at" in cols


def test_walker_idempotent(tmp_path):
    """Running walker twice is a no-op the second time."""
    engine = create_engine(f"sqlite:///{tmp_path / 'idempotent.db'}")
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)
    ensure_schema_up_to_date(engine)  # second run · should not raise

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("concept_nodes")}
    assert "follow_up_at" in cols


def test_walker_creates_partial_index(tmp_path):
    """Partial index for active follow-up scan is created."""
    engine = create_engine(f"sqlite:///{tmp_path / 'index.db'}")
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)

    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_concept_follow_up_active'"
            )
        )
        names = [r[0] for r in result]
    assert "idx_concept_follow_up_active" in names

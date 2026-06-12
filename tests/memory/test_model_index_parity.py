"""Model-declared secondary indexes exist on a fresh database.

``ensure_schema_up_to_date`` pre-creates several tables via raw DDL
before ``SQLModel.metadata.create_all`` runs. SQLAlchemy's checkfirst
skips the whole CreateTable (indexes included) for tables that already
exist, so the migration must emit every model-declared index itself.
This suite diffs ``sqlite_master`` against the model metadata so any
future drift between models.py and the migration path fails loudly.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import SQLModel

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date

# Indexes declared in models.py on tables that the migration raw-DDL
# path creates first (entities / concept_node_entities /
# slow_cycle_stats / core_block_appends).
_MIGRATION_OWNED_INDEXES = (
    "idx_entities_persona_user",
    "idx_entities_merge_status",
    "idx_cne_entity",
    "idx_core_block_appends_persona_user_label",
    "idx_core_block_appends_created",
    "idx_slow_cycle_stats_persona_date",
)


def _db_index_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
    return {r[0] for r in rows}


def _declared_index_names() -> set[str]:
    return {
        index.name
        for table in SQLModel.metadata.tables.values()
        for index in table.indexes
        if index.name
    }


def test_fresh_db_has_every_model_declared_index():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    missing = _declared_index_names() - _db_index_names(engine)
    assert not missing, f"model-declared indexes absent from fresh DB: {sorted(missing)}"


def test_fresh_db_has_migration_owned_table_indexes():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    db_indexes = _db_index_names(engine)
    for name in _MIGRATION_OWNED_INDEXES:
        assert name in db_indexes, f"missing index {name}"


def test_fresh_db_has_follow_up_partial_index():
    """idx_concept_follow_up_active must exist after a single
    create_all_tables on a fresh DB — not only from the second daemon
    run onwards."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    assert "idx_concept_follow_up_active" in _db_index_names(engine)


def test_index_sync_is_idempotent():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    before = _db_index_names(engine)

    ensure_schema_up_to_date(engine)
    ensure_schema_up_to_date(engine)

    assert _db_index_names(engine) == before


def test_legacy_db_gains_model_indexes_on_migration():
    """A DB whose entities table predates the index declarations gets
    them added by ensure_schema_up_to_date."""
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX idx_entities_persona_user"))
        conn.execute(text("DROP INDEX idx_cne_entity"))

    assert "idx_entities_persona_user" not in _db_index_names(engine)

    ensure_schema_up_to_date(engine)

    db_indexes = _db_index_names(engine)
    assert "idx_entities_persona_user" in db_indexes
    assert "idx_cne_entity" in db_indexes

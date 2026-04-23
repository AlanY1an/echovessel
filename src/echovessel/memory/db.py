"""Database engine, sqlite-vec loading, FTS5 virtual tables, and schema creation.

This module is the single place that touches raw SQL for schema creation.
Everything else should go through SQLAlchemy/SQLModel ORM or through the
StorageBackend abstraction.

Key responsibilities:

1. Verify SQLite version supports trigram FTS5 tokenizer (>= 3.34)
2. Create the main SQLAlchemy engine
3. Load sqlite-vec extension on each connection
4. Provide `create_all_tables(engine)` that creates:
   - All SQLModel-declared tables (personas, users, core_blocks, sessions,
     recall_messages, concept_nodes, concept_node_filling)
   - recall_messages_fts virtual table + sync triggers
   - concept_nodes_vec virtual table (sqlite-vec)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec
from sqlalchemy import Engine, event, text
from sqlalchemy import create_engine as _sa_create_engine
from sqlmodel import SQLModel

# Embedding dimension. Matches intfloat/multilingual-e5-small.
# See docs/memory/04-schema-v0.2.md Q-schema-1.
VECTOR_DIM = 384

MIN_SQLITE_VERSION = (3, 34, 0)


# ---------------------------------------------------------------------------
# SQLite capability check
# ---------------------------------------------------------------------------


def check_sqlite_version() -> None:
    """Ensure the runtime SQLite supports FTS5 trigram tokenizer.

    Trigram tokenizer requires SQLite >= 3.34. Older builds will fail to
    create the FTS5 virtual table. macOS and most Linux distros ship modern
    enough versions, but some Python builds bundle older SQLite.
    """
    version = sqlite3.sqlite_version_info
    if version < MIN_SQLITE_VERSION:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE_VERSION))}+ required for "
            f"FTS5 trigram support, found {sqlite3.sqlite_version}. "
            f"Consider installing pysqlite3-binary."
        )


# ---------------------------------------------------------------------------
# Engine creation
# ---------------------------------------------------------------------------


def create_engine(db_path: str | Path, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for a local SQLite file.

    Loads the sqlite-vec extension automatically on every new connection so
    that vector tables and `vec0` virtual tables are available.

    Args:
        db_path: Path to the SQLite file. Use `:memory:` for in-memory (tests).
        echo: If True, log all SQL. Useful for debugging.
    """
    check_sqlite_version()

    if db_path != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    else:
        url = "sqlite:///:memory:"

    engine = _sa_create_engine(url, echo=echo)

    # On every new connection: load sqlite-vec, switch to WAL journaling,
    # and set a generous busy timeout. Multiple actors share the same
    # ``memory.db`` (Web + Discord ingest, idle scanner, consolidate
    # worker, proactive scheduler) and the daemon is single-process, so
    # WAL gives concurrent readers + one writer instead of the default
    # rollback-journal mode where every reader blocks every writer. The
    # busy timeout makes that one writer wait up to five seconds for an
    # in-flight transaction to commit instead of immediately raising
    # ``OperationalError: database is locked`` — which is what was
    # killing catchup consolidate when discord ingest happened to commit
    # in the same window. ``:memory:`` accepts the PRAGMAs but treats
    # WAL as a no-op (no file = no journal); leaving the call in place
    # keeps the production and test code paths identical.
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record):
        dbapi_connection.enable_load_extension(True)
        sqlite_vec.load(dbapi_connection)
        dbapi_connection.enable_load_extension(False)

        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.execute("PRAGMA busy_timeout = 5000")
        finally:
            cur.close()

    return engine


# ---------------------------------------------------------------------------
# Virtual table DDL
# ---------------------------------------------------------------------------

_FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS recall_messages_fts USING fts5(
    content,
    content='recall_messages',
    content_rowid='id',
    tokenize='trigram'
)
"""

_FTS_TRIGGER_INSERT = """
CREATE TRIGGER IF NOT EXISTS recall_fts_insert
AFTER INSERT ON recall_messages
BEGIN
    INSERT INTO recall_messages_fts(rowid, content)
    VALUES (new.id, new.content);
END
"""

_FTS_TRIGGER_DELETE = """
CREATE TRIGGER IF NOT EXISTS recall_fts_delete
AFTER DELETE ON recall_messages
BEGIN
    INSERT INTO recall_messages_fts(recall_messages_fts, rowid, content)
    VALUES('delete', old.id, old.content);
END
"""

_FTS_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS recall_fts_update
AFTER UPDATE ON recall_messages
BEGIN
    INSERT INTO recall_messages_fts(recall_messages_fts, rowid, content)
    VALUES('delete', old.id, old.content);
    INSERT INTO recall_messages_fts(rowid, content)
    VALUES (new.id, new.content);
END
"""

_VEC_CREATE = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS concept_nodes_vec USING vec0(
    id INTEGER PRIMARY KEY,
    embedding FLOAT[{VECTOR_DIM}]
)
"""

# v0.4 · entities_vec carries the canonical-name embedding used by the
# extraction-time dedup pass (plan §6.2 Level 2 cosine match). Same
# dimension as concept_nodes_vec so the same embed function feeds both.
_ENTITIES_VEC_CREATE = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS entities_vec USING vec0(
    id INTEGER PRIMARY KEY,
    embedding FLOAT[{VECTOR_DIM}]
)
"""

# Worker θ · Memory search FTS5 index over ConceptNode.description.
# We use the same trigram tokenizer as recall_messages_fts so CJK
# substring search works. ``description`` is the ONLY indexed column —
# emotion_tags / relational_tags filtering happens with regular
# WHERE clauses on the JSON columns (Worker θ scope: don't widen the
# index this round).
_CONCEPT_FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS concept_nodes_fts USING fts5(
    description,
    content='concept_nodes',
    content_rowid='id',
    tokenize='trigram'
)
"""

_CONCEPT_FTS_TRIGGER_INSERT = """
CREATE TRIGGER IF NOT EXISTS concept_fts_insert
AFTER INSERT ON concept_nodes
BEGIN
    INSERT INTO concept_nodes_fts(rowid, description)
    VALUES (new.id, new.description);
END
"""

_CONCEPT_FTS_TRIGGER_DELETE = """
CREATE TRIGGER IF NOT EXISTS concept_fts_delete
AFTER DELETE ON concept_nodes
BEGIN
    INSERT INTO concept_nodes_fts(concept_nodes_fts, rowid, description)
    VALUES('delete', old.id, old.description);
END
"""

_CONCEPT_FTS_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS concept_fts_update
AFTER UPDATE ON concept_nodes
BEGIN
    INSERT INTO concept_nodes_fts(concept_nodes_fts, rowid, description)
    VALUES('delete', old.id, old.description);
    INSERT INTO concept_nodes_fts(rowid, description)
    VALUES (new.id, new.description);
END
"""

# Backfill any concept_nodes rows that exist on disk but are missing
# from the FTS index — covers both the legacy DB upgrade path AND
# the case where a fresh DB had rows seeded before FTS triggers fired.
_CONCEPT_FTS_BACKFILL = """
INSERT INTO concept_nodes_fts(rowid, description)
SELECT id, description FROM concept_nodes
WHERE id NOT IN (SELECT rowid FROM concept_nodes_fts)
"""


def create_all_tables(engine: Engine) -> None:
    """Create all tables and virtual indexes for the memory system.

    Safe to call multiple times — uses `CREATE TABLE IF NOT EXISTS` semantics.

    v0.3: runs `ensure_schema_up_to_date(engine)` BEFORE the ORM create_all.
    That path is the idempotent ALTER migration for databases that
    predate v0.3 (review M4). On a fresh DB it is a no-op, on a legacy
    DB it brings the existing tables up to the current column set so the
    subsequent `metadata.create_all` doesn't encounter a shape mismatch.
    """
    # 0. Idempotent schema migration for pre-v0.3 databases (review M4)
    # Imported lazily to avoid a circular import at module load time
    # (migrations.py reads from sqlalchemy only, but being conservative).
    from echovessel.memory.migrations import ensure_schema_up_to_date

    ensure_schema_up_to_date(engine)

    # 1. SQLModel declared tables
    SQLModel.metadata.create_all(engine)

    # 2. Raw DDL for virtual tables and triggers
    with engine.begin() as conn:
        conn.execute(text(_FTS_CREATE))
        conn.execute(text(_FTS_TRIGGER_INSERT))
        conn.execute(text(_FTS_TRIGGER_DELETE))
        conn.execute(text(_FTS_TRIGGER_UPDATE))
        conn.execute(text(_VEC_CREATE))
        # v0.4 · entities_vec virtual table for L5 canonical-name dedup.
        conn.execute(text(_ENTITIES_VEC_CREATE))
        # Worker θ · concept_nodes FTS5 index for the admin search bar.
        conn.execute(text(_CONCEPT_FTS_CREATE))
        conn.execute(text(_CONCEPT_FTS_TRIGGER_INSERT))
        conn.execute(text(_CONCEPT_FTS_TRIGGER_DELETE))
        conn.execute(text(_CONCEPT_FTS_TRIGGER_UPDATE))
        # Idempotent backfill — covers legacy DBs and fresh DBs that
        # had rows seeded before the trigger existed (e.g. tests that
        # bulk-INSERT then build their first FTS index).
        conn.execute(text(_CONCEPT_FTS_BACKFILL))

"""Embedding-model tag + startup re-embed sync.

Vectors produced by different embedding models live in incompatible
spaces — mixing them in one vec0 table silently corrupts KNN similarity.
The memory DB therefore records which model produced its stored vectors
in the ``memory_meta`` key-value table, and ``ensure_embedding_model``
compares that tag against the configured model at startup, re-embedding
every stored vector when they differ.

Encoding is symmetric: the same ``embed_fn`` output is both stored and
used as the query vector (no e5-style ``query:`` / ``passage:``
prefixes), so re-embedding the source texts with the new model is all
it takes to restore comparability.

The whole sync — vector replacement plus tag update — runs in one
transaction with the tag written last. An interruption rolls back to
the pre-sync state and the next startup redoes the work (idempotent).
Synchronous by design: the DB is single-user and local, and a few
thousand short texts embed in seconds on CPU.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, text

from echovessel.memory.backend import StorageBackend
from echovessel.memory.entities import entity_embed_input

log = logging.getLogger(__name__)

EMBEDDING_MODEL_KEY = "embedding_model"

# Databases that carry vectors but no tag predate the tag itself; every
# such database was built with the embedder that was the default before
# the tag existed.
UNTAGGED_VECTORS_MODEL = "all-MiniLM-L6-v2"

_META_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
)
"""

EmbedFn = Callable[[str], list[float]]


def ensure_embedding_model(
    engine: Engine,
    backend: StorageBackend,
    model_name: str,
    embed_fn: EmbedFn,
) -> int:
    """Make stored vectors consistent with the configured embedding model.

    Returns the number of re-embedded vector rows (0 when the stored tag
    already matches ``model_name``). Must run after ``create_all_tables``
    so the vec tables exist.
    """
    with engine.begin() as conn:
        conn.execute(text(_META_TABLE_DDL))
        stored = conn.execute(
            text("SELECT value FROM memory_meta WHERE key = :k"),
            {"k": EMBEDDING_MODEL_KEY},
        ).scalar()

        if stored is None:
            stored = UNTAGGED_VECTORS_MODEL if _has_vectors(conn) else model_name

        if stored == model_name:
            _write_tag(conn, model_name)
            return 0

        log.info(
            "embedding model changed %s -> %s: re-embedding stored vectors", stored, model_name
        )
        count = _reembed_all(conn, backend, embed_fn)
        _write_tag(conn, model_name)
        log.info("embedding sync complete: re-embedded %d vectors with %s", count, model_name)
        return count


def _has_vectors(conn: Any) -> bool:
    for table in ("concept_nodes_vec", "entities_vec"):
        if conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            return True
    return False


def _write_tag(conn: Any, model_name: str) -> None:
    conn.execute(
        text(
            "INSERT INTO memory_meta (key, value) VALUES (:k, :v) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        ),
        {"k": EMBEDDING_MODEL_KEY, "v": model_name},
    )


def _reembed_all(conn: Any, backend: StorageBackend, embed_fn: EmbedFn) -> int:
    # Every row in a vec table participates in KNN candidate selection
    # (the live/deleted filter joins in afterwards), so every row that
    # has a vector gets re-embedded — including soft-deleted or
    # superseded nodes that still carry one.
    count = 0

    nodes = conn.execute(
        text(
            "SELECT cn.id, cn.description FROM concept_nodes_vec v "
            "JOIN concept_nodes cn ON cn.id = v.id"
        )
    ).all()
    for node_id, description in nodes:
        backend.insert_vector(int(node_id), embed_fn(description or ""), conn=conn)
        count += 1

    entities = conn.execute(
        text(
            "SELECT e.id, e.canonical_name, e.description FROM entities_vec v "
            "JOIN entities e ON e.id = v.id"
        )
    ).all()
    for entity_id, canonical_name, description in entities:
        embedding = embed_fn(entity_embed_input(canonical_name, description))
        backend.insert_entity_vector(int(entity_id), embedding, conn=conn)
        count += 1

    return count


__all__ = [
    "EMBEDDING_MODEL_KEY",
    "UNTAGGED_VECTORS_MODEL",
    "ensure_embedding_model",
]

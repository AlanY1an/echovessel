"""SQLite + sqlite-vec implementation of StorageBackend.

This is the ONLY file allowed to contain raw SQL for vector / FTS operations.
Any other module that touches vectors or FTS must go through this class.

See docs/memory/04-schema-v0.2.md §8 for the architectural rule.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine, text

from echovessel.memory.backend import FtsHit, StorageBackend, VectorHit


def _pack_vector(v: Sequence[float]) -> bytes:
    """sqlite-vec expects vectors as little-endian float32 byte blobs."""
    return struct.pack(f"{len(v)}f", *v)


class SQLiteBackend(StorageBackend):
    """SQLite-backed memory operations using sqlite-vec and FTS5 trigram."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- Vector operations ---------------------------------------------

    def vector_search(
        self,
        query_embedding: list[float],
        persona_id: str,
        user_id: str,
        types: tuple[str, ...],
        top_k: int,
    ) -> list[VectorHit]:
        if not types:
            return []

        # sqlite-vec requires `k = N` inside the virtual table's own WHERE
        # clause when joining. Over-fetch 3x+ to survive post-filter loss
        # on persona/user/type/deleted_at constraints.
        candidates_k = max(top_k * 4, 50)

        # Build IN clause for types. SQLAlchemy bound parameters don't
        # always expand tuples cleanly across SQLite FTS/vec, so we sanitize
        # and interpolate.
        clean_types = tuple(t for t in types if t.replace("_", "").isalpha())
        if not clean_types:
            return []
        types_in = ",".join(f"'{t}'" for t in clean_types)

        sql = text(
            f"""
            SELECT v.id, v.distance
            FROM concept_nodes_vec v
            JOIN concept_nodes cn ON cn.id = v.id
            WHERE v.embedding MATCH :query_vec
              AND k = :candidates_k
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.type IN ({types_in})
              AND cn.deleted_at IS NULL
            ORDER BY v.distance
            LIMIT :top_k
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "query_vec": _pack_vector(query_embedding),
                    "candidates_k": candidates_k,
                    "persona_id": persona_id,
                    "user_id": user_id,
                    "top_k": top_k,
                },
            ).all()
        return [VectorHit(concept_node_id=r[0], distance=float(r[1])) for r in rows]

    def insert_vector(
        self,
        concept_node_id: int,
        embedding: list[float],
        *,
        conn: Any | None = None,
    ) -> None:
        # INSERT OR REPLACE semantics so re-extraction can overwrite.
        # vec0 does not support REPLACE semantics cleanly across all
        # versions, so we DELETE then INSERT.
        #
        # When ``conn`` is passed, the writes join the caller's active
        # transaction (avoids self-deadlock against a DbSession that has
        # already flushed an INSERT to ``concept_nodes``). When absent,
        # we open our own short-lived transaction as before.
        if conn is not None:
            conn.execute(
                text("DELETE FROM concept_nodes_vec WHERE id = :id"),
                {"id": concept_node_id},
            )
            conn.execute(
                text("INSERT INTO concept_nodes_vec (id, embedding) VALUES (:id, :vec)"),
                {"id": concept_node_id, "vec": _pack_vector(embedding)},
            )
            return

        with self._engine.begin() as c:
            c.execute(
                text("DELETE FROM concept_nodes_vec WHERE id = :id"),
                {"id": concept_node_id},
            )
            c.execute(
                text("INSERT INTO concept_nodes_vec (id, embedding) VALUES (:id, :vec)"),
                {"id": concept_node_id, "vec": _pack_vector(embedding)},
            )

    def delete_vector(
        self,
        concept_node_id: int,
        *,
        conn: Any | None = None,
    ) -> None:
        if conn is not None:
            conn.execute(
                text("DELETE FROM concept_nodes_vec WHERE id = :id"),
                {"id": concept_node_id},
            )
            return

        with self._engine.begin() as c:
            c.execute(
                text("DELETE FROM concept_nodes_vec WHERE id = :id"),
                {"id": concept_node_id},
            )

    # --- Full-text operations ------------------------------------------

    @staticmethod
    def _sanitize_fts5_query(raw: str) -> str:
        """Escape FTS5 special characters so arbitrary user input is safe.

        FTS5 reserves: AND OR NOT NEAR ( ) * : ^ " ?
        Wrapping each whitespace-delimited token in double quotes neutralises
        all operators except the quote itself, which we strip.
        """
        # Remove any double-quotes (they would break the quoting)
        cleaned = raw.replace('"', " ")
        # Split into tokens, wrap each in quotes, rejoin
        tokens = cleaned.split()
        if not tokens:
            return '""'
        return " ".join(f'"{t}"' for t in tokens)

    def fts_search(
        self,
        query_text: str,
        persona_id: str,
        user_id: str,
        top_k: int,
    ) -> list[FtsHit]:
        # FTS5 rank is bm25; lower is better. We join back to recall_messages
        # to filter by persona/user and exclude deleted rows.
        #
        # User input is sanitized by wrapping each token in double-quotes
        # so FTS5 special characters (? * : ^ etc.) are treated as literals.
        safe_query = self._sanitize_fts5_query(query_text)
        sql = text(
            """
            SELECT rm.id, fts.rank
            FROM recall_messages_fts fts
            JOIN recall_messages rm ON rm.id = fts.rowid
            WHERE recall_messages_fts MATCH :query
              AND rm.persona_id = :persona_id
              AND rm.user_id = :user_id
              AND rm.deleted_at IS NULL
            ORDER BY fts.rank
            LIMIT :top_k
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "query": safe_query,
                    "persona_id": persona_id,
                    "user_id": user_id,
                    "top_k": top_k,
                },
            ).all()
        return [FtsHit(recall_message_id=r[0], rank=float(r[1])) for r in rows]

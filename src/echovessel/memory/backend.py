"""StorageBackend abstraction.

The backend encapsulates the THREE dialect-sensitive operations that can't
be expressed portably through SQLAlchemy/SQLModel:

    1. Vector search (sqlite-vec vs pgvector)
    2. Full-text search (FTS5 trigram vs pg_trgm)
    3. Inserting/deleting rows in the vector virtual table

Everything else (CRUD, session management, cascade deletes) goes through
SQLModel ORM directly and is automatically portable.

See docs/memory/04-schema-v0.2.md §8 for the full rationale.

MVP ships with SQLiteBackend only. A PostgresBackend would satisfy the same
Protocol and be a drop-in replacement (post-MVP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class VectorHit:
    """A single candidate returned from a vector search, pre-rerank.

    `distance` is the backend's native distance metric (cosine distance for
    sqlite-vec). Smaller = more similar. The rerank step converts this to
    a [0, 1] relevance score.
    """

    concept_node_id: int
    distance: float


@dataclass(slots=True)
class FtsHit:
    """A single hit from full-text search on recall_messages."""

    recall_message_id: int
    rank: float  # FTS5 bm25 rank (smaller = better)


class StorageBackend(Protocol):
    """Dialect-sensitive memory operations. SQLite is the only MVP impl."""

    # --- vector operations on concept_nodes_vec -------------------------

    def vector_search(
        self,
        query_embedding: list[float],
        persona_id: str,
        user_id: str,
        types: tuple[str, ...],
        top_k: int,
    ) -> list[VectorHit]:
        """Return the top-K nearest ConceptNode ids for the query embedding.

        Implementations MUST:
          - Respect persona_id / user_id filters
          - Restrict to the given `type` values (e.g. ('event', 'thought'))
          - Exclude rows where concept_nodes.deleted_at IS NOT NULL
          - Order by distance ascending
        """
        ...

    def insert_vector(
        self,
        concept_node_id: int,
        embedding: list[float],
        *,
        conn: Any | None = None,
    ) -> None:
        """Insert (or upsert) the vector for a concept_node_id.

        If ``conn`` is provided (a SQLAlchemy Connection bound to an
        outer transaction), the write joins that transaction instead of
        opening a new connection. This is how callers like
        ``consolidate_session`` avoid a self-deadlock — the main
        DbSession has already flushed an INSERT to ``concept_nodes`` and
        is holding SQLite's single writer lock; an independent
        ``engine.begin()`` would deadlock against it.

        Callers that do NOT have an outer transaction (import pipeline,
        ad-hoc reindex) can omit ``conn`` and the backend will open its
        own connection.
        """
        ...

    def delete_vector(
        self,
        concept_node_id: int,
        *,
        conn: Any | None = None,
    ) -> None:
        """Remove a vector from the vector table. Called during physical
        cleanup after soft-delete retention expires.

        ``conn`` has the same semantics as in ``insert_vector``.
        """
        ...

    # --- vector operations on entities_vec (L5 · R5) --------------------

    def vec_search_entities(
        self,
        query_embedding: list[float],
        persona_id: str,
        user_id: str,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return the top-K nearest (entity_id, distance) pairs.

        Powers Level-2 embedding dedup in
        :func:`echovessel.memory.entities.resolve_entity`.
        Implementations MUST filter by persona/user, exclude soft-deleted
        rows, and order ascending by distance. Returned distance uses the
        same metric as :func:`vector_search` so callers share the
        ``1 - distance/2`` cosine conversion.
        """
        ...

    def insert_entity_vector(
        self,
        entity_id: int,
        embedding: list[float],
        *,
        conn: Any | None = None,
    ) -> None:
        """Insert or upsert the canonical-name embedding for an entity.

        ``conn`` has the same semantics as :func:`insert_vector`: when
        provided, the write joins the caller's active transaction so
        consolidate can build entity + vector atomically.
        """
        ...

    def delete_entity_vector(
        self,
        entity_id: int,
        *,
        conn: Any | None = None,
    ) -> None:
        """Remove an entity's vector. Called during physical cleanup
        after the entity's soft-delete retention expires.
        """
        ...

    # --- full-text operations on recall_messages_fts --------------------

    def fts_search(
        self,
        query_text: str,
        persona_id: str,
        user_id: str,
        top_k: int,
    ) -> list[FtsHit]:
        """Return the top-K matching RecallMessage ids for the query text.

        Implementations MUST:
          - Exclude rows where recall_messages.deleted_at IS NOT NULL
          - Respect persona_id / user_id filters
          - Order by the native rank (ascending for bm25-style, descending
            if the backend returns similarity instead — normalize if needed)
        """
        ...

"""Post-write embed pass.

Tracker §2.2 / M-round3 §7.4 hard constraint:

    After memory writes, vectors for the new L3 / L4 ``concept_nodes``
    rows must be computed and written into the ``concept_nodes_vec``
    sqlite-vec virtual table, or ``retrieve.vector_search`` will never
    return them.

This module accepts an ``embed_fn`` callable that maps a batch of text
strings to a batch of float vectors, and a ``vector_writer`` callable
that persists one ``(concept_node_id, vector)`` pair at a time
(typically ``SQLiteBackend.insert_vector``). Both are injected — we
never import ``echovessel.memory.backends.sqlite`` from here so the
test suite can stub them in-process.

A ``None`` ``embed_fn`` is allowed **only** when there is nothing to
embed (the pipeline emitted zero L3 / L4 rows). If the caller produced
rows and passed ``embed_fn=None``, we raise :class:`EmbedError` — this
is the "must not silently skip" half of the constraint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.import_.errors import EmbedError
from echovessel.memory.models import ConceptNode

log = logging.getLogger(__name__)


EmbedFn = Callable[[list[str]], list[list[float]]]
VectorWriter = Callable[[int, list[float]], None]


def run_embed_pass(
    *,
    db: DbSession,
    concept_node_ids: list[int],
    embed_fn: EmbedFn | None,
    vector_writer: VectorWriter | None,
) -> int:
    """Compute embeddings for ``concept_node_ids`` and write vectors.

    Returns the number of vectors written.

    Raises:
        EmbedError: ``concept_node_ids`` is non-empty AND either
        ``embed_fn`` or ``vector_writer`` is ``None`` — the tracker
        forbids silent skips in this case.
    """
    if not concept_node_ids:
        if embed_fn is None:
            # No rows and no embed_fn — nothing to do, but log at
            # debug so tests can see the skip happened cleanly.
            log.debug(
                "embed pass: nothing to embed (0 rows, embed_fn=None)"
            )
        return 0

    if embed_fn is None or vector_writer is None:
        raise EmbedError(
            "run_embed_pass: produced "
            f"{len(concept_node_ids)} concept_nodes but embed_fn / "
            "vector_writer is None. Tracker §2.2 forbids silent skip."
        )

    # Fetch (id, description) pairs in a single query, preserving the
    # input order so vectors line up with IDs.
    stmt = select(ConceptNode).where(
        ConceptNode.id.in_(concept_node_ids)  # type: ignore[attr-defined]
    )
    rows = {row.id: row for row in db.exec(stmt).all() if row.id is not None}

    ordered_texts: list[str] = []
    ordered_ids: list[int] = []
    for cid in concept_node_ids:
        node = rows.get(cid)
        if node is None:
            log.warning(
                "embed pass: concept_node id=%s missing at read time", cid
            )
            continue
        ordered_ids.append(cid)
        ordered_texts.append(node.description or "")

    if not ordered_texts:
        return 0

    vectors = embed_fn(ordered_texts)
    if not isinstance(vectors, list) or len(vectors) != len(ordered_texts):
        raise EmbedError(
            f"embed_fn returned {len(vectors) if isinstance(vectors, list) else '?'} "
            f"vectors for {len(ordered_texts)} texts"
        )

    written = 0
    for cid, vec in zip(ordered_ids, vectors, strict=True):
        if not vec:
            log.warning(
                "embed pass: embed_fn returned empty vector for id=%s", cid
            )
            continue
        vector_writer(cid, list(vec))
        written += 1
    return written


def sqlite_backend_vector_writer(backend: Any) -> VectorWriter:
    """Adapter: build a :data:`VectorWriter` that calls
    ``backend.insert_vector(id, vec)``.

    Used by the runtime wiring in :mod:`echovessel.runtime.wiring.importer`
    so the pipeline can stay backend-agnostic. Tests pass their own
    lambda instead.
    """

    def _write(concept_node_id: int, vec: list[float]) -> None:
        backend.insert_vector(concept_node_id, vec)

    return _write


__all__ = [
    "EmbedFn",
    "VectorWriter",
    "run_embed_pass",
    "sqlite_backend_vector_writer",
]

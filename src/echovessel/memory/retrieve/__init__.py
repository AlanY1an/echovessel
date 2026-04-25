"""Memory retrieval — L1 core blocks + L3 concept nodes + scoring + search.

This sub-package contains the read-side of memory: building the
context window for a turn (`retrieve`), looking up specific concept
nodes (`search_concept_nodes`, `list_concept_nodes`), and the scoring
weights / functions used to rank candidates.

Internal layout:

- :mod:`.core`    — main :func:`retrieve` pipeline + L1 / L2 reads + entity
                    anchor helpers + force-load thoughts + event-time
                    rendering
- :mod:`.scoring` — rerank weights / decay constants / per-candidate
                    score helpers + :class:`ScoredMemory`
- :mod:`.search`  — :func:`search_concept_nodes` (FTS5 + LIKE-fallback)
                    and :func:`list_concept_nodes` (admin pagination) +
                    :class:`ConceptSearchHit`

Public API is re-exported here so callers continue to use
``from echovessel.memory.retrieve import …`` without depending on
the internal split.
"""

from echovessel.memory.retrieve.core import (
    RetrievalResult,
    derive_event_status,
    find_query_entities,
    get_nodes_linked_to_entities,
    list_recall_messages,
    load_core_blocks,
    load_persona_thoughts_force,
    render_event_delta_phrase,
    retrieve,
)
from echovessel.memory.retrieve.scoring import (
    DEFAULT_MIN_RELEVANCE,
    ENTITY_ANCHOR_BONUS_VALUE,
    RECENCY_HALF_LIFE_DAYS,
    RELATIONAL_BONUS_VALUE,
    WEIGHT_ENTITY_ANCHOR,
    WEIGHT_IMPACT,
    WEIGHT_RECENCY,
    WEIGHT_RELATIONAL_BONUS,
    WEIGHT_RELEVANCE,
    ScoredMemory,
)
from echovessel.memory.retrieve.search import (
    ConceptSearchHit,
    list_concept_nodes,
    search_concept_nodes,
)

__all__ = [
    # Scoring constants (re-exported for tests / docs that tune them)
    "WEIGHT_RECENCY",
    "WEIGHT_RELEVANCE",
    "WEIGHT_IMPACT",
    "WEIGHT_RELATIONAL_BONUS",
    "WEIGHT_ENTITY_ANCHOR",
    "RELATIONAL_BONUS_VALUE",
    "ENTITY_ANCHOR_BONUS_VALUE",
    "RECENCY_HALF_LIFE_DAYS",
    "DEFAULT_MIN_RELEVANCE",
    # Result types
    "ScoredMemory",
    "RetrievalResult",
    "ConceptSearchHit",
    # Loaders / queries
    "load_core_blocks",
    "load_persona_thoughts_force",
    "find_query_entities",
    "get_nodes_linked_to_entities",
    # Entry points
    "retrieve",
    "list_recall_messages",
    "list_concept_nodes",
    "search_concept_nodes",
    # Event-time helpers
    "derive_event_status",
    "render_event_delta_phrase",
]

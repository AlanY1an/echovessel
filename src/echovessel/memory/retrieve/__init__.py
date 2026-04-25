"""Memory retrieval — L1 core blocks + L3 concept nodes + scoring + search.

This sub-package contains the read-side of memory: building the
context window for a turn (`retrieve`), looking up specific concept
nodes (`search_concept_nodes`, `list_concept_nodes`), and the scoring
weights / functions used to rank candidates.

Public API is re-exported here so callers continue to use
``from echovessel.memory.retrieve import …`` without depending on
the internal split between core / scoring / search.
"""

from echovessel.memory.retrieve.core import (
    DEFAULT_MIN_RELEVANCE,
    ENTITY_ANCHOR_BONUS_VALUE,
    RECENCY_HALF_LIFE_DAYS,
    RELATIONAL_BONUS_VALUE,
    WEIGHT_ENTITY_ANCHOR,
    WEIGHT_IMPACT,
    WEIGHT_RECENCY,
    WEIGHT_RELATIONAL_BONUS,
    WEIGHT_RELEVANCE,
    ConceptSearchHit,
    RetrievalResult,
    ScoredMemory,
    derive_event_status,
    find_query_entities,
    get_nodes_linked_to_entities,
    list_concept_nodes,
    list_recall_messages,
    load_core_blocks,
    load_persona_thoughts_force,
    render_event_delta_phrase,
    retrieve,
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

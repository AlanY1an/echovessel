"""Memory subsystem.

Public API (import from `echovessel.memory` directly):

    from echovessel.memory import (
        # Models
        Persona, User, CoreBlock, Session, RecallMessage,
        ConceptNode, ConceptNodeFilling, CoreBlockAppend,
        # DB
        create_engine, create_all_tables, ensure_schema_up_to_date,
        # Observers (round 3 per-write hooks + round 4 lifecycle hooks)
        MemoryEventObserver, NullObserver,
        register_observer, unregister_observer,
        # Import API (round 3)
        import_content, append_to_core_block,
        bulk_create_events, bulk_create_thoughts,
        count_events_by_imported_from, count_thoughts_by_imported_from,
        EventInput, ThoughtInput, ImportResult,
        # Episodic state (L6, v0.4)
        update_episodic_state,
    )

Only depends on `echovessel.core`. Must not import from voice/channels/runtime.
"""

from echovessel.memory.consolidate import consolidate_session
from echovessel.memory.db import create_all_tables, create_engine
from echovessel.memory.episodic import update_episodic_state
from echovessel.memory.imports import (
    EventInput,
    ImportResult,
    ThoughtInput,
    append_to_core_block,
    bulk_create_events,
    bulk_create_thoughts,
    count_events_by_imported_from,
    count_thoughts_by_imported_from,
    import_content,
)
from echovessel.memory.migrations import ensure_schema_up_to_date
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    CoreBlock,
    CoreBlockAppend,
    ExternalIdentity,
    Persona,
    RecallMessage,
    Session,
    User,
)
from echovessel.memory.observers import (
    MemoryEventObserver,
    NullObserver,
    register_observer,
    unregister_observer,
)
from echovessel.memory.retrieve import (
    ConceptSearchHit,
    list_concept_nodes,
    list_recall_messages,
    retrieve,
    search_concept_nodes,
)
from echovessel.memory.slow_cycle import (
    SlowCycleBudgetExceeded,
    SlowCycleExpectationInput,
    SlowCycleFn,
    SlowCycleOutput,
    SlowCycleRunResult,
    SlowCycleThoughtInput,
    bulk_create_expectations,
    bulk_create_slow_thoughts,
    run_slow_cycle,
    should_run_slow_cycle,
)

__all__ = [
    # Models
    "Persona",
    "User",
    "CoreBlock",
    "Session",
    "RecallMessage",
    "ConceptNode",
    "ConceptNodeFilling",
    "CoreBlockAppend",
    "ExternalIdentity",
    # DB
    "create_engine",
    "create_all_tables",
    "ensure_schema_up_to_date",
    # Observers
    "MemoryEventObserver",
    "NullObserver",
    "register_observer",
    "unregister_observer",
    # Import API
    "import_content",
    "append_to_core_block",
    "bulk_create_events",
    "bulk_create_thoughts",
    "count_events_by_imported_from",
    "count_thoughts_by_imported_from",
    "EventInput",
    "ThoughtInput",
    "ImportResult",
    # Episodic state (L6, v0.4)
    "update_episodic_state",
    # Slow cycle (Spec 6)
    "SlowCycleBudgetExceeded",
    "SlowCycleExpectationInput",
    "SlowCycleFn",
    "SlowCycleOutput",
    "SlowCycleRunResult",
    "SlowCycleThoughtInput",
    "bulk_create_expectations",
    "bulk_create_slow_thoughts",
    "run_slow_cycle",
    "should_run_slow_cycle",
    # Queries
    "list_recall_messages",
    "list_concept_nodes",
    "search_concept_nodes",
    "retrieve",
    "ConceptSearchHit",
    # Consolidation
    "consolidate_session",
]

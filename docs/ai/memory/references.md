# memory · references

Pointers into the codebase. Always re-check — paths and line numbers
shift.

---

## Source files

```
src/echovessel/memory/
├── __init__.py              public API surface
├── models.py                every SQLModel table
├── db.py                    engine + virtual tables
├── migrations.py            idempotent ALTER path for legacy DBs
├── backend.py               StorageBackend Protocol
├── backends/sqlite.py       only backend impl
├── ingest.py                L2 write path
├── sessions.py              session lifecycle (idle/length/lifecycle)
├── episodic.py              L6 mood snapshot
├── entities.py              L5 dedup (3-tier)
├── imports.py               import-pipeline write API
├── retrieve/
│   ├── core.py              retrieve pipeline (D4 铁律 docstring)
│   ├── scoring.py           rerank weights + ScoredMemory
│   └── search.py            admin search (FTS5 + LIKE fallback)
├── consolidate/
│   ├── core.py              phase A→F orchestration
│   ├── phase_a.py           trivial-skip gate
│   ├── phase_bce.py         extraction helpers + SHOCK/TIMER thresholds
│   └── tracer.py            per-session dev-mode trace recorder
├── slow_cycle.py            phase G — cross-session reflection
├── forget.py                soft-delete + retention sweep
├── observers.py             MemoryEventObserver Protocol + registry
├── events.py                re-export shim for memory.observers
├── tokens.py                tiktoken wrapper
└── identity.py              ExternalIdentity → user_id resolver
```

---

## Core types memory imports

`src/echovessel/core/types.py` — `MessageRole`, `SessionStatus`,
`NodeType`, `BlockLabel`, `EventTime`, `SHARED_BLOCK_LABELS`,
`PER_USER_BLOCK_LABELS`. Everything memory persists references one of
these.

---

## Tests

```
tests/memory/
├── test_schema.py                            table shape + constraints
├── test_engine_pragmas.py                    WAL + busy_timeout
├── test_migrations_idempotent.py             idempotent ALTER path
├── test_migrations_from_old_db.py            legacy DB upgrade
├── test_v04_migration.py                     v0.4 schema bump
│
├── test_ingest.py                            ingest_message happy path
├── test_sessions_concurrency.py              partial unique index guards
├── test_event_time_status.py                 event_time monotonic CHECK
├── test_episodic_state.py                    L6 update path
│
├── test_consolidate.py                       phase A→F
├── test_consolidate_tracer.py                trace recorder
├── test_consolidate_entity_link_guard.py     L5 junction
├── test_supersedes.py                        contradiction handling
├── test_mention_dedup.py                     mention_count aggregation
│
├── test_retrieve.py                          retrieve pipeline
├── test_retrieve_entity_anchor.py            entity-anchor bonus
├── test_recall_messages_turn_id.py           L2 turn_id read path
├── test_force_load_persona_thoughts.py       v0.5 persona_thoughts
│
├── test_slow_cycle.py                        phase G
├── test_forget.py                            soft-delete + retention
│
├── test_observer_called.py                   per-write hook
├── test_lifecycle_on_new_session_started.py  lifecycle hook fires post-commit
├── test_lifecycle_on_session_closed.py       lifecycle hook fires post-commit
├── test_lifecycle_observer_exception_swallowed.py  observer failure ≠ rollback
├── test_register_unregister_roundtrip.py     observer registry
│
├── test_external_identities.py               channel_id → user_id mapping
├── test_entity_resolve.py                    L5 three-tier dedup
├── test_concept_nodes_check_constraint.py    imported_from XOR source_session_id
├── test_concept_nodes_imported_from.py       import provenance
├── test_concept_nodes_source_turn_id.py      v0.3 turn provenance
├── test_core_block_appends_audit.py          L1 audit log
├── test_core_block_label_enum.py             v0.5 BlockLabel collapse
│
├── test_backend_shared_transaction.py        StorageBackend conn= behavior
├── test_config_wiring.py                     runtime → memory wiring
├── test_events_module_reexport.py            memory.events re-export
└── test_force_load_persona_thoughts.py       v0.5 force-load
```

```
tests/memory_eval/
├── fixtures/scripted/                        eval scenarios
└── *.py                                      retrieval/extraction quality eval
```

---

## How runtime consumes memory

| File | Role |
|---|---|
| `runtime/wiring/memory.py` | `MemoryFacade` — proactive's view; `ProactiveChannelRegistry` adapter |
| `runtime/wiring/prompts.py` | binds `ExtractFn` / `ReflectFn` to LLM provider via `make_extract_fn` / `make_reflect_fn`; consumed by `consolidate_worker` |
| `runtime/wiring/persona_extraction.py` | onboarding-time persona-fact extraction (blank-write + import-upload paths via `LLMProvider.complete`); NOT involved in per-session extraction |
| `runtime/wiring/memory_observer.py` | `RuntimeMemoryObserver` — registered once at startup |
| `runtime/loops/consolidate_worker.py` | dequeues closed sessions, calls `consolidate_session`; runs `run_slow_cycle` (phase G) at the tail |
| `runtime/loops/idle_scanner.py` | scans for idle sessions, marks `CLOSING` |
| `runtime/turn/coordinator.py` | `assemble_turn()` — calls `ingest_message()` for each user message AND calls `retrieve()` once per turn (Stage 6) |
| `runtime/turn/prompt_assembly.py` | consumes retrieval result; does NOT call `retrieve()` itself |
| `runtime/turn/dispatcher.py` | forwards `IncomingTurn`s to `assemble_turn`; does NOT call `ingest_message()` directly |
| `runtime/app.py` | startup: `create_engine`, `create_all_tables`, `register_observer` |

---

## Schema invariants enforced at the SQLite level

| Constraint | Table | What it guards |
|---|---|---|
| `uq_core_block_persona_user_label` | `core_blocks` | one row per (persona, user, label) |
| `uq_sessions_one_open_per_channel` | `sessions` | partial unique on `WHERE status='open'` |
| `ck_concept_nodes_source_mutex` | `concept_nodes` | `imported_from IS NULL OR source_session_id IS NULL` (NAND — at most one set) |
| `ck_concept_nodes_event_time_monotonic` | `concept_nodes` | `event_time_start <= event_time_end` |
| `uq_filling_parent_child` | `concept_node_filling` | one (parent, child) link |
| `uq_entities_canonical` | `entities` | one canonical name per (persona, user) |
| `uq_entities_single_self` | `entities` | partial unique — one self-entity per (persona, user) |

Application code may also enforce rules that the DB cannot — e.g.
"persona/style L1 blocks have user_id=NULL" is enforced in
`memory.imports.append_to_core_block` and `memory.entities.resolve_entity`,
not as a CHECK.

---

## Human docs (for context, not for facts)

These are narrative / rationale and may lag behind code:

- `docs/en/memory.md` · `docs/zh/memory.md`
- `docs/en/memory-testing.md`
- `docs/memory/` — historical design pages

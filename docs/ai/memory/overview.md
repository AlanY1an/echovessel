# memory · overview

The persona's long-term store: raw messages, distilled events,
reflective thoughts, third-party entities, and a current mood snapshot.

Single-process, single-writer SQLite. Free-function API takes a
`DbSession` (SQLModel) as the first argument; the runtime opens the
session and threads it through.

---

## What it owns

- **Persistence layer.** Every table in the daemon is here.
- **Session boundaries.** Decides when to close a session for extraction
  (`idle` / `max_length` / `explicit` / `lifecycle` / `catchup`).
- **Extraction → reflection state machine** (`consolidate_session`).
- **Retrieval pipeline** (`retrieve` — what goes into each prompt).
- **Storage abstraction** (`StorageBackend` Protocol — vector + FTS).
- **Observer fan-out** for memory writes (post-commit, fire-and-forget).

## What it does NOT own

- LLM access — extraction / reflection / embeddings come in as injected
  callables (`ExtractFn`, `ReflectFn`, `EmbedFn`). Memory never imports
  `runtime` or `prompts`.
- Channel concerns — sessions are sharded by `channel_id` for boundary
  purposes, but retrieval **never filters by channel** (D4 铁律).
- Voice / TTS / STT.

---

## Public API entry points

Most are importable as `from echovessel.memory import …` per
`src/echovessel/memory/__init__.py`. **Exception**: `ingest_message`
lives at `echovessel.memory.ingest` and is NOT re-exported at the
package level — runtime imports it directly via
`from echovessel.memory.ingest import ingest_message`.

| Symbol | Source file | Purpose |
|---|---|---|
| `create_engine` | `memory/db.py` | Build the SQLAlchemy engine + load `sqlite-vec`, set WAL/busy_timeout |
| `create_all_tables` | `memory/db.py` | Idempotent: run migrations, `metadata.create_all`, FTS5 + vec0 DDL |
| `ensure_schema_up_to_date` | `memory/migrations.py` | Idempotent ALTER path for legacy DBs |
| `ingest_message` ⚠ | `memory/ingest.py` | Write to L2, update session counters, fire `on_message_ingested`. *Not in package `__init__.py`.* |
| `consolidate_session` | `memory/consolidate/core.py` | Phase A → F: trivial-skip → extract → SHOCK/TIMER reflect → close |
| `retrieve` | `memory/retrieve/core.py` | Build the per-turn context: L1 + L3/L4 ranked + L2 fallback |
| `search_concept_nodes` | `memory/retrieve/search.py` | Admin search (FTS5 + LIKE fallback) |
| `list_recall_messages` | `memory/retrieve/core.py` | Paginated L2 read, used by web history view |
| `update_episodic_state` | `memory/episodic.py` | L6 mood snapshot writer (called by extraction) |
| `run_slow_cycle` | `memory/slow_cycle.py` | Phase G: cross-session reflection that writes L4 thoughts/expectations. Called by `consolidate_worker`, NOT by `consolidate_session` itself. |
| `import_content` | `memory/imports.py` | Import-pipeline entry; dispatches to `bulk_create_events` / `bulk_create_thoughts` / `append_to_core_block` |
| `register_observer` | `memory/observers.py` | One-time at startup; runtime registers a single `RuntimeMemoryObserver` |
| `apply_entity_clarification`, `update_entity_description` | `memory/entities.py` | L5 admin-side mutations (post-resolve user clarification, owner-override description) |

---

## Invariants — do not break

1. **Retrieval never filters by `channel_id`.** Verified by
   `tests/runtime/test_memory_facade.py::test_no_channel_id_kwarg_in_reads`.
   See `memory/retrieve/core.py` module docstring for the full rationale.
2. **`concept_nodes.imported_from` and `source_session_id` are mutually
   exclusive.** Enforced by SQLite CHECK constraint
   `ck_concept_nodes_source_mutex`.
3. **`extracted=True` implies `extracted_events=True`** on `Session`.
   Never the reverse — extracted_events is the resume point that lets a
   transient reflection failure be retried without re-running extraction.
4. **`event_time_start <= event_time_end`** on concept_nodes (CHECK
   `ck_concept_nodes_event_time_monotonic`).
5. **At most one OPEN session per `(persona_id, user_id, channel_id)`.**
   Enforced by partial unique index `uq_sessions_one_open_per_channel`.
6. **Observers fire post-commit.** Observer exceptions are caught and
   logged — they MUST NOT roll back the memory write.
7. **`slow_cycle` never touches L1 core blocks** and never reschedules
   itself. Persona-side reflection writes to
   `L4.thought[subject='persona']`, not `core_blocks`.
8. **`SHOCK_IMPACT_THRESHOLD = 8`** is duplicated as `_SHOCK_IMPACT_THRESHOLD`
   in `slow_cycle.py:83` (avoids a circular import). Keep both in sync if
   you ever change one.

---

## Layer model (L1–L6)

| Layer | Storage | What it holds |
|---|---|---|
| L1 | `core_blocks` (+ `core_block_appends` audit log) | Always-in-prompt prose: `persona`, `user`, `style` |
| L2 | `recall_messages` (+ `recall_messages_fts` virtual table) | Raw messages, ground truth, never in default retrieval |
| L3 | `concept_nodes` WHERE type=`event` (+ `concept_nodes_vec`) | Extracted events from closed sessions |
| L4 | `concept_nodes` WHERE type IN (`thought`, `intention`, `expectation`) | Reflection / commitment / forecast outputs |
| L5 | `entities` + `entity_aliases` + `concept_node_entities` (+ `entities_vec`) | Canonical third-party identities |
| L6 | `personas.episodic_state` (JSON column) | Current mood / energy / last user signal |

L3 and L4 share one table — `NodeType` is the discriminator. They share
retrieval, scoring, and lifecycle; the type is provenance only.

---

## Reading order for new contributors to this system

1. `src/echovessel/memory/__init__.py` — public API surface
2. `src/echovessel/memory/models.py` — every table + invariants in
   docstrings
3. `src/echovessel/memory/db.py` — engine setup + virtual tables
4. `src/echovessel/memory/ingest.py` — simplest write path
5. `src/echovessel/memory/retrieve/core.py` module docstring — D4 铁律
6. `src/echovessel/memory/consolidate/core.py` — phase A→F orchestration
7. `src/echovessel/runtime/wiring/memory.py` — how runtime consumes memory

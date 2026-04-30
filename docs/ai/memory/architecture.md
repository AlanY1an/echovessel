# memory · architecture

How the `memory/` package is wired together and what each file owns.

---

## Module map

```
memory/
├── __init__.py              public API re-exports
├── models.py                SQLModel tables (every table in the daemon)
├── db.py                    engine + sqlite-vec + FTS5/vec0 DDL
├── migrations.py            idempotent ALTER path for legacy DBs
├── backend.py               StorageBackend Protocol (vector + FTS abstraction)
├── backends/
│   └── sqlite.py            the only backend impl (sqlite-vec + FTS5)
├── tokens.py                tiktoken wrapper
├── identity.py              ExternalIdentity → internal user_id resolver
│
├── ingest.py                L2 write path
├── sessions.py              session lifecycle (idle / max_length / lifecycle / catchup)
├── episodic.py              L6 mood snapshot writer
├── entities.py              L5 dedup (3-tier: alias → embedding → new entity);
│                             post-resolve clarification flow is separate
├── imports.py               import-pipeline write API (bulk_create_events / append_to_core_block)
│
├── retrieve/
│   ├── core.py              retrieve pipeline + L1/L2 reads + entity anchor
│   ├── scoring.py           rerank weights + ScoredMemory
│   └── search.py            admin search (FTS5 + LIKE fallback)
│
├── consolidate/
│   ├── core.py              phase A→F orchestration; ExtractFn/ReflectFn/EmbedFn
│   ├── phase_a.py           trivial-skip thresholds + is_trivial()
│   ├── phase_bce.py         extraction helpers + SHOCK/TIMER thresholds
│   └── tracer.py            per-session dev-mode trace recorder
│
├── slow_cycle.py            phase G — cross-session reflection writer
├── forget.py                soft-delete + retention sweep
├── observers.py             MemoryEventObserver Protocol + registry
└── events.py                re-export shim for memory.observers symbols
```

`backends/sqlite.py` is the only place outside `db.py` that writes raw
SQL. Everything else goes through SQLModel ORM or the StorageBackend
Protocol.

---

## Data flow — the three pipelines

### 1. INGEST · message arrives on a channel

```
channel.send → runtime.turn.dispatcher
  └→ ingest_message(db, persona_id, user_id, channel_id, role, content, turn_id)
      ├→ get_or_create_open_session()        (sessions.py)
      ├→ INSERT recall_messages              (L2 write + FTS trigger)
      ├→ session.message_count += 1          (counters)
      ├→ check_length_trigger()              (may close session)
      ├→ db.commit()
      ├→ drain_and_fire_pending_lifecycle_events()  → on_new_session_started
      └→ observer.on_message_ingested(msg)
```

### 2. CONSOLIDATE · session closes

`consolidate_session()` owns phases **A→F**. Phase **G** (`run_slow_cycle`)
runs in `runtime.loops.consolidate_worker._process_one` AFTER
`consolidate_session()` returns. Hooks fire from inside individual
phases as soon as the relevant write commits — there is no end-of-pipeline
fan-out.

```
runtime.loops.consolidate_worker._process_one (or catchup at startup)
  ├→ consolidate_session(db, backend, session, extract_fn, reflect_fn, embed_fn, ...)
  │   │
  │   ├→ Phase A · is_trivial() → if yes, mark CLOSED, fire on_session_closed, return
  │   │
  │   ├→ Phase B · extraction (always runs if not trivial)
  │   │     extract_fn(messages) → events + intentions + entities + mood signal
  │   │     ├ detect_mention_dedup → existing nodes get mention_count++  (no insert)
  │   │     ├ INSERT concept_nodes (type='event'|'intention') + concept_nodes_vec
  │   │     ├ supersedes: write superseded_by_id on older nodes (soft-replace)
  │   │     ├ resolve_entity (L5 three-tier: alias → embedding → new entity)
  │   │     │   junction-row defensive filter: drop links whose canonical/alias
  │   │     │   does not appear literally in the event description
  │   │     ├ session-summary thought → INSERT concept_nodes (type='thought',
  │   │     │   emotion_tags=['session_summary'])
  │   │     ├ update_episodic_state() (L6) → fires on_mood_updated
  │   │     ├ session.extracted_events = True (resume point — survives retry)
  │   │     └ fires: on_event_created (per event), on_thought_created (summary),
  │   │              on_mood_updated, on_entity_confirmed (per entity)
  │   │
  │   ├→ Phase C · SHOCK gate · any |emotional_impact| ≥ SHOCK_IMPACT_THRESHOLD (=8)
  │   ├→ Phase D · TIMER gate · > TIMER_REFLECTION_HOURS (=24h) since last reflection
  │   │
  │   ├→ Phase E · reflection (RUNS ONLY IF SHOCK or TIMER fired in C/D)
  │   │     hard cap: REFLECTION_HARD_LIMIT_24H (=3) reflections per 24h
  │   │     reflect_fn(reflection_inputs) → thoughts (L4, type='thought')
  │   │     │ NB: phase E thoughts do NOT default subject='persona' — only
  │   │     │   slow_cycle (G) does that
  │   │     └ fires: on_thought_created (per thought, source='reflection')
  │   │
  │   └→ Phase F · session.status = CLOSED + db.commit()
  │         queues drain → fires on_session_closed
  │
  └→ Phase G · run_slow_cycle (cross-session reflection)
        wrapped in try/except — failure logs WARNING, session stays CLOSED
        gated by SlowCycleStats daily cap + token budgets
        produces L4 thoughts (subject='persona') + expectations
        fires: on_thought_created (source='slow_tick')
```

Resume invariant: `extracted_events = True` (set in B) implies B has
committed. `extracted = True` (set in F) implies the whole A→F ran.
Never the reverse — if B succeeds but reflection crashes, retry can
resume from C without re-running B.

### 3. RETRIEVE + recent-window · build context for next turn

`coordinator.assemble_turn` runs two parallel context paths per
`IncomingTurn`. Both wrapped in try/except → empty on failure.

#### Path A · `retrieve()` — semantic memory (Stage 6)

Every sub-stage is on by default; "empty" means "no data matched", not
"feature off".

```
retrieve(db, backend, persona_id, user_id, query_text, embed_fn,
         top_k=10, now,
         fallback_threshold=3, expand_session_context=True,
         context_window=3, min_relevance=0.4,
         relational_bonus_weight=1.0,
         force_load_user_thoughts=10,    ← coordinator default
         force_load_persona_thoughts=5)  ← coordinator default
  ├→ load_core_blocks()              (L1)
  ├→ find_query_entities()           (alias → entity anchor bonus)
  ├→ get_nodes_linked_to_entities()  (anchored nodes: synthetic distance=2.0,
  │                                   bypass min_relevance floor)
  ├→ vector_search()                 (L3+L4 unified, top_k)
  ├→ rerank = 0.5*recency + 3*relevance + 2*impact
  │           + 1.0*relational_bonus + 1.5*entity_anchor
  ├→ side effect: access_count += 1, last_accessed_at = now (per returned)
  ├→ L2 session expansion            (context_window=3 around event hits)
  ├→ L2 FTS fallback                 (when raw vector hits < fallback_threshold=3;
  │                                   NOT post-rerank)
  ├→ pinned_thoughts                 (force_load_user_thoughts=10 default;
  │                                   ranked recency × impact, subject='user')
  └→ persona_thoughts                (force_load_persona_thoughts=5 default;
                                      ranked recency-only, subject='persona')
```

#### Path B · `list_recall_messages()` — recent raw chat (Step 4)

```
list_recall_messages(db, persona_id, user_id, limit=20, before=None)
  ├→ DESC by created_at, limit N (default memory.recent_window_size=20, range 1-200)
  ├→ caller reverses to chronological
  ├→ NO channel_id filter (D4)
  └→ no scoring / rerank — pure time slice
```

#### Section → source map (user prompt)

| Section | Source |
|---|---|
| `# Recent thoughts you've had about this person` | A · `top_memories[type='thought']` |
| `# About {speaker}` | A · `pinned_thoughts` |
| `# How you see yourself lately` | A · `persona_thoughts` |
| `# Recent things you remember happened` | A · `top_memories[type='event']` |
| `# Our recent conversation` | **B** · `recent_messages` (day-bucketed) |
| `# What they just said` | current `turn.messages` |

Skipped only when `turn.messages` is empty or user ingest failed
upstream.

**Constants** in `memory/retrieve/scoring.py`: `WEIGHT_RECENCY=0.5`,
`WEIGHT_RELEVANCE=3.0`, `WEIGHT_IMPACT=2.0`,
`WEIGHT_RELATIONAL_BONUS=1.0`, `WEIGHT_ENTITY_ANCHOR=1.5`,
`RELATIONAL_BONUS_VALUE=0.5`, `ENTITY_ANCHOR_BONUS_VALUE=1.0`,
`RECENCY_HALF_LIFE_DAYS=14`, `DEFAULT_MIN_RELEVANCE=0.4`.

**Config** in `runtime/config.py::MemorySection`: `retrieve_k=10`,
`recent_window_size=20`, `relational_bonus_weight=1.0`.

---

## StorageBackend abstraction

`memory/backend.py` defines a Protocol with the THREE dialect-sensitive
operations:

1. Vector search on `concept_nodes_vec` and `entities_vec`
2. Vector insert / delete (with optional `conn=` to join an outer txn)
3. Full-text search on `recall_messages_fts` (and indirectly
   `concept_nodes_fts` via `retrieve.search`)

Everything else (CRUD, cascade deletes, session management) goes through
SQLModel ORM and is automatically portable.

`SQLiteBackend` (`backends/sqlite.py`) is the only impl. A future
PostgresBackend would satisfy the same Protocol — drop-in.

---

## Schema entity relationships (high level)

```
Persona ─┬─ episodic_state (JSON column, L6)
         ├─ last_slow_tick_at (throttle anchor for slow_cycle)
         ├─ CoreBlock           (L1, persona/user/style)
         │   └─ CoreBlockAppend (L1 audit, append-only)
         ├─ Session ─┬─ RecallMessage   (L2)
         │           └─ ConceptNode     (L3 events, source_session_id)
         ├─ ConceptNode (L3+L4 unified)
         │   ├─ ConceptNodeFilling     (L4 provenance: thought ← evidence)
         │   └─ ConceptNodeEntity      (L3 ↔ L5 junction)
         ├─ Entity (L5) ─ EntityAlias
         └─ SlowCycleStats             (daily slow_cycle budget — per (date, persona))

User ─ ExternalIdentity (channel_id, external_id) → internal_user_id
       (MVP: every external_id resolves to "self")
```

Plus dev-mode trace tables (`turn_traces`, `session_traces`) created via
migration but not part of the core memory contract.

---

## Migrations

`migrations.py::ensure_schema_up_to_date()` runs **before**
`SQLModel.metadata.create_all()` in `db.py::create_all_tables()`. It is
the idempotent ALTER path for databases predating the current schema —
on a fresh DB it's a no-op; on a legacy DB it brings tables up to the
current column set so the subsequent `create_all` doesn't choke on a
shape mismatch.

When adding a new column: add to `models.py`, then add the corresponding
ALTER step in `migrations.py` — never rely on `create_all` to add
columns to an existing table (it doesn't).

---

## Concurrency model

- **One DB, one writer.** SQLite WAL mode + `busy_timeout=5000`.
  Multiple actors share `memory.db`: web ingest, Discord ingest,
  consolidate worker, idle scanner, proactive scheduler. WAL gives
  concurrent readers + one serialized writer.
- **Vector writes inside extraction transactions** must pass `conn=` to
  `insert_vector` / `insert_entity_vector`. An independent
  `engine.begin()` would deadlock against the outer DbSession's writer
  lock. See `backend.py::insert_vector` docstring.
- **Lifecycle pending lists** (`_pending_new_sessions`,
  `_pending_closed_sessions`) in `sessions.py` are module-level and
  single-threaded. A future concurrent backend would need `ContextVar`s.
  Drain order: all `on_new_session_started` fire BEFORE all
  `on_session_closed` for a given drain call.

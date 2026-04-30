# memory · workflow

Guideposts (not runbooks) for common memory-side changes. Each section
lists touch points and the order to think about them. Always re-confirm
against the actual code — function signatures shift faster than this
file.

---

## Add a new column to an existing table

**When:** schema gains a field — e.g. tagging concept_nodes with a new
attribute.

**Touch points:**

1. `memory/models.py` — declare the column on the SQLModel class.
   - For enum columns, use `_str_enum_column()` (see `conventions.md`).
   - For NOT NULL columns, supply a `server_default` so legacy rows
     auto-fill on ALTER.
2. `memory/migrations.py` — add an idempotent ALTER step in
   `ensure_schema_up_to_date()`. Pattern: PRAGMA-check, conditional
   ALTER. SQLite cannot ALTER + ADD CHECK; use a table rebuild only when
   absolutely necessary.
3. `tests/memory/test_migrations_idempotent.py` — extend the
   "running twice is a no-op" coverage.
4. `tests/memory/test_migrations_from_old_db.py` — extend the
   "starting from a v0.x schema" coverage.

**Pitfalls:**

- `metadata.create_all()` does NOT add columns to existing tables. The
  migration step is what does the actual ALTER.
- Server defaults on JSON columns must be a literal SQL string (see
  `EPISODIC_STATE_SQL_DEFAULT` in `models.py`).

---

## Add a new storage backend (e.g. Postgres)

**When:** you want to run the same code against pgvector / pg_trgm.

**Touch points:**

1. `memory/backends/postgres.py` — implement the `StorageBackend`
   Protocol from `memory/backend.py`. Six methods: `vector_search`,
   `insert_vector`, `delete_vector`, `vec_search_entities`,
   `insert_entity_vector`, `delete_entity_vector`, `fts_search`.
2. `memory/db.py` — generalize engine creation. Today it's hardcoded to
   SQLite + `sqlite-vec`. Likely refactor to a `create_engine(url, ...)`
   that dispatches on the URL scheme.
3. `memory/db.py` virtual-table DDL — move the FTS5 / vec0 raw SQL
   behind the backend (or into a `Backend.create_schema()` hook).
4. `runtime/wiring/memory.py` — pick the right backend based on config.

**Pitfalls:**

- Migrations (`ensure_schema_up_to_date`) are SQLite-flavored PRAGMA +
  ALTER. Postgres needs a different idempotent path — likely Alembic at
  that point.
- The `conn=` kwarg pattern on vector ops exists to avoid SQLite's
  single-writer self-deadlock. Postgres won't have that constraint, but
  the kwarg should still work (treat it as "join my transaction").

---

## Add a memory-write observer hook

**When:** runtime / channels need to react to a new memory event (e.g.
"persona just produced a thought, broadcast it on SSE").

**Touch points:**

1. `memory/observers.py` — add the method to the
   `MemoryEventObserver` Protocol AND a no-op to `NullObserver`.
   - If it's a per-write hook: pass via `observer=` kwarg on the write
     function.
   - If it's a lifecycle hook: fire through `_fire_lifecycle("name",
     ...)` from the write function, after `db.commit()`.
2. The write function (e.g. `ingest_message`, `consolidate_session`) —
   call the hook **after** `db.commit()`. Wrap in try/except and log
   warnings; observer failure must not unwind the write.
3. `runtime/memory_observers.py` (the `RuntimeMemoryObserver` impl) —
   implement the new method.
4. `tests/memory/test_observer_*` — add a fixture observer and assert
   the hook fires post-commit, and that an observer exception does NOT
   roll back the write.

**Pitfalls:**

- Don't fire hooks inside an open transaction — readers in other
  threads might see inconsistent state.
- The Protocol uses `runtime_checkable` structural subtyping. Don't
  subclass it; provide methods.

---

## Tweak retrieval scoring

**When:** changing how candidates are ranked (e.g. boost recency).

**Touch points:**

1. `memory/retrieve/scoring.py` — adjust constants
   (`WEIGHT_RECENCY` / `WEIGHT_RELEVANCE` / `WEIGHT_IMPACT` /
   `WEIGHT_RELATIONAL_BONUS` / `WEIGHT_ENTITY_ANCHOR` /
   `RECENCY_HALF_LIFE_DAYS`) or modify `_score_node()`.
2. `tests/memory/test_retrieve.py` — update fixtures that pin specific
   ordering. Many tests are sensitive to the relative weights.
3. `tests/memory_eval/` — the eval harness has scripted fixtures that
   verify retrieval picks the "right" answer. Re-run.

**Pitfalls:**

- Don't add a `channel_id` filter (D4 铁律). If you find yourself
  wanting to "boost same-channel results," that signal belongs in the
  output layer, not retrieval.
- The reranking math assumes scores are in approximately the same
  range — if you add a new weighted dimension, keep its raw values in
  `[0, 1]`.

---

## Add a new consolidation phase

**When:** the extraction → reflection state machine grows a step (e.g.
post-reflection summarization).

**Touch points:**

1. `memory/consolidate/phase_X.py` — new module for the phase logic.
   Mirror the shape of `phase_a.py` (constants + pure functions).
2. `memory/consolidate/core.py::consolidate_session()` — sequence the
   new phase. The function is the only place phases are ordered; keep
   the orchestration linear.
3. `memory/consolidate/__init__.py` — re-export anything callers need.
4. `memory/consolidate/tracer.py` — extend `ConsolidateTracer` with a
   record method for the new phase, so the dev console can show it.
5. `tests/memory/test_consolidate.py` + `test_consolidate_tracer.py` —
   add coverage. Mock `ExtractFn` / `ReflectFn` / `EmbedFn`.

**Pitfalls:**

- A phase that fails should NOT roll back earlier phases. Use
  `session.extracted_events` / `session.extracted` as resume points so
  retries are idempotent.
- LLM access only via the injected callables. Memory must not import
  `runtime` or `prompts`.

---

## Add an importer source (e.g. WhatsApp export)

**Touch points are mostly outside `memory/`:**

1. `import_/extraction.py` — parse the source format into normalized
   chunks.
2. `import_/pipeline.py` — wire the extractor into the pipeline.
3. `memory/imports.py` — usually no change. The pipeline calls
   `import_content` / `bulk_create_events` / `append_to_core_block` —
   memory just persists.
4. `channels/web/routes/admin_import.py` — UI surface if user-facing.

**Memory-side rules:**

- Set `imported_from = <file_hash>` on every concept_nodes row written
  by import. Never set `source_session_id` (CHECK constraint).
- Use `count_events_by_imported_from` / `count_thoughts_by_imported_from`
  to dedup — re-importing the same file should be a no-op.
- Embeddings live in the pipeline, not in `imports.py`. Memory writes
  the row; the pipeline pushes the vector.

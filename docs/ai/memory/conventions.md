# memory · conventions

Patterns specific to the memory subsystem. Repo-wide rules live in
`docs/ai/conventions.md`.

---

## Free-function API, not a class

Every public memory operation is a free function whose first argument is
a `sqlmodel.Session` (`DbSession`). Callers open the session, thread it
through, and commit. Memory does not own connection lifecycle.

```python
def ingest_message(db: DbSession, persona_id: str, ..., observer=None) -> IngestResult:
    ...
```

The runtime adapts this into a stateful `MemoryFacade`
(`runtime/wiring/memory.py`) for proactive, which expects an OO Protocol.
That adapter opens a short-lived session per call. Don't replicate the
adapter pattern inside memory itself — keep memory function-shaped.

---

## D4 铁律 · retrieval never filters by `channel_id`

Stamped at the top of `memory/retrieve/core.py` in red.

- No `WHERE channel_id = …` in retrieve, vector_search, FTS fallback,
  session expansion, L1 loading. None.
- Sessions are sharded by channel for *boundary* purposes only (idle
  trigger fires per channel). Once L3/L4 nodes exist, channel is gone.
- Verified in `tests/runtime/test_memory_facade.py::test_no_channel_id_kwarg_in_reads`.

If a feature seems to need per-channel filtering, the answer is almost
always "do that filtering at the *output* layer (channels / interaction
policy), not at retrieval."

---

## Soft delete everywhere

Every table that can lose rows has a `deleted_at: datetime | None`
column. Reads filter `WHERE deleted_at IS NULL`.

`forget.py` is the **soft-delete** writer — it sets `deleted_at`, it
does NOT physically remove node rows. `sweep_dead_vectors` (same file,
run once per day by the consolidate worker's idle branch) physically
removes only the *search-index* rows: nodes soft-deleted >30 days lose
their `concept_nodes_vec` row + `concept_nodes_fts` entry; nodes
superseded >30 days (measured from the successor's `created_at`) lose
the vec row only. `concept_nodes` rows are kept in both cases.
The one exception is `delete_core_block_append` which is a true
`db.delete()` for the audit row only.

- Reset `deleted_at` to `NULL` to undelete (not exposed in MVP, but the
  column allows it).
- `ConceptNodeFilling.orphaned` is the special case for "user deleted
  the evidence node but kept the thought" — preserves auditability of
  the forgetting-rights flow.

---

## Observers fire post-commit · exceptions are swallowed

From `memory/observers.py`:

- All hooks fire **after** `db.commit()` succeeds. The write IS
  persisted by the time the hook runs.
- Observer exceptions are caught + logged at `WARNING`. They MUST NOT
  roll back the memory write. If you find yourself wanting transactional
  observers, redesign — that's a different architecture.

Two flavours coexist on the same Protocol:

1. **Per-write hooks** — passed in via `observer=` kwarg on the
   individual write API (`ingest_message`, `append_to_core_block`,
   `consolidate_session`, `import_content`). Used by callers that care
   about their own write.
2. **Lifecycle hooks** — registered once via `register_observer(...)`.
   Runtime calls this at startup with a single `RuntimeMemoryObserver`.
   Fan-out happens via the module-level `_observers` list.

`on_event_created` / `on_thought_created` / `on_mood_updated` fire
through **both** paths when an explicit `observer=` is provided —
that's intentional, not a bug. Note: `on_mood_updated` receives
`repr(new_state_dict)` as the `new_mood_text` arg post-v0.4 (the L1
mood block is gone); the Protocol signature is still `str` so older
observers keep working.

**The trivial branch fires `on_session_closed` too.** Phase A's
short-circuit isn't a "no lifecycle event" path — observers see the
same close hook whether the session went through full extraction or
got skipped as trivial.

---

## Transactions span memory function calls — pass `conn=` to vec writes

The single-writer SQLite lock is the trap. Inside `consolidate_session`
the outer `DbSession` already holds the writer lock when it INSERTs into
`concept_nodes`. If you then call `backend.insert_vector(node_id, vec)`
without `conn=`, the backend opens its own `engine.begin()` and
deadlocks against itself.

Rule:

```python
# Inside an outer transaction:
backend.insert_vector(node.id, embedding, conn=db.connection())  # ← join txn

# Standalone (import pipeline, ad-hoc reindex):
backend.insert_vector(node.id, embedding)                        # ← own conn OK
```

Same rule for `insert_entity_vector`, `delete_vector`,
`delete_entity_vector`.

---

## Trivial gate runs before extraction

`consolidate_session` Phase A short-circuits sessions with
`messages < 3 AND tokens < 200 AND no strong-emotion keywords` —
sets `session.trivial = True`, marks CLOSED, no LLM call, no events
written. Saves cost on greetings / "ok" / "thanks".

Don't bypass this gate. If you want to *force* extraction in tests,
inject content that crosses the threshold or insert nodes directly.

---

## `imported_from` and `source_session_id` are mutually exclusive

A `ConceptNode` row is either session-sourced (extraction) or
import-sourced (import pipeline). Never both. The CHECK constraint
`ck_concept_nodes_source_mutex` is `imported_from IS NULL OR
source_session_id IS NULL` — so technically NAND, not XOR (both NULL is
allowed and exists on legacy rows). New writes from either path set
exactly one.

Implication: if you write a new ingestion path, decide which provenance
column it sets and stick to it. Don't try to be clever with mixed
sources.

---

## Migrations are idempotent ALTERs, not Alembic

`memory/migrations.py::ensure_schema_up_to_date()` runs before every
`create_all`. It is hand-rolled idempotent SQL:

```python
existing_columns = {row[1] for row in conn.exec_driver_sql(
    "PRAGMA table_info(concept_nodes)").fetchall()}
if "event_time_start" not in existing_columns:
    conn.exec_driver_sql("ALTER TABLE concept_nodes ADD COLUMN event_time_start ...")
```

When you add a column to a model:

1. Add to `models.py`.
2. Add an idempotent ALTER step in `migrations.py`.
3. Update tests in `tests/memory/test_migrations_*.py`.

`create_all` does NOT add columns to existing tables — only the
`ensure_schema_up_to_date` step does.

---

## Never persist enums by name

`SQLModel` defaults to storing enums by Python name (`USER`, `EVENT`),
which breaks SQL queries that compare against string literals. Memory's
`_str_enum_column()` helper in `models.py` declares enum columns as
plain `String` so they store the enum `.value` (`'user'`, `'event'`).

If you add a new enum-typed column, use `_str_enum_column()`. If you see
`SQLModel.Enum(MyEnum)`, that's a bug.

---

## `core/types.py` is the boundary for shared enums

`MessageRole`, `SessionStatus`, `NodeType`, `BlockLabel`, `EventTime`
all live in `echovessel.core.types`. Memory imports them; prompts
imports them; channels imports them. Don't redefine.

If a new enum needs to cross the memory/prompts boundary (extraction
output schema → memory write), it goes in `core/types.py`.

# Memory testing

> How the memory pipeline is exercised · organised by data flow from write to read · cross-referenced to every test file.

The memory system is the densest surface of EchoVessel: one write touches schema, session lifecycle, vector indexing, and a background consolidate queue; one read blends L1 core blocks, vector search, rerank, and an FTS fallback. Each of those steps has its own failure mode. This page walks down the pipeline and tells you, at every stop, what the test suite pins and where to find it.

Unless noted, every file is under `tests/`. All tests live on stub providers by default (`pytest` is free / deterministic); the `eval` layer at the end hits a live LLM and is default-skipped.

```
 ingest              consolidate                 retrieve
   │                     │                          │
 L1 ──────────────────── L1 ──── L1 core blocks + facts
 L2  messages ────────►  L2 (read as batch)
                         │ extract
                         ▼
                         L3 events ──────────►  vector / FTS rerank
                         │ reflect (shock|timer)
                         ▼
                         L4 thoughts ────────►  mixed with L3 in retrieve
```

---

## Stage 1 · L1 core blocks + biographic facts

**What this layer does.** Five prose core blocks (`persona / self / user / relationship / style`) plus fifteen structured biographic columns on the `personas` row (`full_name`, `gender`, `birth_date`, `timezone`, `occupation`, …). Both are loaded on every turn and injected into the system prompt; only five facts render into the prompt's `# Who you are` section (C-option contract). Current emotional state is not in L1 — it lives in L6 episodic state (see [`memory.md`](./memory.md)).

**What's tested.**

- Prompt template + parser — round-trip JSON, enum / date normalisation, malformed-JSON handling · `tests/prompts/test_persona_facts.py` (20 tests)
- Runtime orchestrator — LARGE tier default, `existing_blocks` flows into the LLM prompt, parser error becomes `PersonaExtractionError` · `tests/runtime/test_persona_extraction.py` (7 tests)
- Admin API — onboarding with/without facts, GET returns all 15 keys, PATCH partial update + explicit-null clears, enum coercion vs 422 on bad date · `tests/channels/web/test_persona_facts_routes.py` (17 tests)
- System prompt contract — only five facts render, `birth_date.year` not ISO, `timezone` stays out, empty view equals legacy prompt · `tests/runtime/test_interaction.py` (6 new tests) + `tests/memory/test_stage1_facts_addons.py` (4 tests)
- Schema migration + idempotency — 15 columns on fresh + legacy DBs, rerun is no-op · `tests/memory/test_migrations_idempotent.py`, `tests/memory/test_migrations_from_old_db.py`

---

## Stage 2 · L1 → L2 · ingest

**What this layer does.** Every inbound user / persona message is written verbatim to `recall_messages` with a `channel_id` provenance tag, and the owning session's `(message_count, total_tokens, last_message_at)` counters advance. Sessions are sharded by `(persona_id, user_id, channel_id)`. Lifecycle triggers:

- **IDLE** — 30 min without a new message → next `catch_up_stale_sessions` call marks it closing
- **MAX_LENGTH** — `message_count ≥ 200` or `total_tokens ≥ 20 000` → immediate close
- **Catchup at startup** — stale OPEN rows from a previous boot transition to CLOSING on first scan
- **Concurrency** — Web / Discord / iMessage ingest, idle scanner, consolidate worker all write the same SQLite file

**What's tested.**

- Single message + session creation, per-channel sharding, turn_id grouping, idle trigger under a fake clock, MAX_LENGTH close, catchup scan · `tests/memory/test_ingest.py`, `tests/memory/test_sessions_concurrency.py`, `tests/memory/test_recall_messages_turn_id.py`
- **WAL + busy_timeout pragmas** — pin `journal_mode=wal / synchronous=NORMAL / busy_timeout=5000` at connect time · `tests/memory/test_engine_pragmas.py` (4 tests)
- **Concurrent writers across channels** — three threads, three channels, 15 messages each, zero `OperationalError: database is locked` · `tests/memory/test_stage2_concurrency_and_catchup.py`
- **Worker drains orphan CLOSING session** — simulates a daemon restart; `initial_session_ids` + `drain_once()` consolidates the left-behind session · same file
- **Max-retries → FAILED** — `LLMTransientError` past the retry budget marks the session FAILED with the cause stamped on `close_trigger`, and an unrelated CLOSED session is not touched (no contagion) · same file

---

## Stage 3 · L2 → L3 · consolidate (extract)

**What this layer does.** When a session closes, the consolidate worker reads the session's L2 messages, asks the extraction LLM for zero or more events, embeds each, inserts vectors into the sqlite-vec companion table, and flips the session to CLOSED. Events carry `source_session_id` (optionally `source_turn_id`) for audit and a resume flag (`extracted_events`) so a mid-pass failure can retry without duplicating writes.

**Failure surfaces we care about.**

- Malformed extractor JSON (the LLM drifted off the schema)
- Enum-out-of-range `relational_tag`
- Vector insert mid-loop raises (backend outage)
- Session gets stuck in FAILED and needs a manual retry

**What's tested.**

- Trivial skip (msgs < 3 or tokens < 200) · normal session creates events · SHOCK triggers reflect · idempotent rerun · resume flag honoured · already-CLOSED session no-ops · bootstrap call creates the right block shape · `tests/memory/test_consolidate.py`
- **`make_extract_fn` returns `[]` on bad LLM JSON** — no session marked FAILED · top-level-array shape is also degraded to empty · `tests/memory/test_stage3_consolidate_addons.py`
- **Enum-out-of-range `relational_tag` dropped** at the parser layer in a full `make_extract_fn` round-trip · same file
- **Atomicity bug surfaced (xfail, strict)** — `backend.insert_vector` uses `engine.begin()` which auto-commits on its own connection, so a mid-loop failure can leave one event visible while the resume flag is still `False` · documented fix: either wrap vector writes inside the SQLAlchemy transaction, or batch-insert events then batch-insert vectors · `tests/memory/test_stage3_consolidate_addons.py::test_consolidate_atomic_when_vector_insert_raises_mid_event`
- **FAILED → CLOSING manual retry** — operator unwedge path works · worker consolidates the retried session normally · `same file`
- Parser layer — 15+ edge cases (malformed JSON, impact OOB, decimal impact, bool-as-int, unknown enum tag, truncation) · `tests/prompts/test_extraction.py`

---

## Stage 4 · L3 → L4 · reflect

**What this layer does.** After extract commits events for a session, consolidate decides whether to run reflection:

- **SHOCK** — any newly-created event with `|emotional_impact| ≥ 8`
- **TIMER** — no thought exists in the last 24 h
- **Hard gate** — cap at 3 thoughts per 24 h regardless of triggers

Reflection reads the last 24 h of events, asks the reflection LLM for higher-order thoughts, writes `ConceptNode(type=thought)` + `concept_node_filling` rows linking each thought to its source events.

**What's tested.** All under `tests/memory/test_stage4_reflect.py` — this layer had zero dedicated tests before.

- SHOCK trigger fires `reflect_fn` with `reason="shock"`
- TIMER fires when no prior thought sits inside the 24 h window
- TIMER is suppressed by a thought already within 24 h (no SHOCK = no reflect)
- 24 h hard gate blocks reflection past 3 thoughts *even on SHOCK*
- Filling chain lands with correct parent → child mappings, `orphaned=False`
- Soft-deleting one source event flips its filling row's `orphaned=True` but keeps the thought alive (forgetting-rights contract)
- Reflect crash leaves events committed and replayable: a second consolidate pass on the same session skips extraction (resume flag) and retries reflection without duplicating events

---

## Stage 5 · Retrieve

**What this layer does.** Before the persona speaks, retrieve assembles:

1. All L1 core blocks
2. Vector search over `concept_nodes` (`top_k` candidates)
3. Rerank with `0.5 * recency + 3 * relevance + 2 * impact + relational_bonus_weight * relational_bonus`
4. Minimum-relevance floor cuts orthogonal hits before rerank can promote them
5. Session-context expansion (pulls a few neighbouring L2 messages per event hit)
6. FTS fallback over L2 when the vector index itself returned too little

**Iron rule D4.** No function in the read path accepts a `channel_id` filter. Ever.

**What's tested.**

- Load all core blocks · shared vs per-user rows · vector search returns nearest · access_count increments on hit · rerank honours `relational_bonus_weight` · FTS fallback triggers on empty vector return · cross-channel list_recall_messages unified · `tests/memory/test_retrieve.py`
- **D4 signature guard** — `inspect.signature(retrieve)` and `inspect.signature(list_recall_messages)` both asserted to have no `channel_id` parameter · `tests/memory/test_stage5_retrieve_addons.py`
- **`min_relevance` floor drops orthogonal SHOCK** — a `|impact|=-9` event on an unrelated vector axis must NOT surface; dropping the floor to 0 proves it would otherwise rank above a mild aligned hit · same file
- **FTS fallback does not fire** when the vector index returns enough hits — protects against double-billing latency and stale L2 noise leaking into the prompt · same file
- **Recent L2 window stays independent of retrieve** — with zero ConceptNodes, `retrieve.memories == []` but `list_recall_messages` still hands back the last N turns (the window the runtime always feeds into the user prompt) · same file

---

## Cross-cutting invariants

- **F10 · no transport identity in prompts** — `assemble_turn` never leaks `channel_id` or a transport-name literal into the system or user prompt, even under mixed history · `tests/runtime/test_f10_no_channel_in_prompt.py`
- **Cross-channel unified persona** — A Discord session and a Web session for the same `(persona, user)` share memory · events from either surface in the other channel's retrieval · `tests/integration/test_cross_channel_unified_persona.py`
- **L6 episodic state write** — extraction's `session_mood_signal` field flows through consolidate and lands in the `personas.episodic_state` JSON column · `tests/memory/test_episodic_state.py`
- **Forget + orphan** — deleting an event cascades, orphans, or is cancelled per `DeletionChoice` · `tests/memory/test_forget.py`

---

## Eval layer · live LLM + judge (default-skipped)

**What this layer does.** The stage tests above run on a stub LLM — they pin logic, schema, concurrency. They say nothing about whether your actual prompt + model combo extracts the right events in the first place. The `tests/memory_eval/` suite fills that gap: eight fixture scenarios, each replayed through the real consolidate / retrieve pipeline with live LLM calls, with a second LLM acting as judge on the output.

**Architecture.**

```
tests/memory_eval/
├── fixtures/
│   ├── scripted/         · hand-written YAML · deterministic
│   └── synthesized/      · LLM-authored companion · generated once
├── harness.py            · load fixture → run pipeline → check invariants
├── judge.py              · same LLM asks yes/no on produced output
├── synthesize.py         · generates the synthesized companions (LARGE tier)
└── test_eval_fixtures.py · parametrized over every YAML · @pytest.mark.eval
```

**Fixtures.** One YAML per scenario. Scripted and synthesized share the same invariants + judge_prompts so one harness runs both; the only difference is who wrote the user messages.

| # | Scenario | Exercises |
|---|---|---|
| **E1** | User offers biographic facts + widowhood in casual chat | user-centric extraction, relational_tag = identity-bearing / vulnerability |
| **E2** | User only asks persona questions, never discloses | extractor correctly returns 0-1 events |
| **E3** | Buried SHOCK (mother's death) mid chit-chat | extractor catches peak + reflection triggers |
| **E4** | User corrects a fact they stated earlier | relational_tag = correction |
| **E5** | Five seeded events + short new session | TIMER reflect runs · thought is an abstraction, filling ≥ 2 |
| **E6** | Ten seeded events, query about Mochi | top-3 contains ≥ 2 Mochi / medical events |
| **E8** | Bilingual session, Chinese majority | extraction output in Chinese |

**Hard invariants** (checked by `harness.check_invariants`): event count bounds, `shock_event_present`, `reflection_triggered`, `must_mention_any` substring match, `must_have_relational_tag_any`, `filling_min`, `top3_relevant_min`, `output_language`.

**Soft invariants** (checked by `judge.judge_prompts`): each fixture carries one or more yes/no questions answered by the same LLM on MEDIUM tier with the evidence rendered by `harness.render_evidence`. A "no" fails the test.

**How to run.**

```bash
# run the eval layer once (costs cents-level LLM spend)
uv run pytest tests/memory_eval/ -m eval -v

# generate the synthesized companion fixtures (one-time, LLM-authored)
uv run python -m tests.memory_eval.synthesize
#   → writes tests/memory_eval/fixtures/synthesized/e*.yaml
#   skim · tweak · commit
```

Scripted fixtures pin regressions you can reason about line-by-line. Synthesized fixtures stress the pipeline against phrasing you did not anticipate. Both feed the same test runner.

---

## Running the suite

```bash
# everything except eval (default)
uv run pytest

# just the memory stage tests
uv run pytest tests/memory/ tests/prompts/ tests/runtime/

# eval layer · live LLM · costs money
uv run pytest tests/memory_eval/ -m eval -v

# lint + import contracts (runs on every memory change)
uv run ruff check src/ tests/
uv run lint-imports
```

One `xfail` is expected today — the consolidate atomicity bug surfaced in Stage 3. A red there means the bug has been fixed (and the `xfail` should be converted to a regular test).

# Memory

> Hierarchical persona memory. L1 core blocks that always enter the prompt, L2 raw messages as ground truth, L3 extracted events, L4 distilled thoughts. One continuous identity across every channel the persona speaks on.

Memory is EchoVessel's core asset. Everything else — runtime, channels, voice, proactive — exists to feed it, query it, or surface what it remembers. A digital persona is only as persistent as the memory behind it, and this module is where that persistence lives.

---

## Overview

Memory gives a persona a durable sense of self and of the people it talks to. It is hierarchical because the six layers answer six different questions: "who am I right now" (L1), "what was literally said" (L2), "what happened in that conversation" (L3), "what do I believe about this person across many conversations, what have I promised, what am I expecting" (L4), "what are the canonical names and aliases of the third-party people, places, and organisations I know about" (L5), and "how do I feel right now" (L6). Each layer has its own write path, its own retrieval role, and its own forgetting semantics. Collapsing them into a single store would either bloat the prompt or lose the ability to reflect.

The module is deliberately decoupled from every other layer. It does not know what a channel is, it does not know what an LLM provider is, it does not know how runtime streams responses. Higher layers inject embedding functions, extraction functions, and reflection functions as plain callables; memory handles storage, scoring, and lifecycle. This discipline is enforced by the layering contract in `pyproject.toml` — memory may depend on `echovessel.core` and nothing else.

The promise the rest of the system makes on top of memory is simple: one persona is one continuous identity. When the user talks to it on the web this afternoon and on Discord tomorrow night, memory is the same pool. Retrieval never filters by which channel a message arrived on. That single rule shapes most of the design decisions below.

---

## Core Concepts

**L1 core blocks** — short, stable pieces of text that are injected into every prompt unconditionally. Three labels live in `core_blocks`: `persona`, `user`, `style`. `persona` / `style` are shared across users (the persona is one character, and the owner-curated style instructions don't fork per user). `user` is per-user, keyed by `(persona_id, user_id)`. Each block is capped at 5000 characters and has an append-only audit log in `core_block_appends`. Emotional state is not in L1 — current mood lives in L6 episodic state.

**L1 is the "human-authored identity" layer — never auto-updated by code.** All three write entry points are human-driven: onboarding bootstrap, the admin UI (`POST /api/admin/persona` and `POST /api/admin/persona/style`), and the import pipeline's `bootstrap_from_material`. Code paths inside `slow_tick` / `consolidate` / extraction **must not** write to `core_blocks` — the `tests/memory/test_slow_cycle.py` "L1-never-auto-update invariant" pins this rule. Persona reflection grows into L4 thoughts (`subject='persona'`); persona descriptions of third-party people / places / orgs grow into L5 `entities.description`. Neither lands in L1.

**Biographic facts** — fifteen structured identity columns on the `personas` row itself (`full_name`, `gender`, `birth_date`, `nationality`, `native_language`, `timezone`, `occupation`, `relationship_status`, …). These live alongside the prose core blocks rather than inside them, so code that needs "what timezone is she in" or "what year was she born" can query a column instead of re-parsing the persona block. All fifteen are nullable — the LLM extraction fills what it can during onboarding and the user corrects the rest in the review page. Five of them (name, gender, birth year, occupation, native language) get rendered into the system prompt's "# Who you are" section every turn; the other ten are for system use only.

**L2 raw messages** — every user and persona message is written verbatim to `recall_messages`. This is the archival ground truth. The table stores `channel_id` alongside each row so a frontend can render a "via Web" or "via Discord" badge, but queries that feed the prompt never filter on it. L2 is indexed by FTS5 for keyword fallback but does not participate in the main retrieval pipeline; it is the layer the system can always fall back to when everything else fails.

**L3 events** — facts extracted from a closed session. Stored as `ConceptNode` rows with `type='event'`: a natural-language description, an `emotional_impact` in `-10..+10`, emotion and relational tags, an embedding in the sqlite-vec companion table, and a pointer back to the `source_session_id` they came from. Events are the main unit of episodic memory — "the conversation where the user told me Mochi had surgery".

**L4 thoughts** — longer-term observations distilled from many events. Same table as L3, differentiated by `type='thought'`. Each thought carries a `filling` chain (via `concept_node_filling`) that records which events it was generated from, so a user who deletes the source events can choose to keep the thought as orphaned. Thoughts are written by two paths: the SHOCK / TIMER reflection inside consolidate (fast loop) and the slow_tick reflection phase that runs between sessions (slow loop · forward-looking inference). Slow_tick can also produce `type='expectation'` sub-class nodes ("she'll probably update grad school next week"); the fast loop matches new user messages against `expectation.description` by embedding similarity, marking hits `fulfilled` and overdue ones `expired`.

Each thought row carries a `subject` column: `subject='user'` is the persona's judgement *about the user* ("she's been stretched thin lately"), `subject='persona'` is the persona's reflection *about itself* ("I've been replying too tersely"). The `subject='persona'` rows are produced by the slow_tick G phase — they are the only physical path by which the persona's self-image grows over time (the L1 `persona` block itself is never written by code). The prompt renderer surfaces the most recent ≤5 of them in the user prompt's `# How you see yourself lately` section, fetched by `retrieve.force_load_persona_thoughts(top_n=5)` directly, bypassing query embedding.

**L5 entities** — canonical identity for third-party people / places / orgs / pets. Three tables: `entities` (canonical name + kind + tri-state `merge_status` + a `description` prose column), `entity_aliases` (many-to-one alias → entity), and `concept_node_entities` (many-to-many junction between L3 events and L5 entities). Extraction creates a new entity when a new name appears and only appends an alias row when a known entity surfaces under a new alias. At retrieve time, any alias hit in the query text pulls every ConceptNode linked to that entity into the candidate pool with a score bump — this is the engineering basis for cross-language / cross-alias recall, since vector distance alone cannot bridge "Scott" and "黄逸扬". Three-tier dedup (alias exact match → embedding 0.65 / 0.85 thresholds → uncertain branch where the persona naturally asks the user) lives in `memory/entities.py`.

`entities.description` is the prose portrait column: slow_tick synthesizes one when an entity's `linked_events_count` crosses a threshold (default ≥ 3), and the owner can override via `PATCH /api/admin/memory/entities/{id}` (which sets `owner_override=true` and locks slow_tick out of further overwrites). Both write paths funnel through the same `update_entity_description(db, *, entity_id, description, source)` primitive, with `source` ∈ {`'slow_tick'`, `'owner'`}. When an alias anchor hits a confirmed entity carrying a non-empty description, retrieve injects an `# About {canonical_name}` section into the system prompt — one entity per section, strict 1:1 cardinality with the entity row.

**L6 episodic state** — a single-row snapshot of how the persona feels right now, stored as JSON in the `personas.episodic_state` column: `{mood, energy, last_user_signal, updated_at}`. The extraction LLM emits a `session_mood_signal` field alongside events when a session closes, and consolidate writes it through — no extra LLM call, no extra round-trip, so updating L6 is free. `assemble_turn` checks for 12h decay on entry; if the snapshot is older than 12 hours, mood resets to `neutral` so a long quiet period doesn't open the next turn under stale affect. Renders to the system prompt's `# How you feel right now` section; when mood is `neutral`, the section is skipped to keep the prompt terse.

**Consolidate** — the pipeline that runs when a session closes. It reads the session's L2 messages in one batch, calls the injected extraction function to produce zero or more L3 events, embeds each event, optionally triggers a reflection pass that writes L4 thoughts, and marks the session `CLOSED`. The entry point is `consolidate_session` in `src/echovessel/memory/consolidate/core.py`.

**Retrieve** — the pipeline that runs before the persona speaks. It loads every L1 core block, asks the storage backend for a vector search over `concept_nodes`, reranks the candidates with a four-factor score, applies a minimum-relevance floor to suppress orthogonal matches, and optionally expands each hit with a few neighbouring L2 messages. If the vector index returns too few hits, an FTS fallback over L2 supplements the result. The entry point is `retrieve` in `src/echovessel/memory/retrieve/core.py`.

**Observer** — a Protocol in `src/echovessel/memory/observers.py` that higher layers implement to react to memory writes. Memory never imports runtime or channels; instead, runtime registers a `MemoryEventObserver` at startup and memory fires hooks into it after every successful commit. Exceptions from observers are caught and logged, never rolled back into the memory write itself.

**Idempotent migration** — the module upgrades an existing `memory.db` without Alembic. `ensure_schema_up_to_date` inspects `sqlite_master` and `PRAGMA table_info` and runs `ADD COLUMN` / `CREATE TABLE IF NOT EXISTS` statements only when the target state is missing. Running it on a fresh database is a no-op; running it on a legacy database brings it up to the current shape in one pass.

---

## Architecture

Memory sits on the lower tier of the five-module stack. Runtime orchestrates. Channels and Proactive live above memory and voice. Memory and Voice sit directly on the shared `echovessel.core` types. Nothing in memory imports from a higher layer, and the `pyproject.toml` import-linter contract enforces this.

```
runtime
   |
   +-- channels    proactive
   |      \\        /
   |       +------+
   |       |
   +----> memory      voice
              \\       /
               core
```

Two data paths run through this module.

### Write path

```
channel / runtime
      |
      v
ingest_message(persona, user, channel, role, content, turn_id)
      |
      v
get_or_create_open_session()  --+  (may queue "new session started")
      |                         |
      v                         |
write RecallMessage to L2       |
      |                         |
      v                         |
update session counters         |
      |                         |
      v                         |
check_length_trigger            |
      |                         |
      v                         |
db.commit()                     |
      |                         |
      v                         |
drain_and_fire_pending_lifecycle_events()  <--+
      |
      v
observer.on_message_ingested(msg)   (per-call hook)
```

Every write commits before any hook fires. The lifecycle queue in `sessions.py` batches "new session" / "session closed" events so that a single commit can dispatch multiple hooks in one drain. `ingest_message` and `append_to_core_block` accept explicit `observer=` parameters (per-write notifications); most other hooks (event/thought/entity/session/mood) fan out from the module-level `_observers` registry populated once via `register_observer(...)` at daemon startup.

When a session crosses `SESSION_MAX_MESSAGES` or `SESSION_MAX_TOKENS`, it is marked for closing and the next `ingest_message` call opens a new one in the same channel. Nothing is visible to the user — the split is an internal extraction boundary. Idle sessions (over 30 minutes without a message) and lifecycle signals from runtime (daemon shutdown, persona swap) close sessions the same way.

Session closure flows into `consolidate_session`, which runs the extraction pass, possibly a reflection pass, and finally flips `session.status` to `CLOSED` before firing `on_session_closed`. Extraction calls the injected LLM once per session, regardless of how many turns it contains; a burst of user messages becomes several L2 rows but a single extraction call.

### Read path

```
runtime asks: "what does memory say about <query>?"
      |
      v
retrieve(db, backend, persona, user, query, embed_fn)
      |
      +-- load_core_blocks()  -> every L1 block enters the result
      |
      v
backend.vector_search(embed_fn(query), types=('event','thought'))
      |
      v
load ConceptNode rows where deleted_at IS NULL
      |
      v
score each = 0.5*recency + 3*relevance + 2*impact + 1*relational_bonus
      |
      v
drop rows where relevance < min_relevance (default 0.4)
      |
      v
sort by total, keep top_k
      |
      v
access_count += 1 on every surviving hit, commit
      |
      v
expand each event hit with +/- N L2 neighbours (optional)
      |
      v
if raw vector hits < fallback_threshold:
    FTS search over L2
      |
      v
return RetrievalResult(core_blocks, memories, context_messages, fts_fallback)
```

The four rerank factors matter individually. Recency is a time-based exponential with a 14-day half-life so that old-but-still-relevant memories do not vanish. Relevance comes straight from the vector backend's distance converted to `[0, 1]`. Impact is `|emotional_impact| / 10` so that a peak event outweighs a forgettable one when relevance ties. The relational bonus is a small flat boost (`0.5`) whenever a node carries any relational tag — `identity-bearing`, `unresolved`, `vulnerability`, `turning-point`, `correction`, `commitment` — so that identity facts are preferred on ties.

The `min_relevance` floor is load-bearing. Without it, strictly-orthogonal vector matches tie at a relevance of `0.5` and the impact weight silently promotes high-intensity events for completely unrelated queries. The default `0.4` is low enough to keep partial-overlap candidates and high enough to reject true strangers. Callers who want the old behaviour pass `min_relevance=0.0`.

### Entity-anchored retrieval (L5 sidecar)

A cheap alias sidecar runs alongside the main vector path: every retrieve call tokenises the query text and feeds it to `find_query_entities()` for case-sensitive exact matches (CJK is not tokenised). Any matched entity is reverse-walked through the `concept_node_entities` junction to collect every linked ConceptNode. Those nodes enter the candidate pool regardless of vector distance and pick up a `WEIGHT_ENTITY_ANCHOR * ENTITY_ANCHOR_BONUS_VALUE` bump (default 1.5 × 1.0) at rerank. This path exists specifically for cross-language alias recall — sentence-transformers cannot pull "Scott" and "黄逸扬" together, but exact alias matching can. When a matched entity has `merge_status='uncertain'` (its embedding sits in the 0.65 ~ 0.85 grey zone where automatic merge is unsafe), retrieve also injects a `# Entity disambiguation pending` hint into the system prompt so the persona naturally asks the user "is Scott the same person as 黄逸扬?" mid-flow; the user's answer flips the entity to confirmed or disambiguated on the next consolidate.

### Force-loaded pinned thoughts (bypasses query similarity)

`retrieve(force_load_user_thoughts=N)` is the sidecar that powers the `# About {speaker}` user-prompt section: it returns the top-N L4 thoughts about the current speaker by `recency × importance`, **without ever consulting the query embedding**. The reason: when the user message is "?" or "嗯" or a single character, the query has nothing to index against, but the persona still ought to know who it's talking to. Runtime defaults to `force_load_user_thoughts=10` from `assemble_turn`, and excludes node ids already returned by the main rerank so render doesn't double-bullet. Returned via `RetrievalResult.pinned_thoughts`.

### Seven kinds of stimulus reactivity

The memory module's response path for all seven stimulus kinds is implemented and event-driven — none of them rely on cron polling:

| # | Stimulus | Reaction path |
| --- | --- | --- |
| 1 | Ordinary user turn | `ingest_message` → session counters → length / idle trigger check · `assemble_turn` entry checks episodic decay · `retrieve` (with entity_anchor + pinned) · LLM stream |
| 2 | High-emotion event (\|impact\| ≥ 8) | Existing SHOCK reflection (fast loop · `consolidate.py`) · slow_tick marks the session as immediate (skips the 30 min cool-down) |
| 3 | Repeat mention | `entities.detect_mention_dedup` runs vector + entity overlap inside extraction · matches bump `mention_count++` and append `source_turn_ids` instead of inserting a new node |
| 4 | Contradicting information | Extraction outputs `superseded_event_ids` · consolidate sets the old node's `superseded_by_id` to the new node (soft delete · row not removed) · retrieve filters `superseded_by_id IS NULL` by default |
| 5 | User correcting style | Owner explicitly writes the `STYLE` block via `POST /api/admin/persona/style` · no NLP keyword auto-detection |
| 6 | Quiet period (no user messages) | `idle_scanner` closes stale sessions (existing) · `assemble_turn` entry runs the 12h episodic_state decay back to neutral · slow_tick does not fire when there's no fresh inbox material |
| 7 | Spontaneous recall (slow_tick) | The G phase appended to `consolidate_worker._process_one` runs the slow_cycle LLM under the right conditions · produces `type='thought'` and `type='expectation'` nodes · likely to be retrieved by the next fast-loop turn (this is the physical realisation of "the persona thinks of you when you're not talking") · the produced thought is broadcast immediately via `on_thought_created` → SSE topic `memory.thought.created`, so the Web chat's Memory Timeline shows it in real time |

Time only legitimately enters the system in three places: L6 episodic 12h decay · `consolidate_worker` 5s polling · TIMER reflection (force one reflect pass if 24h have passed without any thought). Anything else of the "every Tuesday X / first of the month Y" shape is forbidden — the memory module only accepts event-driven triggers.

### Slow_tick consolidate phase (L4 forward-looking)

slow_tick is **not a separate worker** — it's the G phase appended to `consolidate_worker._process_one` after A-F. After each session's CLOSING handler finishes:

- `is_trivial` → skip
- `now - persona.last_slow_tick_at < cool_down_minutes` (default 30) AND no SHOCK in this session → skip
- otherwise run

The run makes one `slow_cycle_llm` call. Input: cross-session material from the cool-down window (events + existing thoughts + the previous cycle's `salient_questions`). Output: three sub-tasks bundled in one return — `new_thoughts` (mixing `subject='persona'` self-reflections and `subject='user'` user-judgements) / `new_expectations` / `salient_questions` (seed questions for the next cycle, written back to the `personas` row). Then:

- `bulk_create_thoughts(new_thoughts)` writes the L4 thought nodes plus their filling chains; `subject='persona'` rows feed the user prompt's `# How you see yourself lately` section
- `type='expectation'` nodes get written with `event_time_end` as the due_at · the fast loop runs embedding similarity between each new user message and pending `expectation.description` · matches mark fulfilled, overdue ones expired
- as a side pass: scan each entity's `linked_events_count`; over-threshold rows funnel through `update_entity_description(...)` to synthesize a description (slow_tick always uses `source='slow_tick'`; rows with `owner_override=true` are skipped)
- `personas.last_slow_tick_at = now`

**slow_tick never writes L1 core_blocks — not one row.** Persona self-reflection lands in L4 thought[subject='persona']; persona descriptions of people/places/orgs land in L5 entities.description. The `BlockLabel` enum has only `persona` / `user` / `style`, and `append_to_core_block` is not in slow_tick's call chain — pinned by the `tests/memory/test_slow_cycle.py` "L1-never-auto-update invariant" test.

Guardrails (load-bearing invariants): per-cycle token wall (input ≤ 8k · output ≤ 1k) · daily 36 cycles + 150k input + 30k output · `cfg.slow_tick.enabled = false` global kill switch · each event reflected on ≤ 3 times. Slow_tick is forbidden from: writing any L1 block · creating intentions without source-turn evidence · scheduling itself · editing nodes in place · calling external APIs · creating new NodeTypes / BlockLabels · recursively self-invoking · bypassing the token budget. The closed tool enumeration plus schema rejection enforce these invariants. Transcripts land at `develop-docs/slow_tick_transcripts/<cycle_id>.json`; the admin tab exposes `GET /api/admin/slow-tick/transcripts` for browsing history.

### One persona across channels

Memory retrieval never filters by `channel_id`. Not in the vector search. Not in the FTS fallback. Not in session context expansion. Not in core-block loading. A human in a group chat still remembers every private conversation they have had; memory should behave the same way. Deciding whether a given remembered fact is appropriate to bring up in the current channel is the job of higher layers, not of retrieval.

Session sharding is the one place channel identity matters inside memory: a session is created per `(persona_id, user_id, channel_id)` so that idle timers and max-length triggers in one channel do not close an active session in another. Once a session's L3 events are extracted, those events join the unified pool and retrieval treats them as channel-agnostic.

### Session lifecycle

```
get_or_create_open_session()      -- OPEN
       |
       v
ingest_message() x N              -- OPEN (counters accumulate)
       |
       v
idle > 30min OR length trigger OR lifecycle signal
       |
       v
consolidate_session()             -- CLOSED after extract + reflect
       |
       +-- A. trivial? skip extraction
       +-- B. extract_fn(messages) -> L3 events    [sets extracted_events=True]
       +-- C. any event with |impact| >= 8 -> SHOCK reflection
       +-- D. > 24h since last reflection -> TIMER reflection
       +-- E. reflect_fn(recent events) -> L4 thoughts (hard gate: max 3 per 24h)
       +-- F. mark CLOSED
       |
       v
on_session_closed fires via the lifecycle queue
```

Every step commits before the next one begins, and the observer dispatch sits strictly after the commit that transitioned `session.status`. A consolidation that crashes midway leaves the database in a recoverable state: the session stays in `CLOSING`, the next startup's catch-up pass picks it up, and no lifecycle hook fires for a session that was never really closed.

### Triggers and thresholds

The numeric thresholds that shape the flow above are module-level constants, not config knobs:

| Transition | Trigger | Where | Constant(s) |
| --- | --- | --- | --- |
| L2 → (close) | idle timeout | `memory/sessions.py` | `SESSION_IDLE_MINUTES = 30` |
| L2 → (close) | length cap | `memory/sessions.py` | `SESSION_MAX_MESSAGES = 200`, `SESSION_MAX_TOKENS = 20_000` |
| L2 → (close) | runtime lifecycle | channels / catchup | — |
| L3 → L4 | SHOCK — any new event crosses the impact floor | `memory/consolidate/phase_bce.py` | `SHOCK_IMPACT_THRESHOLD = 8` (absolute value) |
| L3 → L4 | TIMER — no recent reflection | `memory/consolidate/phase_bce.py` | `TIMER_REFLECTION_HOURS = 24` |
| L3 → L4 (gate) | cap reflections per 24 h | `memory/consolidate/phase_bce.py` | `REFLECTION_HARD_LIMIT_24H = 3` |

Two things worth calling out because they trip new readers:

- **The `emotional_impact` that drives SHOCK is produced by the extraction LLM itself**, not by a separate classifier. Each event it emits must carry a signed integer in `[-10, +10]`; consolidate just checks `max(|impact|) >= 8` over the events that were just written. The extraction prompt (`prompts/extraction.py`) tells the model how to use the scale, with `-7 = serious loss / grief` and `+7 = major positive milestone` as anchor points.
- **SHOCK and TIMER are OR'd; the hard gate is AND'd on top.** The rule is "run reflection if (shock OR timer_due), UNLESS three thoughts already landed in the last 24 h". The gate protects against a single emotionally dense day racking up an unbounded reflection bill.

### End-to-end walkthrough · life of one message

Concrete timeline, one message's worth. Times are illustrative; exact values come from the constants in the table above.

```
t = 0s             User types "hi" in the Web channel
                   ↓
  WebChannel debounces (~2s), emits IncomingTurn to runtime
                   ↓
  runtime.handle_turn() begins
                   ↓
┌─ memory.ingest_message(persona, user, channel, USER, "hi")
│    get_or_create_open_session(persona, user, channel)
│      · SELECT OPEN sessions WHERE (persona, user, channel, deleted_at IS NULL)
│      · any row with last_message_at > now - 30min?      → return it
│      · stale row (idle > 30min)?                         → mark_closing('idle'), make new
│      · no row at all?                                    → INSERT new OPEN session
│    INSERT recall_messages (role=USER, content, turn_id, channel_id, day)
│    UPDATE session: message_count++, total_tokens+=N, last_message_at=now
│    check_length_trigger
│      · message_count ≥ 200 OR total_tokens ≥ 20_000?    → mark_closing('max_length')
│    db.commit()
│    fire on_message_ingested(msg) · drain pending lifecycle queue
└─

t ≈ 0.1s           runtime prepares the reply
                   ↓
┌─ memory.load_core_blocks(persona, user)
│    → 3 rows (persona / style [shared] + user [per-user])
│
├─ assemble_turn entry checks L6 episodic_state · 12h decay resets mood to neutral
│
├─ memory.retrieve(persona, user, query=last_user_msg, embed_fn, top_k=10,
│                  user_now=msg.received_at)
│    query_vec = embed_fn(query)
│    L5 entity-anchor cheap match · alias hits pull linked ConceptNodes into pool
│    backend.vector_search(query_vec, types=('event','thought','intention','expectation'), top_k=40)
│    load ConceptNode rows where deleted_at IS NULL AND superseded_by_id IS NULL
│    rerank each: 0.5·recency + 3·relevance + 2·impact + relational_bonus + entity_anchor_bonus
│    drop where relevance < min_relevance (default 0.4)       ← over-recall floor
│    keep top_k, UPDATE access_count++, last_accessed_at
│    derive_event_status(event_time_*, user_now) · render_event_delta_phrase
│    optional: pull ±N L2 neighbours per event hit
│    FTS fallback on L2 if raw vector hits < fallback_threshold
│
├─ force_load_user_thoughts(persona, user, top_n=10) · pinned thoughts bypass query similarity
│
├─ assemble_turn → LLM.stream(system_prompt, user_prompt)
│    See docs/en/runtime.md § Prompt section order for the canonical order.
│    system_prompt = opener + # Right now (dual TZ) + # Who you are (7 facts)
│                    + # How you feel right now (L6 episodic) + L1 core blocks
│                    + # Style preferences + # About {canonical_name} (alias hit)
│                    + # Entity disambiguation pending + STYLE_INSTRUCTIONS
│    user_prompt   = # Recent sessions (day-bucket) + retrieved thoughts/events
│                    + # About {speaker} (pinned user-thoughts)
│                    + # How you see yourself lately (pinned persona-thoughts)
│                    + # Promises you've made + # You've been expecting
│                    + # Our recent conversation + # What they just said
│
└─ stream tokens → channel → write accumulated reply back via ingest_message(PERSONA)

t = a few seconds  persona reply is persisted. session is still OPEN.

                   (user walks away · no further messages)

t ≈ 30-60min       idle_scanner wakes (every 60s by default)
                   ↓
                   catch_up_stale_sessions(db, now)
                     · SELECT status=OPEN AND last_message_at < now - 30min
                     · mark_closing('catchup') each
                     · commit

t ≈ 30-60min + 5s  consolidate_worker polls (every 5s by default)
                   ↓
                   SELECT status=CLOSING AND extracted=False
                   enqueue each session_id, then for each:
                   ↓
┌─ consolidate_session(session)
│
│  [A] is_trivial(session)?                                ← short + no emotion
│        msgs < 3 AND tokens < 200 AND no peak signal
│        yes → status=CLOSED, trivial=True, fire hook, DONE
│        no  → continue
│
│  [B] if session.extracted_events is False (first pass):
│        events = await extract_fn(messages)               ← LLM call (SMALL tier)
│        for e in events:
│          INSERT concept_nodes(type='event', ...)
│          backend.insert_vector(id, embed_fn(e.description))
│        session.extracted_events = True
│        session.extracted_events_at = now
│        db.commit()                                       ← RESUME POINT
│      else (retry after a prior crash):
│        events = SELECT concept_nodes WHERE source_session_id=session.id
│
│  [C] shock_event = first e where |e.emotional_impact| ≥ 8     ← extractor's score
│
│  [D] timer_due = no thought exists younger than 24h
│
│  [E] should_reflect = (shock_event OR timer_due)
│      AND reflections_last_24h < 3                        ← hard gate
│      if should_reflect:
│        inputs = recent events in last 24h (+ shock_event if not already in)
│        thoughts = await reflect_fn(inputs, reason)       ← LLM call (SMALL tier)
│        for t in thoughts:
│          INSERT concept_nodes(type='thought', ...)
│          backend.insert_vector(id, embed_fn(t.description))
│          for src_id in t.filling:
│            INSERT concept_node_filling(parent=t.id, child=src_id)
│        db.commit()
│
│  [F] session.status = 'closed'
│      session.extracted = True
│      session.extracted_at = now
│      db.commit()
│      fire on_session_closed(session)                     ← lifecycle queue drain
│
│  [G] slow_tick consolidate phase — runs only when should_run_slow_cycle agrees
│      (cool_down OK, session non-trivial, daily-cap and token-wall not breached):
│        run_slow_cycle(persona, recent_events, recent_thoughts, ...)
│          → typed ConceptNode output · type IN ('thought','expectation')
│          → thought rows carry subject='user' or subject='persona'
│            · subject='persona' is the home for persona self-reflection
│              · then rendered into the user prompt's
│                # How you see yourself lately section
│          → each carries filling_event_ids · expectation requires non-empty
│            reasoning_event_ids
│          → side pass: scan entities, synthesize description for any
│            entity over linked_events_count threshold
│          → personas.last_slow_tick_at = now
│      slow_tick never writes L1 · no append_to_core_block in this path
│      Failure does not roll back the session ([F] is already committed) · just logs.
│
│  · session_mood_signal (emitted by the [B] extraction LLM as a side field) is
│    written into personas.episodic_state JSON between [B] and [F] · no extra LLM.
│  · L5 entities: while [B] writes events, the three-tier dedup
│    (alias / embedding / ask-user) updates entities + entity_aliases and writes
│    the concept_node_entities junction.
└─

Next turn, retrieve sees the newly-written L3 events + L4 thoughts; L6 episodic_state
reflects the session's emotional arc; expectations from slow_cycle surface in the
# You've been expecting section of the user prompt.
```

Three states the session can land in after all of this:

| Status | Meaning | Is it retryable? |
| --- | --- | --- |
| `CLOSED` | happy path — `extracted=True`, session is done | no |
| `FAILED` | `consolidate_worker` exhausted `worker_max_retries` attempts — terminal | not automatically; operator resets `status=CLOSING` + `extracted=False` to retry |
| `CLOSING` (stuck) | daemon crashed mid-consolidate — the next startup's catchup picks it up | automatic on next boot |

### Retry safety

Stage B commits the extracted L3 events **in the same transaction** as a new `extracted_events=True` flag on the session. If stage E (reflection) then raises — a transient LLM error, a timeout, even `SIGTERM` — the worker retries `consolidate_session` from the top. The top-of-function guard reads `extracted_events` and skips B entirely: already-persisted events are loaded from the database, fed into SHOCK/TIMER detection, and reflection runs against them. Extraction LLM calls are therefore run at most once per session, regardless of how many times reflection fails.

This invariant matters in both directions:

- `extracted=True` implies `extracted_events=True` (F only runs after B committed its flag).
- `extracted_events=True` does NOT imply `extracted=True` — that's the whole point of the resume state.

Sessions that die in state `extracted_events=True, status=CLOSING` are retried safely by the worker. Sessions that transition to `FAILED` (catch-all in `consolidate_worker._mark_failed`) are terminal and never retried automatically; admin intervention is required to reset them.

### Manual re-consolidation

Idempotency lives in the persistent `Session.extracted` flag, not in worker memory. A running worker instance will re-consolidate any session that transitions into `status='closing', extracted=false` — **no daemon restart required**. This matters for debugging (change a consolidate threshold, re-run on an old session), for recovering `FAILED` sessions after fixing a root cause, and for any "please run this again" ops flow.

Flip the flags directly:

```sql
UPDATE sessions
   SET status = 'closing', extracted = 0, extracted_events = 0
 WHERE id = 's_xxx';
```

Within one poll interval (default 5s) the worker picks the session up and runs `consolidate_session` against the current config. `is_trivial` is re-evaluated against the live `[consolidate].trivial_*` thresholds, so a session skipped under an old threshold can now extract events if the threshold was lowered.

The same short-circuit applies even if someone force-appends an already-`extracted=True` session id onto the queue: `_process_one` reads the flag and returns without calling the extractor. The persistent flag is the single source of truth; there is no in-memory `seen` set.

### Schema migration

`ensure_schema_up_to_date(engine)` is called before `create_all_tables(engine)` during daemon startup. It walks a hardcoded list of "add column if not exists" and "create table if not exists" steps, each guarded by `PRAGMA table_info` or a `sqlite_master` lookup. Every new column is either nullable or has a SQL default, so existing rows do not need backfilling. The migrator does not support renames, drops, or type changes — those are postponed to a later migration framework. Failure is fatal: a half-migrated schema fails fast at startup rather than silently corrupting writes later.

### Observer contract

Observers are fire-and-forget post-commit notifications. The Protocol lives in `observers.py` and exposes 9 hooks split across two firing paths — pure per-write and lifecycle:

```
MemoryEventObserver  (Protocol · source of truth in observers.py)
  # Pure per-write hooks · only fire when caller passes observer=
  on_message_ingested(msg)
  on_core_block_appended(append)

  # 7 lifecycle hooks · fan out automatically through the _observers registry
  on_event_created(event)
  on_thought_created(thought, source)        # source ∈ {reflection, slow_tick, import}
  on_entity_confirmed(entity)                # uncertain entities are not broadcast — see plan §3.1
  on_entity_description_updated(entity, source)  # source ∈ {slow_tick, owner}
  on_new_session_started(session_id, persona_id, user_id)
  on_session_closed(session_id, persona_id, user_id)
  on_mood_updated(persona_id, user_id, new_mood_text)  # mood actually lives in L6 · no L1 write
```

`on_event_created` and `on_thought_created` fire **on both paths** — they fan out through the lifecycle registry (so `RuntimeMemoryObserver` sees them) AND honour the explicit `observer=` parameter on `consolidate_session` / `import_content` (so import pipelines and tests can keep their per-call callbacks). The fan-out is additive; the older per-call invocation semantics are unchanged.

All methods are plain `def` (not `async def`). Exceptions raised by a hook are caught at the memory boundary and logged via the module logger; the memory write that fired the hook has already committed by then and is never rolled back. A consumer that implements only some of the hooks relies on structural subtyping — `NullObserver` is provided as a no-op base for subclassing.

Session lifecycle events flow through a small queue in `sessions.py`: the code path that mutates `session.status` enqueues a pending event and the committing caller drains the queue immediately after `db.commit()` returns, so a single commit can dispatch several lifecycle hooks in one pass. Entity hooks (`on_entity_confirmed` / `on_entity_description_updated`) skip the session queue and fire `_fire_lifecycle(...)` directly from the entity write path in `entities.resolve_entity` / `entities.apply_entity_clarification` / `update_entity_description` — they have no relationship to session boundaries.

The runtime-side `RuntimeMemoryObserver` (in `src/echovessel/runtime/wiring/memory_observer.py`) implements every lifecycle hook above and turns each into an SSE topic broadcast across every channel that exposes `push_sse`. See `docs/en/runtime.md § Cross-Channel SSE` and `docs/en/channels.md § Web channel` for the topic catalog.

---

## How to Extend

Three common extensions, each shown as a minimal working sketch. Point them at a real persona and a real database before running.

### 1. Register a custom observer

Implement the Protocol (or subclass `NullObserver`) and register the instance at startup. Hooks fire on the memory module's thread immediately after the commit that produced them.

```python
from echovessel.memory import (
    MemoryEventObserver,
    NullObserver,
    ConceptNode,
    register_observer,
)


class EventLogger(NullObserver):
    """Toy observer that logs every new L3 event as it lands."""

    def __init__(self) -> None:
        self.count = 0

    def on_event_created(self, event: ConceptNode) -> None:
        self.count += 1
        print(
            f"[event #{self.count}] {event.description!r} "
            f"impact={event.emotional_impact} "
            f"tags={event.relational_tags}"
        )

    def on_session_closed(
        self, session_id: str, persona_id: str, user_id: str
    ) -> None:
        print(f"[session closed] {session_id} for {persona_id}/{user_id}")


logger = EventLogger()
register_observer(logger)
# Once registered, every lifecycle hook fires automatically — including
# on_event_created and on_thought_created. on_message_ingested /
# on_core_block_appended still require the caller to pass observer=logger
# explicitly into the relevant write API.
```

After registration, every lifecycle hook (`on_new_session_started` / `on_session_closed` / `on_mood_updated` / `on_event_created` / `on_thought_created` / `on_entity_confirmed` / `on_entity_description_updated`) fires automatically — no caller needs to thread `observer=` through. The pure per-write hooks (`on_message_ingested`, `on_core_block_appended`) still fire only when the caller passes `observer=...` into `ingest_message` / `append_to_core_block`. `on_event_created` / `on_thought_created` are dual-fire: the lifecycle fan-out **plus** the per-call `observer=` parameter on `consolidate_session` / `import_content` (so import pipelines and tests retain their per-call callbacks). Structural subtyping means you only need to implement the hooks you care about.

### 2. Add a new retrieve scorer

The rerank weights live as module constants in `retrieve.py`. Bumping a weight is a one-line patch, but a cleaner extension wraps the scorer so the default behaviour is untouched and your bias is opt-in.

```python
from datetime import datetime
from echovessel.memory import retrieve as m_retrieve
from echovessel.memory.retrieve import ScoredMemory, RetrievalResult


def retrieve_with_access_boost(
    db, backend, persona_id, user_id, query, embed_fn, *, top_k=10
) -> RetrievalResult:
    """Same as memory.retrieve.retrieve but boosts often-accessed nodes."""

    result = m_retrieve.retrieve(
        db,
        backend,
        persona_id,
        user_id,
        query,
        embed_fn,
        top_k=top_k * 2,            # over-fetch so our rerank has headroom
        min_relevance=0.4,          # keep the orthogonality floor in place
    )

    boosted: list[ScoredMemory] = []
    for sm in result.memories:
        # simple log-bonus on access_count; tune or replace freely
        import math
        bonus = 0.25 * math.log1p(sm.node.access_count)
        sm.total += bonus
        boosted.append(sm)

    boosted.sort(key=lambda s: -s.total)
    result.memories = boosted[:top_k]
    return result
```

The `min_relevance` filter runs before the rerank, so any custom weight you add only competes against candidates that already cleared the floor. If your scorer needs to promote low-relevance-but-high-impact memories (say, to resurface a trauma when the user mentions it obliquely), lower `min_relevance` at the call site instead of working around it in the scorer — the filter exists precisely to prevent tie-break tricks from leaking orthogonal peak events into the prompt.

### 3. Add a new L3 event extraction rule

`bulk_create_events` is the import-side write primitive for events. Use it to post-process a just-closed session with your own heuristic and insert an additional L3 row whenever the pattern fires. Remember: a bulk-written event without an embedding is invisible to vector retrieve, so the embed pass is mandatory, not optional.

```python
from echovessel.memory import (
    EventInput,
    bulk_create_events,
    ConsolidateResult,  # returned by consolidate_session
)
from echovessel.memory.models import RecallMessage
from sqlmodel import select


def detect_apology_and_write_event(
    db, backend, embed_fn, result: ConsolidateResult
) -> None:
    """If the user apologized in this session, add an extra L3 event."""

    session = result.session
    msgs = db.exec(
        select(RecallMessage).where(RecallMessage.session_id == session.id)
    ).all()

    apology_lines = [m for m in msgs if "sorry" in m.content.lower()]
    if not apology_lines:
        return

    inputs = [
        EventInput(
            persona_id=session.persona_id,
            user_id=session.user_id,
            description=f"User apologized: {apology_lines[0].content}",
            emotional_impact=-3,
            emotion_tags=("regret",),
            relational_tags=("vulnerability",),
            imported_from=f"rule:apology:{session.id}",
        )
    ]
    event_ids = bulk_create_events(db, events=inputs)

    # Mandatory embed pass — without this, the new event will never be
    # returned by retrieve()'s vector search.
    for eid, ev_input in zip(event_ids, inputs):
        backend.insert_vector(eid, embed_fn(ev_input.description))
```

`bulk_create_events` sets `imported_from` and leaves `source_session_id` `NULL` — the schema's CHECK constraint forbids both being set. Use a stable rule-specific prefix (here `rule:apology:`) as the `imported_from` value so that `count_events_by_imported_from` can answer "did we already run this rule for this session?" and make the rule idempotent.

The same pattern extends to L4: call `bulk_create_thoughts` with a `ThoughtInput` list and embed each thought before it can be retrieved. The soul-chain evidence links live in `concept_node_filling` and are written by the consolidate pass, not by the bulk primitives — if a custom rule produces a thought that references specific events, insert the filling rows yourself in the same transaction.

---

## See also

- [`configuration.md`](./configuration.md) — memory-related config fields and tunables
- [`runtime.md`](./runtime.md) — startup sequence, how memory is wired into the daemon
- [`channels.md`](./channels.md) — the debounce/turn layer that produces `turn_id` values memory stores
- [`import.md`](./import.md) — the offline import pipeline that writes into memory via `import_content`

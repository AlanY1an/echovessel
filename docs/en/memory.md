# Memory

> Hierarchical persona memory. L1 core blocks that always enter the prompt, L2 raw messages as ground truth, L3 extracted events, L4 distilled thoughts. One continuous identity across every channel the persona speaks on.

Memory is EchoVessel's core asset. Everything else — runtime, channels, voice, proactive — exists to feed it, query it, or surface what it remembers. A digital persona is only as persistent as the memory behind it, and this module is where that persistence lives.

---

## Overview

Memory gives a persona a durable sense of self and of the people it talks to. It is hierarchical because the four layers answer four different questions: "who am I right now" (L1), "what was literally said" (L2), "what happened in that conversation" (L3), and "what do I believe about this person across many conversations" (L4). Each layer has its own write path, its own retrieval role, and its own forgetting semantics. Collapsing them into a single store would either bloat the prompt or lose the ability to reflect.

The module is deliberately decoupled from every other layer. It does not know what a channel is, it does not know what an LLM provider is, it does not know how runtime streams responses. Higher layers inject embedding functions, extraction functions, and reflection functions as plain callables; memory handles storage, scoring, and lifecycle. This discipline is enforced by the layering contract in `pyproject.toml` — memory may depend on `echovessel.core` and nothing else.

The promise the rest of the system makes on top of memory is simple: one persona is one continuous identity. When the user talks to it on the web this afternoon and on Discord tomorrow night, memory is the same pool. Retrieval never filters by which channel a message arrived on. That single rule shapes most of the design decisions below.

---

## Core Concepts

**L1 core blocks** — short, stable pieces of text that are injected into every prompt unconditionally. Five labels live in `core_blocks`: `persona`, `self`, `mood`, `user`, `relationship`. The first three are shared across users (the persona is one character, its self-image and mood do not fork per user). The last two are per-user, keyed by `(persona_id, user_id)`. Each block is capped at 5000 characters and has an append-only audit log in `core_block_appends`.

**Biographic facts** — fifteen structured identity columns on the `personas` row itself (`full_name`, `gender`, `birth_date`, `nationality`, `native_language`, `timezone`, `occupation`, `relationship_status`, …). These live alongside the prose core blocks rather than inside them, so code that needs "what timezone is she in" or "what year was she born" can query a column instead of re-parsing the persona block. All fifteen are nullable — the LLM extraction fills what it can during onboarding and the user corrects the rest in the review page. Five of them (name, gender, birth year, occupation, native language) get rendered into the system prompt's "# Who you are" section every turn; the other ten are for system use only.

**L2 raw messages** — every user and persona message is written verbatim to `recall_messages`. This is the archival ground truth. The table stores `channel_id` alongside each row so a frontend can render a "via Web" or "via Discord" badge, but queries that feed the prompt never filter on it. L2 is indexed by FTS5 for keyword fallback but does not participate in the main retrieval pipeline; it is the layer the system can always fall back to when everything else fails.

**L3 events** — facts extracted from a closed session. Stored as `ConceptNode` rows with `type='event'`: a natural-language description, an `emotional_impact` in `-10..+10`, emotion and relational tags, an embedding in the sqlite-vec companion table, and a pointer back to the `source_session_id` they came from. Events are the main unit of episodic memory — "the conversation where the user told me Mochi had surgery".

**L4 thoughts** — longer-term observations distilled from many events. Same table as L3, differentiated by `type='thought'`. Each thought carries a `filling` chain (via `concept_node_filling`) that records which events it was generated from, so a user who deletes the source events can choose to keep the thought as orphaned. Thoughts are written by reflection passes, not by the extraction of a single session.

**Consolidate** — the pipeline that runs when a session closes. It reads the session's L2 messages in one batch, calls the injected extraction function to produce zero or more L3 events, embeds each event, optionally triggers a reflection pass that writes L4 thoughts, and marks the session `CLOSED`. The entry point is `consolidate_session` in `src/echovessel/memory/consolidate.py`.

**Retrieve** — the pipeline that runs before the persona speaks. It loads every L1 core block, asks the storage backend for a vector search over `concept_nodes`, reranks the candidates with a four-factor score, applies a minimum-relevance floor to suppress orthogonal matches, and optionally expands each hit with a few neighbouring L2 messages. If the vector index returns too few hits, an FTS fallback over L2 supplements the result. The entry point is `retrieve` in `src/echovessel/memory/retrieve.py`.

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

Every write commits before any hook fires. The lifecycle queue in `sessions.py` batches "new session" / "session closed" events so that a single commit can dispatch multiple hooks in one drain. Per-write hooks travel through an explicit `observer=` parameter on `ingest_message`, `bulk_create_events`, and `append_to_core_block`; lifecycle hooks travel through the module-level `_observers` registry populated once via `register_observer(...)`.

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

### Retry safety

Stage B commits the extracted L3 events **in the same transaction** as a new `extracted_events=True` flag on the session. If stage E (reflection) then raises — a transient LLM error, a timeout, even `SIGTERM` — the worker retries `consolidate_session` from the top. The top-of-function guard reads `extracted_events` and skips B entirely: already-persisted events are loaded from the database, fed into SHOCK/TIMER detection, and reflection runs against them. Extraction LLM calls are therefore run at most once per session, regardless of how many times reflection fails.

This invariant matters in both directions:

- `extracted=True` implies `extracted_events=True` (F only runs after B committed its flag).
- `extracted_events=True` does NOT imply `extracted=True` — that's the whole point of the resume state.

Sessions that die in state `extracted_events=True, status=CLOSING` are retried safely by the worker. Sessions that transition to `FAILED` (catch-all in `consolidate_worker._mark_failed`) are terminal and never retried automatically; admin intervention is required to reset them.

### Schema migration

`ensure_schema_up_to_date(engine)` is called before `create_all_tables(engine)` during daemon startup. It walks a hardcoded list of "add column if not exists" and "create table if not exists" steps, each guarded by `PRAGMA table_info` or a `sqlite_master` lookup. Every new column is either nullable or has a SQL default, so existing rows do not need backfilling. The migrator does not support renames, drops, or type changes — those are postponed to a later migration framework. Failure is fatal: a half-migrated schema fails fast at startup rather than silently corrupting writes later.

### Observer contract

Observers are fire-and-forget post-commit notifications. The protocol lives in `observers.py`:

```
MemoryEventObserver
  on_message_ingested(msg)        per-call, via observer= parameter
  on_event_created(event)         per-call, via observer= parameter
  on_thought_created(thought)     per-call, via observer= parameter
  on_core_block_appended(append)  per-call, via observer= parameter
  on_new_session_started(...)     lifecycle, via _observers registry
  on_session_closed(...)          lifecycle, via _observers registry
  on_mood_updated(...)            lifecycle, via _observers registry
```

All methods are plain `def` (not `async def`). Exceptions raised by a hook are caught at the memory boundary and logged via the module logger; the memory write that fired the hook has already committed by then and is never rolled back. A consumer that implements only some of the hooks relies on structural subtyping — `NullObserver` is provided as a no-op base for subclassing.

Lifecycle events flow through a small queue in `sessions.py`. The code path that mutates `session.status` enqueues a pending event and the committing caller drains the queue immediately after `db.commit()` returns. This lets a single commit dispatch several lifecycle hooks in one pass without each function needing to know whether a hook should fire.

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
register_observer(logger)  # lifecycle hooks auto-fire after register
# Per-write hooks (on_event_created, etc.) also work here when the caller
# passes observer=logger into consolidate_session / bulk_create_events.
```

The lifecycle hooks (`on_new_session_started`, `on_session_closed`, `on_mood_updated`) fire automatically once the observer is registered. The per-write hooks (`on_event_created`, `on_thought_created`, `on_message_ingested`, `on_core_block_appended`) fire only when the caller explicitly passes `observer=...` into the relevant write API. Structural subtyping means you only need to implement the hooks you care about.

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

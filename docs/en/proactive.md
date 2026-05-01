# Proactive

## Overview

Real relationships are not transactional. People reach out first — a friend remembers you said you had an interview on Monday and checks in Sunday night, a partner sends a quiet message the morning of an exam. A persona that only ever *responds* to user input feels lifeless: technically present, relationally absent. The **proactive** module is EchoVessel's answer to that gap. It decides when the persona should **speak first**, on its own initiative, without any user prompt — and crucially, it speaks about something the user actually mentioned, anchored to a future moment the user is moving toward.

That anchor is what separates EchoVessel proactive from the schedule-driven "digital companions" that pick a time slot and fire a templated "Good morning!" at it. Schedule-driven proactive feels mechanical because the persona has no reason to speak — it is just the clock striking. EchoVessel never speaks because of the clock. It speaks because something the user told it about (an interview, a surgery, a paper deadline, a mom's check-up) is approaching, currently happening, or just finished. **Memory is the source of truth for what the persona cares about; proactive is a reactive view that wakes when memory's clock for a particular event ticks.**

This shape is unusual enough to be worth naming explicitly. There is no proactive-side table tracking what to follow up on, no proactive-side LLM call detecting follow-up candidates, no proactive-side polling loop checking the calendar. The memory layer's Phase B extraction (the same LLM call that already produces L3 events) annotates every disclosure with `follow_up_at` / `follow_up_hint` / `advance_pre_hours` / `advance_post_hours`, and proactive subscribes to memory's `on_event_created` lifecycle hook. When an event with a `follow_up_at` lands, proactive arms an asyncio timer for the corresponding phase window. The timer fires, a 5-gate policy engine asks the only question proactive has ever asked — *given quiet hours, forbidden topics, in-flight turns, the rate limit, and the engagement score, should the persona say something right now?* — and either fires a message or writes a suppression record to the audit trail.

Every gate decision, **including every decision to stay quiet**, lands in `proactive_decisions`. The expensive LLM call that writes the actual outgoing message is the *last* thing proactive does, not the first, so the common case (gates fire, nothing to say) costs almost nothing. Operators can answer the only question users ever ask about a system like this: *why did it speak then?* — or, just as often, *why did it stay quiet?*

## Core Concepts

**Follow-up event.** A `concept_nodes` row (memory L3 event) with a non-null `follow_up_at` and `follow_up_hint`. Phase B extraction tags the row when the LLM judges the disclosure has a future arc the persona would naturally come back to: an interview next Monday, a surgery in three days, an ongoing paper. Plain past-tense disclosures ("I had a sandwich at noon") leave `follow_up_at` null; events that explicitly request quiet ("I'll handle this myself, don't ask") also leave it null.

**Phase window.** Mechanical computation by the FollowUpScheduler that turns one `follow_up_at` into up to three fire windows: `pre` (the lead-up — persona starts caring), `on` (the day-of), `post` (the check-back — persona asks how it went). Phases are derived per-event from `advance_pre_hours` and `advance_post_hours`; surgery gets a 72h pre and 0h post (let the user rest), an interview gets 24h pre and 24h post, a casual reminder gets 0h/0h (only `on`). Phases live nowhere on disk — they are recomputed every time, reading the live event row.

**Reminder request.** Phase B's special case for "remind me in 10 minutes" / "wake me at 9pm" — both `advance_pre_hours == 0` and `advance_post_hours == 0`. The scheduler fires exactly one `on` phase at `follow_up_at` and never fires `pre` or `post` (a reminder has no preparation arc and no follow-back).

**Policy gate.** A single check inside the policy engine that can cause a follow-up fire to be skipped. Five gates run in fixed priority order; the first that fires short-circuits the rest. Every skip produces a named `suppress_reason` in the audit trail — `quiet_hours`, `forbidden_topics`, `in_flight_turn`, `rate_limit`, `low_engagement`.

**Smart cooldown.** When a fire is suppressed, the next eligible retry depends on *why* it was suppressed. `forbidden_topics` permanently closes the event (sets `proactive_suppressed_at`); `quiet_hours` allows immediate retry as soon as the window ends (no artificial cooldown); `rate_limit` and other generic gates wait 4 hours so the rolling 24h budget naturally clears. The point is not to nag — a suppressed fire should retry on the same physical reason the gate cleared, not on an arbitrary timer.

**Supersede close.** When the user reports an outcome ("the interview's done, it went okay"), Phase B extracts a new event and populates `superseded_event_ids` pointing back at the original disclosure. The memory layer marks the old event's `superseded_by_id`. The FollowUpScheduler's `_eligible` check filters `superseded_by_id IS NULL`, so once the user has closed the loop, no further pre / on / post fire happens. **There is no separate close-detection LLM call** — the same memory mechanism that powers contradiction handling powers proactive close.

**Audit trail.** Every fire and every suppress writes a `proactive_decisions` row with `phase`, `action` (`fire` / `suppress`), `suppress_reason`, and a `gate_state_snapshot` JSON for forensic debugging ("which gate said no, with what state?"). Records also feed the policy engine's own reads — rate-limit counts the rows, smart cooldown reads the latest row per `(event_id, phase)`.

**`PersonaView`.** A live-reading adapter runtime injects into the scheduler. It exposes `voice_enabled` and `voice_id` as `@property` accessors that re-read from the current runtime context on every access. When an admin flips the voice toggle via the persona admin API, the *next* fire picks it up — no scheduler restart, no reload hook.

**Delivery inheritance.** Proactive never chooses voice vs text on its own. It reads `persona.voice_enabled` at send time and inherits the answer. When `voice_enabled == True` and a `voice_id` is configured, it calls `VoiceService.generate_voice()` to produce a playable artifact; otherwise it publishes pure text. Failure on the voice path always degrades to text — the audit trail records `voice_error` but the channel send always has a payload.

## Architecture

### Position in the 5-module stack

```
               Layer 4   runtime
                         │
                         ▼
               Layer 3   channels   proactive      ◄── this module
                            │          │
                            ▼          ▼
               Layer 2    memory     voice
                            │          │
                            ▼          ▼
               Layer 1              core
```

Proactive is a Layer 3 module alongside `channels`. Its import budget is small: it imports `memory` (read-only plus a single `ingest_message` write for recording the outgoing persona message), a duck-typed view of `voice.VoiceService`, the `channels.base` Protocol (never concrete channel implementations), and `core` types. It is never imported *by* memory or voice — the dependency arrow points strictly downward. Internal sub-package layering is `execution → engines → core`, locked by `import-linter`.

Runtime sits above proactive and constructs it at daemon startup, injecting every dependency proactive needs: a `MemoryApi` facade, a `ChannelRegistryApi`, the runtime-built `proactive_fn` LLM callable, a `PersonaView`, an optional `VoiceService`, and an `is_turn_in_flight` predicate that closes over runtime's channel registry. Runtime also registers `MemoryFollowUpObserver` against memory's lifecycle hooks so that every newly-extracted event with a `follow_up_at` reaches the scheduler.

### Layer 1 · PROFILE

`persona_profile` is the persona's relational policy surface. Three derived fields drive proactive directly:

- **`style_summary`** — a short prose anchor of how the persona talks, fed into the generator prompt so the outgoing message sounds like the persona rather than a generic LLM.
- **`forbidden_topics`** — a list of substrings the persona refuses to bring up first. Matched against the candidate event's `follow_up_hint` (not the full description — hints are the proactive-side anchor). A match permanently closes the event for proactive.
- **`quiet_hours`** — `[start_hour, end_hour]` local time, wrap-midnight aware. Independent from runtime's reactive reply path; a user message during quiet hours is still answered.

The profile is derived by `proactive/engines/profile_derivation.py` from L1 core blocks + recent L4 thoughts on a slow cadence, but reads from `persona_profile` are live: any admin-side edit through the admin API takes effect on the next fire.

### Layer 2 · DECISION · 5 gates

When the FollowUpScheduler's timer fires for a `(event_id, phase)`, it pushes a `FOLLOW_UP_DUE` event into the proactive scheduler's queue. The proactive scheduler drains the queue and calls `PolicyEngine.evaluate(events, ...)`, which walks a fixed priority ladder. The first gate that fires short-circuits the rest:

```
  ┌───────────────────────────────────────────────────────────┐
  │  1.  quiet_hours          time-of-day check               │
  │      fires  ─────────►    skip(quiet_hours)               │
  ├───────────────────────────────────────────────────────────┤
  │  2.  forbidden_topics     hint vs profile substring list  │
  │      fires  ─────────►    skip(forbidden_topics)          │
  ├───────────────────────────────────────────────────────────┤
  │  3.  in_flight_turn       don't interrupt a live turn     │
  │      fires  ─────────►    skip(in_flight_turn)            │
  ├───────────────────────────────────────────────────────────┤
  │  4.  rate_limit           ≤ 3 fires per rolling 24h       │
  │      fires  ─────────►    skip(rate_limit)                │
  ├───────────────────────────────────────────────────────────┤
  │  5.  engagement_score     BA contingent reward dampener   │
  │      fires  ─────────►    skip(low_engagement)            │
  │      passes ─────────►    action = fire                   │
  └───────────────────────────────────────────────────────────┘
```

Each gate sits at its position for a specific reason:

1. **Quiet hours** is cheapest and most absolute. Pure arithmetic on `now.hour`. If the user is asleep, nothing else matters.
2. **Forbidden topics** checks the candidate event's `follow_up_hint` against `persona_profile.forbidden_topics` (case-insensitive substring). A match here is treated as a *permanent* close: the smart-cooldown layer sets `proactive_suppressed_at` on the event so the scheduler will never re-arm a timer for it.
3. **In-flight turn** is the only semantic-safety gate. Runtime injects a predicate closure that scans its channel registry for any channel with a non-null `in_flight_turn_id`. If any channel is mid-turn, proactive defers — no legitimate scenario justifies interrupting a live turn.
4. **Rate limit** is a single rolling-24h cap (`max_per_24h`, default 3) read against `proactive_decisions`. Fine-grained minimum-interval throttles were deliberately cut: redundant with the daily cap, no UX gain.
5. **Engagement score** is a soft dampener from the BA contingent-reward loop in `proactive_state`. Every unanswered fire decays the score; a passing user reply rebuilds it. Below the pass threshold the gate fires `low_engagement` — the persona has been talking into silence and should back off. Bypassed for high-confidence reminder requests so a 10-minute "remind me to take my pills" cannot be silenced by a low score.

The expensive LLM call (`generator.generate(...)` writes the outgoing message) runs only after all five gates pass.

### Trigger · event-driven via `on_event_created` + asyncio timers

Proactive holds **no background time loop**. There is no per-second tick, no per-minute scanner, no consolidate-worker hook. The trigger surface is a single asyncio object — `FollowUpScheduler` — that turns memory lifecycle hooks into per-event wake-ups.

```
     memory.consolidate Phase B extracts ConceptNode
                      │
                      │  follow_up_at != None ?
                      ▼
            on_event_created(event)
                      │
                      ▼
       MemoryFollowUpObserver.on_event_created
                      │
                      ▼
          FollowUpScheduler.on_event_created
                      │
                      │  compute earliest phase wake-up
                      │  (pre / on / post / check_N)
                      ▼
       loop.call_later(delay, _fire_follow_up_due)
                      │
                      ▼  (when timer fires)
       FOLLOW_UP_DUE event → proactive scheduler queue
                      │
                      ▼
          5-gate evaluate → fire | suppress
                      │
                      ▼
       audit · then schedule next phase if any remain
```

Three things follow from this shape:

- **Cold start** is handled in `FollowUpScheduler.start()`: at daemon boot the scheduler runs one query for `concept_nodes WHERE follow_up_at IS NOT NULL AND superseded_by_id IS NULL AND deleted_at IS NULL AND proactive_suppressed_at IS NULL`, computes the next phase for each, and arms timers. The pending-reload happens once per process lifetime.
- **No polling** ever scans the database for due events. The scheduler's only reason to consult the database after start-up is the `_eligible` check inside the timer callback (re-verify supersede / suppress / soft-delete state has not changed since the timer was armed) and the `_smart_cooldown_passed` audit lookup.
- **One timer per `(event_id, phase)`**. After a fire (or a non-permanent suppress with cooldown), the scheduler arms the next phase's timer if any remain. The asyncio loop owns scheduling; the scheduler owns translating event state into delays.

### Phase windows

For events with `event_time_end` set (date-anchored — interviews, surgeries, exams):

```
  target = follow_up_at
  pre  : [target - advance_pre_hours,  target - 1h)
  on   : [target - 1h,                 target + 1h)
  post : [target + advance_post_hours, target + 2*advance_post_hours)
```

`advance_post_hours == 0` is a deliberate signal: the user should not be bothered after this event (post-surgery rest, post-trip "you're back, leave them alone"). The scheduler skips the `post` phase entirely. Reminder requests (`advance_pre_hours == 0 AND advance_post_hours == 0`) collapse to a single `on` fire at `target` — no `pre`, no `post`.

For events without `event_time_end` (ongoing arcs, unresolved emotional threads):

```
  follow_up_at acts as a check-back time
  first fire   = check_1   at follow_up_at
  subsequent   = check_2, check_3, …  (progressive numbering)
```

Progressive numbering exists so the generator prompt knows when to back off — `check_1` opens softly, `check_3` is the last gentle ask before the persona drops the topic.

### Resolution close · supersede chain

The mechanism that closes a follow-up loop has zero proactive-side code. When the user reports an outcome, Phase B extracts the report as a new `concept_nodes` row and includes the original disclosure's id in `superseded_event_ids`. Memory's consolidate path sets the old event's `superseded_by_id` to point at the new event. The FollowUpScheduler's `_eligible` check (run on every timer wake-up before sending `FOLLOW_UP_DUE`) re-reads the live event row and filters `superseded_by_id IS NULL`. A timer that wakes for an already-superseded event is a no-op — no fire, no audit row.

This is also how user-side "stop bothering me" works. The admin UI's `PATCH /api/admin/memory/events/{id}` setting `proactive_suppressed_at = now` is read by the same `_eligible` check; the next timer wake-up sees it and drops.

### Audit · `proactive_decisions`

Every fire and every suppress writes one row:

| Column | Meaning |
|--------|---------|
| `id` | UUID |
| `timestamp` | When the decision was evaluated |
| `persona_id` / `user_id` | Who the decision is for |
| `trigger_type` | `follow_up` (v3 has one fireable lane) |
| `source_event_id` | `concept_nodes.id` of the underlying event |
| `phase` | `pre` / `on` / `post` / `check_1` / `check_2` / … / NULL |
| `action` | `fire` or `suppress` |
| `suppress_reason` | `quiet_hours` / `forbidden_topics` / `in_flight_turn` / `rate_limit` / `low_engagement` / NULL when fired |
| `gate_state_snapshot` | JSON snapshot of every gate's input at evaluation time |
| `send_ok` / `send_error` | Channel send outcome (fire path only) |
| `ingest_message_id` | The L2 row id of the outgoing persona message |
| `delivery` | `text` / `voice_neutral` |
| `voice_used` / `voice_error` | Voice path outcome (fire path only) |
| `llm_latency_ms` | Generator LLM call duration |

The `gate_state_snapshot` field is what makes the system debuggable. When a user asks "why didn't she check in this morning?" the row carries every variable the gate ladder saw — quiet_hours active until 7am, in_flight_turn = true on the discord channel, rate_limit count = 4 in the last 24h. The audit row is the single source of truth for "what happened, why".

Two-phase write: the skeleton row (timestamp / trigger / phase / action / reason) is committed before the LLM call, so a crash mid-send still leaves evidence. After the channel send completes, `update_latest` patches the outcome fields (`send_ok`, `ingest_message_id`, `delivery`, `voice_used`, `voice_error`, `llm_latency_ms`) onto the same row.

### Send flow · ingest-before-send invariant

When the policy returns `action = fire`:

```
       generator.generate(decision)                  prompt assembly · LLM call
              │
              ▼
       delivery.pick_channel(...)                    user's recent channel, else 'web'
              │
              ▼
       memory.ingest_message(PERSONA, text)          ◄── ingest BEFORE send
              │                                         (gives us message_id)
              ▼
       delivery.prepare_voice(                       voice if enabled, else text
           text, message_id,
           persona.voice_enabled,
           persona.voice_id,
       )
              │
              ▼
       channel.send(text)                            may fail; memory already has a record
              │
              ▼
       audit.update_latest(                          two-phase write completes
           send_ok, send_error,
           ingest_message_id, delivery,
           voice_used, voice_error,
           llm_latency_ms,
       )
```

The invariant is: **`memory.ingest_message` runs before `channel.send`, and before `VoiceService.generate_voice` is invoked.** Two reasons.

First, if the channel send fails — network drop, transport error, remote rejection — the persona's memory still has a record of what it said. The internal state stays consistent with itself even when the external world fails. The alternative (send first, ingest on success) produces personas whose memories silently diverge from what they actually emitted.

Second, the voice cache is keyed on `message_id`: the L2 row id returned from `ingest_message`. Voice generation must happen *after* ingest because otherwise there is no stable id to cache the audio artifact against. This also gives voice its idempotency property — re-rendering the same `message_id` hits the on-disk cache instead of re-billing the TTS provider.

### Generator prompt · `follow_up_hint` + phase guidance

The outgoing message is written by one LLM call. Inputs:

- `persona_profile.style_summary` — voice
- The full `ConceptNode` row of the event being followed up on — context
- `phase` — `pre` / `on` / `post` / `check_N`
- `PHASE_GUIDANCE[phase]` — short directive on how to open

`PHASE_GUIDANCE` lives in `proactive/engines/generator.py` as a flat dict keyed by phase. The `pre` directive tells the model to ask about preparation without assuming an outcome; `on` directives tell it to be brief and warm; `post` directives ask after the result while avoiding pre-judging good or bad; `check_N` directives back off progressively. The generator never invents anchors — it uses `event.follow_up_hint` (the 5-15 char anchor Phase B produced like "interview result" or "mom's check-up") as the topic the message is about.

### Delivery inheritance

The scheduler reads `persona.voice_enabled` and `persona.voice_id` live, right before calling `prepare_voice`. If an admin toggled voice off between fires, the next fire sees the new value on the very next property access. `DeliveryRouter.prepare_voice` then decides delivery:

| Condition                                  | Delivery        |
|--------------------------------------------|-----------------|
| `persona.voice_enabled == False`           | `text`          |
| `voice_service is None`                    | `text`          |
| `persona.voice_id is None` or empty        | `text`          |
| `generate_voice(...)` raises any error     | `text` (downgrade; `voice_error` recorded) |
| `generate_voice(...)` returns successfully | `voice_neutral` |

`prepare_voice` never raises. Every voice-path failure — transient provider outage, permanent misconfiguration, budget exhaustion, unexpected exception — resolves to a text fallback so the channel send always has a payload. The failure is captured in `voice_error`.

## Admin UI

`/admin/proactive` exposes the proactive surface to operators:

- **Active events** — `GET /api/admin/proactive/events` returns every `concept_nodes` row with a non-null `follow_up_at` that is not superseded, suppressed, or soft-deleted. The frontend groups them by phase window (`pre` / `on` / `post` / `check_N`) so the operator can see "what is the persona about to bring up, and when".
- **Decision history** — `GET /api/admin/proactive/decisions` returns recent `proactive_decisions` rows with `phase`, `action`, `suppress_reason`, and the outgoing message text when `action = fire`. The history view is the answer to "did she fire? why or why not?".
- **Suppress** — `PATCH /api/admin/memory/events/{id}` with `{proactive_suppressed_at: now}` permanently closes a single event for proactive. The scheduler stops arming timers for it; existing armed timers no-op when they wake. The event itself stays in memory — it is still retrievable for the reactive reply path.

## Configuration

Everything lives in the `[proactive]` and `[memory]` TOML sections, parsed into Pydantic models at daemon startup.

```toml
[proactive]
enabled                          = true   # master on/off switch
max_per_24h                      = 3      # rate-limit cap (rolling 24h)

# Quiet hours (local time, 24h clock; wraps midnight when start > end)
quiet_hours_start                = 23
quiet_hours_end                  = 7

# Engagement gate
engagement_pass_threshold        = 0.4    # below this, low_engagement fires

# Smart cooldown (used after a non-permanent suppress)
default_cooldown_hours           = 4

# Shutdown
stop_grace_seconds               = 10     # wait for in-flight fire on stop

[memory]
session_idle_minutes             = 10     # was 30 in earlier versions; lowered so
                                          # short reminder requests fire within 10 min
                                          # of session close
```

Two operational notes:

- **Config is read once, at scheduler construction.** Proactive does not watch the TOML file and does not respond to SIGHUP. To apply new values, restart the daemon. Live-reloading a policy engine while it is mid-fire is strictly more complex than the value it buys.
- **`session_idle_minutes`** is a memory-side knob but it directly bounds proactive's lower latency for short reminders. A reminder request like "remind me in 8 minutes" cannot fire until the session closes (Phase B has not extracted the event yet); with `session_idle_minutes = 10`, that fire will be at most 10 minutes late from the user's intended time. Lowering further is allowed but trades off against reflection cost (every closed session may run extraction).

## Known limitations

The v3 architecture treats reminder requests as a special case of memory disclosures (the LLM tags `advance_pre_hours = 0` and `advance_post_hours = 0` during Phase B). This composition is honest about what it cannot do well today:

1. **Reminders shorter than `session_idle_minutes`.** A user who says "remind me in 3 minutes" has not given the session enough time to close before the target moment. Phase B has not run, the event does not exist yet, the scheduler has nothing to arm. The fire happens once the session closes — at most `session_idle_minutes` late, but always late for very short windows.
2. **Mid-conversation reminders.** A user who keeps typing every few minutes ("remind me in 10 minutes, anyway, also …") never lets the session go idle. The session does not close, Phase B does not run, the reminder waits. It fires once the user finally goes idle for `session_idle_minutes`, which can be much later than intended.
3. **Reminders straddling the idle boundary.** "Wake me at 9pm" said at 8:55pm typically works (session closes at 9:05pm idle, scheduler arms a timer for 9pm and immediately fires) but the edge case where the user is still typing past 9pm misses.

These are accepted trade-offs for v3, not bugs. The proper fix is a `set_reminder` tool that the LLM calls inside the turn, writing `concept_nodes(follow_up_at)` directly without waiting for session close. That requires a tool-execution architecture (LLM provider abstraction, tool result feedback loop, cross-channel tool dispatch) that is a project-level change in its own right — deferred to v4.

## Design rationale · why memory carries follow-up state

There is a more obvious shape this module could have taken: a separate `follow_up_threads` table owned by proactive, populated by a proactive-side LLM call that walks recent events and decides which ones are worth following up on. The shape would have a parallel close-detection LLM call that watches for outcome reports and closes threads. That shape is structurally appealing — proactive is in charge of its own state, memory does not know about follow-ups.

The reason v3 collapsed that shape into memory's Phase B is not aesthetic but operational. Phase B already runs an LLM call over the session messages to extract events, including emotional impact, relational tags, and `superseded_event_ids` for contradiction handling. A second LLM call to detect follow-ups would be reading the same input messages and emitting a strict subset of the same judgements (is this a future arc? what's the anchor?). The two calls duplicate work; worse, they can disagree, leaving proactive with a follow-up thread for an event memory has marked superseded, or vice versa.

The collapse keeps memory's invariant: **memory is the single source of truth for what happened and what is expected next.** Proactive is a reactive view that schedules timers off memory's annotations and decides — through five gates — whether a particular fire should leave the building. That alignment with the project's "memory is the shared substrate" principle is what makes the architecture live up to the local-first bias: one LLM call per session close, one supersede chain for both contradictions and follow-up close, one row per event.

## How to extend

### 1. Add a custom suppress reason

Suppress reasons are string constants in `proactive/core/base.py` and are written verbatim into `proactive_decisions.suppress_reason`. Adding a new gate is a four-step change:

1. Add the constant alongside the existing reasons.
2. Insert the gate at the appropriate position in `PolicyEngine.evaluate()`. Position matters — cheaper / more absolute gates first, soft gates last.
3. Decide the smart-cooldown semantics in `FollowUpScheduler._smart_cooldown_passed`: permanent close (set `proactive_suppressed_at`), instant retry, or generic 4h cooldown.
4. Add to the admin UI's decision-history filter so operators can see the new reason in context.

### 2. Hook a custom audit sink

The default sink writes to `proactive_decisions`. The `AuditSink` Protocol from `proactive/core/base.py`:

```python
class AuditSink(Protocol):
    def record(self, decision: ProactiveDecision) -> None: ...
    def update_latest(self, decision_id: str, **outcome_fields) -> None: ...
    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]: ...
    def count_sends_in_last_24h(self, *, now: datetime) -> int: ...
```

A custom sink can tee to JSONL, push to Prometheus, or stream to an external observability platform. Two rules: `record()` must never raise (the scheduler tick has no recovery path for an exploding sink), and `recent_sends` / `count_sends_in_last_24h` are the read side of the policy engine — stub them as `[]` / `0` and you have effectively disabled rate-limit and engagement-history.

### 3. Tune phase windows for a new event class

`advance_pre_hours` and `advance_post_hours` are produced by Phase B, not by proactive. Tuning the window for a class of events (say, "every birthday should have 6h pre and 0h post") is a memory-side prompt change in `prompts/extraction.py` PART F. The proactive scheduler will pick up the new windows on the very next event Phase B writes. There is no proactive-side calibration knob — and that is the point.

For a complete reference, the authoritative source is `src/echovessel/proactive/` — every file has detailed docstrings, and the policy engine's gate order is locked in by the unit tests under `tests/proactive/`. Memory's PART F prompt and the six `concept_nodes` columns it populates are documented in [`memory.md`](./memory.md).

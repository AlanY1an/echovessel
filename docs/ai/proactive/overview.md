# proactive · overview

The persona's autonomous initiative layer. Decides when to speak first,
what to say, and which channel carries the message. Runs as a passive
event-driven consumer of memory writes — no polling, no time loop.

memory is the single source of truth for what's worth following up on.
Phase B's extraction prompt (PART F) annotates each `ConceptNode` with
`follow_up_at` + `follow_up_hint` + `advance_pre/post_hours`. Proactive
subscribes to `on_event_created` and creates an asyncio timer per
event; the timer wakes at the computed phase window, re-validates
eligibility, and dispatches a `FOLLOW_UP_DUE` event into the policy
engine.

---

## What it owns

- **Event-driven scheduling.** `FollowUpScheduler` (asyncio timer per
  event) — see `proactive/execution/follow_up_scheduler.py`.
- **5-gate policy.** quiet_hours / forbidden_topics / in_flight_turn /
  rate_limit / engagement_score evaluated per fire candidate.
- **Generation.** `MessageGenerator` calls the injected `proactive_fn`
  (LLM closure) with a `MemorySnapshot`; checks the F10 channel-leak
  guard before output reaches a channel.
- **Delivery.** `DeliveryRouter` picks an enabled channel from the
  runtime registry, ingests the persona message into L2, and emits
  through `Channel.send()`.
- **Audit.** Every decision (fire OR suppress) lands as one
  `ProactiveDecision` row in `proactive_decisions`. The two-phase write
  pattern (record → update_outcome) carries send_ok / voice_used etc.
- **BA contingent reward loop.** `engagement_updater.py` adjusts
  `proactive_state.engagement_score` after fire / silence / user reply.
- **Layer 1 PROFILE.** `PersonaProfile` (style_summary + quiet_hours +
  forbidden_topics) generated once at onboarding by LARGE-tier LLM.

## What it does NOT own

- **Detection.** Phase B (memory) decides which events have a follow-up
  arc and writes `follow_up_at` + companion fields. Proactive only
  reads.
- **Resolution close.** When the user reports an outcome, Phase B marks
  `superseded_by_id` on the original event. The scheduler's
  `_eligible` check skips superseded events — no separate close-detection
  LLM call.
- **LLM access.** `proactive_fn` is injected by runtime; proactive must
  not import `runtime` or `prompts`.
- **Channel lifecycle.** Channels are runtime-owned. `ChannelRegistryApi`
  is the proactive-side view; `list_enabled()` returns the current set.
- **Persona profile generation prompts.** `runtime/wiring/proactive_profile.py`
  builds the LLM closure; the profile derivation logic lives in
  `proactive/engines/profile_derivation.py` and is invoked by runtime.

---

## Public API entry points

All importable as `from echovessel.proactive import …` per
`src/echovessel/proactive/__init__.py`.

| Symbol | Source file | Purpose |
|---|---|---|
| `build_proactive_scheduler` | `proactive/factory.py` | Wire one `DefaultScheduler` from injected dependencies (memory api, channel registry, proactive_fn, audit_sink, persona view, voice service) |
| `ProactiveScheduler` | `proactive/core/base.py` | Protocol the runtime holds (`start` / `stop` / `notify`) |
| `DefaultScheduler` | `proactive/execution/scheduler.py` | The concrete impl returned by the factory |
| `ProactiveConfig` | `proactive/core/config.py` | Validated `[proactive]` TOML section (`max_per_24h`, `tick_interval_seconds`, etc.) |
| `ProactiveEvent` | `proactive/core/base.py` | Frozen dataclass enqueued via `notify()` |
| `EventType` | `proactive/core/base.py` | StrEnum: `FOLLOW_UP_DUE` (production), plus legacy `THREAD_DUE` / `EVENT_EXTRACTED` / `SESSION_CLOSED` / `TURN_COMPLETED` |
| `ProactiveDecision` (value-type) | `proactive/core/base.py` | In-memory audit row; the SQLModel-table version with the same name is in `memory/models.py` |
| `MemoryApi` / `ChannelRegistryApi` / `VoiceServiceProtocol` / `PersonaView` / `AuditSink` / `ProactiveFn` | `proactive/core/base.py` | Injection-only Protocols — proactive depends on shapes, runtime supplies impls |
| `SQLiteAuditSink` | `proactive/execution/audit.py` | Production audit sink writing `proactive_decisions` rows |
| `PolicyEngine` / `MessageGenerator` / `DeliveryRouter` | `proactive/engines/*` + `proactive/execution/delivery.py` | The three components wired together by `DefaultScheduler` |
| `FollowUpScheduler` | `proactive/execution/follow_up_scheduler.py` | Event-driven timer manager · subscribed via `MemoryFollowUpObserver` |
| `MemoryFollowUpObserver` | `proactive/execution/observer.py` | Bridges memory's `on_event_created` to `FollowUpScheduler` |
| `PersonaProfile` / `ProactiveState` | `proactive/core/models.py` | Two SQLModel tables proactive owns |
| `ProactiveDecision` (table) | `memory/models.py` | Audit row table — anchored in memory because both proactive (writer) and channels (admin reader) need it |
| `derive_profile_from_core_blocks` | `proactive/engines/profile_derivation.py` | Onboarding-time LARGE-tier LLM call building the PersonaProfile row |

---

## Invariants — do not break

1. **D4 铁律 still holds inside proactive.** No method on `MemoryApi`
   accepts `channel_id` for a read. The `ingest_message` write does
   take `channel_id` as delivery metadata only. Verified by
   `tests/proactive/test_d4_no_channel_filter.py`.
2. **F10 channel-leak guard.** `MessageGenerator._assert_no_channel_leak`
   rejects any `MemorySnapshot` containing a string with `channel_id`,
   `discord:`, `imessage:`, `wechat:`, or `web:`. Stops ID strings from
   ever reaching the LLM prompt. Verified by
   `tests/proactive/test_f10_no_channel_in_prompt.py`.
3. **No background time loop in proactive.** The only callable that
   sleeps is `FollowUpScheduler._wake_and_attempt`, scheduled per
   event. There is no periodic tick.
4. **Timer self-validation on wake.** Before dispatching, the timer
   re-loads the event and runs `_eligible`: skips if the event was
   deleted, superseded, user-suppressed, or aged past
   `3 × estimated_arc_days`.
5. **Resolution close via supersede chain.** When Phase B writes a new
   event with `superseded_event_ids`, the original event's
   `superseded_by_id` becomes non-NULL and `_eligible` returns False.
   No proactive-side close detection LLM call exists.
6. **Smart cooldown per gate.** `forbidden_topic` permanently closes the
   event (sets `proactive_suppressed_at`); `quiet_hours` retries
   immediately when the window exits; `rate_limit` and other suppress
   reasons retry after `DEFAULT_COOLDOWN_HOURS = 4`.
7. **Attempt cap = 5.** Once 5 audit rows exist for a `(event_id, phase)`
   pair the scheduler stops trying that phase.
8. **Reminder semantics.** When `advance_pre_hours == 0` and
   `advance_post_hours == 0`, the candidate phase list is `["on"]`
   only — no pre, no post. Used for explicit "10 minutes remind me"
   flows.
9. **`ProactiveDecision` table lives in `memory/models.py`.** The same
   name is used for the in-memory value-type in
   `proactive/core/base.py`. Same name, different layer — the value-type
   is what `PolicyEngine.evaluate` returns; the SQLModel row is what
   `SQLiteAuditSink.record` writes.
10. **`audit_sink` is required, no default.** `build_proactive_scheduler`
    raises if `audit_sink` is `None`. Production wires
    `SQLiteAuditSink`; tests wire stubs.

---

## Layer model

### Layer 1 · PROFILE

`PersonaProfile` row (one per persona). Fields:

| Field | Purpose |
|---|---|
| `style_summary` | Free-text summary of how the persona speaks · injected verbatim into generation prompts |
| `quiet_hours` | `[start_hour, end_hour]` — gate 1 reads this |
| `forbidden_topics` | List of strings; gate 2 substring-matches against `follow_up_hint` |
| `voice_id` | Cloned voice id; runtime's `PersonaView.voice_id` reads this |
| `profile_source` | `llm_onboarding` / `user_edited` / `fallback_default` |

### Layer 2 · DECISION

Per-fire candidate runs through 5 gates (in `proactive/engines/policy.py`):

1. `quiet_hours` — current hour falls outside `[start, end)`?
2. `forbidden_topics` — `follow_up_hint` contains a forbidden substring?
3. `in_flight_turn` — runtime predicate says a turn is in flight?
4. `rate_limit` — fewer than `max_per_24h` (default 3) sends in last 24h?
5. `engagement_score` — `proactive_state.engagement_score >= threshold`
   (with high-confidence bypass for `relational_tags ∋ {commitment,
   vulnerability}` or `|emotional_impact| >= 7`).

The first failing gate decides the suppress reason; remaining gates are
not evaluated.

---

## Reading order for new contributors to this system

1. `src/echovessel/proactive/__init__.py` — public API surface
2. `src/echovessel/proactive/core/base.py` — Protocols + dataclasses + enums
3. `src/echovessel/proactive/factory.py` — how the components get wired
4. `src/echovessel/proactive/execution/follow_up_scheduler.py` — the
   event-driven timer model
5. `src/echovessel/proactive/execution/observer.py` — how `on_event_created`
   reaches the scheduler
6. `src/echovessel/proactive/engines/policy.py` — the 5-gate evaluator
7. `src/echovessel/proactive/execution/scheduler.py` — `tick_once` /
   `_dispatch_event` / `_send_with_voice_inheritance`
8. `src/echovessel/runtime/wiring/follow_up.py` +
   `src/echovessel/runtime/wiring/proactive_profile.py` — how runtime
   composes the system at startup

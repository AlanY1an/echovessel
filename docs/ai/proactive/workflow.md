# proactive · workflow

Guideposts (not runbooks) for common proactive-side changes. Each
section lists touch points and the order to think about them. Always
re-confirm against the actual code — function signatures shift faster
than this file.

---

## Add a new gate to the policy engine

**When:** there's a new condition under which proactive should suppress
(e.g. "do not fire when persona's mood is below threshold").

**Touch points:**

1. `proactive/core/base.py::SkipReason` — add the new reason as a
   StrEnum value (e.g. `LOW_MOOD = "low_mood"`).
2. `proactive/core/base.py::TriggerReason` — add the matching
   gate-reason variant if the gate's first argument needs reporting
   (e.g. `MOOD_GATE = "mood_gate"`).
3. `proactive/engines/policy.py::PolicyEngine.evaluate` — insert the
   new gate at the right ordering position. Order matters: cheap +
   data-only gates run early, gates that read disk run late.
4. `proactive/execution/follow_up_scheduler.py::_compute_when` — decide
   the smart-cooldown branch for this new suppress reason. If the
   condition is permanent (like `forbidden_topic`), set
   `proactive_suppressed_at` and return None. If transient, pick a
   sensible cooldown.
5. `tests/proactive/test_policy_v2.py` — add a fixture that exercises
   the new gate's pass / fail / cooldown paths.

**Pitfalls:**

- A gate that reads from memory must use `MemoryApi`, not direct DB
  access; the lint-imports contract enforces this.
- The first failing gate sets `skip_reason`; remaining gates are not
  evaluated. If the gate's data is expensive to fetch, run it after
  `quiet_hours` / `forbidden_topics` / `in_flight_turn` so cheap rejects
  short-circuit.

---

## Add a new phase (e.g. "check_in_morning")

**When:** the persona should fire on a new schedule beyond pre/on/post
or check_N (e.g. a "30-day check-in" anniversary phase).

**Touch points:**

1. `proactive/execution/follow_up_scheduler.py::_candidate_phases` —
   teach it which event shape produces the new phase (likely a new
   relational_tag or a new column on `concept_nodes` driving the
   branch).
2. `proactive/execution/follow_up_scheduler.py::_compute_when` — supply
   the base time formula for the new phase.
3. `proactive/engines/generator.py::PHASE_GUIDANCE` — add a phase →
   guidance string entry. The generation prompt reads this to hint at
   the framing the LLM should use.
4. `tests/proactive/test_follow_up_scheduler.py` — exercise the new
   phase end-to-end (event → timer → notify → audit row with the new
   phase string).

**Pitfalls:**

- The phase string is a free-form key — `proactive_decisions.phase` is
  TEXT, not an enum. Don't break existing audit rows by repurposing an
  old phase string.
- `_has_fired` and `_attempt_cap_reached` query by the literal phase
  string. If you typo "check_in_moring" anywhere, the queries will
  silently match nothing and you'll get duplicate fires.

---

## Add a new audit field

**When:** new observability data needs to land alongside each decision
(e.g. `llm_input_tokens` distinct from `prompt_tokens`).

**Touch points:**

1. `memory/models.py::ProactiveDecision` (the SQLModel table) — add the
   column.
2. `memory/migrations.py::ensure_schema_up_to_date` — append a v0.X
   `_ColumnSpec` and splice into the dispatch loop. SQLModel's
   `metadata.create_all` adds the column on fresh DBs; the migration
   path adds it on legacy DBs.
3. `proactive/core/base.py::ProactiveDecision` (the value-type) — add
   the matching field with a None default.
4. `proactive/core/base.py::ProactiveDecision.update_outcome` — accept
   the new field as a kwarg and write it through.
5. `proactive/execution/audit.py::SQLiteAuditSink._to_row` — read the
   value-type field and map onto the SQLModel column.
6. `proactive/execution/audit.py::SQLiteAuditSink._from_row` — round-trip
   back to the value-type for `recent_sends` queries.
7. `proactive/execution/scheduler.py` — wherever `update_outcome` is
   called, pass the new value.
8. `tests/proactive/test_audit_sqlite.py` — extend the round-trip
   coverage.

**Pitfalls:**

- The two `ProactiveDecision` classes share the field name space
  semantically but not literally — keep them in lock-step or the
  round-trip breaks.

---

## Wire a new memory hook into proactive

**When:** memory grows a new lifecycle hook and proactive needs to
react (e.g. `on_entity_confirmed` for L5 promotion → fire a "I learned
who X is" message).

**Touch points:**

1. `memory/observers.py` — confirm the hook exists. If not, that's a
   memory-side workflow first (see `docs/ai/memory/workflow.md`).
2. `proactive/execution/observer.py::MemoryFollowUpObserver` — add a
   method matching the Protocol's signature. Currently the observer is
   no-op for everything except `on_event_created`.
3. Decide: does the hook trigger immediate timer scheduling
   (`FollowUpScheduler.on_event_created` style) or feed a different
   downstream? The observer is thin — it should only forward to a
   purpose-built handler.
4. `tests/proactive/test_memory_follow_up_observer.py` — add coverage
   for the new hook path AND the structural-subtyping check
   (`isinstance(obs, MemoryEventObserver)` must still hold).

**Pitfalls:**

- The Protocol is `@runtime_checkable` — adding a method to it without
  also adding a no-op to `MemoryFollowUpObserver` breaks
  `isinstance()` checks throughout the codebase.

---

## Change a `ProactiveConfig` field

**When:** a new `[proactive]` TOML knob (e.g. `max_per_24h` adjusted to
6, or new `engagement_threshold`).

**Touch points:**

1. `proactive/core/config.py::ProactiveConfig` — add / modify the
   Pydantic field. Validators go here.
2. `runtime/config.py::ProactiveSection` — mirror the field on the
   runtime-side TOML model and pass it through to `ProactiveConfig`.
3. `resources/config.toml.sample` — document the new key with a
   one-line comment about the trade-off.
4. `proactive/engines/policy.py` — read the field at the gate that
   uses it.
5. `tests/runtime/test_config.py` — add a roundtrip test (TOML →
   `ProactiveConfig` → expected default).

**Pitfalls:**

- `ProactiveConfig` is validated at runtime startup — an invalid value
  raises `ProactivePermanentError` and aborts daemon boot. That's
  intentional (better than a silent bad value).

---

## Switch the proactive_fn to a different LLM provider

**When:** Anthropic → OpenAI / local model change.

**Touch points:**

1. `runtime/wiring/prompts.py::make_proactive_fn` — this is where the
   LLM closure is built. Swap the call to the provider's `complete`
   method.
2. `runtime/llm/anthropic.py` (or wherever the provider lives) — the
   provider abstraction handles the actual request.
3. Nothing in `proactive/` should change. `ProactiveFn` is a Callable
   alias; the closure shape is invariant under provider swap.

**Pitfalls:**

- LLM tier (LARGE) is baked into `make_proactive_fn` — proactive does
  NOT pass a tier argument. If you want a different tier per-call,
  redesign at the wiring layer.

---

## Add a follow-up trigger that is NOT memory-driven

**When:** something other than `on_event_created` should produce a
proactive event (e.g. external calendar import, scheduled cron).

**Touch points:**

1. New caller — anywhere in the daemon that wants to trigger a
   proactive event. It should call
   `proactive_scheduler.notify(ProactiveEvent(event_type=..., ...))`.
2. `proactive/core/base.py::EventType` — if the new trigger needs a
   distinct event type, add it.
3. `proactive/engines/policy.py::PolicyEngine._pick_fireable` — teach
   it how to recognize the new event type and which `TriggerReason` it
   maps to.
4. `proactive/engines/generator.py::build_message_prompt` — if the new
   trigger needs a different `phase` semantics or guidance, extend
   `PHASE_GUIDANCE`.

**Pitfalls:**

- The whole point of v0.7 is "memory is single source of truth." If
  the new trigger writes its own state instead of going through
  memory, you're recreating v2's parallel-state problem. Strongly
  prefer: new caller writes a `ConceptNode` (with `follow_up_at`) into
  memory, then `on_event_created` fires naturally.

---

## Investigate "she didn't fire when I expected"

**When:** dogfood feedback says proactive missed a moment.

**Touch points (in order):**

1. `proactive_decisions` audit table — was there a row for this event?
   Filter by `source_event_id` or recent timestamps.
   - No row at all → the event never landed in the timer (check
     `concept_nodes.follow_up_at` was set; check `MemoryFollowUpObserver`
     was registered).
   - Row with `action='skip'` and `skip_reason=...` → which gate
     blocked it. The `skip_reason` enum is in
     `proactive/core/base.py::SkipReason`.
   - Row with `action='send'` and `send_ok=False` → delivery failed;
     `send_error` carries the message.
2. `concept_nodes` for the event — verify all v0.7 fields populated
   correctly. If `follow_up_at` is None, Phase B's PART F prompt didn't
   recognize the event as worth following up — that's a memory-side
   bug, not proactive.
3. `proactive_state.engagement_score` — if low, the engagement gate
   may be silently rejecting low-confidence events.
4. Check `superseded_by_id` and `proactive_suppressed_at` on the event.
   If non-NULL, the event was disqualified.

**Pitfalls:**

- The audit log is the SOURCE OF TRUTH for "did proactive try to fire."
  In-memory state (timers, the queue) doesn't survive restart and is
  not authoritative.
- Stale events (older than `3 × estimated_arc_days`) silently fall
  through `_eligible`. They don't get a "stale" audit row — they just
  stop being scheduled. If you want a stale audit trail, that's a
  different feature.

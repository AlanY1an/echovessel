# proactive · references

Pointers into the codebase. Always re-check — paths and line numbers
shift.

---

## Source files

```
src/echovessel/proactive/
├── __init__.py                 public API surface (PROactiveScheduler,
│                                build_proactive_scheduler, value types,
│                                Protocols, errors)
├── factory.py                  build_proactive_scheduler — single wiring entry
│
├── core/
│   ├── __init__.py
│   ├── base.py                 EventType / TriggerReason / SkipReason / ActionType,
│   │                            ProactiveEvent, ProactiveDecision (value-type),
│   │                            MemorySnapshot, ProactiveMessage, ProactiveFn,
│   │                            MemoryApi, ChannelProtocol, ChannelRegistryApi,
│   │                            VoiceServiceProtocol, PersonaView, AuditSink,
│   │                            ProactiveScheduler
│   ├── config.py               ProactiveConfig — Pydantic for [proactive] TOML
│   ├── errors.py               ProactiveError + Transient + Permanent
│   └── models.py               PersonaProfile + ProactiveState (proactive's own tables)
│
├── engines/
│   ├── __init__.py
│   ├── policy.py               PolicyEngine.evaluate (5-gate ordered eval),
│   │                            SHOCK_IMPACT constant
│   ├── generator.py            MessageGenerator, F10Violation,
│   │                            _assert_no_channel_leak, PHASE_GUIDANCE,
│   │                            build_message_prompt, GenerationOutcome
│   └── profile_derivation.py   onboarding-time PersonaProfile build
│                                (LARGE-tier LLM call)
│
└── execution/
    ├── __init__.py
    ├── scheduler.py            DefaultScheduler — tick loop / queue drain /
    │                            _dispatch_event / _send_with_voice_inheritance
    ├── queue.py                ProactiveEventQueue — DEFAULT_MAX_EVENTS overflow
    ├── delivery.py             DeliveryRouter, VoiceTransientError /
    │                            VoicePermanentError / VoiceBudgetError,
    │                            channel pick + voice fallback
    ├── audit.py                SQLiteAuditSink — record / update_latest /
    │                            recent_sends / count_sends_in_last_24h
    ├── engagement_updater.py   BA contingent reward · update_after_fire /
    │                            update_after_silence_window /
    │                            update_after_user_turn
    ├── follow_up_scheduler.py  FollowUpScheduler — event-driven asyncio timers
    │                            (start, stop, on_event_created,
    │                            _wake_and_attempt, _compute_next_attempt,
    │                            _candidate_phases, _compute_when, _eligible)
    └── observer.py             MemoryFollowUpObserver — bridges memory's
                                 on_event_created → FollowUpScheduler
```

`scripts/migrate_proactive_jsonl.py` — one-shot legacy v0.6 JSONL audit
log → SQLite migration utility.

---

## Tables proactive reads / writes

```
src/echovessel/memory/models.py
├── ConceptNode                 v0.7 columns:
│                                  follow_up_at, follow_up_hint,
│                                  estimated_arc_days, advance_pre_hours,
│                                  advance_post_hours, proactive_suppressed_at;
│                                superseded_by_id chain handles resolution close
└── ProactiveDecision           audit row (table); the value-type with the
                                 same name is in proactive/core/base.py

src/echovessel/proactive/core/models.py
├── PersonaProfile              Layer 1 — style_summary + quiet_hours +
│                                forbidden_topics + voice_id
└── ProactiveState              engagement_score per (persona_id, user_id)
```

Indexes that proactive depends on:

| Index | Table | Purpose |
|---|---|---|
| `idx_concept_follow_up_active` | `concept_nodes` | partial index `WHERE follow_up_at IS NOT NULL AND deleted_at IS NULL AND superseded_by_id IS NULL AND proactive_suppressed_at IS NULL` — `FollowUpScheduler.start()` scan |
| `idx_decisions_phase_event` | `proactive_decisions` | `_has_fired` / `_attempt_cap_reached` / `_last_decision` queries |
| `idx_decisions_persona_user_time` | `proactive_decisions` | `count_sends_in_last_24h` rate-limit gate |

---

## Runtime wiring

```
src/echovessel/runtime/wiring/
├── proactive.py                wraps memory + channel registry + persona view +
│                                LLM closure into the right Protocols and calls
│                                build_proactive_scheduler (if used directly)
├── proactive_profile.py        builds the LARGE-tier LLM closure for
│                                derive_profile_from_core_blocks; admin route
│                                /api/admin/persona/profile/regenerate uses it
└── follow_up.py                make_follow_up_scheduler — returns
                                 (FollowUpScheduler, MemoryFollowUpObserver) pair;
                                 register_follow_up / unregister_follow_up
                                 wrap memory's _observers list

src/echovessel/runtime/app.py
├── _build_proactive_scheduler  build_proactive_scheduler call site at startup
└── _start_proactive +
    _start_follow_up            ordered start: proactive scheduler first, then
                                 FollowUpScheduler (re-init from DB)
                                Order on stop: unregister observer first, then
                                 stop FollowUpScheduler, then proactive
                                 scheduler (drain queue with grace timeout)
```

---

## Tests

```
tests/proactive/
├── __init__.py
├── fakes.py                              shared Protocol stubs:
│                                          FakeMemoryApi / FakeChannel /
│                                          FakeVoiceService / FakePersonaView /
│                                          FakeAuditSink
│
├── test_base.py                          enum stability + Protocol shape
├── test_d4_no_channel_filter.py          guards MemoryApi has no channel_id read kwarg
├── test_f10_no_channel_in_prompt.py      F10 channel-leak guard
├── test_factory.py                       build_proactive_scheduler wiring
│
├── test_queue.py                         ProactiveEventQueue overflow rule
├── test_scheduler.py                     DefaultScheduler tick / drain / dispatch
├── test_round2_delivery_inheritance.py   voice path + fallback to text
├── test_round2_removed_min_interval.py   v0.2 min_interval removal regression
│
├── test_policy_v2.py                     5-gate evaluator (parametric)
├── test_engagement_updater.py            BA contingent reward paths
├── test_audit_sqlite.py                  ProactiveDecision round-trip + phase
│                                          column + sentinel datetime mapping
│
├── test_generator.py                     MessageGenerator + F10 + LLM stub
├── test_generator_v2.py                  PHASE_GUIDANCE coverage,
│                                          build_message_prompt phase routing,
│                                          unknown-phase fallback
├── test_delivery.py                      DeliveryRouter channel pick + voice
│                                          inheritance + error fallback
├── test_profile_derivation.py            LARGE-tier LLM closure happy path /
│                                          fallback to default profile
│
├── test_follow_up_scheduler.py           event-driven dispatch · self-validation
│                                          on wake · start() rehydrate
├── test_memory_follow_up_observer.py     observer bridges on_event_created;
│                                          all 9 lifecycle hooks satisfied
│                                          (structural Protocol check)
├── test_proactive_models_v3.py           PersonaProfile + ProactiveState +
│                                          ProactiveDecision (table) shape
├── test_round_v3_e2e.py                  happy path: user mentions exam → fire
│                                          pre/on → user reports outcome →
│                                          supersede → no post fire (parametric
│                                          across pre/on/post phases)
└── test_migrate_proactive_jsonl.py       legacy JSONL → SQLite migration tool

tests/channels/web/test_admin_proactive_routes.py
                                           /api/admin/proactive/events,
                                           /api/admin/proactive/decisions,
                                           DELETE
                                           /api/admin/memory/events/{id}/proactive-follow-up

tests/memory/test_phase_b_follow_up_extraction.py
                                           Phase B PART F field roundtrip; 6
                                           parametric scenarios (interview /
                                           medical / surgery / reminder /
                                           ongoing / trivial)
tests/memory/test_concept_node_v0_7_columns.py
                                           v0.7 column shape on ConceptNode
tests/memory/test_v0_7_follow_up_migration.py
                                           legacy DB upgrade path for v0.7

tests/runtime/test_follow_up_wiring.py     make_follow_up_scheduler smoke
tests/runtime/test_session_idle_config.py  SESSION_IDLE_MINUTES default 10 +
                                            override via [memory] config
```

---

## Schema invariants enforced at the SQLite level

| Constraint | Table | Effect |
|---|---|---|
| `idx_concept_follow_up_active` | `concept_nodes` | partial index — make scheduler's pending-events scan O(active) |
| `proactive_state.PRIMARY KEY (persona_id, user_id)` | `proactive_state` | one engagement signal per pair |
| `persona_profile.PRIMARY KEY (persona_id)` | `persona_profile` | one profile per persona |
| `proactive_decisions` `phase` column | `proactive_decisions` | TEXT, NULL allowed for legacy / non-FOLLOW_UP_DUE rows |
| `concept_nodes.advance_pre_hours / advance_post_hours` | `concept_nodes` | INTEGER, NULL allowed (defaults to 24h pre/post when NULL) |

Application-level invariants (not DB-enforced):

- `event.advance_pre_hours == 0 AND advance_post_hours == 0` ⇒ reminder
  semantics (only `on` phase). Enforced in `_candidate_phases`.
- `event.estimated_arc_days IS NOT NULL` triggers stale aging at
  `created_at + 3 × estimated_arc_days`. Enforced in `_eligible`.
- `proactive_decisions.action ∈ {'fire', 'skip'}` matches
  `ActionType.SEND` / `ActionType.SKIP`. Set by policy/dispatch; no
  CHECK constraint.

---

## Human docs (for context, not for facts)

These are narrative / rationale and may lag behind code:

- `docs/en/proactive.md` · `docs/zh/proactive.md`
- `docs/en/memory.md` v0.7 columns section
- `develop-docs/initiatives/_archive/2026-04-memory-driven-proactive/` —
  the v3 plan + spec + stage trackers (gitignored on most branches)

The single source of truth is code (and the audit log). When the docs
disagree, fix the docs.

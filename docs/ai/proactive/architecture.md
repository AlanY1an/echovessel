# proactive · architecture

How the `proactive/` package is wired together and what each file owns.

---

## Module map

```
proactive/
├── __init__.py                 public API re-exports
├── factory.py                  build_proactive_scheduler — single wiring entry
│
├── core/
│   ├── __init__.py
│   ├── base.py                 Protocols + dataclasses + enums (no I/O)
│   ├── config.py               ProactiveConfig — validates [proactive] TOML
│   ├── errors.py               ProactiveError / TransientError / PermanentError
│   └── models.py               SQLModel · PersonaProfile + ProactiveState
│
├── engines/
│   ├── __init__.py
│   ├── policy.py               5-gate evaluator · PolicyEngine + SHOCK_IMPACT
│   ├── generator.py            MessageGenerator + F10 guard + PHASE_GUIDANCE +
│   │                            build_message_prompt
│   └── profile_derivation.py   onboarding-time PersonaProfile build
│
└── execution/
    ├── __init__.py
    ├── scheduler.py            DefaultScheduler · the queue-drain loop
    ├── queue.py                ProactiveEventQueue · MAX_EVENTS overflow rule
    ├── delivery.py             DeliveryRouter · channel pick + voice inherit
    ├── audit.py                SQLiteAuditSink · two-phase ProactiveDecision write
    ├── engagement_updater.py   BA contingent reward loop · adjusts ProactiveState
    ├── follow_up_scheduler.py  event-driven timer manager · per-event asyncio.Task
    └── observer.py             MemoryFollowUpObserver · bridges memory hook
```

`core/` is pure types — no async, no I/O, no imports from runtime/prompts.
`engines/` is decision logic. `execution/` is dispatch + persistence.
Sub-package layering enforced by lint-imports: `execution → engines → core`.

The audit row's SQLModel table (`ProactiveDecision`) lives in
`memory/models.py`, NOT `proactive/core/models.py`. Two siblings need
to read it (proactive writes, channels admin reads), and channels↔proactive
cannot cross-import; anchoring the row one layer below in memory lets both
sides reach it.

---

## Data flow — three pipelines

### 1. DETECTION · Phase B writes follow-up annotations

This pipeline lives in **memory**, not proactive. Listed here because
proactive depends on it.

```
session closes (idle / explicit / lifecycle)
  └→ runtime.loops.consolidate_worker._process_one
      └→ memory.consolidate_session(...)
          └→ Phase B · extract_fn(messages)
              ├ LLM produces ExtractedEvent[] including v0.7 fields:
              │    follow_up_at, follow_up_hint, estimated_arc_days,
              │    advance_pre_hours, advance_post_hours
              ├ INSERT concept_nodes — fields above land in row
              ├ supersedes (when LLM emits superseded_event_ids):
              │    UPDATE old_node.superseded_by_id = new_node.id
              └ memory.observers._fire_lifecycle("on_event_created", node)
```

Proactive's only entry into this pipeline is the `on_event_created`
observer hook fired post-commit on the new node.

### 2. SCHEDULING · event lands → timer scheduled

```
memory.observers.on_event_created(event: ConceptNode)
  └→ MemoryFollowUpObserver.on_event_created(event)
      └→ FollowUpScheduler.on_event_created(event)
          ├ if event.persona_id / user_id mismatch: return
          ├ if event.follow_up_at is None: return
          ├ if not _eligible(event): return  (deleted / superseded /
          │                                   suppressed / stale)
          └→ _schedule_next(event)
              ├ _compute_next_attempt(event):
              │   ├ for phase in _candidate_phases(event, db, now):
              │   │     skip if _has_fired or _attempt_cap_reached
              │   │     last = _last_decision(event_id, phase)
              │   │     when = _compute_when(event, phase, last, now)
              │   │     return (phase, when)
              │   └ no phase available → return None
              └ asyncio.create_task(_wake_and_attempt(event_id, phase, delay))
```

### 3. DISPATCH · timer wakes → policy → fire OR suppress

```
asyncio.sleep(delay) returns
  ↓
FollowUpScheduler._wake_and_attempt:
  ├ db.get(ConceptNode, event_id) + _eligible re-check
  │      (catches supersede/suppress/delete that happened during sleep)
  ├ proactive_scheduler.notify(ProactiveEvent(
  │     event_type=FOLLOW_UP_DUE,
  │     payload={event_id, phase, follow_up_hint, decision_id}))
  ↓
DefaultScheduler.tick (or notify-driven inline drain):
  └ _dispatch_event(event):
      ├ PolicyEngine.evaluate(event, db) → ProactiveDecision
      │      (5-gate evaluation; first failing gate sets skip_reason)
      ├ if decision.action == 'skip':
      │     audit_sink.record(decision)
      │     return
      └ MessageGenerator.generate(snapshot) → ProactiveMessage
            ├ _assert_no_channel_leak(snapshot)  · F10 guard
            ├ proactive_fn(snapshot) → text + rationale
            └ DeliveryRouter.deliver(persona_view, message, decision):
                  ├ pick channel from channel_registry.list_enabled()
                  ├ voice path (when persona.voice_enabled and voice_service):
                  │     voice_service.generate_voice(...) → audio cache
                  │     fallback to text on Voice* errors
                  ├ memory_api.ingest_message(persona, channel_id, ...) → row
                  ├ channel.send(text)
                  └ audit_sink.update_latest(decision.decision_id, send_ok=...)
  ↓
back in _wake_and_attempt:
  └ _schedule_next(event)  · next phase (or retry on suppress)
```

### Smart cooldown · phase retry decision

When a previous attempt was suppressed, `_compute_when` re-bases the
next attempt on the suppress reason:

| Last suppress reason | Next attempt | Reason |
|---|---|---|
| `forbidden_topic` | None — set `proactive_suppressed_at` and stop | Permanent ban; user / profile said no |
| `quiet_hours` | `max(base, now)` — retry immediately when window exits | The gate is time-bound; retry the moment we're out |
| `rate_limit` | `max(base, last.timestamp + 4h)` | 24h rolling reset eventually; 4h is a budget-safe retry |
| anything else (`in_flight_turn`, `low_engagement`, etc.) | `max(base, last.timestamp + 4h)` | Default cooldown |
| no prior attempt OR last was `fire` | `max(base, now)` — fresh first-attempt | First try or new phase after a fire |

`max(base, now)` ensures we never schedule into the past.

---

## Runtime integration · how proactive gets wired in

```
runtime.app.Runtime.start()
  ├→ build_proactive_scheduler(config, memory_api, channel_registry,
  │                            proactive_fn, audit_sink, persona, ...)
  │      → DefaultScheduler instance
  ├→ proactive_scheduler.start()  · spawns the dispatch task
  ├→ make_follow_up_scheduler(db_factory, proactive_scheduler,
  │                           persona_id, user_id)
  │      → (FollowUpScheduler, MemoryFollowUpObserver)
  ├→ follow_up_scheduler.start()  · re-schedules pending events from DB
  └→ register_follow_up(observer)  · adds observer to memory's
                                     module-level _observers list

Runtime.stop():
  ├→ unregister_follow_up(observer)  · stop new dispatches first
  ├→ follow_up_scheduler.stop()      · cancel all in-flight timers
  └→ proactive_scheduler.stop()      · drain queue with grace timeout
```

Wiring lives in `runtime/wiring/follow_up.py` (event-driven side) and
`runtime/wiring/proactive_profile.py` (PersonaProfile derivation closure).

---

## Persistence model · proactive's tables

| Table | Owner module | Purpose |
|---|---|---|
| `concept_nodes` | memory | `follow_up_at`, `follow_up_hint`, `estimated_arc_days`, `advance_pre_hours`, `advance_post_hours`, `proactive_suppressed_at` columns are read by FollowUpScheduler; `superseded_by_id` chain handles resolution close |
| `proactive_decisions` | memory (table) / proactive (writer) | Audit row · phase + action + suppress_reason + send outcome. `idx_decisions_phase_event` supports the dispatch-side `_has_fired` / `_attempt_cap_reached` queries |
| `proactive_state` | proactive | One row per (persona_id, user_id) · `engagement_score` float |
| `persona_profile` | proactive | One row per persona · style_summary + quiet_hours + forbidden_topics + voice_id |

The `concept_nodes.idx_concept_follow_up_active` partial index
(`WHERE follow_up_at IS NOT NULL AND deleted_at IS NULL AND
superseded_by_id IS NULL AND proactive_suppressed_at IS NULL`) makes
`FollowUpScheduler.start()`'s pending-events scan O(active follow-ups)
not O(all concept_nodes).

---

## Resume invariants

`FollowUpScheduler.start()` is the resume point on daemon restart. It
scans `concept_nodes` for `follow_up_at IS NOT NULL` events that haven't
been disqualified, then re-creates one timer per event.

Stale timers from before restart are GONE (asyncio tasks don't survive
process death). The `_has_fired` check on the next attempt prevents
re-firing a phase that already landed before the crash.

The `proactive_decisions` audit log is the source of truth for "what
already happened"; everything else is derivable from it. If you need to
debug "did the persona fire on this event yesterday?", read the
audit table — don't infer from in-memory state.

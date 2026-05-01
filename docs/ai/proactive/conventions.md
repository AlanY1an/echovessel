# proactive · conventions

Patterns specific to the proactive subsystem. Repo-wide rules live in
`docs/ai/conventions.md`.

---

## Layer rule · proactive imports memory + voice + channels(Protocol) + core

Enforced by `lint-imports` (4 contracts in `pyproject.toml`):

```
runtime → channels | proactive → memory | voice → core
```

Plus three sub-package contracts:

1. `proactive` MUST NOT import `runtime` or `prompts`.
2. `proactive` sub-packages: `execution → engines → core`. No reverse.
3. `runtime.wiring` MUST NOT import `runtime.turn` or `runtime.loops`.

Concretely for proactive:

- `proactive/core/*` is pure data — no I/O, no async, no DB.
- `proactive/engines/*` is decision logic — may import `core` and read
  through `MemoryApi`, but never calls a channel or writes a row directly.
- `proactive/execution/*` is dispatch / persistence — may import
  `engines` + `core` + memory's SQLModel rows and observer registry.
- `runtime/wiring/follow_up.py` and `runtime/wiring/proactive_profile.py`
  are the only files that compose proactive components into a running
  system; they may import everything proactive but stay away from
  `runtime.turn` and `runtime.loops`.

If you need a runtime-side capability inside proactive, accept it as
an injected callable (`ProactiveFn`, `is_turn_in_flight`, `db_factory`).
Don't add a `from echovessel.runtime` import — that's a redesign signal.

---

## Injected Protocols · proactive doesn't own connection lifecycle

Every external dependency proactive uses is a Protocol it depends on
shape-wise, never on import. Concrete impls are runtime's job.

| Protocol | Defined in | Concrete impl lives in |
|---|---|---|
| `MemoryApi` | `proactive/core/base.py` | `runtime/wiring/memory.py::MemoryFacade` |
| `ChannelRegistryApi` | same | runtime owns channel registry |
| `ChannelProtocol` | same | each channel adapter (Web / Discord / iMessage / WeChat) satisfies it duck-style |
| `VoiceServiceProtocol` | same | `voice/voice_service.py::VoiceService` |
| `PersonaView` | same | `runtime/wiring/persona.py` builds a property-driven view re-reading `RuntimeContext.persona` per access |
| `AuditSink` | same | `proactive/execution/audit.py::SQLiteAuditSink` (production) or test stubs |
| `ProactiveFn` | same (Callable alias) | `runtime/wiring/prompts.py::make_proactive_fn(llm_provider)` builds the LLM closure |

**`MemoryApi` has zero `channel_id` read params.** D4 铁律 baked into
the Protocol — adding one is a breaking change that
`tests/proactive/test_d4_no_channel_filter.py` catches.

`ingest_message` IS allowed `channel_id` because it's delivery metadata
(which pipe did the message leave through), not a memory filter.

---

## F10 channel-leak guard · ID strings never enter the LLM prompt

Owned by `MessageGenerator._assert_no_channel_leak` in
`proactive/engines/generator.py`. The check runs over the
`MemorySnapshot` before `proactive_fn(snapshot)` is invoked.

Forbidden substrings (any one fails the snapshot):

```python
FORBIDDEN_CHANNEL_TOKENS = ("channel_id", "discord:", "imessage:",
                            "wechat:", "web:")
```

If the snapshot ever carries a string containing one of these, raise
`F10Violation`. `tests/proactive/test_f10_no_channel_in_prompt.py`
guards this; if you find yourself wanting to "just" pass channel info
into the prompt, redesign — there's a different way.

---

## Two-phase audit write

Spec §7.3 pattern. Every fire goes through two writes:

1. `audit_sink.record(decision)` — synchronous, lands the
   `ProactiveDecision` row before the send begins. If the daemon
   crashes between record and send, the row will show `send_ok=None`
   and `ingest_message_id=None` — that's how we tell "we tried but
   never got there."
2. `audit_sink.update_latest(decision_id, send_ok=..., ...)` — fills in
   send outcome AFTER `channel.send()` returns. `update_latest` only
   touches non-None kwargs; it's safe to call partially.

Suppress decisions only do step 1 — there's nothing to send.

---

## Voice inheritance · proactive never decides on its own

Spec §6.2a + Stage 2 review Check 3. Delivery type comes from the
`PersonaView`, not from any proactive logic:

```python
delivery = "voice_neutral" if persona.voice_enabled else "text"
```

If `voice_enabled=True` but `voice_id is None` or `voice_service is None`
or `voice_service.generate_voice` raises, fall back to text and stamp
`voice_error` on the decision. Audit row keeps `voice_used=False`.

`PersonaView.voice_enabled` re-reads from `RuntimeContext.persona` per
property access — admin toggles apply on the next tick without needing
a reload hook.

---

## Smart cooldown per gate · phases retry differently

`FollowUpScheduler._compute_when` reads the last `ProactiveDecision`
row for the same `(event_id, phase)` pair and chooses the retry base:

- `forbidden_topic` → set `event.proactive_suppressed_at = now`, return
  None (no further attempts).
- `quiet_hours` → `max(base, now)` (immediate retry as soon as the
  daemon polls back; gate re-evaluates fresh).
- `rate_limit` → `max(base, last.timestamp + 4h)`.
- Any other suppress reason → `max(base, last.timestamp + 4h)`
  (`DEFAULT_COOLDOWN_HOURS`).
- `last.action == 'fire'` → fresh first-attempt for the next phase
  (`max(base, now)`).

The base is the natural phase window: `target - advance_pre_hours` for
pre, `target - 1h` for on (1h tolerance window), `target +
advance_post_hours` for post. Reminder events (`advance_pre=0,
advance_post=0`) skip pre and post entirely; `_candidate_phases`
returns `["on"]` only.

---

## Phase computation · derived from event fields, not stored

Phase windows are not in any table — they are derived per-tick from
`ConceptNode.advance_pre_hours` / `advance_post_hours` /
`event_time_end`:

| Event shape | Candidate phases |
|---|---|
| `advance_pre=0 AND advance_post=0` | `["on"]` (reminder) |
| `event_time_end IS NOT NULL` AND `advance_post > 0` | `["pre", "on", "post"]` |
| `event_time_end IS NOT NULL` AND `advance_post = 0` | `["pre", "on"]` (e.g. surgery — no post-bother) |
| `event_time_end IS NULL` (ongoing/unresolved) | `[f"check_{N}"]` where N = `count(prior decisions for event) + 1` |

The `_has_fired` check on each phase uses the audit log to gate "this
phase already landed once, skip it" — that's the only reason fire
happens at most once per phase.

---

## ProactiveDecision · two distinct types, same name

Two classes are named `ProactiveDecision`. Don't conflate them.

| Class | Where | Shape | Purpose |
|---|---|---|---|
| `ProactiveDecision` (value-type) | `proactive/core/base.py` | `@dataclass(slots=True)` with `update_outcome(...)` mutator | What `PolicyEngine.evaluate` returns and `DefaultScheduler` carries through dispatch |
| `ProactiveDecision` (table) | `memory/models.py` | SQLModel `table=True` | What `SQLiteAuditSink` writes to `proactive_decisions` |

The audit sink converts the value-type to the table row at `record()`
time. The table is anchored in `memory/` because both proactive (writer)
and channels (admin reader) need it; siblings can't cross-import.

---

## No backcompat shims · pre-1.0 semantics

When `EventType.HIGH_EMOTIONAL_EVENT` was removed in v0.7, every
producer + consumer was updated in the same commit; no alias was left
behind. `TriggerReason.FOLLOW_UP` is the v0.7 canonical name (string
value `"follow_up"`); historical audit rows with
`trigger="high_emotional_event"` round-trip through the
`ProactiveTriggerType` Literal but no new code emits that string.

If you find yourself wanting to leave a deprecated alias, the
appropriate fix is to update all call sites in the same commit. The
0-grep gate from Stage 4 is the pattern to follow.

---

## Tests live where source lives

`tests/proactive/` mirrors `src/echovessel/proactive/`. Pytest is
`asyncio_mode = "auto"` — `async def test_*` runs without decorators.
The `tests/proactive/fakes.py` module holds shared stubs for
`MemoryApi`, `ChannelProtocol`, `VoiceServiceProtocol`, `PersonaView`,
`AuditSink`. New tests should consume those over rolling new ones,
unless the test specifically exercises a Protocol shape edge case.

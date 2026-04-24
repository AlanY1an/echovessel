# Proactive

## Overview

Real relationships are not transactional. People reach out first — a friend remembers something you said last week and checks in, a partner sends a quiet message when they notice you have been quiet for too long. A persona that only ever *responds* to user input feels lifeless: technically present, relationally absent. The **proactive** module is EchoVessel's answer to that gap. It decides when the persona should **speak first**, on its own initiative, without any user prompt.

Why isn't proactive just a daily schedule? Schedule-driven "digital companions" pick a time slot, fire a templated "Good morning!" message, and feel instantly robotic. They cannot react to *why* the user might need attention right now, and they cannot stay quiet when that is the right thing to do. EchoVessel's proactive module is therefore **event-driven plus periodic tick**: the memory subsystem pushes lifecycle events (a high-emotional-impact event was extracted, a conversation session closed, a relationship state changed) onto a bounded queue, and a background tick loop — default every 60 seconds — wakes up, drains the queue, and asks a policy engine a single question: *given what just happened, should the persona say something, and if so, what?*

Every decision, **including every decision to stay quiet**, is evaluated through a stack of policy gates whose only job is to protect the user from being interrupted, nagged, or woken up. The gates run first; message generation happens only if every gate passes. This ordering matters: the expensive LLM call that writes the outgoing message is the *last* thing proactive does, not the first, so the common case (gates fire, nothing to say) costs almost nothing. Every gate decision is written to an audit trail so operators can answer the only question users ever ask about this kind of subsystem: *why did it speak then?* — or, just as often, *why did it stay quiet?*

## Core Concepts

**Policy gate.** A single check inside the policy engine that can cause a proactive send to be skipped. Gates run in a fixed priority order. A gate that fires short-circuits the engine: later gates are not consulted, and no message is generated. Every gate produces a named `SkipReason` that lands in the audit trail — for example `quiet_hours`, `rate_limited`, `in_flight_turn`, `low_presence_mode`.

**Relationship trigger.** The positive counterpart to a gate. Triggers answer the question "what would make the persona want to speak?" The MVP ships two: `HIGH_EMOTIONAL_EVENT` (a memory event with absolute emotional impact at or above the shock threshold) and `LONG_SILENCE` (the user's last message is older than the configured `long_silence_hours`). Both are evaluated **after** every gate has passed, so a trigger never overrides a gate — quiet hours always win.

**Cold user.** A protection mode for new users (or for users who have stopped responding). If the persona sends N consecutive proactive messages and none of them get a reply within the response window, proactive enters a cold-user skip state and stops reaching out until the user speaks first. This prevents the worst failure mode of a proactive subsystem: a persona that keeps talking to someone who has walked away.

**In-flight turn.** Runtime is "in-flight" when it has accepted a user message on some channel and is currently generating the persona's reactive reply. If proactive were allowed to speak during that window, the visible output would reorder to `[user question] → [proactive interrupt] → [real reply]` — a race-condition-grade UX bug. The `no_in_flight_turn` gate forbids this. It has no config knob: there is no legitimate scenario for "let proactive interrupt a live turn."

**Audit trail.** Every call to `PolicyEngine.evaluate()` produces exactly one `ProactiveDecision` record, whether the outcome was `send` or `skip`. Records go to `~/.echovessel/logs/proactive-YYYY-MM-DD.jsonl` by default. When the scheduler sends, it uses a two-phase write: skeleton row first (so a crash mid-send still leaves evidence), outcome fields (`send_ok`, `ingest_message_id`, `delivery`, `voice_used`, `voice_error`, `llm_latency_ms`) patched in after the send completes.

**`PersonaView`.** A live-reading adapter runtime injects into the scheduler. It exposes `voice_enabled` and `voice_id` as `@property` accessors that re-read from the current runtime context on every access. This means when an admin flips the voice toggle via the persona admin API, the *next* tick picks it up — no scheduler restart, no reload hook. Proactive is the reader, runtime is the writer; the adapter keeps them decoupled.

**Delivery inheritance.** Proactive never chooses voice vs text on its own. It reads `persona.voice_enabled` at send time and inherits the answer. When `voice_enabled == True` and a `voice_id` is configured, it calls `VoiceService.generate_voice()` to produce a playable artifact; otherwise it publishes pure text. This is the single source of truth for delivery — there is no separate proactive-side switch.

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

Proactive is a Layer 3 module alongside `channels`. Its import budget is deliberately small: it imports `memory` (read-only plus a single `ingest_message` write for recording the outgoing persona message), a duck-typed view of `voice.VoiceService`, the `channels.base` Protocol (never concrete channel implementations), and `core` types. It is never imported *by* memory or voice — the dependency arrow points strictly downward.

Runtime sits above proactive and constructs it at daemon startup via `build_proactive_scheduler(...)`, injecting every dependency proactive needs: a `MemoryApi` facade, a `ChannelRegistryApi`, the runtime-built `proactive_fn` LLM callable, a `PersonaView`, an optional `VoiceService`, and an `is_turn_in_flight` predicate that closes over runtime's channel registry.

### The policy gate order

When the tick loop wakes up, drains the queue, and calls `PolicyEngine.evaluate(events, ...)`, the engine walks a fixed priority ladder. The first gate that fires short-circuits the rest:

```
  ┌─────────────────────────────────────────────────────┐
  │  1.  quiet hours        time-of-day check           │
  │      fires  ─────────►  skip(quiet_hours)           │
  ├─────────────────────────────────────────────────────┤
  │  2.  cold user          new-user ramp-up protection │
  │      fires  ─────────►  skip(low_presence_mode)     │
  ├─────────────────────────────────────────────────────┤
  │  3.  rate limit         max per rolling 24h         │
  │      fires  ─────────►  skip(rate_limited)          │
  ├─────────────────────────────────────────────────────┤
  │  4.  no in-flight turn  don't interrupt a live turn │
  │      fires  ─────────►  skip(in_flight_turn)        │
  ├─────────────────────────────────────────────────────┤
  │  5.  trigger match      any registered trigger?     │
  │      none  ──────────►  skip(no_trigger_match)      │
  │      match ──────────►  action = send               │
  └─────────────────────────────────────────────────────┘
```

Each gate has a specific reason for sitting where it sits:

1. **Quiet hours** is cheapest and most absolute. It is pure arithmetic on `now.hour`. If the user is asleep, nothing else matters.
2. **Cold user** is a read against the audit trail — the engine asks "did my last N proactive sends get a reply within the response window?" If every one of them was ignored, the user is cold and proactive backs off.
3. **Rate limit** is a coarse "how many sends in the last 24 hours?" read, also against the audit trail. The MVP uses a single daily cap (`max_per_24h`, default 3). Fine-grained minimum-interval throttles were deliberately cut: they were redundant with the daily cap and added configuration surface with no UX gain.
4. **No in-flight turn** is the only semantic-safety gate. Runtime injects a predicate closure that scans its channel registry for any channel with a non-`None` `in_flight_turn_id`. If any channel is mid-turn, proactive defers. When no predicate is injected (older runtimes, unit tests), the gate is permissive — it never blocks, matching the spec's "no channel readable means no in-flight turn" rule.
5. **Trigger match** is the last step. It walks the drained event batch looking for a `HIGH_EMOTIONAL_EVENT` match first, then `LONG_SILENCE`. If nothing matches, the decision is `skip(no_trigger_match)` — a completely normal outcome that just means "nothing worth speaking about right now."

### The tick loop

```
┌─────────────────────────────────────────────────────────────┐
│  asyncio background task: proactive-scheduler               │
└─────────────────────────────────────────────────────────────┘
        │
        │   every tick_interval_seconds (default 60)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  tick_once()                                                │
│    1. self-enqueue heartbeat TICK event                     │
│    2. drain queue                                           │
│    3. policy.evaluate(events) → ProactiveDecision           │
│    4. audit.record(decision)          ◄── always, even skip │
│    5. if action == send: _handle_send_action(...)           │
└─────────────────────────────────────────────────────────────┘
```

The loop is a single asyncio task. Memory's observer callbacks and runtime's turn-completed hooks all feed the queue via `scheduler.notify(event)`, which is non-blocking and safe from any async or sync caller. Overflow is handled by the queue itself: when `max_events_in_queue` is reached, the oldest non-critical event is dropped, an internal counter increments, and the next tick emits a meta-`ProactiveDecision` with `trigger = queue_overflow` so operators can see drops in the audit file.

### The send flow and the ingest-before-send invariant

When policy returns `action = send`, the scheduler takes over:

```
       generator.generate(decision)                  build snapshot, call LLM
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

The invariant is: **`memory.ingest_message` runs before `channel.send`, and before `VoiceService.generate_voice` is invoked.** Two reasons for that ordering.

First, if the channel send fails — network drop, transport error, remote rejection — the persona's memory still has a record of what it said. The internal state stays consistent with itself even when the external world fails. The alternative (send first, ingest on success) produces personas whose memories silently diverge from what they actually emitted, which is much worse than an inconsistency between memory and the outbound wire.

Second, the voice cache is keyed on `message_id`: the L2 row id returned from `ingest_message`. Voice generation must happen *after* ingest because otherwise there is no stable id to cache the audio artifact against. This also gives voice its idempotency property — re-rendering the same `message_id` hits the on-disk cache instead of re-billing the TTS provider.

### Delivery inheritance

The scheduler reads `persona.voice_enabled` and `persona.voice_id` live, right before calling `prepare_voice`. If an admin toggled voice off between tick N and tick N+1, tick N+1 sees the new value on the very next property access. `DeliveryRouter.prepare_voice` then decides the delivery:

| Condition                                  | Delivery        |
|--------------------------------------------|-----------------|
| `persona.voice_enabled == False`           | `text`          |
| `voice_service is None`                    | `text`          |
| `persona.voice_id is None` or empty        | `text`          |
| `generate_voice(...)` raises any error     | `text` (downgrade; `voice_error` recorded) |
| `generate_voice(...)` returns successfully | `voice_neutral` |

`prepare_voice` never raises. Every voice-path failure — transient provider outage, permanent misconfiguration, budget exhaustion, unexpected exception — resolves to a text fallback so the channel send always has a text payload to publish. The failure is captured in the `voice_error` field of the audit trail.

### Relationship to slow_tick (no v1 integration)

The slow_tick reflection phase lives inside the memory module (see [`memory.md` § Slow_tick consolidate phase](./memory.md#slow_tick-consolidate-phase-l4-forward-looking) for full mechanics). Slow_tick runs between sessions and produces both `type='thought'` nodes and `type='expectation'` sub-class nodes (with `event_time_end` doubling as a due_at). An expectation expresses "I expect the user to do X within window Y" — which looks like the natural trigger source for proactive: when an expectation's due_at is near, the persona could spontaneously open with "you mentioned you'd update tonight, anything to share?".

**Proactive does NOT wire this path up today** — proactive currently reads L3 events, L4 thoughts, and RecallMessages, but does not read expectation nodes and will not fire a proactive message because an expectation is about to expire. The reasons:

- Expectations are slow_tick output, and we don't yet have enough dogfood signal to calibrate how often the LLM's expectations actually land. Let the fast loop fulfill or expire them through natural user replies for a week or two before deciding whether to wire them as a proactive trigger.
- Proactive's Send-N-per-window gates are already tight; adding another trigger source is easy to overshoot.
- Expectation `due_at` precision is day-level; the proactive tick is 60s. Bridging the granularity mismatch needs day-bucket throttling logic that is out of scope for this ship.

v2 plan: add a new trigger under `proactive/triggers/` (analogous to `relationship_anniversary`) that reads `concept_nodes WHERE type='expectation' AND event_time_end BETWEEN now AND now + 24h AND superseded_by_id IS NULL`, then routes through the existing policy gate chain (quiet hours / 24h cap / cool-down / …) before actually sending. Anyone reading this section today: **don't add it yet** — wait for dogfood data.

The `on_thought_created` observer hook that fires when memory writes an expectation row IS visible to proactive, but in v1 the handler is a deliberate no-op (receives, ignores).

## How to Extend

### 1. Add a new relationship trigger

Triggers in MVP live inside `PolicyEngine._match_trigger`. To prototype a new trigger, wrap the policy engine with a small subclass, push a synthetic event onto the queue, and let the existing audit path record the decision. Here is a minimal "recurring concern" trigger that fires when the user has mentioned a specific topic three or more times in the last week.

```python
from datetime import datetime, timedelta
from echovessel.proactive.base import (
    EventType,
    ProactiveEvent,
    TriggerReason,
)
from echovessel.proactive.policy import PolicyEngine, TriggerMatch


class ExtendedPolicyEngine(PolicyEngine):
    """Adds a third trigger: user mentioned a concern N+ times recently."""

    min_mentions: int = 3
    lookback_days: int = 7
    keywords: tuple[str, ...] = ("worried", "anxious", "stressed")

    def _match_trigger(self, events, persona_id, user_id, now):
        base = super()._match_trigger(events, persona_id, user_id, now)
        if base is not None:
            return base

        since = now - timedelta(days=self.lookback_days)
        recent_events = self.memory.get_recent_events(
            persona_id, user_id, since=since, limit=50,
        )
        hits = [
            e for e in recent_events
            if any(
                kw in (getattr(e, "summary", "") or "").lower()
                for kw in self.keywords
            )
        ]
        if len(hits) >= self.min_mentions:
            return TriggerMatch(
                reason=TriggerReason.HIGH_EMOTIONAL_EVENT,  # reuse enum for MVP
                payload={
                    "trigger_event_id": getattr(hits[-1], "id", None),
                    "match_label": "recurring_concern",
                    "hit_count": len(hits),
                },
            )
        return None
```

To actually drive it, push a synthetic event onto the scheduler's queue — any time-based wake-up will do:

```python
scheduler.notify(
    ProactiveEvent(
        event_type=EventType.TICK,
        persona_id="default",
        user_id="self",
        created_at=datetime.now(),
        payload={},
        critical=False,
    )
)
```

Because `PolicyEngine.evaluate` walks the gate ladder before consulting triggers, your new trigger still respects quiet hours, cold-user protection, the rate limit, and the in-flight-turn check — you get the safety rails for free.

### 2. Tune policy thresholds

Every knob lives in the `[proactive]` TOML section and is parsed into a `ProactiveConfig` Pydantic model at daemon startup. The fields below are the ones you are most likely to want to change:

```toml
[proactive]
enabled                          = true   # master on/off switch
tick_interval_seconds            = 60     # how often the loop wakes (10-3600)

# Quiet hours (local time, 24h clock; wraps midnight when start > end)
quiet_hours_start                = 23     # 23:00 local
quiet_hours_end                  = 7      # 07:00 local — window is 23:00-07:00

# Rate limit
max_per_24h                      = 3      # absolute daily cap on sends (0-100)

# Cold-user protection
cold_user_threshold              = 2      # N unanswered sends -> cold mode
cold_user_response_window_hours  = 6      # a reply within this window resets it

# Long-silence trigger
long_silence_hours               = 48     # silence >= this -> nudge candidate

# Queue
max_events_in_queue              = 64     # hard cap; oldest non-critical dropped

# Shutdown
stop_grace_seconds               = 10     # wait-time for in-flight tick on stop
```

Two important operational notes:

- **Config is read once, at scheduler construction.** Proactive does not watch the TOML file and does not respond to SIGHUP. To apply new values, restart the daemon. This is intentional — live-reloading a policy engine while it is mid-tick is strictly more complex than the value it buys.
- **`persona_id` and `user_id`** default to `"default"` and `"self"` and match the MVP single-persona shape. Multi-persona setups get one scheduler per persona, each with its own `ProactiveConfig`.

### 3. Hook a custom audit sink

The default sink is `JSONLAuditSink`, which writes one JSON object per line to `~/.echovessel/logs/proactive-YYYY-MM-DD.jsonl`. It implements the `AuditSink` Protocol from `echovessel.proactive.base`:

```python
class AuditSink(Protocol):
    def record(self, decision: ProactiveDecision) -> None: ...
    def update_latest(self, decision_id: str, **outcome_fields) -> None: ...
    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]: ...
    def count_sends_in_last_24h(self, *, now: datetime) -> int: ...
```

To ship decisions somewhere else — a SQLite table, a Prometheus exporter, a third-party observability platform — implement the Protocol and pass the instance to `build_proactive_scheduler(audit_sink=...)`. The scheduler will use your sink instead of the default.

Here is a minimal sink that forwards every decision to a sibling JSONL file while delegating the policy-read methods (`recent_sends`, `count_sends_in_last_24h`) to the stock JSONL sink so that rate-limit and cold-user checks still have something to read against:

```python
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from echovessel.proactive.audit import JSONLAuditSink
from echovessel.proactive.base import AuditSink, ProactiveDecision


class TeeJSONLAuditSink(AuditSink):
    """Writes to a custom JSONL path AND delegates reads to the stock sink."""

    def __init__(self, custom_path: Path, stock_log_dir: Path):
        self._custom_path = Path(custom_path).expanduser()
        self._custom_path.parent.mkdir(parents=True, exist_ok=True)
        self._stock = JSONLAuditSink(log_dir=stock_log_dir)

    def record(self, decision: ProactiveDecision) -> None:
        self._stock.record(decision)           # keeps read queries working
        try:
            with self._custom_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_to_json(decision), ensure_ascii=False) + "\n")
        except OSError:
            # NEVER raise from record(); log and swallow.
            pass

    def update_latest(self, decision_id: str, **outcome_fields) -> None:
        self._stock.update_latest(decision_id, **outcome_fields)

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        return self._stock.recent_sends(last_n=last_n)

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        return self._stock.count_sends_in_last_24h(now=now)


def _to_json(d: ProactiveDecision) -> dict:
    raw = asdict(d)
    raw["timestamp"] = d.timestamp.isoformat()  # datetimes need isoformat
    return raw
```

Wire it up from your runtime bootstrap:

```python
from pathlib import Path
from echovessel.proactive import build_proactive_scheduler

scheduler = build_proactive_scheduler(
    config=proactive_config,
    memory_api=memory_facade,
    channel_registry=registry,
    proactive_fn=proactive_fn,
    persona=persona_view,
    voice_service=voice_service,
    is_turn_in_flight=lambda: registry.any_in_flight(),
    audit_sink=TeeJSONLAuditSink(
        custom_path=Path("~/.echovessel/logs/proactive-tee.jsonl"),
        stock_log_dir=Path("~/.echovessel/logs"),
    ),
)
```

Two implementation notes for custom sinks:

- **`record()` must never raise.** The scheduler tick loop cannot tolerate an audit sink that throws. If your sink does I/O that can fail, wrap it in `try`/`except` and log the error rather than propagating.
- **`recent_sends` and `count_sends_in_last_24h` are the read side of the policy engine.** If you want cold-user and rate-limit protection to continue working, either delegate these to the stock sink (as above) or implement them against your own storage. A sink that stubs them as `return []` / `return 0` effectively disables those two gates.

For a complete reference, the authoritative source is `src/echovessel/proactive/` — every file has detailed docstrings, and the policy engine's gate order is locked in by the unit tests under `tests/proactive/`.

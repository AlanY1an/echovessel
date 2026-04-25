"""Core Protocols, dataclasses and enums for the proactive subsystem.

Everything in this file is pure data or structural typing — no I/O, no
async, no imports from runtime/prompts. Submodules (queue, audit, policy,
generator, delivery, scheduler, factory) build on these types.

Spec references:
- §2 trigger model (ProactiveEvent / EventType)
- §3 policy engine (ProactiveDecision / TriggerReason / SkipReason / ActionType)
- §4 scheduler Protocol
- §5 message generation (ProactiveMessage / MemorySnapshot / ProactiveFn)
- §7 audit (AuditSink Protocol)
- §10 voice integration (VoiceServiceProtocol duck type)
- §11 runtime integration (MemoryApi / ChannelRegistryApi duck types)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

CONFIG_VERSION = "v0.1"


# ---------------------------------------------------------------------------
# Event / decision enums (spec §2.4, §3, §7)
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """Event types the scheduler queue accepts. Ordered roughly by
    production frequency (tick > turn_completed > session_closed > ...)."""

    TICK = "time.tick"
    LONG_SILENCE_DETECTED = "time.long_silence_detected"
    EVENT_EXTRACTED = "memory.event_extracted"
    SESSION_CLOSED = "memory.session_closed"
    RELATIONSHIP_CHANGED = "memory.relationship_changed"
    TURN_COMPLETED = "runtime.turn_completed"


class TriggerReason(StrEnum):
    """Which branch of the policy engine produced the current decision.

    Gate reasons (evaluate short-circuited) and match reasons (a trigger
    rule fired) share this enum because they both sit in
    ``ProactiveDecision.trigger``.
    """

    # Gates — action will always be SKIP when one of these wins
    QUIET_HOURS_GATE = "quiet_hours_gate"
    COLD_USER_GATE = "cold_user_gate"
    RATE_LIMIT_GATE = "rate_limit_gate"
    IN_FLIGHT_TURN_GATE = "in_flight_turn_gate"  # v0.2 · review M5
    NO_TRIGGER_MATCH = "no_trigger_match"
    QUEUE_OVERFLOW = "queue_overflow"

    # Matches — action will be SEND when one of these wins
    HIGH_EMOTIONAL_EVENT = "high_emotional_event"
    LONG_SILENCE = "long_silence"
    WARMTH_BURST = "warmth_burst"


class SkipReason(StrEnum):
    """Why a decision's action is 'skip'. Mirrors spec §7.3.

    Note: ``no_trigger_match`` is the default 'nothing to say right now'
    skip; it is NOT a failure. ``llm_error`` / ``llm_parse_error`` /
    ``llm_output_invalid`` are observed during message generation and
    reported by the scheduler after generate() returns.
    """

    QUIET_HOURS = "quiet_hours"
    LOW_PRESENCE_MODE = "low_presence_mode"
    RATE_LIMITED = "rate_limited"
    RATE_LIMIT_READ_ERROR = "rate_limit_read_error"
    IN_FLIGHT_TURN = "in_flight_turn"  # v0.2 · review M5 · spec §3.5a
    NO_TRIGGER_MATCH = "no_trigger_match"
    LLM_ERROR = "llm_error"
    LLM_PARSE_ERROR = "llm_parse_error"
    LLM_OUTPUT_INVALID = "llm_output_invalid"
    QUEUE_OVERFLOW = "queue_overflow"
    NO_ENABLED_CHANNEL = "no_enabled_channel"
    NO_PUSHABLE_CHANNEL = "no_pushable_channel"
    SEND_FAILED = "send_failed"


class ActionType(StrEnum):
    SEND = "send"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# ProactiveEvent — what goes into the queue (spec §2.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProactiveEvent:
    """Unit of work in the scheduler queue.

    ``critical`` controls MAX_EVENTS overflow behaviour (spec §2.5): when
    the queue is full, the oldest non-critical event is dropped. The
    classification rule is owned by whoever produces the event (the
    adapter turning a memory/runtime signal into an event); the queue
    itself just respects the flag.
    """

    event_type: EventType
    persona_id: str
    user_id: str
    created_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    critical: bool = False


# ---------------------------------------------------------------------------
# ProactiveDecision — audit log row (spec §7.3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProactiveDecision:
    """Full audit row. Every call to PolicyEngine.evaluate produces exactly
    one of these, regardless of action. ``update_outcome`` is how the
    scheduler fills in send_ok / ingest_message_id after the async send
    completes (spec §7.3 two-phase write pattern)."""

    # Identity
    decision_id: str
    persona_id: str
    user_id: str
    timestamp: datetime

    # Trigger
    trigger: str                              # str for forward compat
    trigger_payload: Mapping[str, Any] | None = None

    # Decision
    action: str = ActionType.SKIP.value
    skip_reason: str | None = None

    # Send outcome (None when action == 'skip')
    target_channel_id: str | None = None
    message_text: str | None = None
    rationale: str | None = None             # internal; never enters prompt
    # v0.2 · delivery inherits from persona.voice_enabled (review R1 +
    # Check 3). None when action == 'skip'. Values are "text" or
    # "voice_neutral" — proactive never picks prosody tone variants
    # (deferred to v1.0 along with persona-selected delivery).
    delivery: str | None = None
    voice_used: bool = False
    voice_error: str | None = None
    send_ok: bool | None = None
    send_error: str | None = None
    ingest_message_id: int | None = None

    # Observability
    llm_latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # Reproducibility
    memory_snapshot_hash: str | None = None
    policy_snapshot: Mapping[str, Any] = field(default_factory=dict)
    config_version: str = CONFIG_VERSION

    def update_outcome(
        self,
        *,
        send_ok: bool | None = None,
        send_error: str | None = None,
        ingest_message_id: int | None = None,
        delivery: str | None = None,
        voice_used: bool | None = None,
        voice_error: str | None = None,
        llm_latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Fill in post-evaluate fields. Called once by the scheduler after
        the send + ingest round-trip completes. Accepts None for 'don't
        touch this field'."""
        if send_ok is not None:
            self.send_ok = send_ok
        if send_error is not None:
            self.send_error = send_error
        if ingest_message_id is not None:
            self.ingest_message_id = ingest_message_id
        if delivery is not None:
            self.delivery = delivery
        if voice_used is not None:
            self.voice_used = voice_used
        if voice_error is not None:
            self.voice_error = voice_error
        if llm_latency_ms is not None:
            self.llm_latency_ms = llm_latency_ms
        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = completion_tokens


# ---------------------------------------------------------------------------
# Message generation (spec §5)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemorySnapshot:
    """Immutable input to ProactiveFn. Must NOT contain any channel_id
    string — the F10 guard (see generator._assert_no_channel_leak) rejects
    snapshots that fail this invariant.

    The fields are intentionally Any-typed: generator.py and tests both
    need to swap in plain dicts / dataclasses without importing the
    memory SQLModel types. This keeps Protocol + Layer 3 import rules
    clean.
    """

    trigger: str
    trigger_payload: Mapping[str, Any]
    core_blocks: tuple[Any, ...]
    recent_l3_events: tuple[Any, ...]
    recent_l2_window: tuple[Any, ...]
    relationship_state: Any | None
    snapshot_hash: str


@dataclass(slots=True)
class ProactiveMessage:
    """What proactive_fn returns. ``rationale`` is observability-only: it
    is recorded in the audit trail but never reaches the channel or any
    other prompt (spec §5.5 / §7.7)."""

    text: str
    rationale: str | None = None
    voice_hint: str | None = None
    audio_blob: bytes | None = None
    llm_latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# v0.2 · review R1 + Check 3: delivery is populated FROM persona.voice_enabled,
# never chosen by proactive. Two values only for MVP — prosody tone
# variants are deferred to v1.0 along with persona-selected delivery.
DeliveryKind = Literal["text", "voice_neutral"]
OutgoingMessageKind = Literal["reactive", "proactive"]


# ProactiveFn is the async callable the runtime injects. Spec §5.1 + §11.3 #5.
# Runtime builds it in runtime/prompts_wiring.py::make_proactive_fn (Round 2).
ProactiveFn = Callable[[MemorySnapshot], Awaitable[ProactiveMessage]]


# ---------------------------------------------------------------------------
# Injected I/O Protocols (spec §8 / §9 / §10 / §11)
# ---------------------------------------------------------------------------


class MemoryApi(Protocol):
    """Read + single-write facade over the memory subsystem.

    Proactive only uses these five methods. **None of them accept a
    ``channel_id`` parameter** — that is the D4 guarantee baked into the
    Protocol. Adding a channel_id parameter to any signature here is a
    breaking change that the D4 guard test will catch.
    """

    def load_core_blocks(self, persona_id: str, user_id: str) -> list[Any]: ...

    def list_recall_messages(
        self,
        persona_id: str,
        user_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[Any]: ...

    def get_recent_events(
        self,
        persona_id: str,
        user_id: str,
        *,
        since: datetime,
        limit: int = 20,
    ) -> list[Any]: ...

    def get_session_status(self, session_id: str) -> Any | None: ...

    def ingest_message(
        self,
        *,
        persona_id: str,
        user_id: str,
        channel_id: str,
        role: Any,
        content: str,
        now: datetime | None = None,
    ) -> Any:
        """Write a persona-generated message into L2. channel_id here is
        **delivery metadata** (which pipe did the message leave through),
        not a memory filter; D4 only applies to READ paths."""


@runtime_checkable
class ChannelProtocol(Protocol):
    """Structural duck type for the subset of channels.base.Channel that
    proactive actually uses. Using a local Protocol keeps proactive's
    import footprint minimal and forward-compatible with channels spec
    v0.2 capability flag additions.

    The ``in_flight_turn_id`` attribute is **optional** — spec §3.5a
    says it is a capability. Channels that don't expose it are read as
    ``None`` via ``getattr`` fallback, meaning they never block the
    no_in_flight_turn gate.
    """

    name: str

    async def send(self, text: str) -> None: ...


class ChannelRegistryApi(Protocol):
    """Proactive-side view of the runtime-owned channel registry.

    Methods are synchronous: the registry is an in-memory dict guarded
    by runtime; no I/O. The scheduler calls list_enabled() once per
    dispatch, never holds long-term references to concrete Channel
    instances (spec §9.2)."""

    def list_enabled(self) -> list[ChannelProtocol]: ...


class VoiceServiceProtocol(Protocol):
    """Local duck-typed view of ``voice.VoiceService.generate_voice()``.

    Authoritative signature: ``docs/voice/01-spec-v0.1.md`` §4.7a
    (v0.2 generate_voice facade). Proactive round2 calls **only**
    ``generate_voice`` — never ``speak()`` directly, per tracker hard
    constraint #4 (preserves R3 layering).

    ``generate_voice`` is:
        - idempotent on ``message_id`` (on-disk cache at
          ``~/.echovessel/voice_cache/<message_id>.mp3``)
        - raises ``VoiceTransientError`` / ``VoicePermanentError`` /
          ``VoiceBudgetError`` on failure; callers downgrade to text

    Why no ``is_available`` method anymore: the real VoiceService has
    ``health_check()`` instead, and the answer is already captured by
    (a) voice_service being non-None and (b) generate_voice either
    succeeding or raising. The round1 is_available probe was extra
    coupling that round2 strips out.
    """

    async def generate_voice(
        self,
        text: str,
        *,
        voice_id: str,
        message_id: int,
        tone_hint: Literal["neutral", "tender", "whisper"] = "neutral",
    ) -> Any:
        """Produce a playable audio artifact for ``message_id``.

        Return type is ``Any`` at this Protocol layer so proactive does
        not have to import ``echovessel.voice.VoiceResult`` — that would
        couple Layer 3 (proactive) to Layer 2 (voice) types tighter than
        necessary. At the call site in delivery.py, the return value is
        stored as an opaque artifact; scheduler records
        ``voice_used=True`` in audit and moves on."""


class PersonaView(Protocol):
    """Read-only view of the parts of persona state that proactive needs
    at tick time. Spec §6.2a + review Check 3: delivery inherits
    **entirely** from ``voice_enabled``; proactive never makes its own
    voice decisions.

    The Runtime owns the concrete implementation and re-reads from
    ``RuntimeContext.persona`` on every property access so that a
    ``POST /api/admin/persona/voice-toggle`` toggle is picked up on the
    next tick without needing a reload hook.

    Single-persona MVP shape: one PersonaView is injected per scheduler.
    Multi-persona v1.0 will use one scheduler per persona, each with
    its own view.
    """

    @property
    def voice_enabled(self) -> bool:
        """Is the persona's voice output currently enabled? When False,
        proactive emits pure text regardless of channel / voice_id /
        voice_service availability. Spec §6.2a main switch."""

    @property
    def voice_id(self) -> str | None:
        """The cloned voice id to use for TTS, or None if no voice is
        configured. When None, the voice path is a no-op even if
        ``voice_enabled`` is True."""


class AuditSink(Protocol):
    """What the scheduler writes decisions to. Spec §7.6.

    ``record`` is synchronous and blocking-safe: a JSONL implementation
    flushes ~1KB per call, well under scheduler tick budget. Async impls
    (v1.0 memory_db sink) are welcome but must finish in ``record``'s
    synchronous frame.
    """

    def record(self, decision: ProactiveDecision) -> None: ...

    def update_latest(
        self,
        decision_id: str,
        *,
        send_ok: bool | None = None,
        send_error: str | None = None,
        ingest_message_id: int | None = None,
        delivery: str | None = None,
        voice_used: bool | None = None,
        voice_error: str | None = None,
        llm_latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None: ...

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]: ...

    def count_sends_in_last_24h(self, *, now: datetime) -> int: ...


# ---------------------------------------------------------------------------
# Scheduler Protocol (spec §4.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class ProactiveScheduler(Protocol):
    """Public contract for the scheduler. Runtime constructs one of these
    via ``proactive.factory.build_proactive_scheduler`` and holds the
    reference throughout daemon lifetime."""

    async def start(self) -> None:
        """Spawn the tick loop as an asyncio background task. Safe to call
        exactly once per instance per process."""

    async def stop(self) -> None:
        """Cooperative shutdown. Waits up to ``stop_grace_seconds`` for
        the current tick's send + ingest to finish before returning."""

    def notify(self, event: ProactiveEvent) -> None:
        """Push an event into the internal queue. MUST be non-blocking
        and safe to call from any thread/task context — the queue drops
        the oldest non-critical event when full (spec §2.5)."""


__all__ = [
    "CONFIG_VERSION",
    "EventType",
    "TriggerReason",
    "SkipReason",
    "ActionType",
    "DeliveryKind",
    "OutgoingMessageKind",
    "ProactiveEvent",
    "ProactiveDecision",
    "MemorySnapshot",
    "ProactiveMessage",
    "ProactiveFn",
    "MemoryApi",
    "ChannelProtocol",
    "ChannelRegistryApi",
    "VoiceServiceProtocol",
    "PersonaView",
    "AuditSink",
    "ProactiveScheduler",
]

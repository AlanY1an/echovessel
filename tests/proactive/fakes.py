"""Shared fakes for proactive tests.

Fakes — not mocks — because the surface area is small enough that tests
are easier to read with a handwritten InMemoryMemoryApi / FakeChannel /
FakeVoiceService than with Mock.call_args assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from echovessel.proactive.core.base import (
    AuditSink,
    ChannelProtocol,
    PersonaView,
    ProactiveDecision,
    ProactiveMessage,
)

# ---------------------------------------------------------------------------
# Memory fake
# ---------------------------------------------------------------------------


@dataclass
class FakeMessage:
    """Stand-in for memory.models.RecallMessage."""

    content: str
    role: str = "user"
    channel_id: str = "web"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 4, 15, 12, 0))
    id: int | None = None


@dataclass
class FakeCoreBlock:
    label: str = "persona"
    content: str = "温暖的陪伴"
    description: str | None = None


@dataclass
class FakeEvent:
    id: int
    description: str
    emotional_impact: int = 0
    emotion_tags: tuple[str, ...] = ()
    relational_tags: tuple[str, ...] = ()
    source_session_id: str | None = None


@dataclass
class FakeIngestResult:
    """Mimic memory.ingest.IngestResult just enough for scheduler to extract
    a message id."""

    class _Msg:
        def __init__(self, id: int) -> None:
            self.id = id

    def __init__(self, message_id: int) -> None:
        self.message = self._Msg(message_id)


@dataclass
class InMemoryMemoryApi:
    """Fake MemoryApi.

    Tests populate ``_core_blocks``, ``_recent_events``, ``_recent_messages``
    directly. ``ingest_message`` appends to ``ingested`` in order so tests
    can assert on ``ingested[0].content``. The ORDER INVARIANT test uses
    this to verify ingest was recorded BEFORE channel.send was called.
    """

    core_blocks: list[FakeCoreBlock] = field(default_factory=list)
    recent_events: list[FakeEvent] = field(default_factory=list)
    recent_messages: list[FakeMessage] = field(default_factory=list)

    ingested: list[FakeMessage] = field(default_factory=list)
    _next_msg_id: int = 1000

    def load_core_blocks(self, persona_id: str, user_id: str) -> list[Any]:
        return list(self.core_blocks)

    def list_recall_messages(
        self,
        persona_id: str,
        user_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[Any]:
        msgs = self.recent_messages
        if before is not None:
            msgs = [m for m in msgs if m.created_at < before]
        return list(msgs[:limit])

    def get_recent_events(
        self,
        persona_id: str,
        user_id: str,
        *,
        since: datetime,
        limit: int = 20,
    ) -> list[Any]:
        return list(self.recent_events[:limit])

    def get_session_status(self, session_id: str) -> Any | None:
        return None

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
        msg = FakeMessage(
            content=content,
            role=getattr(role, "value", role),
            channel_id=channel_id,
            created_at=now or datetime(2026, 4, 15, 12, 0),
            id=self._next_msg_id,
        )
        self._next_msg_id += 1
        self.ingested.append(msg)
        return FakeIngestResult(message_id=msg.id or 0)


# ---------------------------------------------------------------------------
# Channel fake
# ---------------------------------------------------------------------------


@dataclass
class FakeChannel:
    """Duck-typed ChannelProtocol. ``sent`` records every outgoing text
    so tests can assert on order (scheduler must ingest BEFORE calling
    send)."""

    name: str = "web"
    channel_id: str = "web"
    supports_audio: bool = False
    supports_outgoing_push: bool = True
    sent: list[str] = field(default_factory=list)
    _raise_on_send: type[Exception] | None = None

    async def send(self, text: str) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send("simulated send failure")
        self.sent.append(text)


@dataclass
class FakeChannelRegistry:
    channels: list[ChannelProtocol] = field(default_factory=list)

    def list_enabled(self) -> list[ChannelProtocol]:
        return list(self.channels)


# ---------------------------------------------------------------------------
# Voice fake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeVoiceResult:
    """Stand-in for echovessel.voice.types.VoiceResult. Tests don't need
    the real dataclass — proactive treats it as an opaque ``Any``."""

    url: str = "/api/chat/voice/100.mp3"
    duration_seconds: float = 1.23
    provider: str = "stub"
    cost_usd: float = 0.0
    cached: bool = False


@dataclass
class FakeVoiceService:
    """Fake matching the v0.2 VoiceServiceProtocol duck type.

    Exposes the single ``generate_voice`` facade. Tests set ``_raise``
    to a ``VoiceTransientError`` / ``VoicePermanentError`` /
    ``VoiceBudgetError`` class to verify the graceful downgrade path,
    or leave it None for the happy path.

    ``last_call`` captures the most recent generate_voice arguments so
    tests can assert on voice_id / message_id / tone_hint without
    a separate Mock wrapper.
    """

    _raise: type[Exception] | None = None
    _result: FakeVoiceResult = field(default_factory=FakeVoiceResult)
    last_call: dict[str, Any] | None = field(default=None, init=False)
    call_count: int = field(default=0, init=False)

    async def generate_voice(
        self,
        text: str,
        *,
        voice_id: str,
        message_id: int,
        tone_hint: str = "neutral",
    ) -> Any:
        self.call_count += 1
        self.last_call = {
            "text": text,
            "voice_id": voice_id,
            "message_id": message_id,
            "tone_hint": tone_hint,
        }
        if self._raise is not None:
            raise self._raise("simulated voice failure")
        return self._result


@dataclass(frozen=True)
class FakePersonaView(PersonaView):
    """Matches PersonaView Protocol: two read-only properties.

    Frozen so tests can't accidentally mutate and rely on stale state.
    To simulate an admin toggle mid-run, construct a new FakePersonaView
    and swap it onto the scheduler.
    """

    voice_enabled_value: bool = False
    voice_id_value: str | None = None

    @property
    def voice_enabled(self) -> bool:
        return self.voice_enabled_value

    @property
    def voice_id(self) -> str | None:
        return self.voice_id_value


# ---------------------------------------------------------------------------
# Audit fake
# ---------------------------------------------------------------------------


@dataclass
class FakeAuditSink(AuditSink):
    """In-memory AuditSink for unit tests. ``sends_count`` and
    ``cold_user_sends`` let the policy test suite dial in edge cases
    without touching the JSONL file system."""

    recorded: list[ProactiveDecision] = field(default_factory=list)
    sends_count_24h: int = 0
    cold_user_sends: list[ProactiveDecision] = field(default_factory=list)

    def record(self, decision: ProactiveDecision) -> None:
        self.recorded.append(decision)

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
    ) -> None:
        if not self.recorded:
            return
        target = None
        for d in reversed(self.recorded):
            if d.decision_id == decision_id:
                target = d
                break
        if target is None:
            return
        target.update_outcome(
            send_ok=send_ok,
            send_error=send_error,
            ingest_message_id=ingest_message_id,
            delivery=delivery,
            voice_used=voice_used,
            voice_error=voice_error,
            llm_latency_ms=llm_latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        return list(self.cold_user_sends[:last_n])

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        return self.sends_count_24h


# ---------------------------------------------------------------------------
# ProactiveFn fake
# ---------------------------------------------------------------------------


def make_fake_proactive_fn(
    text: str = "hey there",
    *,
    rationale: str | None = None,
    raise_exc: type[Exception] | None = None,
) -> Any:
    async def _fn(snapshot: Any) -> ProactiveMessage:
        if raise_exc is not None:
            raise raise_exc("simulated LLM failure")
        return ProactiveMessage(
            text=text,
            rationale=rationale,
            llm_latency_ms=42,
        )

    return _fn


__all__ = [
    "FakeMessage",
    "FakeCoreBlock",
    "FakeEvent",
    "FakeIngestResult",
    "InMemoryMemoryApi",
    "FakeChannel",
    "FakeChannelRegistry",
    "FakeVoiceService",
    "FakeVoiceResult",
    "FakePersonaView",
    "FakeAuditSink",
    "make_fake_proactive_fn",
]

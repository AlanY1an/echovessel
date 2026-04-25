"""DefaultScheduler end-to-end tests.

The HEADLINE test here is ``test_order_invariant_ingest_before_send`` —
the "先 ingest 再 send" rule (spec §4.5 + §7.4). A fake channel whose
``send`` raises lets us verify that ``memory.ingest_message`` still ran
before ``channel.send`` was attempted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from echovessel.core.types import MessageRole
from echovessel.proactive.core.base import (
    ActionType,
    EventType,
    ProactiveEvent,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.engines.generator import MessageGenerator
from echovessel.proactive.engines.policy import PolicyEngine
from echovessel.proactive.execution.delivery import DeliveryRouter
from echovessel.proactive.execution.queue import ProactiveEventQueue
from echovessel.proactive.execution.scheduler import DefaultScheduler
from tests.proactive.fakes import (
    FakeAuditSink,
    FakeChannel,
    FakeChannelRegistry,
    FakeCoreBlock,
    FakeMessage,
    FakePersonaView,
    FakeVoiceService,
    InMemoryMemoryApi,
    make_fake_proactive_fn,
)


def _build_scheduler(
    *,
    config: ProactiveConfig | None = None,
    memory: InMemoryMemoryApi | None = None,
    audit: FakeAuditSink | None = None,
    channel: FakeChannel | None = None,
    voice_service=None,
    proactive_fn=None,
    clock=None,
    persona: FakePersonaView | None = None,
    is_turn_in_flight=None,
) -> tuple[DefaultScheduler, dict]:
    cfg = config or ProactiveConfig(
        persona_id="p",
        user_id="u",
        quiet_hours_start=23,
        quiet_hours_end=7,
        max_per_24h=3,
        cold_user_threshold=2,
        long_silence_hours=48,
    )
    memory = memory or InMemoryMemoryApi(
        core_blocks=[FakeCoreBlock(content="温暖")],
    )
    audit = audit or FakeAuditSink()
    channel = channel or FakeChannel(name="web", channel_id="web")
    registry = FakeChannelRegistry(channels=[channel])
    proactive_fn = proactive_fn or make_fake_proactive_fn(
        text="hi user, how are you today"
    )
    clock = clock or (lambda: datetime(2026, 4, 15, 12, 0))

    scheduler = DefaultScheduler(
        config=cfg,
        memory=memory,
        audit=audit,
        policy=PolicyEngine(
            config=cfg,
            audit=audit,
            memory=memory,
            is_turn_in_flight=is_turn_in_flight,
        ),
        generator=MessageGenerator(memory=memory, proactive_fn=proactive_fn),
        delivery=DeliveryRouter(
            memory=memory,
            channel_registry=registry,
            voice_service=voice_service,
        ),
        queue=ProactiveEventQueue(max_events=cfg.max_events_in_queue),
        persona=persona,
        clock=clock,
    )
    return scheduler, {
        "memory": memory,
        "audit": audit,
        "channel": channel,
        "registry": registry,
    }


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tick orchestration
# ---------------------------------------------------------------------------


def test_tick_long_silence_triggers_send():
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        core_blocks=[FakeCoreBlock(content="warm")],
        recent_messages=[
            FakeMessage(
                content="hey",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )
    scheduler, state = _build_scheduler(
        memory=memory, clock=lambda: now
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.LONG_SILENCE.value
    assert state["channel"].sent == ["hi user, how are you today"]


def test_tick_quiet_hours_skips_without_send():
    now = datetime(2026, 4, 15, 2, 0)  # 2am
    scheduler, state = _build_scheduler(clock=lambda: now)
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.QUIET_HOURS.value
    assert state["channel"].sent == []
    assert state["memory"].ingested == []


def test_tick_rate_limit_skips():
    now = datetime(2026, 4, 15, 12, 0)
    audit = FakeAuditSink()
    audit.sends_count_24h = 3  # at cap
    scheduler, state = _build_scheduler(audit=audit, clock=lambda: now)
    decision = _run(scheduler.tick_once())
    assert decision.skip_reason == SkipReason.RATE_LIMITED.value
    assert state["channel"].sent == []


# ---------------------------------------------------------------------------
# Order invariant (spec §4.5 + §7.4)
# ---------------------------------------------------------------------------


def test_order_invariant_ingest_before_send():
    """When a send succeeds, memory.ingest_message must have been called
    before channel.send. Verified by inspecting insertion order on both
    fakes."""
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )

    # Instrument channel.send to snapshot ingested state at the moment
    # of the send call.
    channel = FakeChannel(name="web", channel_id="web")
    ingested_at_send_time: list[int] = []
    original_send = channel.send

    async def _spy_send(text):
        ingested_at_send_time.append(len(memory.ingested))
        await original_send(text)

    channel.send = _spy_send  # type: ignore[method-assign]

    scheduler, _ = _build_scheduler(
        memory=memory, channel=channel, clock=lambda: now
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SEND.value
    assert ingested_at_send_time == [1], (
        "channel.send was called but memory.ingest_message had not yet run"
    )
    # Final state: exactly one ingested message and one send
    assert len(memory.ingested) == 1
    assert memory.ingested[0].role == MessageRole.PERSONA.value
    assert channel.sent == ["hi user, how are you today"]


def test_order_invariant_send_failure_keeps_ingest():
    """If channel.send fails AFTER memory.ingest_message succeeds,
    persona still remembers saying it (spec §16.2). Audit records
    send_ok=False but the L2 row is there."""
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )
    channel = FakeChannel(name="web", channel_id="web")
    channel._raise_on_send = RuntimeError  # type: ignore[attr-defined]

    scheduler, state = _build_scheduler(
        memory=memory, channel=channel, clock=lambda: now
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SEND.value
    assert len(memory.ingested) == 1   # ingest succeeded first
    # The decision in audit reflects send failure
    audit = state["audit"]
    assert audit.recorded[-1].send_ok is False
    assert audit.recorded[-1].send_error is not None


# ---------------------------------------------------------------------------
# LLM failure path
# ---------------------------------------------------------------------------


def test_llm_error_converts_send_to_skip():
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )
    boom = make_fake_proactive_fn(raise_exc=RuntimeError)
    scheduler, state = _build_scheduler(
        memory=memory, proactive_fn=boom, clock=lambda: now
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SKIP.value
    assert decision.skip_reason == SkipReason.LLM_ERROR.value
    # No ingest, no send (§5.6: LLM failures never advance to delivery)
    assert memory.ingested == []
    assert state["channel"].sent == []


# ---------------------------------------------------------------------------
# Start/stop lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_disabled_config():
    cfg = ProactiveConfig(
        persona_id="p", user_id="u", enabled=False, tick_interval_seconds=10
    )
    scheduler, _ = _build_scheduler(config=cfg)

    async def _go():
        await scheduler.start()
        await scheduler.stop()

    _run(_go())
    # No task was created because enabled=False
    assert scheduler._task is None


def test_stop_is_idempotent():
    scheduler, _ = _build_scheduler()

    async def _go():
        await scheduler.stop()  # first
        await scheduler.stop()  # second — should be a no-op

    _run(_go())


# ---------------------------------------------------------------------------
# notify() queue push
# ---------------------------------------------------------------------------


def test_notify_pushes_event_into_queue():
    scheduler, _ = _build_scheduler()
    scheduler.notify(
        ProactiveEvent(
            event_type=EventType.EVENT_EXTRACTED,
            persona_id="p",
            user_id="u",
            created_at=datetime(2026, 4, 15, 12, 0),
            payload={"emotional_impact": -9},
            critical=True,
        )
    )
    assert len(scheduler.queue) == 1
    drained = scheduler.queue.drain()
    assert drained[0].event_type == EventType.EVENT_EXTRACTED
    assert drained[0].critical is True


def test_high_emotional_event_notify_triggers_send_on_next_tick():
    now = datetime(2026, 4, 15, 12, 0)
    scheduler, state = _build_scheduler(clock=lambda: now)
    scheduler.notify(
        ProactiveEvent(
            event_type=EventType.EVENT_EXTRACTED,
            persona_id="p",
            user_id="u",
            created_at=now,
            payload={"event_id": 7, "emotional_impact": -9},
            critical=True,
        )
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.HIGH_EMOTIONAL_EVENT.value
    assert state["channel"].sent  # actually sent


# ---------------------------------------------------------------------------
# Voice path integration
# ---------------------------------------------------------------------------


def test_voice_failure_downgrades_to_text():
    """Scheduler with a voice_service whose generate_voice raises — send
    still goes through with text only, audit records voice_error and
    delivery='text' (downgrade from voice_neutral)."""
    from echovessel.proactive.execution.delivery import VoiceTransientError

    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        recent_messages=[
            FakeMessage(
                content="long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )
    channel = FakeChannel(
        name="web", channel_id="web", supports_audio=True
    )
    voice = FakeVoiceService(_raise=VoiceTransientError)

    scheduler, state = _build_scheduler(
        memory=memory,
        channel=channel,
        voice_service=voice,
        persona=FakePersonaView(voice_enabled_value=True, voice_id_value="vid_123"),
        clock=lambda: now,
    )
    decision = _run(scheduler.tick_once())
    assert decision.action == ActionType.SEND.value
    # Text went through the channel
    assert state["channel"].sent == ["hi user, how are you today"]
    # Audit shows the voice error + downgraded delivery
    audited = state["audit"].recorded[-1]
    assert audited.voice_used is False
    assert audited.voice_error == "VoiceTransientError"
    assert audited.delivery == "text"

"""Round2 · review Check 3 + R1 · delivery inherits from persona.voice_enabled.

Covers tracker §2.3 #4 and #5:

    test_generator_delivery_voice_enabled  — persona.voice_enabled=True
                                              → delivery='voice_neutral'
                                              + generate_voice called
    test_generator_delivery_voice_disabled — persona.voice_enabled=False
                                              → delivery='text'
                                              + generate_voice NOT called

These live at the scheduler-tick level because the delivery field
population happens when the scheduler stitches together the generator
output + the persona view + the delivery router. Lower-level tests of
the same behaviour are in test_delivery.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from echovessel.core.types import MessageRole
from echovessel.proactive.core.base import ActionType, TriggerReason
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


def _build_with_persona(persona: FakePersonaView, voice_service: FakeVoiceService):
    now = datetime(2026, 4, 15, 12, 0)
    memory = InMemoryMemoryApi(
        core_blocks=[FakeCoreBlock(content="温暖")],
        recent_messages=[
            FakeMessage(
                content="long ago",
                role="user",
                created_at=now - timedelta(hours=72),
            )
        ],
    )
    audit = FakeAuditSink()
    channel = FakeChannel(name="web", channel_id="web")
    registry = FakeChannelRegistry(channels=[channel])
    cfg = ProactiveConfig(
        persona_id="p",
        user_id="u",
        long_silence_hours=48,
    )

    scheduler = DefaultScheduler(
        config=cfg,
        memory=memory,
        audit=audit,
        policy=PolicyEngine(
            config=cfg,
            audit=audit,
            memory=memory,
            is_turn_in_flight=lambda: False,
        ),
        generator=MessageGenerator(
            memory=memory,
            proactive_fn=make_fake_proactive_fn(
                text="hey, thinking of you today"
            ),
        ),
        delivery=DeliveryRouter(
            memory=memory,
            channel_registry=registry,
            voice_service=voice_service,
        ),
        queue=ProactiveEventQueue(max_events=cfg.max_events_in_queue),
        persona=persona,
        clock=lambda: now,
    )
    return scheduler, {
        "memory": memory,
        "audit": audit,
        "channel": channel,
        "voice_service": voice_service,
    }


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The two tracker-mandated tests
# ---------------------------------------------------------------------------


def test_generator_delivery_voice_enabled():
    """persona.voice_enabled=True + voice_id set + voice_service present
    → scheduler calls generate_voice with tone_hint='neutral', sets
    decision.delivery='voice_neutral', and audit records voice_used=True.

    Also confirms the 'order invariant': ingest_message ran BEFORE
    generate_voice, and the message_id passed to generate_voice is the
    L2 row id from the ingest (spec §6.2b)."""
    voice = FakeVoiceService()
    persona = FakePersonaView(
        voice_enabled_value=True, voice_id_value="vid_abc"
    )
    scheduler, state = _build_with_persona(persona, voice)

    decision = _run(scheduler.tick_once())

    assert decision.action == ActionType.SEND.value
    assert decision.trigger == TriggerReason.LONG_SILENCE.value
    assert decision.delivery == "voice_neutral"

    # generate_voice was called exactly once
    assert voice.call_count == 1
    assert voice.last_call is not None
    assert voice.last_call["voice_id"] == "vid_abc"
    assert voice.last_call["tone_hint"] == "neutral"
    # message_id passed to generate_voice == the ingested L2 row id
    ingested = state["memory"].ingested
    assert len(ingested) == 1
    assert ingested[0].role == MessageRole.PERSONA.value
    assert voice.last_call["message_id"] == ingested[0].id

    # Audit reflects the voice_neutral delivery
    audited = state["audit"].recorded[-1]
    assert audited.delivery == "voice_neutral"
    assert audited.voice_used is True
    assert audited.voice_error is None

    # Channel still got the text payload (current text-only Channel protocol)
    assert state["channel"].sent == ["hey, thinking of you today"]


def test_generator_delivery_voice_disabled():
    """persona.voice_enabled=False → delivery='text', generate_voice is
    NOT called, audit records voice_used=False. This is the Check 3
    main case — proactive NEVER second-guesses the toggle."""
    voice = FakeVoiceService()
    persona = FakePersonaView(
        voice_enabled_value=False, voice_id_value="vid_abc"
    )
    scheduler, state = _build_with_persona(persona, voice)

    decision = _run(scheduler.tick_once())

    assert decision.action == ActionType.SEND.value
    assert decision.delivery == "text"

    # generate_voice must NOT have been called
    assert voice.call_count == 0

    audited = state["audit"].recorded[-1]
    assert audited.delivery == "text"
    assert audited.voice_used is False
    assert audited.voice_error is None

    # Channel still got the text
    assert state["channel"].sent == ["hey, thinking of you today"]


# ---------------------------------------------------------------------------
# Edge cases guarding against regressions
# ---------------------------------------------------------------------------


def test_delivery_defaults_to_text_when_persona_view_absent():
    """If no PersonaView is injected at all (legacy construction),
    delivery defaults to 'text'. This matches the 'never voice without
    explicit opt-in' spirit of Check 3."""
    voice = FakeVoiceService()
    scheduler, state = _build_with_persona(persona=None, voice_service=voice)  # type: ignore[arg-type]

    decision = _run(scheduler.tick_once())

    assert decision.action == ActionType.SEND.value
    assert decision.delivery == "text"
    assert voice.call_count == 0


def test_delivery_text_when_voice_enabled_true_but_no_voice_service():
    """persona wants voice but ops-side voice_service is not injected →
    graceful 'text' delivery. Covers the RT round-3 rollout window
    where persona config enables voice before VoiceService is wired."""
    persona = FakePersonaView(
        voice_enabled_value=True, voice_id_value="vid_abc"
    )
    scheduler, state = _build_with_persona(persona, voice_service=None)  # type: ignore[arg-type]

    decision = _run(scheduler.tick_once())

    assert decision.action == ActionType.SEND.value
    assert decision.delivery == "text"


def test_delivery_text_when_voice_enabled_true_but_no_voice_id():
    """persona.voice_enabled=True but persona.voice_id=None → text,
    generate_voice not called (persona has not yet cloned a voice)."""
    voice = FakeVoiceService()
    persona = FakePersonaView(voice_enabled_value=True, voice_id_value=None)
    scheduler, state = _build_with_persona(persona, voice)

    decision = _run(scheduler.tick_once())

    assert decision.action == ActionType.SEND.value
    assert decision.delivery == "text"
    assert voice.call_count == 0

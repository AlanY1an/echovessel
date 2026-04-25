"""DeliveryRouter tests — channel pick + voice path + graceful downgrades."""

from __future__ import annotations

import asyncio
from datetime import datetime

from echovessel.proactive.execution.delivery import (
    DeliveryRouter,
    VoiceBudgetError,
    VoicePermanentError,
    VoiceTransientError,
)
from tests.proactive.fakes import (
    FakeChannel,
    FakeChannelRegistry,
    FakeMessage,
    FakeVoiceService,
    InMemoryMemoryApi,
)


def _router(
    *,
    channels: list[FakeChannel],
    voice_service=None,
    messages: list[FakeMessage] | None = None,
) -> DeliveryRouter:
    memory = InMemoryMemoryApi(recent_messages=messages or [])
    registry = FakeChannelRegistry(channels=list(channels))
    return DeliveryRouter(
        memory=memory,
        channel_registry=registry,
        voice_service=voice_service,
    )


# ---------------------------------------------------------------------------
# Channel selection (spec §6.1)
# ---------------------------------------------------------------------------


def test_pick_channel_prefers_user_recent_activity():
    web = FakeChannel(name="web", channel_id="web")
    discord = FakeChannel(name="discord:g1", channel_id="discord:g1")
    router = _router(
        channels=[web, discord],
        messages=[
            FakeMessage(
                content="hi from discord",
                role="user",
                channel_id="discord:g1",
                created_at=datetime(2026, 4, 15, 11, 0),
            )
        ],
    )
    pick = router.pick_channel(persona_id="p", user_id="u")
    assert pick.channel is discord
    assert pick.reason == "recent_user_activity"


def test_pick_channel_falls_back_to_default_web():
    web = FakeChannel(name="web", channel_id="web")
    other = FakeChannel(name="discord:g1", channel_id="discord:g1")
    router = _router(channels=[other, web])
    pick = router.pick_channel(persona_id="p", user_id="u")
    assert pick.channel is web
    assert pick.reason == "default_channel"


def test_pick_channel_no_enabled_returns_none():
    router = _router(channels=[])
    pick = router.pick_channel(persona_id="p", user_id="u")
    assert pick.channel is None
    assert pick.reason == "no_enabled_channel"


def test_pick_channel_skips_non_pushable():
    web = FakeChannel(name="web", channel_id="web", supports_outgoing_push=False)
    discord = FakeChannel(name="discord:g1", channel_id="discord:g1")
    router = _router(channels=[web, discord])
    pick = router.pick_channel(persona_id="p", user_id="u")
    assert pick.channel is discord  # web got filtered out


def test_pick_channel_memory_failure_degrades_gracefully():
    web = FakeChannel(name="web", channel_id="web")
    memory = InMemoryMemoryApi()

    def _boom(*args, **kwargs):
        raise OSError("memory broken")

    memory.list_recall_messages = _boom  # type: ignore[assignment]
    registry = FakeChannelRegistry(channels=[web])
    router = DeliveryRouter(memory=memory, channel_registry=registry)
    pick = router.pick_channel(persona_id="p", user_id="u")
    # With no recent-activity hint, falls back to default_channel
    assert pick.channel is web
    assert pick.reason == "default_channel"


# ---------------------------------------------------------------------------
# Voice path (v0.2 — spec §6.2a + §4.7a generate_voice facade · review M5 + Check 3)
# ---------------------------------------------------------------------------


def _run_voice(
    router: DeliveryRouter,
    *,
    persona_voice_enabled: bool = True,
    persona_voice_id: str | None = "vid_123",
    message_id: int = 100,
):
    return asyncio.run(
        router.prepare_voice(
            text="hello",
            message_id=message_id,
            persona_voice_enabled=persona_voice_enabled,
            persona_voice_id=persona_voice_id,
        )
    )


def test_voice_enabled_false_returns_text():
    """Spec §6.2a single source of truth: persona.voice_enabled=False
    unconditionally yields delivery='text', regardless of what voice_service
    is. This is the review Check 3 main switch."""
    voice = FakeVoiceService()
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router, persona_voice_enabled=False)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert outcome.voice_result is None
    # voice_service must NOT have been called
    assert voice.call_count == 0


def test_voice_service_none_returns_text():
    """No voice_service injection → graceful text fallback."""
    router = _router(channels=[], voice_service=None)
    outcome = _run_voice(router)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False


def test_voice_no_voice_id_returns_text():
    """Persona has voice_enabled=True but no voice_id configured →
    text fallback (graceful, no crash)."""
    voice = FakeVoiceService()
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router, persona_voice_id=None)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert voice.call_count == 0


def test_voice_happy_path_calls_generate_voice():
    """voice_enabled + voice_id + voice_service → generate_voice runs
    with tone_hint='neutral' and delivery='voice_neutral'."""
    voice = FakeVoiceService()
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(
        router,
        persona_voice_enabled=True,
        persona_voice_id="vid_abc",
        message_id=42,
    )
    assert outcome.delivery == "voice_neutral"
    assert outcome.voice_used is True
    assert outcome.voice_result is not None
    assert voice.call_count == 1
    assert voice.last_call == {
        "text": "hello",
        "voice_id": "vid_abc",
        "message_id": 42,
        "tone_hint": "neutral",
    }


def test_voice_transient_error_downgrades():
    voice = FakeVoiceService(_raise=VoiceTransientError)
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert outcome.voice_error == "VoiceTransientError"


def test_voice_permanent_error_downgrades():
    voice = FakeVoiceService(_raise=VoicePermanentError)
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert outcome.voice_error == "VoicePermanentError"


def test_voice_budget_error_downgrades():
    voice = FakeVoiceService(_raise=VoiceBudgetError)
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert outcome.voice_error == "VoiceBudgetError"


def test_voice_unexpected_exception_downgrades():
    """Non-voice exceptions from generate_voice (e.g. RuntimeError) also
    downgrade rather than propagate — spec §6.3 says the caller must
    still be able to publish the message."""
    voice = FakeVoiceService(_raise=RuntimeError)
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router)
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert outcome.voice_error == "RuntimeError"


def test_voice_empty_voice_id_returns_text():
    """Empty-string voice_id is treated the same as None."""
    voice = FakeVoiceService()
    router = _router(channels=[], voice_service=voice)
    outcome = _run_voice(router, persona_voice_id="")
    assert outcome.delivery == "text"
    assert outcome.voice_used is False
    assert voice.call_count == 0

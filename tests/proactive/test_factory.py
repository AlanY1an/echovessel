"""Factory smoke tests — build_proactive_scheduler wiring."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from echovessel.proactive import (
    DefaultScheduler,
    ProactiveConfig,
    ProactiveScheduler,
    build_proactive_scheduler,
)
from echovessel.proactive.core.base import ProactiveDecision
from echovessel.proactive.core.errors import ProactivePermanentError
from tests.proactive.fakes import (
    FakeChannelRegistry,
    FakePersonaView,
    FakeVoiceService,
    InMemoryMemoryApi,
    make_fake_proactive_fn,
)


class _StubAuditSink:
    """Minimal AuditSink for factory smoke tests — records nothing."""

    def __init__(self) -> None:
        self.recorded: list[ProactiveDecision] = []

    def record(self, decision: ProactiveDecision) -> None:
        self.recorded.append(decision)

    def update_latest(self, decision_id: str, **kwargs: Any) -> None:
        pass

    def recent_sends(self, *, last_n: int) -> list[ProactiveDecision]:
        return []

    def count_sends_in_last_24h(self, *, now: datetime) -> int:
        return 0


def _cfg(**overrides) -> ProactiveConfig:
    base = {
        "persona_id": "p",
        "user_id": "u",
        "enabled": True,
    }
    base.update(overrides)
    return ProactiveConfig(**base)


def test_build_minimal():
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        audit_sink=_StubAuditSink(),
    )
    assert isinstance(scheduler, ProactiveScheduler)
    assert isinstance(scheduler, DefaultScheduler)


def test_build_with_voice():
    voice = FakeVoiceService()
    persona = FakePersonaView(voice_enabled_value=True, voice_id_value="vid_123")
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        persona=persona,
        voice_service=voice,
        audit_sink=_StubAuditSink(),
    )
    assert scheduler.persona is persona
    assert scheduler.persona.voice_id == "vid_123"
    assert scheduler.persona.voice_enabled is True
    assert scheduler.delivery.voice_service is voice


def test_build_without_voice_service_is_allowed():
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        voice_service=None,
        audit_sink=_StubAuditSink(),
    )
    assert scheduler.delivery.voice_service is None


def test_build_rejects_non_config_object():
    with pytest.raises(ProactivePermanentError):
        build_proactive_scheduler(
            config="not a config",  # type: ignore[arg-type]
            memory_api=InMemoryMemoryApi(),
            channel_registry=FakeChannelRegistry(),
            proactive_fn=make_fake_proactive_fn(),
            audit_sink=_StubAuditSink(),
        )


def test_build_respects_config_max_events_in_queue():
    scheduler = build_proactive_scheduler(
        config=_cfg(max_events_in_queue=16),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        audit_sink=_StubAuditSink(),
    )
    assert scheduler.queue.max_events == 16


def test_build_injects_clock():
    marker = datetime(2026, 4, 15, 12, 0)
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        clock=lambda: marker,
        audit_sink=_StubAuditSink(),
    )
    assert scheduler._now() == marker

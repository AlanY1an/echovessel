"""Factory smoke tests — build_proactive_scheduler wiring."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from echovessel.proactive import (
    DefaultScheduler,
    ProactiveConfig,
    ProactiveScheduler,
    build_proactive_scheduler,
)
from echovessel.proactive.core.errors import ProactivePermanentError
from tests.proactive.fakes import (
    FakeChannelRegistry,
    FakePersonaView,
    FakeVoiceService,
    InMemoryMemoryApi,
    make_fake_proactive_fn,
)


def _cfg(**overrides) -> ProactiveConfig:
    base = {
        "persona_id": "p",
        "user_id": "u",
        "enabled": True,
        "tick_interval_seconds": 60,
    }
    base.update(overrides)
    return ProactiveConfig(**base)


def test_build_minimal(tmp_path: Path):
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        log_dir=tmp_path,
    )
    assert isinstance(scheduler, ProactiveScheduler)
    assert isinstance(scheduler, DefaultScheduler)


def test_build_with_voice(tmp_path: Path):
    voice = FakeVoiceService()
    persona = FakePersonaView(voice_enabled_value=True, voice_id_value="vid_123")
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        persona=persona,
        voice_service=voice,
        log_dir=tmp_path,
    )
    assert scheduler.persona is persona
    assert scheduler.persona.voice_id == "vid_123"
    assert scheduler.persona.voice_enabled is True
    assert scheduler.delivery.voice_service is voice


def test_build_without_voice_service_is_allowed(tmp_path: Path):
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        voice_service=None,
        log_dir=tmp_path,
    )
    assert scheduler.delivery.voice_service is None


def test_build_rejects_non_config_object():
    with pytest.raises(ProactivePermanentError):
        build_proactive_scheduler(
            config="not a config",  # type: ignore[arg-type]
            memory_api=InMemoryMemoryApi(),
            channel_registry=FakeChannelRegistry(),
            proactive_fn=make_fake_proactive_fn(),
        )


def test_build_respects_config_max_events_in_queue(tmp_path: Path):
    scheduler = build_proactive_scheduler(
        config=_cfg(max_events_in_queue=16),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        log_dir=tmp_path,
    )
    assert scheduler.queue.max_events == 16


def test_build_injects_clock(tmp_path: Path):
    marker = datetime(2026, 4, 15, 12, 0)
    scheduler = build_proactive_scheduler(
        config=_cfg(),
        memory_api=InMemoryMemoryApi(),
        channel_registry=FakeChannelRegistry(),
        proactive_fn=make_fake_proactive_fn(),
        clock=lambda: marker,
        log_dir=tmp_path,
    )
    assert scheduler._now() == marker


def test_config_rejects_memory_db_audit_sink():
    with pytest.raises(ValueError):
        _cfg(audit_sink="memory_db")

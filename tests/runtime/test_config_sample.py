"""Guard: the shipped config.toml.sample must parse through the real
config loader. ``echovessel init`` copies the sample verbatim, so any
field the Pydantic models no longer accept (every section is
``extra="forbid"``) would crash first boot for a fresh user."""

from importlib import resources

import pytest

from echovessel.runtime.config import Config, load_config_from_str


@pytest.fixture
def sample_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars the sample's enabled sections require at validation
    time: [llm] (openai_compat) and [voice] (fishaudio TTS + whisper
    STT) check their api_key_env on load."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FISH_AUDIO_KEY", "test-key")


def _sample_text() -> str:
    return (
        resources.files("echovessel.resources")
        .joinpath("config.toml.sample")
        .read_text(encoding="utf-8")
    )


def test_sample_config_loads_through_real_loader(sample_env: None) -> None:
    config = load_config_from_str(_sample_text())
    assert isinstance(config, Config)


def test_sample_proactive_section_matches_model(sample_env: None) -> None:
    """The [proactive] table must only carry keys ProactiveSection
    accepts — extra keys are rejected at load (extra_forbid)."""
    config = load_config_from_str(_sample_text())
    assert config.proactive.enabled is False
    assert config.proactive.max_per_24h == 3
    assert config.proactive.max_events_in_queue == 64
    assert config.proactive.stop_grace_seconds == 10

"""Config loading / validation tests."""

from __future__ import annotations

import pytest

from echovessel.runtime.config import Config, load_config_from_str

MINIMAL_TOML = """
[persona]
id = "default"
display_name = "Test Persona"

[llm]
provider = "stub"
api_key_env = ""
"""


def test_minimal_config_loads():
    cfg = load_config_from_str(MINIMAL_TOML)
    assert isinstance(cfg, Config)
    assert cfg.persona.id == "default"
    assert cfg.llm.provider == "stub"
    assert cfg.memory.retrieve_k == 10
    assert cfg.runtime.log_level == "info"
    assert cfg.idle_scanner.interval_seconds == 60.0


def test_openai_compat_default_zero_config(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    toml = """
[persona]
id = "default"
display_name = "A"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"
"""
    cfg = load_config_from_str(toml)
    assert cfg.llm.provider == "openai_compat"
    assert cfg.llm.base_url is None
    assert cfg.llm.model is None
    assert cfg.llm.tier_models == {}


def test_missing_env_var_fails(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"
"""
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_config_from_str(toml)


def test_custom_base_url_without_model_fails(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"
base_url = "https://openrouter.ai/api/v1"
"""
    with pytest.raises(ValueError, match="custom base_url"):
        load_config_from_str(toml)


def test_custom_base_url_with_pinned_model_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-sonnet-4"
"""
    cfg = load_config_from_str(toml)
    assert cfg.llm.model == "anthropic/claude-sonnet-4"


def test_unknown_tier_name_fails(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "openai_compat"
api_key_env = "OPENAI_API_KEY"

[llm.tier_models]
huge = "gpt-foo"
"""
    with pytest.raises(ValueError, match="Unknown tier names"):
        load_config_from_str(toml)


def test_local_ollama_base_url_no_key_needed():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "openai_compat"
api_key_env = ""
base_url = "http://localhost:11434/v1"

[llm.tier_models]
small = "llama3:8b"
medium = "llama3:70b"
large = "llama3:70b"
"""
    cfg = load_config_from_str(toml)
    assert cfg.llm.base_url == "http://localhost:11434/v1"


def test_extra_field_rejected():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[runtime]
bogus = "field"
"""
    with pytest.raises(ValueError):
        load_config_from_str(toml)


def test_tier_models_partial_allowed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

[llm.tier_models]
large = "claude-opus-4-6"
"""
    cfg = load_config_from_str(toml)
    assert cfg.llm.tier_models == {"large": "claude-opus-4-6"}


# ---------------------------------------------------------------------------
# Round 2 · Persona voice binding
# ---------------------------------------------------------------------------


def test_persona_voice_id_defaults_to_none():
    cfg = load_config_from_str(MINIMAL_TOML)
    assert cfg.persona.voice_id is None
    assert cfg.persona.voice_provider is None


def test_persona_voice_id_set():
    toml = """
[persona]
id = "default"
display_name = "Test"
voice_id = "fishmodel_xxx"
voice_provider = "fishaudio"

[llm]
provider = "stub"
api_key_env = ""
"""
    cfg = load_config_from_str(toml)
    assert cfg.persona.voice_id == "fishmodel_xxx"
    assert cfg.persona.voice_provider == "fishaudio"


# ---------------------------------------------------------------------------
# Round 2 · VoiceSection
# ---------------------------------------------------------------------------


def test_voice_section_defaults_disabled():
    cfg = load_config_from_str(MINIMAL_TOML)
    assert cfg.voice.enabled is False
    assert cfg.voice.tts_provider == "fishaudio"
    assert cfg.voice.stt_provider == "whisper_api"
    assert cfg.voice.default_audio_format == "mp3"


def test_voice_enabled_requires_tts_env_var(monkeypatch):
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[voice]
enabled = true
tts_provider = "fishaudio"
stt_provider = "stub"
"""
    with pytest.raises(ValueError, match="FISH_API_KEY"):
        load_config_from_str(toml)


def test_voice_enabled_requires_stt_env_var(monkeypatch):
    monkeypatch.setenv("FISH_API_KEY", "fk")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[voice]
enabled = true
tts_provider = "fishaudio"
stt_provider = "whisper_api"
"""
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        load_config_from_str(toml)


def test_voice_enabled_stub_stub_no_env_needed(monkeypatch):
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[voice]
enabled = true
tts_provider = "stub"
stt_provider = "stub"
"""
    cfg = load_config_from_str(toml)
    assert cfg.voice.enabled is True
    assert cfg.voice.tts_provider == "stub"


def test_voice_disabled_skips_env_var_checks(monkeypatch):
    """`enabled=false` must not block on missing env vars."""
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[voice]
enabled = false
tts_provider = "fishaudio"
stt_provider = "whisper_api"
"""
    cfg = load_config_from_str(toml)
    assert cfg.voice.enabled is False


def test_voice_unknown_provider_rejected():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[voice]
tts_provider = "bogus"
"""
    with pytest.raises(ValueError):
        load_config_from_str(toml)


# ---------------------------------------------------------------------------
# Round 2 · ProactiveSection extension
# ---------------------------------------------------------------------------


def test_proactive_defaults_match_spec():
    cfg = load_config_from_str(MINIMAL_TOML)
    assert cfg.proactive.enabled is False
    assert cfg.proactive.tick_interval_seconds == 60
    assert cfg.proactive.quiet_hours_start == 23
    assert cfg.proactive.quiet_hours_end == 7
    assert cfg.proactive.max_per_24h == 3
    assert cfg.proactive.cold_user_threshold == 2
    assert cfg.proactive.cold_user_response_window_hours == 6
    assert cfg.proactive.long_silence_hours == 48
    assert cfg.proactive.max_events_in_queue == 64
    assert cfg.proactive.use_voice_when_available is True
    assert cfg.proactive.audit_sink == "jsonl"
    assert cfg.proactive.stop_grace_seconds == 10


def test_proactive_full_override():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[proactive]
enabled = true
tick_interval_seconds = 30
quiet_hours_start = 22
quiet_hours_end = 6
max_per_24h = 5
cold_user_threshold = 3
cold_user_response_window_hours = 12
long_silence_hours = 72
max_events_in_queue = 128
use_voice_when_available = false
audit_sink = "jsonl"
stop_grace_seconds = 20
"""
    cfg = load_config_from_str(toml)
    assert cfg.proactive.enabled is True
    assert cfg.proactive.tick_interval_seconds == 30
    assert cfg.proactive.max_per_24h == 5
    assert cfg.proactive.use_voice_when_available is False


def test_proactive_memory_db_audit_sink_rejected():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[proactive]
enabled = true
audit_sink = "memory_db"
"""
    # Runtime section still accepts "memory_db" at the Literal level;
    # the ProactiveConfig-side validator rejects it in to_proactive_config.
    cfg = load_config_from_str(toml)
    with pytest.raises(ValueError, match="v1.0 only"):
        cfg.proactive.to_proactive_config(persona_id="x")


def test_proactive_tick_interval_min_bound():
    toml = """
[persona]
id = "x"
display_name = "x"

[llm]
provider = "stub"
api_key_env = ""

[proactive]
enabled = true
tick_interval_seconds = 5
"""
    with pytest.raises(ValueError):
        load_config_from_str(toml)


def test_proactive_to_proactive_config():
    """ProactiveSection → ProactiveConfig conversion preserves all fields."""
    toml = """
[persona]
id = "alan"
display_name = "Alan"

[llm]
provider = "stub"
api_key_env = ""

[proactive]
enabled = true
max_per_24h = 5
long_silence_hours = 24
"""
    cfg = load_config_from_str(toml)
    pconfig = cfg.proactive.to_proactive_config(persona_id="alan")
    assert pconfig.enabled is True
    assert pconfig.max_per_24h == 5
    assert pconfig.long_silence_hours == 24
    assert pconfig.persona_id == "alan"
    assert pconfig.user_id == "self"


def test_proactive_to_proactive_config_explicit_user_id():
    cfg = load_config_from_str(MINIMAL_TOML)
    pconfig = cfg.proactive.to_proactive_config(persona_id="alan", user_id="alice")
    assert pconfig.persona_id == "alan"
    assert pconfig.user_id == "alice"

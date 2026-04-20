"""Config migration: legacy `llm.tier_models` → `llm.models`.

Pins the one-release deprecation path where users upgrading from the old
tier-based config keep working, with a warning, until they update their
TOML to the new role-based keys.

Mapping:
    SMALL  → fast
    MEDIUM → judge
    LARGE  → main

Three cases matter:
  A · only legacy `tier_models` present → auto-migrate, warn
  B · only new `models` present → pass through untouched, no warning
  C · both set → `models` wins, `tier_models` ignored with a warning
"""

from __future__ import annotations

import logging

import pytest

from echovessel.runtime.config import LLMSection


@pytest.fixture(autouse=True)
def _fake_openai_key(monkeypatch):
    """Ensure api_key_env validation passes for these model-loading tests."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    yield
    # monkeypatch unsets automatically


def test_legacy_tier_models_auto_migrates_to_models(caplog):
    caplog.set_level(logging.WARNING)
    cfg = LLMSection(
        provider="openai_compat",
        api_key_env="OPENAI_API_KEY",
        tier_models={"small": "gpt-4o-mini", "medium": "gpt-4o", "large": "gpt-4o"},
    )
    # Post-migration the `models` field reflects the role-keyed mapping.
    assert cfg.models == {"fast": "gpt-4o-mini", "judge": "gpt-4o", "main": "gpt-4o"}
    # Legacy field retains original content (we don't wipe it, just mirror).
    assert cfg.tier_models == {"small": "gpt-4o-mini", "medium": "gpt-4o", "large": "gpt-4o"}
    assert any("tier_models is deprecated" in r.getMessage() for r in caplog.records)


def test_new_models_passthrough_no_warning(caplog):
    caplog.set_level(logging.WARNING)
    cfg = LLMSection(
        provider="openai_compat",
        api_key_env="OPENAI_API_KEY",
        models={"fast": "gpt-4o-mini", "main": "gpt-4o"},
    )
    assert cfg.models == {"fast": "gpt-4o-mini", "main": "gpt-4o"}
    assert cfg.tier_models == {}
    assert not any("tier_models" in r.getMessage() for r in caplog.records)


def test_both_set_models_wins_tier_ignored(caplog):
    caplog.set_level(logging.WARNING)
    cfg = LLMSection(
        provider="openai_compat",
        api_key_env="OPENAI_API_KEY",
        models={"fast": "gpt-4o-mini", "main": "gpt-4o"},
        tier_models={"small": "should-be-ignored", "large": "also-ignored"},
    )
    # `models` untouched; legacy field still stored but not merged.
    assert cfg.models == {"fast": "gpt-4o-mini", "main": "gpt-4o"}
    assert any(
        "BOTH tier_models" in r.getMessage() or "ignoring tier_models" in r.getMessage()
        for r in caplog.records
    )


def test_unknown_role_in_models_raises():
    with pytest.raises(ValueError, match="Unknown role names"):
        LLMSection(
            provider="openai_compat",
            api_key_env="OPENAI_API_KEY",
            models={"huge": "gpt-4o"},
        )


def test_unknown_tier_in_tier_models_raises():
    with pytest.raises(ValueError, match="Unknown tier names"):
        LLMSection(
            provider="openai_compat",
            api_key_env="OPENAI_API_KEY",
            tier_models={"extra-large": "gpt-4o"},
        )


def test_custom_base_url_with_only_tier_models_still_validates():
    """Regression guard: after migration, a legacy custom-base_url config
    that used tier_models should still pass the 'custom base_url requires
    explicit model/models' check."""
    cfg = LLMSection(
        provider="openai_compat",
        api_key_env="OPENAI_API_KEY",
        base_url="http://localhost:11434/v1",
        tier_models={"small": "llama3:8b", "large": "llama3:70b"},
    )
    assert cfg.models["fast"] == "llama3:8b"
    assert cfg.models["main"] == "llama3:70b"


def test_empty_api_key_env_with_local_base_url_passes(monkeypatch):
    """Sanity: local endpoints without api_key_env still don't require
    the env var to exist."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LLMSection(
        provider="openai_compat",
        api_key_env="",
        base_url="http://localhost:11434/v1",
        models={"fast": "llama3:8b", "main": "llama3:70b"},
    )
    assert cfg.models["fast"] == "llama3:8b"

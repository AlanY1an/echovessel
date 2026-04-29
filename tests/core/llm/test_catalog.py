from echovessel.core.llm.catalog import PRESETS, PresetEntry, presets_for


def test_presets_have_expected_anthropic_entries() -> None:
    ids = [e.model for e in PRESETS if e.provider == "anthropic"]
    assert "claude-haiku-4-5" in ids
    assert "claude-sonnet-4-6" in ids
    assert "claude-opus-4-7" in ids


def test_presets_have_expected_openai_entries() -> None:
    ids = [e.model for e in PRESETS if e.provider == "openai_compat"]
    assert "gpt-4o-mini" in ids
    assert "gpt-4o" in ids


def test_presets_for_filters_by_provider() -> None:
    items = presets_for("anthropic")
    assert all(e.provider == "anthropic" for e in items)
    assert len(items) >= 3


def test_presets_for_unknown_provider_returns_empty() -> None:
    assert presets_for("nonexistent") == ()


def test_preset_entry_has_display_name() -> None:
    sonnet = next(
        e for e in PRESETS if e.provider == "anthropic" and e.model == "claude-sonnet-4-6"
    )
    assert isinstance(sonnet, PresetEntry)
    assert sonnet.display_name == "Claude Sonnet 4.6"

"""Curated preset list of (provider, model) for the admin dropdown.

This module ONLY decides what shows up in the picker — it carries no
pricing. Cost is computed via `core.llm.prices.lookup_price`, which
reads vendored LiteLLM rates and covers far more model strings than the
preset list (including ones a user types via the "Custom…" option).

Keep this list short on purpose: it represents "the models a typical
EchoVessel user would actually pick", not "every model that exists".
The Custom… escape hatch in the UI plus LiteLLM's full price table
handle the long tail.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PresetEntry:
    provider: str
    model: str
    display_name: str


PRESETS: tuple[PresetEntry, ...] = (
    PresetEntry("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5"),
    PresetEntry("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6"),
    PresetEntry("anthropic", "claude-opus-4-7", "Claude Opus 4.7"),
    PresetEntry("anthropic", "claude-opus-4-8", "Claude Opus 4.8"),
    PresetEntry("anthropic", "claude-fable-5", "Claude Fable 5"),
    PresetEntry("openai_compat", "gpt-4o-mini", "GPT-4o mini"),
    PresetEntry("openai_compat", "gpt-4o", "GPT-4o"),
)


def presets_for(provider: str) -> tuple[PresetEntry, ...]:
    return tuple(e for e in PRESETS if e.provider == provider)

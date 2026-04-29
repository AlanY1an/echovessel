"""Lookup per-model pricing from a vendored LiteLLM JSON.

Source: https://github.com/BerriAI/litellm (MIT license; see
data/LITELLM-LICENSE for the upstream notice).
Refresh: `uv run python scripts/refresh_litellm_prices.py`.

Why a vendored JSON instead of the LiteLLM SDK: we want only the
pricing data, not the gateway/proxy machinery. Vendoring keeps the
daemon offline-capable (no startup HTTP call) and avoids a heavy
runtime dep. We multiply LiteLLM's per-token rates by 1000 at load
time so the rest of EchoVessel's cost code can stay in the existing
`*_per_1k` convention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

_OUR_TO_LITELLM_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai_compat": "openai",
}


@dataclass(frozen=True)
class ModelPrice:
    input_per_1k_usd: float
    output_per_1k_usd: float
    cache_read_per_1k_usd: float = 0.0
    cache_creation_per_1k_usd: float = 0.0


def _load_data() -> dict[str, dict[str, Any]]:
    raw_text = (
        resources.files("echovessel.runtime.llm.data").joinpath("litellm_prices.json").read_text()
    )
    raw: dict[str, dict[str, Any]] = json.loads(raw_text)
    raw.pop("sample_spec", None)
    return raw


_DATA: dict[str, dict[str, Any]] = _load_data()


def _entry_to_price(entry: dict[str, Any]) -> ModelPrice:
    return ModelPrice(
        input_per_1k_usd=float(entry.get("input_cost_per_token", 0.0)) * 1000.0,
        output_per_1k_usd=float(entry.get("output_cost_per_token", 0.0)) * 1000.0,
        cache_read_per_1k_usd=float(entry.get("cache_read_input_token_cost", 0.0)) * 1000.0,
        cache_creation_per_1k_usd=float(entry.get("cache_creation_input_token_cost", 0.0)) * 1000.0,
    )


def lookup_price(provider: str, model: str | None) -> ModelPrice | None:
    if not model:
        return None
    litellm_prov = _OUR_TO_LITELLM_PROVIDER.get(provider)
    if litellm_prov is None:
        return None
    entry = _DATA.get(model)
    if entry is None:
        return None
    if entry.get("litellm_provider") != litellm_prov:
        return None
    return _entry_to_price(entry)

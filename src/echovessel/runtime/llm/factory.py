"""build_llm_provider — factory from a config.llm section to an LLMProvider.

Called from:
- runtime/app.py at startup (§3 step 6)
- runtime/app.py on SIGHUP reload (§6.5)
- tests that want to spin up a real provider

See docs/runtime/01-spec-v0.1.md §6.2 for the provider table.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.stub import StubProvider

if TYPE_CHECKING:  # pragma: no cover
    from echovessel.runtime.config import LLMSection


def build_llm_provider(cfg: LLMSection) -> LLMProvider:
    """Build an LLMProvider instance from a validated config section.

    Assumes Pydantic validation (§4.4 `LLMSection._validate_provider_config`)
    has already run and the config is coherent.
    """
    provider = cfg.provider

    if provider == "stub":
        return StubProvider(
            fallback="I heard you. (This is stub mode — configure a real LLM provider in config.toml for actual conversations.)"
        )

    api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None

    if provider == "anthropic":
        from echovessel.runtime.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=api_key,
            base_url=cfg.base_url,
            pinned_model=cfg.model,
            role_models=cfg.models or None,
            default_max_tokens=cfg.max_tokens,
            default_temperature=cfg.temperature,
            default_timeout=float(cfg.timeout_seconds),
        )

    if provider == "openai_compat":
        from echovessel.runtime.llm.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=cfg.base_url,
            pinned_model=cfg.model,
            role_models=cfg.models or None,
            default_max_tokens=cfg.max_tokens,
            default_temperature=cfg.temperature,
            default_timeout=float(cfg.timeout_seconds),
        )

    raise ValueError(f"Unknown LLM provider: {provider!r}")


__all__ = ["build_llm_provider"]

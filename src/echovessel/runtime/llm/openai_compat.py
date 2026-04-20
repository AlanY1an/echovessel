"""OpenAICompatibleProvider — native `openai` SDK backed LLMProvider.

A single class that covers 15+ OpenAI-compatible endpoints by letting the
user point `base_url` wherever they want:

    OpenAI official / OpenRouter / Ollama / LM Studio / llama.cpp server /
    vLLM / DeepSeek / Together / Groq / Fireworks / xAI / Perplexity /
    Moonshot / 智谱 GLM / ...

See docs/runtime/01-spec-v0.1.md §6.2 / §6.2.1 / §6.2.2 / §6.2.3.

Default tier → model mapping is applied ONLY when the base_url is OpenAI
official (api.openai.com). For any other endpoint the user MUST supply
`llm.model` or `llm.tier_models` explicitly — we don't ship long-tail maps.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping

from echovessel.runtime.llm.base import LLMTier
from echovessel.runtime.llm.errors import (
    LLMBudgetError,
    LLMPermanentError,
    LLMTransientError,
)
from echovessel.runtime.llm.usage import Usage

log = logging.getLogger(__name__)

_OPENAI_OFFICIAL_DEFAULTS: dict[LLMTier, str] = {
    LLMTier.SMALL: "gpt-4o-mini",
    LLMTier.MEDIUM: "gpt-4o",
    LLMTier.LARGE: "gpt-4o",
}

_OFFICIAL_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleProvider:
    """Wraps `openai.AsyncOpenAI` with the LLMProvider Protocol.

    Uses chat.completions.create under the hood, which is the OpenAI-native
    path and is the one every compatible endpoint implements.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        pinned_model: str | None = None,
        tier_models: Mapping[str, str] | None = None,
        default_max_tokens: int = 1024,
        default_temperature: float = 0.7,
        default_timeout: float = 60.0,
    ) -> None:
        self._pinned_model = pinned_model
        self._tier_models: dict[LLMTier, str] = {}
        if tier_models:
            for k, v in tier_models.items():
                try:
                    self._tier_models[LLMTier(k)] = v
                except ValueError:
                    raise ValueError(
                        f"Unknown tier in tier_models: {k!r} "
                        f"(expected one of {[t.value for t in LLMTier]})"
                    ) from None

        self._base_url_actual = base_url or _OFFICIAL_BASE_URL
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._default_timeout = default_timeout

        # Resolve tier → model at construction time so misconfigs fail fast.
        is_official = _is_official_openai(self._base_url_actual)
        self._resolved_defaults: dict[LLMTier, str] = {}
        for tier in LLMTier:
            if self._pinned_model:
                self._resolved_defaults[tier] = self._pinned_model
            elif tier in self._tier_models:
                self._resolved_defaults[tier] = self._tier_models[tier]
            elif is_official:
                self._resolved_defaults[tier] = _OPENAI_OFFICIAL_DEFAULTS[tier]
            else:
                raise ValueError(
                    f"OpenAICompatibleProvider: cannot resolve model for tier "
                    f"{tier.value!r}. Custom base_url={base_url!r} has no "
                    f"built-in defaults; set `llm.model` or "
                    f"`llm.tier_models.{tier.value}` in config."
                )

        self._api_key = api_key
        self._base_url_kwarg = base_url
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "openai SDK not installed. Install the [llm] extra: "
                "`uv sync --extra llm` or `pip install openai>=1.30`."
            ) from e
        client_kwargs: dict[str, object] = {}
        # OpenAI-compatible endpoints that don't require auth (Ollama, etc.)
        # still want *some* value in api_key, otherwise the SDK raises at
        # client-construction time. Use the documented placeholder.
        client_kwargs["api_key"] = self._api_key or "sk-no-key-required"
        if self._base_url_kwarg:
            client_kwargs["base_url"] = self._base_url_kwarg
        self._client = AsyncOpenAI(**client_kwargs)
        return self._client

    @property
    def provider_name(self) -> str:
        return "openai_compat"

    @property
    def base_url(self) -> str:
        return self._base_url_actual

    def model_for(self, tier: LLMTier) -> str:
        return self._resolved_defaults[tier]

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> tuple[str, Usage | None]:
        model = self.model_for(tier)
        client = self._get_client()
        try:
            resp = await client.chat.completions.create(  # type: ignore[attr-defined]
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens or self._default_max_tokens,
                temperature=temperature,
                timeout=timeout or self._default_timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise _classify_openai_error(e) from e

        choices = getattr(resp, "choices", None) or []
        if not choices:
            return "", None
        msg = choices[0].message
        content = getattr(msg, "content", None)
        raw_usage = getattr(resp, "usage", None)
        usage: Usage | None = None
        if raw_usage is not None:
            details = getattr(raw_usage, "prompt_tokens_details", None)
            usage = Usage(
                input_tokens=raw_usage.prompt_tokens,
                output_tokens=raw_usage.completion_tokens,
                cache_read_input_tokens=getattr(details, "cached_tokens", 0) or 0,
            )
        return content or "", usage

    async def stream(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str | Usage]:
        model = self.model_for(tier)
        client = self._get_client()
        trailing_usage: Usage | None = None
        try:
            stream = await client.chat.completions.create(  # type: ignore[attr-defined]
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens or self._default_max_tokens,
                temperature=temperature,
                timeout=timeout or self._default_timeout,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    raw_usage = getattr(chunk, "usage", None)
                    if raw_usage is not None:
                        # Terminal chunk per OpenAI spec with stream_options.include_usage=True.
                        details = getattr(raw_usage, "prompt_tokens_details", None)
                        trailing_usage = Usage(
                            input_tokens=raw_usage.prompt_tokens,
                            output_tokens=raw_usage.completion_tokens,
                            cache_read_input_tokens=getattr(details, "cached_tokens", 0) or 0,
                        )
                    # Safely skip any other empty-choices chunk (heartbeat, proxy artifact).
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta else None
                if content:
                    yield content
        except Exception as e:  # noqa: BLE001
            raise _classify_openai_error(e) from e
        if trailing_usage is not None:
            yield trailing_usage


def _is_official_openai(url: str) -> bool:
    return "api.openai.com" in url


def _classify_openai_error(e: Exception) -> Exception:
    status = getattr(e, "status_code", None)
    cls_name = e.__class__.__name__
    if status is None:
        if any(
            hint in cls_name for hint in ("Timeout", "Connection", "APIError", "Network")
        ):
            return LLMTransientError(f"{cls_name}: {e}")
        return LLMPermanentError(f"{cls_name}: {e}")

    if status == 429:
        return LLMTransientError(f"rate limited: {e}")
    if status >= 500:
        return LLMTransientError(f"server error {status}: {e}")
    if status in (401, 403):
        return LLMPermanentError(f"auth error {status}: {e}")
    if status == 402:
        return LLMBudgetError(f"budget/quota error {status}: {e}")
    return LLMPermanentError(f"client error {status}: {e}")


def build_openai_compat_from_env(
    *,
    api_key_env: str,
    base_url: str | None = None,
    pinned_model: str | None = None,
    tier_models: Mapping[str, str] | None = None,
    **kwargs: object,
) -> OpenAICompatibleProvider:
    api_key = os.environ.get(api_key_env) if api_key_env else None
    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=base_url,
        pinned_model=pinned_model,
        tier_models=tier_models,
        **kwargs,  # type: ignore[arg-type]
    )


__all__ = ["OpenAICompatibleProvider", "build_openai_compat_from_env"]

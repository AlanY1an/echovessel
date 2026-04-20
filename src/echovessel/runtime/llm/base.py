"""LLMProvider Protocol and model-role semantics.

See docs/runtime/01-spec-v0.1.md §6.1 and §6.2.2.

Runtime holds ONE provider instance. Every call site declares a semantic
**model role** at the call point; the provider internally maps role → concrete
model name. The three roles recognised by the codebase are:

    "fast"   — extraction / reflection (consolidate background, cheap/fast)
    "main"   — interaction / proactive reply (user is waiting, premium quality)
    "judge"  — eval harness (strict reasoning · may fall back to "main")

This replaces the earlier ``LLMTier.SMALL/MEDIUM/LARGE`` abstraction: "tier"
implied a capability ladder that maps poorly onto real provider line-ups (no
provider actually has SMALL/MEDIUM/LARGE), whereas a *role* names the
workload so configuration is concrete: the user sets
``[llm.models] main = "claude-sonnet-4-6"`` instead of guessing which
tier the interaction handler maps to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from echovessel.runtime.llm.usage import Usage

# Canonical role names. Kept as a tuple (not an Enum) so callers can pass plain
# strings — matching how users configure them in TOML. Unknown roles fail at
# provider construction time with a ValueError rather than silently mapping to
# a default.
MODEL_ROLES: tuple[str, ...] = ("fast", "main", "judge")
DEFAULT_ROLE: str = "main"


@runtime_checkable
class LLMProvider(Protocol):
    """Async LLM provider contract.

    All methods are async. ``extract_fn`` / ``reflect_fn`` / ``turn_handler``
    share a single asyncio event loop, so any sync provider would block it.
    """

    @property
    def provider_name(self) -> str:
        """One of 'anthropic' / 'openai_compat' / 'stub'."""
        ...

    def model_for(self, model_role: str) -> str:
        """Resolve which concrete model the provider uses for a given role.

        Exposed for logging / audit / local-first disclosure; not called in
        the hot path (``complete`` / ``stream`` resolve internally).

        Raises ``ValueError`` if ``model_role`` is not one of
        ``MODEL_ROLES``.
        """
        ...

    async def complete(
        self,
        system: str,
        user: str,
        *,
        model_role: str = DEFAULT_ROLE,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> tuple[str, Usage | None]:
        """Single-shot text completion. Returns (text, usage).

        ``usage`` is None when the provider cannot report token counts
        (stub, or a provider that doesn't expose usage in its response).
        Callers MUST unpack the tuple; they MUST NOT assume ``usage`` is
        non-None.

        On transient HTTP failure (5xx, timeout, rate limit): raise
        ``LLMTransientError``. On permanent failure (4xx, auth, content
        filter): raise ``LLMPermanentError``.
        """
        ...

    async def stream(
        self,
        system: str,
        user: str,
        *,
        model_role: str = DEFAULT_ROLE,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str | Usage]:
        """Token-by-token streaming. Yields text deltas, then optionally Usage.

        Text chunks are ``str``. A trailing ``Usage`` item may appear after
        the last text chunk when the provider can report token counts
        mid-stream. Callers MUST skip non-str items or use
        ``isinstance(item, str)`` guards.

        Stub implementations MAY fall back to ``await complete()`` followed
        by one text yield (no trailing Usage).
        """
        ...


__all__ = ["LLMProvider", "Usage", "MODEL_ROLES", "DEFAULT_ROLE"]

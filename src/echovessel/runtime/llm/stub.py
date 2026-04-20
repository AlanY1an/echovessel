"""StubProvider — in-process canned-response provider for tests / EVAL.

Used by:
- tests/runtime/ and tests/eval/ to avoid real LLM calls
- `echovessel run --dry-run` flows (future)
- anywhere a Protocol-satisfying LLMProvider is needed without network

Rules (see docs/runtime/01-spec-v0.1.md §6.2):
- Pure Python, no network, no SDK dependency
- Accepts either a `canned_responses` dict keyed by (system, user) or a
  `responder` callable that returns a response per call
- `stream()` falls back to `complete()` and yields once
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping

from echovessel.runtime.llm.base import LLMProvider, LLMTier
from echovessel.runtime.llm.usage import Usage

_DEFAULT_MODEL = "stub-model"


class StubProvider:
    """Minimal in-process LLMProvider implementation.

    Three modes, chosen by what was passed in:

    1. `canned_responses={(system, user): "reply", ...}`
       Exact-match lookup on the (system, user) tuple. KeyError unless a
       `fallback` is supplied.

    2. `responder=lambda system, user, **kwargs: "reply"`
       Arbitrary callable invoked on every call. May be sync or async.

    3. `fallback="text"`
       Used when neither canned_responses has a hit nor a responder is set.
    """

    def __init__(
        self,
        *,
        canned_responses: Mapping[tuple[str, str], str] | None = None,
        responder: Callable[..., str] | Callable[..., object] | None = None,
        fallback: str | None = "",
        model_for_tier: Mapping[LLMTier, str] | None = None,
    ) -> None:
        self._canned = dict(canned_responses or {})
        self._responder = responder
        self._fallback = fallback
        self._models = dict(model_for_tier or {})

    @property
    def provider_name(self) -> str:
        return "stub"

    def model_for(self, tier: LLMTier) -> str:
        return self._models.get(tier, _DEFAULT_MODEL)

    def set_canned(self, system: str, user: str, reply: str) -> None:
        """Test helper: add/override one canned response."""
        self._canned[(system, user)] = reply

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
        if (system, user) in self._canned:
            return self._canned[(system, user)], None

        if self._responder is not None:
            result = self._responder(
                system=system,
                user=user,
                tier=tier,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            # Support async responders without forcing the test harness to
            # await twice.
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            return str(result), None

        if self._fallback is not None:
            return self._fallback, None

        raise KeyError(
            f"StubProvider has no canned response for system/user pair "
            f"(system={system[:40]!r}..., user={user[:40]!r}...) and no "
            f"fallback configured."
        )

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
        text, _usage = await self.complete(
            system,
            user,
            tier=tier,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        yield text


# Protocol sanity check: StubProvider must structurally satisfy LLMProvider.
# (runtime_checkable makes this an isinstance-based test; see test_llm_base.)
assert isinstance(StubProvider(fallback=""), LLMProvider)

__all__ = ["StubProvider"]

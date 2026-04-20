"""Token-usage envelope returned by LLMProvider calls.

Providers surface this alongside the generated text so the cost ledger
can prefer SDK-reported counts over the local cl100k_base fallback.
See docs/runtime/01-spec-v0.1.md §6.2.4 (introduced in #1).

Cache fields default to 0 and are non-zero only for providers that
report prompt-cache savings (Anthropic extended_thinking, OpenAI cached
prompt tokens). The ledger stores them separately so the admin tab can
show '(of which N cached)' breakdowns without losing the raw totals.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Usage:
    """Immutable token-usage snapshot from one LLM call.

    Fields match the superset of what Anthropic and OpenAI SDKs expose.
    Fields unavailable for a given provider are left at their default (0).
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


__all__ = ["Usage"]

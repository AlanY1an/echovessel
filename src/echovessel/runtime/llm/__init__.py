"""LLM provider abstractions used by Runtime.

Public API:

    from echovessel.runtime.llm import (
        LLMProvider, MODEL_ROLES, DEFAULT_ROLE,
        LLMError, LLMTransientError, LLMPermanentError, LLMBudgetError,
        StubProvider,
        build_llm_provider,
        Usage,
    )

Concrete providers (`AnthropicProvider`, `OpenAICompatibleProvider`) are
imported lazily via `build_llm_provider` so the `anthropic` / `openai`
packages only load when they're actually needed.
"""

from echovessel.runtime.llm.base import DEFAULT_ROLE, MODEL_ROLES, LLMProvider
from echovessel.runtime.llm.errors import (
    LLMBudgetError,
    LLMError,
    LLMPermanentError,
    LLMTransientError,
)
from echovessel.runtime.llm.factory import build_llm_provider
from echovessel.runtime.llm.stub import StubProvider
from echovessel.runtime.llm.usage import Usage

__all__ = [
    "LLMProvider",
    "MODEL_ROLES",
    "DEFAULT_ROLE",
    "LLMError",
    "LLMTransientError",
    "LLMPermanentError",
    "LLMBudgetError",
    "StubProvider",
    "build_llm_provider",
    "Usage",
]

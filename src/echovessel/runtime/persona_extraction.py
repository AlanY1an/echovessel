"""Persona facts + blocks extraction orchestration.

Thin runtime helper that glues together the prompt template in
:mod:`echovessel.prompts.persona_facts` and an ``LLMProvider`` call.
Used by the admin API for two onboarding paths:

1. **Blank-write** — user typed their own persona_block (and maybe
   others) but left facts mostly empty. We feed their prose back into
   the LLM to extract the structured fields.
2. **Import-upload** — the import pipeline already produced events +
   thoughts. We serialize those into a context string and ask for
   both blocks and facts in one call.

Both paths use the LARGE tier because the output sets the persona's
tone and the user will review every field before saving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from echovessel.prompts.persona_facts import (
    PERSONA_FACTS_SYSTEM_PROMPT,
    ExtractedFacts,
    ExtractedPersona,
    PersonaFactsParseError,
    format_persona_facts_user_prompt,
    parse_persona_facts_response,
)
from echovessel.runtime.llm.base import LLMTier

log = logging.getLogger(__name__)


# Dedicated exception so callers can tell "LLM vendor was unreachable"
# (retry) from "LLM returned garbage" (don't retry). Mirrors the same
# split used in the bootstrap route.
class PersonaExtractionError(RuntimeError):
    """Raised when extraction fails in a way the caller cannot retry around."""


class _LLMLike(Protocol):
    """Narrow view of :class:`LLMProvider` used by this module."""

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = ...,
        max_tokens: int = ...,
        temperature: float = ...,
        timeout: float | None = ...,
    ) -> str: ...


# Defensive cap on LLM output tokens. Roughly 2x the combined cap on the
# five blocks (12kB) plus the facts JSON (~1kB) — leaves generous
# headroom for verbose models without letting a runaway output flood
# memory.
_MAX_TOKENS: int = 4096

# Temperature is moderate: we want varied prose for the blocks but
# consistent structured JSON. Empirically 0.5-0.6 is the sweet spot;
# higher values cause schema drift.
_TEMPERATURE: float = 0.5


@dataclass(frozen=True, slots=True)
class ExtractionEvent:
    """One L3 event row distilled from import. Used only by import path."""

    description: str
    emotional_impact: int = 0
    relational_tags: tuple[str, ...] = ()


def format_events_thoughts_as_context(
    *,
    events: list[ExtractionEvent] | list[tuple[str, int, list[str]]],
    thoughts: list[str],
) -> str:
    """Render extracted events + thoughts as the LLM's context material.

    Accepts either :class:`ExtractionEvent` instances or raw tuples
    ``(description, impact, relational_tags)`` for compatibility with
    the existing admin-route call style.
    """
    lines: list[str] = []

    lines.append(f"EVENTS ({len(events)} total):")
    if not events:
        lines.append("  (none — the import produced no events)")
    for i, raw in enumerate(events, start=1):
        if isinstance(raw, ExtractionEvent):
            desc = raw.description
            impact = raw.emotional_impact
            rel_tags = list(raw.relational_tags)
        else:
            desc, impact, rel_tags = raw
        tag_str = f" [{','.join(rel_tags)}]" if rel_tags else ""
        lines.append(f"  {i}. impact={impact:+d}{tag_str} · {desc}")

    lines.append("")
    lines.append(f"THOUGHTS ({len(thoughts)} total):")
    if not thoughts:
        lines.append("  (none — the import produced no long-term thoughts)")
    for i, t in enumerate(thoughts, start=1):
        lines.append(f"  {i}. {t}")

    return "\n".join(lines)


async def extract_persona_facts_and_blocks(
    *,
    llm: _LLMLike,
    context_text: str,
    existing_blocks: dict[str, str] | None = None,
    locale: str | None = None,
    persona_display_name: str | None = None,
    tier: LLMTier = LLMTier.LARGE,
    timeout: float | None = None,
) -> ExtractedPersona:
    """Run one LLM call that returns five blocks plus fifteen facts.

    Parameters
    ----------
    llm
        Any object with an async ``complete(system, user, *, tier,
        max_tokens, temperature, timeout)`` method — the same shape
        :class:`echovessel.runtime.llm.base.LLMProvider` exposes.
    context_text
        Free-form material the LLM reads. Prose for the blank-write
        path; serialized events + thoughts for the import path (use
        :func:`format_events_thoughts_as_context`).
    existing_blocks
        Blocks the user already wrote. The prompt tells the LLM to
        copy these verbatim into its output rather than rewrite them.
    locale / persona_display_name
        Optional hints.
    tier
        Defaults to LARGE — persona-defining call, worth the tokens.
    timeout
        Passed through to the provider; ``None`` means the provider's
        default.

    Raises
    ------
    PersonaExtractionError
        If the LLM responds with something we cannot parse at all (bad
        JSON, missing top-level keys). The caller should translate this
        to a 502 so the user can retry.
    """

    system = PERSONA_FACTS_SYSTEM_PROMPT
    user = format_persona_facts_user_prompt(
        context_text=context_text,
        existing_blocks=existing_blocks,
        locale=locale,
        persona_display_name=persona_display_name,
    )

    response = await llm.complete(
        system,
        user,
        tier=tier,
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
        timeout=timeout,
    )

    try:
        return parse_persona_facts_response(response)
    except PersonaFactsParseError as e:
        log.warning("persona_extraction: LLM returned malformed JSON: %s", e)
        raise PersonaExtractionError(str(e)) from e


def fallback_empty_extraction() -> ExtractedPersona:
    """Return an all-empty :class:`ExtractedPersona`.

    Useful when the caller decides to swallow an extraction failure and
    hand the user an empty review form to fill in manually. All facts
    are None, all blocks are "", confidence is 0.
    """

    return ExtractedPersona(facts=ExtractedFacts.empty(), facts_confidence=0.0)


__all__: tuple[str, ...] = (
    "ExtractionEvent",
    "PersonaExtractionError",
    "extract_persona_facts_and_blocks",
    "fallback_empty_extraction",
    "format_events_thoughts_as_context",
)


# Satisfy mypy on ``Any`` import (re-exported for downstream typing).
_ = Any

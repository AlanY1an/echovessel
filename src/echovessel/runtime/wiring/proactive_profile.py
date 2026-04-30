"""Glue: prompts.proactive_profile + LLM provider → regenerate closure.

This is the only place in the codebase that joins the prompt template
with both the LLM provider and the engine helper from
:mod:`echovessel.proactive.engines.profile_derivation`. Channels can
neither import ``prompts`` (forbidden) nor ``proactive`` (sibling), so
the runtime composition root publishes the resulting closure on
``runtime.ctx.regenerate_profile_fn`` and the Web admin route reaches
it through that opaque attribute.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sqlmodel import Session

from echovessel.proactive.core.models import PersonaProfile
from echovessel.proactive.engines.profile_derivation import (
    derive_profile_from_core_blocks,
)
from echovessel.prompts.proactive_profile import (
    PROFILE_DERIVATION_SYSTEM_PROMPT,
    format_profile_derivation_user_prompt,
    parse_profile_derivation_response,
)
from echovessel.runtime.llm.base import LLMProvider

log = logging.getLogger(__name__)


_LLM_MAX_TOKENS = 1024
_LLM_TEMPERATURE = 0.5


class RegenerateProfileFn(Protocol):
    """Shape of the closure stored on ``runtime.ctx.regenerate_profile_fn``."""

    async def __call__(
        self,
        *,
        db: Session,
        persona_id: str,
    ) -> PersonaProfile:  # pragma: no cover — Protocol stub
        ...


def make_regenerate_profile_fn(
    llm: LLMProvider,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> RegenerateProfileFn:
    """Build the ``(*, db, persona_id) -> PersonaProfile`` closure.

    The closure delegates to
    :func:`derive_profile_from_core_blocks`, which orchestrates the
    common shape: load core_blocks → call the inner derivation_fn →
    fall back on any exception → ``db.merge`` → ``db.commit``.

    The inner ``_llm_derivation`` calls the LLM provider with the
    LARGE-style prompt and parses the response. Any error there
    (network, malformed JSON, missing fields) propagates as an
    exception, which the engine catches and converts into the default
    fallback profile. That keeps the regenerate path strictly
    best-effort: onboarding never aborts because of LLM flake.
    """

    if now_fn is None:
        now_fn = datetime.now

    async def _llm_derivation(
        *,
        persona_id: str,
        core_blocks: dict[str, str],
        locale: str,
        imported_corpus_sample: list[str] | None = None,
    ) -> PersonaProfile:
        user_prompt = format_profile_derivation_user_prompt(
            core_blocks=core_blocks,
            locale=locale,
            imported_corpus_sample=imported_corpus_sample,
        )
        response_text, _usage = await llm.complete(
            PROFILE_DERIVATION_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=_LLM_MAX_TOKENS,
            temperature=_LLM_TEMPERATURE,
        )
        parsed = parse_profile_derivation_response(response_text)
        return PersonaProfile(
            persona_id=persona_id,
            style_summary=parsed.style_summary,
            quiet_hours=parsed.quiet_hours,
            forbidden_topics=parsed.forbidden_topics,
            voice_id=None,
            profile_generated_at=now_fn(),
            profile_source="llm_onboarding",
        )

    async def _regenerate(*, db: Session, persona_id: str) -> PersonaProfile:
        return await derive_profile_from_core_blocks(
            db,
            persona_id=persona_id,
            derivation_fn=_llm_derivation,
            now_fn=now_fn,
        )

    return _regenerate


__all__ = [
    "RegenerateProfileFn",
    "make_regenerate_profile_fn",
]

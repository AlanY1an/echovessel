"""Layer 1 PROFILE derivation.

The runtime composition root injects a ``derivation_fn`` that wraps an
LLM provider and returns a fully-shaped :class:`PersonaProfile`. This
module wires the surrounding behaviour:

  * read the persona's locale + L1 core blocks
  * call the injected fn
  * fall back to a ``profile_source='fallback_default'`` row when the
    LLM call raises — onboarding never aborts because of LLM flake
  * persist via ``db.merge`` so a regenerate request can overwrite an
    existing row in place

Per the import-linter contract ``proactive`` MUST NOT import
``runtime`` or ``prompts``. The engine therefore catches ``Exception``
from the injected fn rather than the LLM-specific error classes (which
live in ``runtime.llm.errors``); the wiring layer translates LLM
failure into whatever it pleases — the engine simply treats *any*
failure as "use fallback".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from sqlmodel import Session

from echovessel.memory.models import Persona
from echovessel.memory.retrieve import load_core_blocks
from echovessel.proactive.core.models import PersonaProfile

log = logging.getLogger(__name__)


_DEFAULT_LOCALE = "zh-CN"
_DEFAULT_USER_ID = "self"


FALLBACK_STYLE_SUMMARY: str = (
    "（尚未生成 · 请在 admin 行为侧写 tab 编辑）。"
    "默认行为：中等长度消息，直接询问，emoji 偶用，"
    "对沉默不催，follow-up 不超过 2 次。"
)


class ProfileDerivationFn(Protocol):
    """Callable shape the runtime injects.

    The implementation lives in ``runtime.wiring.proactive_profile`` and
    closes over an ``LLMProvider``. Returns a fully-populated
    :class:`PersonaProfile` instance (not yet attached to a session).
    Raising any exception is allowed and treated by the engine as
    "fall back to the default profile".
    """

    async def __call__(
        self,
        *,
        persona_id: str,
        core_blocks: dict[str, str],
        locale: str,
        imported_corpus_sample: list[str] | None = None,
    ) -> PersonaProfile:  # pragma: no cover — Protocol stub
        ...


def build_fallback_profile(
    persona_id: str,
    now_fn: Callable[[], datetime],
) -> PersonaProfile:
    """Default profile used when the LLM derivation cannot run.

    ``style_summary`` carries an explicit "尚未生成" marker so the admin
    UI can prompt the user to fill it in. ``quiet_hours`` defaults to
    ``[23, 7]`` (the most common night-silence window in dogfood data).
    """

    return PersonaProfile(
        persona_id=persona_id,
        style_summary=FALLBACK_STYLE_SUMMARY,
        quiet_hours=[23, 7],
        forbidden_topics=[],
        voice_id=None,
        profile_generated_at=now_fn(),
        profile_source="fallback_default",
    )


def _persona_locale(persona: Persona) -> str:
    """Pick a locale string for the LLM prompt.

    ``Persona.native_language`` is the BCP-47 best signal we have.
    ``locale_region`` is free-form and unsuitable as a sole indicator.
    """
    return persona.native_language or _DEFAULT_LOCALE


def _block_dict(blocks: list[Any]) -> dict[str, str]:
    """Project ``list[CoreBlock]`` → ``{label_str: content}``.

    SQLModel may hydrate ``label`` as the enum or the plain string
    depending on whether the row was loaded from DB or built in
    Python; normalise both to ``.value`` strings to keep the prompt
    contract stable.
    """
    out: dict[str, str] = {}
    for b in blocks:
        label = getattr(b.label, "value", b.label)
        out[label] = b.content
    return out


async def derive_profile_from_core_blocks(
    db: Session,
    *,
    persona_id: str,
    derivation_fn: ProfileDerivationFn,
    now_fn: Callable[[], datetime] | None = None,
    user_id: str = _DEFAULT_USER_ID,
    imported_corpus_sample: list[str] | None = None,
) -> PersonaProfile:
    """Onboarding-time entry point. Returns the persisted profile row.

    Caller responsibilities:

    * supply an open ``Session`` (the function ``merge`` + ``commit``s
      on success and on fallback so the row is durable either way)
    * handle the ``ValueError`` raised when ``persona_id`` does not
      resolve to a row — that's a programmer error, not a runtime
      condition the engine can recover from
    """
    if now_fn is None:
        now_fn = datetime.now

    persona = db.get(Persona, persona_id)
    if persona is None:
        raise ValueError(f"persona {persona_id!r} not found")

    blocks = load_core_blocks(db, persona_id, user_id)
    block_dict = _block_dict(blocks)
    locale = _persona_locale(persona)

    try:
        profile = await derivation_fn(
            persona_id=persona_id,
            core_blocks=block_dict,
            locale=locale,
            imported_corpus_sample=imported_corpus_sample,
        )
    except Exception as e:  # noqa: BLE001 — boundary with external LLM
        log.warning(
            "profile derivation failed for persona %s: %s — using fallback",
            persona_id,
            e,
        )
        profile = build_fallback_profile(persona_id, now_fn)

    db.merge(profile)
    db.commit()
    return profile


__all__ = [
    "ProfileDerivationFn",
    "FALLBACK_STYLE_SUMMARY",
    "build_fallback_profile",
    "derive_profile_from_core_blocks",
]

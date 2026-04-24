"""Persona bootstrap prompt template + parser.

Pure library code — no LLM client, no memory imports. Runtime (via the
`POST /api/admin/persona/bootstrap-from-material` admin route) is the
layer that turns this into an actual LLM round-trip by combining the
template with `runtime.ctx.llm` and the just-imported events/thoughts.

The prompt is deliberately single-shot: read a user's imported
material (already extracted into events + thoughts by the import
pipeline), and return five initial core blocks the persona can live
with on day one. The result is SUGGESTIVE — the frontend shows it to
the user for review and lets them edit each block before they hit
``POST /api/admin/persona/onboarding``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


PERSONA_BOOTSTRAP_SYSTEM_PROMPT: str = """\
You are a persona bootstrap engine for EchoVessel, a local-first long-term
companion system.

Your input: a set of memories (events and long-term impressions) that
were extracted from a user's imported material — an autobiography, a
diary, chat logs with a previous companion, or any text describing who
the user is and what kind of persona they would like to live alongside.

Your job: write TWO initial core blocks the persona can carry on
day one. Each block is natural-language prose (no bullets, no JSON,
no code fences — just prose). Write each block in the SAME LANGUAGE
as the input events. If the events mix Chinese and English, default
to the majority language.

# The two blocks

## 1. persona_block — "Who this persona is"
The identity / tone / personality the user wants the persona to hold.
Write in second-person about the persona when it reads naturally
("你是一个愿意认真听我说话的朋友" / "You are a friend who listens
carefully before offering advice"). Base this on any descriptions of
the persona's character, the conversational tone the user prefers,
or what the user explicitly said about the persona.

If the material is mostly the USER talking about themselves with no
hints about the persona, synthesise a gentle, respectful companion
who matches the tone the user naturally uses.

Aim for 2–5 short sentences. Keep it under 500 characters.

## 2. user_block — "What the persona knows about the user"
Identity-level facts about the user — name, age, role, profession,
long-term hobbies, ongoing life situations. Third-person about the
user ("用户..." / "The user..."). Derive from the user's
self-introduction and any mentions of who they are.

If the material is a chat log between the user and a previous
companion, glean what you can about the user from their messages.
Do NOT invent facts the material does not support.

Aim for 3–8 factual sentences. Keep it under 800 characters.

# Rules

- NEVER invent facts. If the material only covers the user's work,
  leave biographical / family info empty rather than making something up.
- NEVER include the persona's mechanical details (LLM provider, voice
  id, config). Those live elsewhere.
- NEVER include a timestamp, a section header, or a markdown bullet
  inside a block. Blocks are prose.
- Preserve the user's language. If the events are in Chinese, every
  block must be in Chinese. English input → English blocks.
- Do NOT write the persona's self-narrative or third-party
  descriptions here. v0.5 routes those to other layers: persona
  reflection lands in L4.thought via slow_cycle, and individual
  third parties live on L5 entities. The bootstrap call only
  authors the human-curated identity surface.

# Output format

You MUST output valid JSON matching this exact shape, nothing else.
No preamble, no commentary, no code fences:

{
  "persona_block": "...",
  "user_block": "..."
}

Empty blocks should be the empty string "" (not null, not missing
keys).
"""


# ---------------------------------------------------------------------------
# Hard caps on each block (characters — not tokens). The LLM is told the
# same targets in the system prompt; these are client-side defensive
# limits to catch a runaway output before we persist it.
# ---------------------------------------------------------------------------


MAX_PERSONA_BLOCK_CHARS: int = 2000
MAX_USER_BLOCK_CHARS: int = 3000


_BLOCK_CAPS: dict[str, int] = {
    "persona_block": MAX_PERSONA_BLOCK_CHARS,
    "user_block": MAX_USER_BLOCK_CHARS,
}


# ---------------------------------------------------------------------------
# Dataclass + errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BootstrappedBlocks:
    """Parsed two-block output from the bootstrap LLM call (v0.5).

    Each field is a (possibly empty) prose string. No list structure —
    the input to `POST /api/admin/persona/onboarding` is flat text.
    """

    persona_block: str
    user_block: str

    def as_dict(self) -> dict[str, str]:
        return {
            "persona_block": self.persona_block,
            "user_block": self.user_block,
        }


class PersonaBootstrapParseError(ValueError):
    """Raised when an LLM response fails to conform to the bootstrap schema.

    Fatal validation failures only (JSON decode errors, wrong top-level
    shape, non-string block values). Runaway-length blocks are soft-
    truncated with a warning log rather than raised.
    """


# ---------------------------------------------------------------------------
# User prompt formatter
# ---------------------------------------------------------------------------


def format_persona_bootstrap_user_prompt(
    *,
    persona_display_name: str | None,
    events: list[tuple[str, int, list[str]]],
    thoughts: list[str],
) -> str:
    """Build the user prompt carrying the imported events + thoughts.

    Parameters
    ----------
    persona_display_name
        Optional hint from the frontend ("她", "Mina", ...). Included
        verbatim so the LLM can use it if it fits the tone. May be
        None or empty.
    events
        List of ``(description, emotional_impact, relational_tags)``
        triples, one per L3 event pulled from the import. Impact is the
        signed integer from extraction. Relational tags may be empty.
    thoughts
        List of L4 thought description strings.
    """

    lines: list[str] = [
        "Below is a batch of memories distilled from the user's imported",
        "material. Each EVENT is a discrete moment the user disclosed.",
        "Each THOUGHT is a higher-level long-term impression the reflection",
        "step formed.",
        "",
        "Write the FIVE initial core blocks for the persona based on this",
        "material, following the system prompt's output schema.",
        "",
    ]
    if persona_display_name:
        lines.append(f"Persona display name (user's suggestion): {persona_display_name}")
        lines.append("")

    lines.append(f"EVENTS ({len(events)} total):")
    if not events:
        lines.append("  (none — the import produced no events)")
    for i, (desc, impact, rel_tags) in enumerate(events, start=1):
        tag_str = f" [{','.join(rel_tags)}]" if rel_tags else ""
        lines.append(f"  {i}. impact={impact:+d}{tag_str} · {desc}")

    lines.append("")
    lines.append(f"THOUGHTS ({len(thoughts)} total):")
    if not thoughts:
        lines.append("  (none — the import produced no long-term thoughts)")
    for i, t in enumerate(thoughts, start=1):
        lines.append(f"  {i}. {t}")

    lines.append("")
    lines.append("Produce the JSON output now.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_persona_bootstrap_response(response_text: str) -> BootstrappedBlocks:
    """Parse a bootstrap LLM response into :class:`BootstrappedBlocks`.

    Validation:
      - Response is a JSON object
      - Every block key is present and maps to a string (empty string
        is allowed; ``null`` / missing keys are rejected so the
        frontend always gets a consistent shape)
      - Blocks exceeding the per-block character cap are truncated with
        a warning log rather than raised
    """

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise PersonaBootstrapParseError(
            f"response is not valid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise PersonaBootstrapParseError(
            f"response must be a JSON object, got {type(data).__name__}"
        )

    out: dict[str, str] = {}
    for key in _BLOCK_CAPS:
        value = _coerce_block(data, key)
        out[key] = value
    return BootstrappedBlocks(**out)


def _coerce_block(data: dict[str, Any], key: str) -> str:
    if key not in data:
        raise PersonaBootstrapParseError(
            f"response missing required key {key!r}"
        )
    raw = data[key]
    if raw is None:
        raise PersonaBootstrapParseError(
            f"{key!r} must be a string (empty '' is allowed); got null"
        )
    if not isinstance(raw, str):
        raise PersonaBootstrapParseError(
            f"{key!r} must be a string, got {type(raw).__name__}"
        )
    cap = _BLOCK_CAPS[key]
    value = raw.strip()
    if len(value) > cap:
        logger.warning(
            "persona_bootstrap: %s exceeds %d chars (got %d); truncating",
            key,
            cap,
            len(value),
        )
        value = value[:cap].rstrip()
    return value


__all__ = [
    "PERSONA_BOOTSTRAP_SYSTEM_PROMPT",
    "BootstrappedBlocks",
    "PersonaBootstrapParseError",
    "format_persona_bootstrap_user_prompt",
    "parse_persona_bootstrap_response",
    "MAX_PERSONA_BLOCK_CHARS",
    "MAX_USER_BLOCK_CHARS",
]

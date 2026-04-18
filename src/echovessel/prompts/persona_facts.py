"""Persona facts + blocks extraction prompt, parser, and dataclasses.

Pure library code — no LLM client, no memory imports. The runtime
(:mod:`echovessel.runtime.persona_extraction`) turns this into an
actual round-trip by combining the template with ``runtime.ctx.llm``.

One LLM call produces two halves of the onboarding output:

- **5 core blocks** (persona / self / user / mood / relationship) —
  same role as :mod:`echovessel.prompts.persona_bootstrap`.
- **15 biographic facts** — structured columns on the ``personas``
  row (full_name / gender / birth_date / nationality / timezone / …)
  that the LLM extracts from whatever context the caller has.

The caller supplies a single ``context_text`` string that may be:
the user's own prose (blank-write path), a serialized dump of the
imported events + thoughts (import path), or a concatenation of the
existing core blocks (re-run-from-admin path). The prompt does NOT
care which — it just reads, extracts, and emits JSON.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum vocabularies — each fact field either has a closed set (LLM MUST
# choose a value from this set or emit null) or accepts free-form strings.
# Keep in sync with the comments on Persona model fields.
# ---------------------------------------------------------------------------


ENUM_GENDER: tuple[str, ...] = ("female", "male", "non_binary")
ENUM_EDUCATION_LEVEL: tuple[str, ...] = (
    "high_school",
    "bachelor",
    "master",
    "phd",
)
ENUM_RELATIONSHIP_STATUS: tuple[str, ...] = (
    "single",
    "married",
    "widowed",
    "divorced",
)
ENUM_LIFE_STAGE: tuple[str, ...] = (
    "student",
    "working",
    "retired",
    "new_parent",
    "between_jobs",
)
ENUM_HEALTH_STATUS: tuple[str, ...] = (
    "healthy",
    "chronic_illness",
    "recovering",
    "serious",
)


# The 15 fact fields, in the order used by the model / UI. Each entry is a
# ``(field_name, kind)`` pair: kind is one of
#   "text"   · free-form string
#   "date"   · ISO-8601 date (YYYY-MM-DD). Year-only is encoded YYYY-01-01.
#   "enum:<name>" · must pick from the enum tuple above
FACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("full_name", "text"),
    ("gender", "enum:gender"),
    ("birth_date", "date"),
    ("ethnicity", "text"),
    ("nationality", "text"),
    ("native_language", "text"),
    ("locale_region", "text"),
    ("education_level", "enum:education_level"),
    ("occupation", "text"),
    ("occupation_field", "text"),
    ("location", "text"),
    ("timezone", "text"),
    ("relationship_status", "enum:relationship_status"),
    ("life_stage", "enum:life_stage"),
    ("health_status", "enum:health_status"),
)


_ENUM_MAP: dict[str, tuple[str, ...]] = {
    "gender": ENUM_GENDER,
    "education_level": ENUM_EDUCATION_LEVEL,
    "relationship_status": ENUM_RELATIONSHIP_STATUS,
    "life_stage": ENUM_LIFE_STAGE,
    "health_status": ENUM_HEALTH_STATUS,
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


PERSONA_FACTS_SYSTEM_PROMPT: str = """\
You are a persona bootstrap engine for EchoVessel, a local-first long-term
companion. The user is creating a digital persona and has given you some
material describing that persona — it could be prose the user wrote about
them, a dump of extracted memories from an import, or both.

Your job has TWO parts:

## Part A — Five core blocks (prose)

Write five prose blocks the persona carries on day one. Each block is
natural-language prose (no bullets, no JSON, no code fences). Use the
SAME LANGUAGE as the input material. If the material mixes Chinese and
English, default to the majority language.

1. **persona_block** — Who this persona is. Identity / tone / personality.
   Second-person when natural ("你是…" / "You are…"). 2–5 sentences.
2. **self_block** — Persona's self-understanding. Often almost empty at
   bootstrap. Leave "" or write one short line. Under 200 chars.
3. **user_block** — Identity-level facts about the user the persona is
   talking to (name, role, long-term life situations). Third-person.
   Under 800 chars.
4. **mood_block** — Persona's starting mood. A gentle, neutral, welcoming
   tone. Under 200 chars.
5. **relationship_block** — People in the user's life (family, friends,
   pets). Empty "" if none were mentioned. Under 800 chars.

Rules:
- NEVER invent facts the material does not support.
- Empty blocks should be the empty string "", never null or missing keys.

## Part B — Fifteen biographic facts (structured)

Extract fifteen structured facts about the persona. Each fact is either
a string, a date (YYYY-MM-DD — use YYYY-01-01 if only the year is known),
or null when the material does not support a confident extraction.

Enum-valued facts MUST use exactly one of the listed values, or null.

Fact fields and their value rules:

- full_name              · free-form string (the persona's real name, may
                           differ from the user's familiar name for them)
- gender                 · one of: female | male | non_binary | null
- birth_date             · YYYY-MM-DD (or YYYY-01-01 for year-only) | null
- ethnicity              · free-form string | null
- nationality            · ISO 3166-1 alpha-2 (e.g. "CN", "US", "JP") | null
- native_language        · BCP 47 (e.g. "zh-CN", "en-US", "ja-JP") | null
- locale_region          · free-form regional descriptor | null
- education_level        · one of: high_school | bachelor | master | phd | null
- occupation             · free-form (e.g. "retired_teacher",
                           "software_engineer") | null
- occupation_field       · free-form (e.g. "literature", "fintech") | null
- location               · free-form (e.g. "沈阳", "Bay Area") | null
- timezone               · IANA tz (e.g. "Asia/Shanghai") | null
- relationship_status         · one of: single | married | widowed | divorced | null
- life_stage             · one of: student | working | retired | new_parent |
                           between_jobs | null
- health_status          · one of: healthy | chronic_illness | recovering |
                           serious | null

Rules:
- Prefer null over a guess. "Maybe female" must be null, not "female".
- For enum-valued fields, if the best natural value is outside the enum,
  emit null.
- If the material mentions only the year of birth, emit YYYY-01-01.

## Output format

Output valid JSON with this EXACT shape, and nothing else. No preamble,
no commentary, no code fences:

{
  "core_blocks": {
    "persona_block": "...",
    "self_block": "...",
    "user_block": "...",
    "mood_block": "...",
    "relationship_block": "..."
  },
  "facts": {
    "full_name": null,
    "gender": null,
    "birth_date": null,
    "ethnicity": null,
    "nationality": null,
    "native_language": null,
    "locale_region": null,
    "education_level": null,
    "occupation": null,
    "occupation_field": null,
    "location": null,
    "timezone": null,
    "relationship_status": null,
    "life_stage": null,
    "health_status": null
  },
  "facts_confidence": 0.5
}

``facts_confidence`` is a float in [0, 1]: your rough self-assessment of
how grounded the facts block is in the input material (1 = every non-null
fact came directly from the text, 0 = everything was a wild guess).
"""


# Per-block character caps mirror :mod:`persona_bootstrap`.
_BLOCK_CAPS: dict[str, int] = {
    "persona_block": 2000,
    "self_block": 1000,
    "user_block": 3000,
    "mood_block": 1000,
    "relationship_block": 3000,
}


# ---------------------------------------------------------------------------
# Dataclasses + errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedFacts:
    """Parsed biographic facts — 15 nullable fields.

    Values are already normalised: enum fields come from the enum
    vocabulary or are None; ``birth_date`` is a :class:`datetime.date`
    or None; everything else is a stripped string or None.
    """

    full_name: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    ethnicity: str | None = None
    nationality: str | None = None
    native_language: str | None = None
    locale_region: str | None = None
    education_level: str | None = None
    occupation: str | None = None
    occupation_field: str | None = None
    location: str | None = None
    timezone: str | None = None
    relationship_status: str | None = None
    life_stage: str | None = None
    health_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "gender": self.gender,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
            "ethnicity": self.ethnicity,
            "nationality": self.nationality,
            "native_language": self.native_language,
            "locale_region": self.locale_region,
            "education_level": self.education_level,
            "occupation": self.occupation,
            "occupation_field": self.occupation_field,
            "location": self.location,
            "timezone": self.timezone,
            "relationship_status": self.relationship_status,
            "life_stage": self.life_stage,
            "health_status": self.health_status,
        }

    @classmethod
    def empty(cls) -> ExtractedFacts:
        """All-null facts. Used as the fallback when LLM output is malformed."""
        return cls()


@dataclass(frozen=True, slots=True)
class ExtractedPersona:
    """Full output of one extraction call — 5 blocks + 15 facts + confidence."""

    persona_block: str = ""
    self_block: str = ""
    user_block: str = ""
    mood_block: str = ""
    relationship_block: str = ""
    facts: ExtractedFacts = field(default_factory=ExtractedFacts.empty)
    facts_confidence: float = 0.0

    def core_blocks_as_dict(self) -> dict[str, str]:
        return {
            "persona_block": self.persona_block,
            "self_block": self.self_block,
            "user_block": self.user_block,
            "mood_block": self.mood_block,
            "relationship_block": self.relationship_block,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "core_blocks": self.core_blocks_as_dict(),
            "facts": self.facts.as_dict(),
            "facts_confidence": self.facts_confidence,
        }


class PersonaFactsParseError(ValueError):
    """Raised when the LLM response fails hard (bad JSON, missing keys).

    Soft issues (bad enum value, non-ISO date) fall back to ``None`` for
    that field with a warning log instead of raising — a partial
    extraction is more useful than a wholesale failure.
    """


# ---------------------------------------------------------------------------
# User prompt formatter
# ---------------------------------------------------------------------------


def format_persona_facts_user_prompt(
    *,
    context_text: str,
    existing_blocks: dict[str, str] | None = None,
    locale: str | None = None,
    persona_display_name: str | None = None,
) -> str:
    """Build the user prompt for one extraction call.

    Parameters
    ----------
    context_text
        The raw material the LLM should reason over. For the blank-write
        path this is the user's prose; for the import path it's a
        formatted dump of the events + thoughts just extracted.
    existing_blocks
        Blocks the user has already handwritten (blank-write path). The
        LLM sees them as authoritative and must not rewrite those keys —
        it only fills in the blanks.
    locale
        Frontend locale (e.g. ``zh-CN`` / ``en-US``). Hint only — the
        system prompt still instructs "preserve the source language".
    persona_display_name
        Optional hint (``"她"`` / ``"Mina"``) so the blocks can address
        the persona naturally.
    """

    lines: list[str] = []

    if locale:
        lines.append(f"Frontend locale (hint): {locale}")
    if persona_display_name:
        lines.append(f"Persona display name (user's suggestion): {persona_display_name}")
    if lines:
        lines.append("")

    lines.append("=== CONTEXT MATERIAL ===")
    if context_text and context_text.strip():
        lines.append(context_text.strip())
    else:
        lines.append("(no material supplied — make your best minimal guess)")
    lines.append("")

    if existing_blocks:
        non_empty = {k: v for k, v in existing_blocks.items() if v and v.strip()}
        if non_empty:
            lines.append("=== EXISTING BLOCKS (authoritative — do NOT rewrite) ===")
            lines.append("The user already wrote the following blocks. Copy them")
            lines.append("verbatim into your output for those keys; extract facts")
            lines.append("from them alongside the context material above.")
            lines.append("")
            for key, value in non_empty.items():
                lines.append(f"### {key}")
                lines.append(value.strip())
                lines.append("")

    lines.append("Produce the JSON output now.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_persona_facts_response(response_text: str) -> ExtractedPersona:
    """Parse a persona-facts LLM response into :class:`ExtractedPersona`.

    Hard failures (not a JSON object, missing ``core_blocks`` key, missing
    ``facts`` key) raise :class:`PersonaFactsParseError` — the caller
    should 502 and ask the user to retry.

    Soft failures (one bad enum value, one malformed date) set that single
    field to ``None`` and continue. The goal is to preserve as much of a
    partial extraction as possible.
    """

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise PersonaFactsParseError(
            f"response is not valid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise PersonaFactsParseError(
            f"response must be a JSON object, got {type(data).__name__}"
        )

    blocks_raw = data.get("core_blocks")
    if not isinstance(blocks_raw, dict):
        raise PersonaFactsParseError(
            "response missing 'core_blocks' object"
        )

    facts_raw = data.get("facts")
    if not isinstance(facts_raw, dict):
        raise PersonaFactsParseError("response missing 'facts' object")

    blocks_out: dict[str, str] = {}
    for key, cap in _BLOCK_CAPS.items():
        blocks_out[key] = _coerce_block(blocks_raw, key, cap)

    facts = _coerce_facts(facts_raw)

    confidence = _coerce_confidence(data.get("facts_confidence"))

    return ExtractedPersona(
        persona_block=blocks_out["persona_block"],
        self_block=blocks_out["self_block"],
        user_block=blocks_out["user_block"],
        mood_block=blocks_out["mood_block"],
        relationship_block=blocks_out["relationship_block"],
        facts=facts,
        facts_confidence=confidence,
    )


def _coerce_block(data: dict[str, Any], key: str, cap: int) -> str:
    raw = data.get(key, "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        logger.warning(
            "persona_facts: block %r not a string (got %s); coercing to empty",
            key,
            type(raw).__name__,
        )
        return ""
    value = raw.strip()
    if len(value) > cap:
        logger.warning(
            "persona_facts: %s exceeds %d chars (got %d); truncating",
            key,
            cap,
            len(value),
        )
        value = value[:cap].rstrip()
    return value


def _coerce_facts(data: dict[str, Any]) -> ExtractedFacts:
    kwargs: dict[str, Any] = {}
    for field_name, kind in FACT_FIELDS:
        raw = data.get(field_name)
        kwargs[field_name] = _coerce_fact(field_name, kind, raw)
    return ExtractedFacts(**kwargs)


def _coerce_fact(field_name: str, kind: str, raw: Any) -> Any:
    if raw is None:
        return None
    if kind == "text":
        if isinstance(raw, str):
            value = raw.strip()
            return value or None
        logger.warning(
            "persona_facts: %r expected string, got %s; dropping",
            field_name,
            type(raw).__name__,
        )
        return None
    if kind == "date":
        if not isinstance(raw, str):
            logger.warning(
                "persona_facts: %r expected ISO date string, got %s; dropping",
                field_name,
                type(raw).__name__,
            )
            return None
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            logger.warning(
                "persona_facts: %r has invalid ISO date %r; dropping",
                field_name,
                raw,
            )
            return None
    if kind.startswith("enum:"):
        enum_name = kind.split(":", 1)[1]
        allowed = _ENUM_MAP[enum_name]
        if isinstance(raw, str):
            value = raw.strip().lower()
            if value in allowed:
                return value
        logger.warning(
            "persona_facts: %r value %r not in enum %s; dropping",
            field_name,
            raw,
            enum_name,
        )
        return None
    raise AssertionError(f"unknown fact kind {kind!r} for field {field_name!r}")


def _coerce_confidence(raw: Any) -> float:
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


__all__ = [
    "ENUM_EDUCATION_LEVEL",
    "ENUM_GENDER",
    "ENUM_HEALTH_STATUS",
    "ENUM_LIFE_STAGE",
    "ENUM_RELATIONSHIP_STATUS",
    "ExtractedFacts",
    "ExtractedPersona",
    "FACT_FIELDS",
    "PERSONA_FACTS_SYSTEM_PROMPT",
    "PersonaFactsParseError",
    "format_persona_facts_user_prompt",
    "parse_persona_facts_response",
]

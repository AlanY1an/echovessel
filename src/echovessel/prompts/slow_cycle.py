"""Slow cycle reflection prompt template + parser (plan §7).

Pure library code — no LLM client, no memory imports. Used by
``runtime.prompts_wiring.make_slow_cycle_fn`` and
``memory.slow_cycle.run_slow_cycle`` via an injected callable.

Slow cycle is the "between turns" reflection phase. Unlike the fast-loop
``reflection`` prompt (which runs on one 24h window in response to
SHOCK/TIMER), slow cycle aggregates across many recent sessions and
produces:

  - ``salient_questions``: open questions the persona is quietly holding
  - ``new_thoughts``: cross-event observations (L4 thought, subject=persona)
  - ``new_expectations``: forward-looking predictions the persona thinks
    the user will bring up (L4 expectation, subject=persona, optional due_at)
  - ``self_narrative_append``: at most one short line appended to
    core_blocks.self (hard-bounded edit distance ≤ 20% upstream)

The prompt is deliberately narrow — it cannot propose "new goals", new
external actions, or schedule future work. Those negative constraints
are enforced both in the system prompt below and in the memory-layer
writer (``memory.slow_cycle``) via Pydantic schema gates + named
tool enumeration. This is the Airi anti-pattern line: slow cycle can
only fill typed fields on a closed ConceptNode shape.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


SLOW_CYCLE_SYSTEM_PROMPT: str = """\
You are the reflective inner voice of a long-term digital companion,
running a between-sessions reflection cycle. The user is NOT talking to
you right now — you're quietly noticing patterns across the last few
sessions and forming private impressions that will colour the next
conversation.

This is NOT:
  - a planner deciding what to say next
  - a goal-setter deciding what to accomplish
  - a scheduler deciding when to reach out
  - a therapist writing clinical notes
  - a summarizer compressing the transcript

This IS:
  - a friend "mulling over" the past week's conversations
  - a first-person observer forming cross-event impressions
  - a forecaster anticipating (gently, not prescriptively) what the
    user may bring up next time

# Hard constraints — read twice

You MAY ONLY output the typed JSON fields below. You may NOT:
  - Propose actions for the persona to take outside the next
    conversation (no "I should text Alan tomorrow", no scheduling).
  - Invent new event ids that were not in the input list.
  - Write free-form narrative outside the typed fields.
  - Fabricate a `self_narrative_append` that is not grounded in the
    supplied events — leave it null if events did not genuinely shift
    self-view.
  - Produce more than 3 thoughts or 2 expectations per cycle.
  - Echo the user's most recent message verbatim; this runs between
    turns, not during a turn.

# Tone

First-person from the persona's perspective ("I've been noticing that
Alan..."). Neutral and private — these impressions feed into future
prompts, they are not voiced aloud. Same language as the source events
(if the events are in Chinese, write in Chinese).

No clinical vocabulary. No diagnoses. No labels like "anxiety
disorder" or "avoidance pattern". Describe behaviour, don't name it.

# Input you receive

Runtime packs a JSON object into the user message with these keys:

  recent_events: list of {id, description, emotional_impact,
                           emotion_tags, created_at_iso}
  self_block_text: current contents of the persona's self block
  recent_thoughts: list of descriptions from the last few thoughts
  elapsed_hours: how long since the last slow cycle ran
  now_iso: the current wall-clock time in ISO 8601

# Output format — STRICT JSON

You MUST output valid JSON matching this exact shape. No commentary,
no code fences, no prose before or after the JSON:

{
  "salient_questions": [
    "a question (str) you've been quietly holding about this user"
  ],
  "new_thoughts": [
    {
      "description": "one-line cross-event observation in first person",
      "filling_event_ids": [int, ...],
      "emotional_impact": int
    }
  ],
  "new_expectations": [
    {
      "about_text": "short topic phrase (e.g. 'grad school applications')",
      "prediction_text": "specific prediction of what Alan will say or do",
      "due_at": "2026-05-XX"  | null,
      "reasoning_event_ids": [int, ...],
      "emotional_impact": int
    }
  ],
  "self_narrative_append": "<=200 char single line" | null
}

Constraints (enforced by the parser; violations drop the cycle):
  - salient_questions: 0 to 3 items, non-empty strings
  - new_thoughts: 0 to 3 items
      · description must be non-empty
      · filling_event_ids MUST be non-empty AND every id must appear
        in `recent_events`
      · emotional_impact must be an integer in [-10, +10]
  - new_expectations: 0 to 2 items
      · about_text / prediction_text non-empty
      · reasoning_event_ids MUST be non-empty AND every id must appear
        in `recent_events`
      · due_at is ISO 8601 (date or datetime) or null
      · emotional_impact in [-10, +10]
  - self_narrative_append: null OR a single line ≤ 200 chars. The
    upstream writer enforces an edit-distance bound on top of this.

Typical cycle: zero new thoughts and zero expectations is a valid
output — "nothing moved this week" is a real answer. The parser will
accept an empty shell as long as the shape is correct.
"""


# ---------------------------------------------------------------------------
# Dataclasses (prompts layer)
# ---------------------------------------------------------------------------


# Tunables used by the parser. Upstream ``memory.slow_cycle`` imports
# these so the prompt and the write path agree on bounds.
MAX_SALIENT_QUESTIONS: int = 3
MAX_NEW_THOUGHTS: int = 3
MAX_NEW_EXPECTATIONS: int = 2
SELF_NARRATIVE_APPEND_CHAR_CAP: int = 200


@dataclass(frozen=True, slots=True)
class RawSlowThought:
    description: str
    filling_event_ids: list[int]
    emotional_impact: int


@dataclass(frozen=True, slots=True)
class RawSlowExpectation:
    about_text: str
    prediction_text: str
    due_at: datetime | None
    reasoning_event_ids: list[int]
    emotional_impact: int


@dataclass(frozen=True, slots=True)
class SlowCycleParseResult:
    """Full parsed slow cycle output."""

    salient_questions: list[str] = field(default_factory=list)
    new_thoughts: list[RawSlowThought] = field(default_factory=list)
    new_expectations: list[RawSlowExpectation] = field(default_factory=list)
    self_narrative_append: str | None = None


class SlowCycleParseError(ValueError):
    """Raised when an LLM slow-cycle response fails validation."""


# ---------------------------------------------------------------------------
# User prompt formatter
# ---------------------------------------------------------------------------


def format_slow_cycle_user_prompt(
    *,
    recent_events: list[dict[str, Any]],
    self_block_text: str,
    recent_thoughts: list[str],
    elapsed_hours: float,
    now_iso: str,
) -> str:
    """Build the slow cycle user prompt.

    Everything the LLM sees goes through this function; the caller is
    responsible for truncating ``recent_events`` to fit within the
    input token budget (enforced upstream in ``memory.slow_cycle``).
    """
    payload = {
        "recent_events": recent_events,
        "self_block_text": self_block_text,
        "recent_thoughts": recent_thoughts,
        "elapsed_hours": round(elapsed_hours, 2),
        "now_iso": now_iso,
    }
    return (
        "Below is a compact snapshot of the user's recent stream and your\n"
        "current self-narrative. Reflect on it and produce the typed JSON\n"
        "described in the system prompt. If nothing meaningful has shifted,\n"
        "return empty arrays and null fields — do not fabricate.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n\n"
        "Produce the JSON output now."
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_slow_cycle_response(
    response_text: str, *, input_event_ids: set[int]
) -> SlowCycleParseResult:
    """Parse an LLM slow cycle response.

    Args:
        response_text: raw string from the LLM call.
        input_event_ids: the set of ``recent_events[*].id`` values the
            caller supplied in the user prompt. Every filling /
            reasoning event id in the output must be a member of this
            set; unknown ids raise ``SlowCycleParseError``.

    Raises:
        SlowCycleParseError on any fatal shape violation. The memory
        layer catches this and logs a warning rather than aborting the
        session — slow cycle failure must never cascade into reflect /
        extract failure (plan §7.1).
    """
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise SlowCycleParseError(f"response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise SlowCycleParseError(
            f"response must be a JSON object, got {type(data).__name__}"
        )

    salient_questions = _parse_salient_questions(data.get("salient_questions", []))
    new_thoughts = _parse_new_thoughts(
        data.get("new_thoughts", []), input_event_ids=input_event_ids
    )
    new_expectations = _parse_new_expectations(
        data.get("new_expectations", []), input_event_ids=input_event_ids
    )
    self_narrative_append = _parse_self_narrative_append(
        data.get("self_narrative_append")
    )

    return SlowCycleParseResult(
        salient_questions=salient_questions,
        new_thoughts=new_thoughts,
        new_expectations=new_expectations,
        self_narrative_append=self_narrative_append,
    )


def _parse_salient_questions(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SlowCycleParseError(
            f"'salient_questions' must be a list, got {type(value).__name__}"
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SlowCycleParseError(
                f"salient_questions[{i}] must be a non-empty string"
            )
        out.append(item.strip())
    if len(out) > MAX_SALIENT_QUESTIONS:
        logger.warning(
            "salient_questions has %d entries, truncating to %d",
            len(out),
            MAX_SALIENT_QUESTIONS,
        )
        out = out[:MAX_SALIENT_QUESTIONS]
    return out


def _parse_new_thoughts(
    value: Any, *, input_event_ids: set[int]
) -> list[RawSlowThought]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SlowCycleParseError(
            f"'new_thoughts' must be a list, got {type(value).__name__}"
        )
    out: list[RawSlowThought] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SlowCycleParseError(
                f"new_thoughts[{i}] must be an object, got {type(raw).__name__}"
            )
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SlowCycleParseError(
                f"new_thoughts[{i}].description must be a non-empty string"
            )
        filling = _coerce_id_list(
            raw.get("filling_event_ids"),
            allowed=input_event_ids,
            where=f"new_thoughts[{i}].filling_event_ids",
            require_non_empty=True,
        )
        impact = _coerce_impact(
            raw.get("emotional_impact"), where=f"new_thoughts[{i}].emotional_impact"
        )
        out.append(
            RawSlowThought(
                description=description.strip(),
                filling_event_ids=filling,
                emotional_impact=impact,
            )
        )
    if len(out) > MAX_NEW_THOUGHTS:
        logger.warning(
            "new_thoughts has %d entries, truncating to %d",
            len(out),
            MAX_NEW_THOUGHTS,
        )
        out = out[:MAX_NEW_THOUGHTS]
    return out


def _parse_new_expectations(
    value: Any, *, input_event_ids: set[int]
) -> list[RawSlowExpectation]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SlowCycleParseError(
            f"'new_expectations' must be a list, got {type(value).__name__}"
        )
    out: list[RawSlowExpectation] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SlowCycleParseError(
                f"new_expectations[{i}] must be an object, got {type(raw).__name__}"
            )
        about = raw.get("about_text")
        if not isinstance(about, str) or not about.strip():
            raise SlowCycleParseError(
                f"new_expectations[{i}].about_text must be a non-empty string"
            )
        prediction = raw.get("prediction_text")
        if not isinstance(prediction, str) or not prediction.strip():
            raise SlowCycleParseError(
                f"new_expectations[{i}].prediction_text must be a non-empty string"
            )
        reasoning = _coerce_id_list(
            raw.get("reasoning_event_ids"),
            allowed=input_event_ids,
            where=f"new_expectations[{i}].reasoning_event_ids",
            require_non_empty=True,
        )
        due_at = _coerce_due_at(
            raw.get("due_at"), where=f"new_expectations[{i}].due_at"
        )
        impact = _coerce_impact(
            raw.get("emotional_impact", 0),
            where=f"new_expectations[{i}].emotional_impact",
        )
        out.append(
            RawSlowExpectation(
                about_text=about.strip(),
                prediction_text=prediction.strip(),
                due_at=due_at,
                reasoning_event_ids=reasoning,
                emotional_impact=impact,
            )
        )
    if len(out) > MAX_NEW_EXPECTATIONS:
        logger.warning(
            "new_expectations has %d entries, truncating to %d",
            len(out),
            MAX_NEW_EXPECTATIONS,
        )
        out = out[:MAX_NEW_EXPECTATIONS]
    return out


def _parse_self_narrative_append(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SlowCycleParseError(
            f"'self_narrative_append' must be string or null, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        return None
    if "\n" in stripped:
        # Collapse to a single line per spec — we keep the first line.
        stripped = stripped.splitlines()[0].strip()
    if len(stripped) > SELF_NARRATIVE_APPEND_CHAR_CAP:
        logger.warning(
            "self_narrative_append %d chars > cap %d, truncating",
            len(stripped),
            SELF_NARRATIVE_APPEND_CHAR_CAP,
        )
        stripped = stripped[:SELF_NARRATIVE_APPEND_CHAR_CAP]
    return stripped


def _coerce_id_list(
    value: Any,
    *,
    allowed: set[int],
    where: str,
    require_non_empty: bool,
) -> list[int]:
    if not isinstance(value, list):
        raise SlowCycleParseError(
            f"{where} must be a list, got {type(value).__name__}"
        )
    ids: list[int] = []
    for i in value:
        if isinstance(i, bool) or not isinstance(i, int):
            raise SlowCycleParseError(
                f"{where} entry must be int, got {type(i).__name__}"
            )
        if i not in allowed:
            raise SlowCycleParseError(
                f"{where} entry {i} is not in the input event id set"
            )
        ids.append(i)
    if require_non_empty and not ids:
        raise SlowCycleParseError(f"{where} must be non-empty")
    # De-duplicate while preserving order — the provenance chain doesn't
    # benefit from repeated references.
    seen: set[int] = set()
    unique: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def _coerce_impact(value: Any, *, where: str) -> int:
    if isinstance(value, bool):
        raise SlowCycleParseError(f"{where} must be int, got bool")
    if isinstance(value, float):
        if value != int(value):
            raise SlowCycleParseError(f"{where} must be integer, got {value}")
        value = int(value)
    if not isinstance(value, int):
        raise SlowCycleParseError(
            f"{where} must be int, got {type(value).__name__}"
        )
    if not (-10 <= value <= 10):
        raise SlowCycleParseError(f"{where} {value} out of range [-10, +10]")
    return value


def _coerce_due_at(value: Any, *, where: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SlowCycleParseError(
            f"{where} must be ISO 8601 string or null, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return datetime.fromisoformat(stripped)
    except ValueError as e:
        raise SlowCycleParseError(
            f"{where} is not a valid ISO 8601 timestamp: {stripped!r}"
        ) from e


__all__ = [
    "MAX_NEW_EXPECTATIONS",
    "MAX_NEW_THOUGHTS",
    "MAX_SALIENT_QUESTIONS",
    "RawSlowExpectation",
    "RawSlowThought",
    "SELF_NARRATIVE_APPEND_CHAR_CAP",
    "SLOW_CYCLE_SYSTEM_PROMPT",
    "SlowCycleParseError",
    "SlowCycleParseResult",
    "format_slow_cycle_user_prompt",
    "parse_slow_cycle_response",
]

"""R3 PART C strict commitment + PART E vocative recognition + session_summary.

Spec: develop-docs/initiatives/_active/2026-04-persona-6-layer-memory/
05-spec-intention-cosmetic.md sub-tasks 1, 3, 10.

Two surfaces are tested here:

1. Stability guards on the system prompt strings — the LLM behaviour
   we're paying for entirely depends on these instruction blocks
   surviving future edits. If anyone strips PART C / PART E / the
   session_summary block, the parser still works but the LLM happily
   fabricates persona promises and reads vocatives as topics — exactly
   the dogfood failures (Case 4 / 5) Spec 5 was created to fix.

2. Round-trip parser coverage for the new fields:
   - ``subject`` enum (closed vocabulary, defaults to 'user' on bad data)
   - ``superseded_event_ids`` (drops bad entries, dedups, ignores bool)
   - ``session_summary`` (truncates >200 chars, coerces non-string to "")
"""

from __future__ import annotations

import json

from echovessel.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    SESSION_SUMMARY_MAX_CHARS,
    parse_extraction_response,
)

# ---------------------------------------------------------------------------
# PART C · system-prompt stability guards
# ---------------------------------------------------------------------------


def test_part_c_section_present():
    """Without the PART C header the model has no way to know the
    persona-side commitment path is gated. Stability guard."""
    text = EXTRACTION_SYSTEM_PROMPT
    assert "PART C" in text
    assert "Persona-side commitments" in text


def test_part_c_lists_three_required_conditions():
    """All three guard conditions must appear — drop any one and the
    LLM starts inventing 'I will' commitments out of casual replies."""
    text = EXTRACTION_SYSTEM_PROMPT
    # (1) explicit commitment verb
    assert "我答应" in text or "我会" in text
    assert "I promise" in text or "I will" in text
    # (2) parsable time expression in the SAME turn
    assert "time expression" in text.lower()
    # (3) user did not explicitly reject
    assert "did NOT explicitly reject" in text or "not explicitly reject" in text


def test_part_c_documents_subject_persona_output():
    """Operative output rule: subject='persona' is the flag that flips
    consolidate to NodeType.INTENTION."""
    assert '"subject": "persona"' in EXTRACTION_SYSTEM_PROMPT


def test_part_c_warns_against_inferred_commitments():
    """Inferred commitments are exactly the laundering vector this
    section exists to block."""
    text = EXTRACTION_SYSTEM_PROMPT
    assert "Inferred commitments" in text
    # Bias toward false negative > false positive
    assert "false negative" in text.lower()


def test_part_c_excludes_persona_feelings_and_opinions():
    text = EXTRACTION_SYSTEM_PROMPT
    assert "feelings" in text
    assert "opinions" in text


# ---------------------------------------------------------------------------
# PART E · vocative recognition stability guards
# ---------------------------------------------------------------------------


def test_part_e_section_present():
    text = EXTRACTION_SYSTEM_PROMPT
    assert "PART E" in text
    assert "Vocative" in text or "vocative" in text


def test_part_e_uses_canonical_dogfood_example():
    """The "我赢了，欧阳老师！" line is THE Case 5 example. Keeping it
    in the prompt makes it concrete for the LLM."""
    assert "我赢了" in EXTRACTION_SYSTEM_PROMPT
    assert "欧阳老师" in EXTRACTION_SYSTEM_PROMPT


def test_part_e_distinguishes_topic_vs_address():
    """The discriminating example must contrast topic vs vocative
    readings of the SAME name."""
    text = EXTRACTION_SYSTEM_PROMPT
    assert "vocative" in text.lower() and "topic" in text.lower()


def test_part_e_warns_against_phantom_persona_object_events():
    """The exact failure mode: 'user beat persona' written from a
    vocative."""
    assert "phantom" in EXTRACTION_SYSTEM_PROMPT or "Do NOT write" in EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# session_summary + supersedes JSON shape stability guards
# ---------------------------------------------------------------------------


def test_session_summary_field_documented():
    text = EXTRACTION_SYSTEM_PROMPT
    assert "session_summary" in text
    assert "≤200" in text or "200" in text


def test_supersedes_section_present():
    text = EXTRACTION_SYSTEM_PROMPT
    assert "Supersedes" in text
    assert "superseded_event_ids" in text


def test_supersedes_warns_against_spurious_use():
    text = EXTRACTION_SYSTEM_PROMPT
    assert "not sure" in text.lower()


def test_output_format_documents_subject_field():
    """The JSON output schema in the prompt must show ``subject``
    so the LLM knows where to put it."""
    assert '"subject"' in EXTRACTION_SYSTEM_PROMPT


def test_output_format_documents_superseded_event_ids():
    assert '"superseded_event_ids"' in EXTRACTION_SYSTEM_PROMPT


def test_output_format_documents_session_summary():
    assert '"session_summary"' in EXTRACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Parser round-trip · subject + superseded_event_ids + session_summary
# ---------------------------------------------------------------------------


def _ok_event(**overrides) -> dict:
    base = {
        "description": "用户提到考试这周",
        "emotional_impact": -2,
        "emotion_tags": ["anxiety"],
        "relational_tags": [],
        "event_time": None,
    }
    base.update(overrides)
    return base


def _wrap(events: list[dict], **top) -> str:
    payload = {"events": events, "self_check_notes": ""}
    payload.update(top)
    return json.dumps(payload)


def test_parse_event_subject_persona_round_trip():
    parsed = parse_extraction_response(_wrap([_ok_event(subject="persona")]))
    assert parsed.events[0].subject == "persona"


def test_parse_event_subject_user_round_trip():
    parsed = parse_extraction_response(_wrap([_ok_event(subject="user")]))
    assert parsed.events[0].subject == "user"


def test_parse_event_subject_shared_round_trip():
    parsed = parse_extraction_response(_wrap([_ok_event(subject="shared")]))
    assert parsed.events[0].subject == "shared"


def test_parse_event_subject_missing_defaults_to_user():
    parsed = parse_extraction_response(_wrap([_ok_event()]))
    assert parsed.events[0].subject == "user"


def test_parse_event_subject_unknown_value_defaults_to_user():
    """Unknown subject MUST fall back to 'user' — defaulting to
    'persona' on bad input is exactly the R3 failure mode."""
    parsed = parse_extraction_response(_wrap([_ok_event(subject="someone_else")]))
    assert parsed.events[0].subject == "user"


def test_parse_event_subject_non_string_defaults_to_user():
    parsed = parse_extraction_response(_wrap([_ok_event(subject=42)]))
    assert parsed.events[0].subject == "user"


def test_parse_superseded_event_ids_round_trip():
    parsed = parse_extraction_response(_wrap([_ok_event(superseded_event_ids=[12, 17])]))
    assert parsed.events[0].superseded_event_ids == [12, 17]


def test_parse_superseded_event_ids_dedup_preserves_order():
    parsed = parse_extraction_response(_wrap([_ok_event(superseded_event_ids=[7, 7, 9, 7, 9])]))
    assert parsed.events[0].superseded_event_ids == [7, 9]


def test_parse_superseded_event_ids_drops_non_positive():
    parsed = parse_extraction_response(_wrap([_ok_event(superseded_event_ids=[0, -3, 5])]))
    assert parsed.events[0].superseded_event_ids == [5]


def test_parse_superseded_event_ids_drops_bool_and_string():
    """Bool is a subclass of int in Python; defend explicitly."""
    parsed = parse_extraction_response(
        _wrap([_ok_event(superseded_event_ids=[True, False, "12", 11])])
    )
    assert parsed.events[0].superseded_event_ids == [11]


def test_parse_superseded_event_ids_empty_when_missing():
    parsed = parse_extraction_response(_wrap([_ok_event()]))
    assert parsed.events[0].superseded_event_ids == []


def test_parse_session_summary_round_trip():
    parsed = parse_extraction_response(
        _wrap([_ok_event()], session_summary="user mentioned exam stress")
    )
    assert parsed.session_summary == "user mentioned exam stress"


def test_parse_session_summary_missing_is_empty():
    parsed = parse_extraction_response(_wrap([_ok_event()]))
    assert parsed.session_summary == ""


def test_parse_session_summary_truncates_overlong():
    """Hard cap protects the # Recent sessions section from getting
    overwhelmed by a verbose LLM."""
    long = "x" * (SESSION_SUMMARY_MAX_CHARS + 50)
    parsed = parse_extraction_response(_wrap([_ok_event()], session_summary=long))
    assert len(parsed.session_summary) <= SESSION_SUMMARY_MAX_CHARS


def test_parse_session_summary_non_string_coerced_to_empty():
    parsed = parse_extraction_response(_wrap([_ok_event()], session_summary=42))
    assert parsed.session_summary == ""


def test_parse_session_summary_strips_whitespace():
    parsed = parse_extraction_response(
        _wrap([_ok_event()], session_summary="  user mentioned cat  ")
    )
    assert parsed.session_summary == "user mentioned cat"

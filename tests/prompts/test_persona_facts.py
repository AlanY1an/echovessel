"""Unit tests for the persona-facts prompt + parser."""

from __future__ import annotations

import json
from datetime import date

import pytest

from echovessel.prompts.persona_facts import (
    ExtractedFacts,
    ExtractedPersona,
    PersonaFactsParseError,
    format_persona_facts_user_prompt,
    parse_persona_facts_response,
)


def _valid_response(
    *,
    blocks: dict[str, str] | None = None,
    facts: dict[str, object] | None = None,
    confidence: float = 0.7,
) -> str:
    default_blocks = {
        "persona_block": "你是温和的陪伴者",
        "self_block": "",
        "user_block": "用户住在沈阳",
        "mood_block": "安静",
        "relationship_block": "",
    }
    default_facts = {
        "full_name": None,
        "gender": None,
        "birth_date": None,
        "ethnicity": None,
        "nationality": None,
        "native_language": None,
        "locale_region": None,
        "education_level": None,
        "occupation": None,
        "occupation_field": None,
        "location": None,
        "timezone": None,
        "relationship_status": None,
        "life_stage": None,
        "health_status": None,
    }
    return json.dumps(
        {
            "core_blocks": {**default_blocks, **(blocks or {})},
            "facts": {**default_facts, **(facts or {})},
            "facts_confidence": confidence,
        }
    )


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------


def test_parse_happy_path_fills_all_blocks_and_null_facts():
    text = _valid_response()
    out = parse_persona_facts_response(text)

    assert isinstance(out, ExtractedPersona)
    assert out.persona_block.startswith("你是温和")
    assert out.user_block == "用户住在沈阳"
    assert out.self_block == ""
    assert out.facts == ExtractedFacts.empty()
    assert out.facts_confidence == pytest.approx(0.7)


def test_parse_populated_facts_returns_typed_values():
    text = _valid_response(
        facts={
            "full_name": "张丽华",
            "gender": "female",
            "birth_date": "1962-03-15",
            "nationality": "CN",
            "native_language": "zh-CN",
            "education_level": "bachelor",
            "occupation": "retired_teacher",
            "timezone": "Asia/Shanghai",
            "relationship_status": "widowed",
            "life_stage": "retired",
            "health_status": "healthy",
        },
        confidence=0.9,
    )
    out = parse_persona_facts_response(text)

    assert out.facts.full_name == "张丽华"
    assert out.facts.gender == "female"
    assert out.facts.birth_date == date(1962, 3, 15)
    assert out.facts.nationality == "CN"
    assert out.facts.education_level == "bachelor"
    assert out.facts.timezone == "Asia/Shanghai"
    assert out.facts.relationship_status == "widowed"
    assert out.facts.life_stage == "retired"
    assert out.facts_confidence == pytest.approx(0.9)


def test_parse_year_only_birth_date_allows_january_first():
    text = _valid_response(facts={"birth_date": "1962-01-01"})
    out = parse_persona_facts_response(text)
    assert out.facts.birth_date == date(1962, 1, 1)


# ---------------------------------------------------------------------------
# Soft failures — one bad field does not kill the rest
# ---------------------------------------------------------------------------


def test_bad_enum_value_is_dropped_not_raised():
    text = _valid_response(
        facts={"gender": "unknown", "occupation": "retired_teacher"}
    )
    out = parse_persona_facts_response(text)

    assert out.facts.gender is None
    assert out.facts.occupation == "retired_teacher"


def test_bad_date_is_dropped_not_raised():
    text = _valid_response(facts={"birth_date": "sometime in 1962"})
    out = parse_persona_facts_response(text)

    assert out.facts.birth_date is None


def test_enum_is_case_normalised_to_lower():
    text = _valid_response(facts={"gender": "FEMALE"})
    out = parse_persona_facts_response(text)
    assert out.facts.gender == "female"


def test_empty_string_free_text_fact_becomes_none():
    text = _valid_response(facts={"full_name": "  "})
    out = parse_persona_facts_response(text)
    assert out.facts.full_name is None


def test_block_over_cap_is_truncated():
    long_prose = "x" * 5000
    text = _valid_response(blocks={"persona_block": long_prose})
    out = parse_persona_facts_response(text)
    # 2000-char cap for persona_block
    assert len(out.persona_block) == 2000


def test_confidence_is_clamped_to_zero_one():
    text = _valid_response(confidence=1.7)
    out = parse_persona_facts_response(text)
    assert out.facts_confidence == pytest.approx(1.0)

    text = _valid_response(confidence=-0.3)
    out = parse_persona_facts_response(text)
    assert out.facts_confidence == pytest.approx(0.0)


def test_missing_confidence_is_zero():
    payload = json.loads(_valid_response())
    payload.pop("facts_confidence", None)
    out = parse_persona_facts_response(json.dumps(payload))
    assert out.facts_confidence == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Hard failures — raise PersonaFactsParseError
# ---------------------------------------------------------------------------


def test_non_json_raises():
    with pytest.raises(PersonaFactsParseError):
        parse_persona_facts_response("not JSON at all")


def test_non_object_raises():
    with pytest.raises(PersonaFactsParseError):
        parse_persona_facts_response("[1, 2, 3]")


def test_missing_core_blocks_raises():
    payload = json.loads(_valid_response())
    payload.pop("core_blocks")
    with pytest.raises(PersonaFactsParseError):
        parse_persona_facts_response(json.dumps(payload))


def test_missing_facts_raises():
    payload = json.loads(_valid_response())
    payload.pop("facts")
    with pytest.raises(PersonaFactsParseError):
        parse_persona_facts_response(json.dumps(payload))


# ---------------------------------------------------------------------------
# User-prompt formatter
# ---------------------------------------------------------------------------


def test_formatter_includes_context_and_locale_hint():
    prompt = format_persona_facts_user_prompt(
        context_text="她是一位退休教师",
        locale="zh-CN",
        persona_display_name="妈",
    )
    assert "zh-CN" in prompt
    assert "妈" in prompt
    assert "她是一位退休教师" in prompt
    assert "Produce the JSON output now." in prompt


def test_formatter_handles_empty_context():
    prompt = format_persona_facts_user_prompt(
        context_text="",
    )
    assert "(no material supplied" in prompt


def test_formatter_inlines_existing_blocks_when_present():
    prompt = format_persona_facts_user_prompt(
        context_text="补充材料",
        existing_blocks={
            "persona_block": "你是一位温和的长者",
            "self_block": "",
            "relationship_block": "",
        },
    )
    assert "EXISTING BLOCKS" in prompt
    assert "你是一位温和的长者" in prompt
    # Empty existing blocks are skipped — we don't flood the prompt with
    # six empty headers.
    assert "### self_block" not in prompt


def test_formatter_omits_existing_blocks_section_when_all_empty():
    prompt = format_persona_facts_user_prompt(
        context_text="材料",
        existing_blocks={"persona_block": "", "self_block": ""},
    )
    assert "EXISTING BLOCKS" not in prompt


# ---------------------------------------------------------------------------
# Dataclass round-trip
# ---------------------------------------------------------------------------


def test_extracted_facts_as_dict_serialises_date():
    facts = ExtractedFacts(full_name="Ann", birth_date=date(1962, 3, 15))
    d = facts.as_dict()
    assert d["full_name"] == "Ann"
    assert d["birth_date"] == "1962-03-15"
    assert d["gender"] is None


def test_extracted_persona_as_dict_is_json_serialisable():
    facts = ExtractedFacts(full_name="Ann", birth_date=date(1962, 3, 15))
    persona = ExtractedPersona(
        persona_block="p",
        user_block="u",
        facts=facts,
        facts_confidence=0.8,
    )
    d = persona.as_dict()
    # If json.dumps succeeds without default= then the shape is clean.
    json.dumps(d)
    assert d["facts"]["birth_date"] == "1962-03-15"
    assert d["facts_confidence"] == pytest.approx(0.8)

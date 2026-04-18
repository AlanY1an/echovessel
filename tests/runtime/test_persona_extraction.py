"""Unit tests for the persona_extraction orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import pytest

from echovessel.runtime.llm.base import LLMTier
from echovessel.runtime.persona_extraction import (
    ExtractionEvent,
    PersonaExtractionError,
    extract_persona_facts_and_blocks,
    fallback_empty_extraction,
    format_events_thoughts_as_context,
)


@dataclass
class _StubLLM:
    response: str
    system_seen: str = ""
    user_seen: str = ""
    tier_seen: LLMTier | None = None
    max_tokens_seen: int = 0
    calls: int = field(default=0)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> str:
        self.system_seen = system
        self.user_seen = user
        self.tier_seen = tier
        self.max_tokens_seen = max_tokens
        self.calls += 1
        return self.response


def _valid_json(**facts_override: object) -> str:
    base_facts: dict[str, object] = dict.fromkeys(
        (
            "full_name", "gender", "birth_date", "ethnicity",
            "nationality", "native_language", "locale_region",
            "education_level", "occupation", "occupation_field",
            "location", "timezone", "marital_status",
            "life_stage", "health_status",
        )
    )
    base_facts.update(facts_override)
    return json.dumps({
        "core_blocks": {
            "persona_block": "你是温和的陪伴者",
            "self_block": "",
            "user_block": "用户在沈阳",
            "mood_block": "安静",
            "relationship_block": "",
        },
        "facts": base_facts,
        "facts_confidence": 0.8,
    })


async def test_extract_uses_large_tier_by_default():
    llm = _StubLLM(response=_valid_json(full_name="张丽华"))

    out = await extract_persona_facts_and_blocks(
        llm=llm,
        context_text="她是退休教师",
        locale="zh-CN",
    )

    assert llm.tier_seen == LLMTier.LARGE
    assert llm.calls == 1
    assert out.facts.full_name == "张丽华"
    assert out.persona_block.startswith("你是温和")


async def test_extract_passes_existing_blocks_into_user_prompt():
    llm = _StubLLM(response=_valid_json())

    await extract_persona_facts_and_blocks(
        llm=llm,
        context_text="材料",
        existing_blocks={"persona_block": "用户的手写人设"},
        locale="zh-CN",
    )

    assert "用户的手写人设" in llm.user_seen
    assert "EXISTING BLOCKS" in llm.user_seen


async def test_extract_malformed_json_raises_extraction_error():
    llm = _StubLLM(response="definitely not json")

    with pytest.raises(PersonaExtractionError):
        await extract_persona_facts_and_blocks(
            llm=llm,
            context_text="anything",
        )


async def test_extract_normalises_date_to_python_date():
    llm = _StubLLM(response=_valid_json(birth_date="1962-03-15"))

    out = await extract_persona_facts_and_blocks(
        llm=llm, context_text="ctx"
    )

    assert out.facts.birth_date == date(1962, 3, 15)


def test_fallback_empty_extraction_is_all_none():
    out = fallback_empty_extraction()
    assert out.persona_block == ""
    assert out.facts.full_name is None
    assert out.facts_confidence == 0.0


def test_format_events_thoughts_with_objects_and_tuples_equivalent():
    events_obj = [
        ExtractionEvent(
            description="领养了黑猫",
            emotional_impact=6,
            relational_tags=("Mochi",),
        ),
        ExtractionEvent(description="搬到沈阳"),
    ]
    events_tup = [
        ("领养了黑猫", 6, ["Mochi"]),
        ("搬到沈阳", 0, []),
    ]

    text_obj = format_events_thoughts_as_context(
        events=events_obj, thoughts=["慢热"]
    )
    text_tup = format_events_thoughts_as_context(
        events=events_tup, thoughts=["慢热"]
    )
    assert text_obj == text_tup
    assert "领养了黑猫" in text_obj
    assert "慢热" in text_obj


def test_format_empty_events_and_thoughts_marks_none():
    text = format_events_thoughts_as_context(events=[], thoughts=[])
    assert "EVENTS (0 total):" in text
    assert "THOUGHTS (0 total):" in text
    assert "(none" in text

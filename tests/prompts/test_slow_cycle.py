"""Slow cycle prompt parser tests (Spec 6 · T1)."""

from __future__ import annotations

import json

import pytest

from echovessel.prompts.slow_cycle import (
    MAX_NEW_EXPECTATIONS,
    MAX_NEW_THOUGHTS,
    SlowCycleParseError,
    format_slow_cycle_user_prompt,
    parse_slow_cycle_response,
)


def _payload(**overrides):
    base = {
        "salient_questions": ["what is Alan avoiding?"],
        "new_thoughts": [
            {
                "description": "Alan keeps circling the grad school topic",
                "filling_event_ids": [1, 2],
                "emotional_impact": -2,
            }
        ],
        "new_expectations": [
            {
                "about_text": "grad school deadline",
                "prediction_text": "Alan will mention it next week",
                "due_at": "2026-05-01",
                "reasoning_event_ids": [1],
                "emotional_impact": 2,
            }
        ],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_parse_valid_payload():
    raw = _payload()
    result = parse_slow_cycle_response(raw, input_event_ids={1, 2})
    assert result.salient_questions == ["what is Alan avoiding?"]
    assert len(result.new_thoughts) == 1
    assert result.new_thoughts[0].filling_event_ids == [1, 2]
    assert len(result.new_expectations) == 1
    assert result.new_expectations[0].reasoning_event_ids == [1]
    # v0.5 · self_narrative_append was removed from the parser schema
    assert not hasattr(result, "self_narrative_append")


def test_reject_filling_id_not_in_input_set():
    raw = _payload(
        new_thoughts=[
            {
                "description": "fabricated",
                "filling_event_ids": [99],
                "emotional_impact": 0,
            }
        ]
    )
    with pytest.raises(SlowCycleParseError, match="not in the input event id set"):
        parse_slow_cycle_response(raw, input_event_ids={1, 2})


def test_reject_empty_filling_ids():
    raw = _payload(
        new_thoughts=[
            {
                "description": "orphan",
                "filling_event_ids": [],
                "emotional_impact": 0,
            }
        ]
    )
    with pytest.raises(SlowCycleParseError, match="must be non-empty"):
        parse_slow_cycle_response(raw, input_event_ids={1, 2})


def test_reject_empty_reasoning_ids_on_expectation():
    raw = _payload(
        new_expectations=[
            {
                "about_text": "x",
                "prediction_text": "y",
                "due_at": None,
                "reasoning_event_ids": [],
                "emotional_impact": 0,
            }
        ]
    )
    with pytest.raises(SlowCycleParseError, match="must be non-empty"):
        parse_slow_cycle_response(raw, input_event_ids={1, 2})


def test_reject_impact_out_of_range():
    raw = _payload(
        new_thoughts=[
            {
                "description": "noticed",
                "filling_event_ids": [1],
                "emotional_impact": 99,
            }
        ]
    )
    with pytest.raises(SlowCycleParseError, match="out of range"):
        parse_slow_cycle_response(raw, input_event_ids={1})


def test_self_narrative_append_field_silently_dropped():
    """v0.5 · a stale LLM that still emits self_narrative_append gets
    its extra field ignored by the parser. The parser used to enforce
    a 200-char cap on that field; now it's not even read, so the
    resulting object has no such attribute and the surrounding typed
    fields parse as normal.
    """
    raw = _payload(self_narrative_append="x" * 400)
    result = parse_slow_cycle_response(raw, input_event_ids={1, 2})
    assert not hasattr(result, "self_narrative_append")
    assert len(result.new_thoughts) == 1


def test_empty_shell_is_valid():
    raw = json.dumps(
        {
            "salient_questions": [],
            "new_thoughts": [],
            "new_expectations": [],
        }
    )
    result = parse_slow_cycle_response(raw, input_event_ids=set())
    assert result.new_thoughts == []
    assert result.new_expectations == []


def test_reject_non_json():
    with pytest.raises(SlowCycleParseError, match="not valid JSON"):
        parse_slow_cycle_response("not json", input_event_ids={1})


def test_thoughts_truncated_to_max():
    raw = _payload(
        new_thoughts=[
            {
                "description": f"thought {i}",
                "filling_event_ids": [1],
                "emotional_impact": 0,
            }
            for i in range(MAX_NEW_THOUGHTS + 2)
        ]
    )
    result = parse_slow_cycle_response(raw, input_event_ids={1})
    assert len(result.new_thoughts) == MAX_NEW_THOUGHTS


def test_expectations_truncated_to_max():
    raw = _payload(
        new_thoughts=[],
        new_expectations=[
            {
                "about_text": f"topic {i}",
                "prediction_text": "something",
                "due_at": None,
                "reasoning_event_ids": [1],
                "emotional_impact": 0,
            }
            for i in range(MAX_NEW_EXPECTATIONS + 3)
        ],
    )
    result = parse_slow_cycle_response(raw, input_event_ids={1})
    assert len(result.new_expectations) == MAX_NEW_EXPECTATIONS


def test_format_user_prompt_includes_payload():
    events = [{"id": 1, "description": "event one"}]
    prompt = format_slow_cycle_user_prompt(
        recent_events=events,
        recent_thoughts=["Alan worries in private"],
        elapsed_hours=12.5,
        now_iso="2026-04-24T12:00:00",
    )
    assert "event one" in prompt
    # Recent thoughts still flow — slow_cycle still sees the persona's
    # prior reflections even though it no longer reads/writes L1.self.
    assert "Alan worries in private" in prompt
    assert "12.5" in prompt
    assert "2026-04-24T12:00:00" in prompt

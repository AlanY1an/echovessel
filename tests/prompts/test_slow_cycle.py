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
        "self_narrative_append": None,
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
    assert result.self_narrative_append is None


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


def test_self_narrative_append_truncated_to_cap():
    long = "x" * 400  # exceeds 200-char cap
    raw = _payload(self_narrative_append=long)
    result = parse_slow_cycle_response(raw, input_event_ids={1, 2})
    assert result.self_narrative_append is not None
    assert len(result.self_narrative_append) == 200


def test_empty_shell_is_valid():
    raw = json.dumps(
        {
            "salient_questions": [],
            "new_thoughts": [],
            "new_expectations": [],
            "self_narrative_append": None,
        }
    )
    result = parse_slow_cycle_response(raw, input_event_ids=set())
    assert result.new_thoughts == []
    assert result.new_expectations == []
    assert result.self_narrative_append is None


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
        self_block_text="quiet, attentive",
        recent_thoughts=["Alan worries in private"],
        elapsed_hours=12.5,
        now_iso="2026-04-24T12:00:00",
    )
    assert "event one" in prompt
    assert "quiet, attentive" in prompt
    assert "12.5" in prompt
    assert "2026-04-24T12:00:00" in prompt

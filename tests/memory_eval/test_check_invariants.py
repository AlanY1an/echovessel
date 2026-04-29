"""Unit tests for ``check_invariants`` — each new invariant field has its own
test pair (positive + negative) so harness extension is TDD-driven."""

from __future__ import annotations

from tests.memory_eval.harness import (
    EvalResult,
    Fixture,
    FixtureSeed,
    check_invariants,
)


def _result(**kw) -> EvalResult:
    """Build a minimal EvalResult with overridable fields."""
    base: dict = {
        "events": [],
        "thoughts": [],
        "filling": [],
        "mood_block_before": "",
        "mood_block_after": "",
        "retrieved": [],
        "reflection_triggered": False,
    }
    base.update(kw)
    return EvalResult(**base)


def _event(**overrides) -> dict:
    base = {
        "id": 1,
        "type": "event",
        "description": "...",
        "emotional_impact": 0,
        "emotion_tags": [],
        "relational_tags": [],
        "source_session_id": "s",
    }
    base.update(overrides)
    return base


def _fixture(invariants: dict) -> Fixture:
    return Fixture(
        fixture_id="t",
        version="scripted",
        generated_at=None,
        model=None,
        scenario="",
        seed=FixtureSeed(),
        turns=[],
        retrieve=None,
        invariants=invariants,
        judge_prompts=[],
    )


# ---------------------------------------------------------------------------
# Field 1 · must_have_event_time
# ---------------------------------------------------------------------------


def test_must_have_event_time_passes_when_all_events_have_time():
    res = _result(events=[_event(event_time_start="2026-04-20")])
    fix = _fixture({"must_have_event_time": True})
    assert check_invariants(fix, res) == []


def test_must_have_event_time_fails_when_event_lacks_time():
    res = _result(events=[_event()])
    fix = _fixture({"must_have_event_time": True})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "event_time" in violations[0]


# ---------------------------------------------------------------------------
# Field 2 · must_have_subject_any
# ---------------------------------------------------------------------------


def test_must_have_subject_any_passes():
    res = _result(events=[_event(subject="persona")])
    fix = _fixture({"must_have_subject_any": ["persona"]})
    assert check_invariants(fix, res) == []


def test_must_have_subject_any_fails_when_no_match():
    res = _result(events=[_event(subject="user")])
    fix = _fixture({"must_have_subject_any": ["persona"]})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "must_have_subject_any" in violations[0]


# ---------------------------------------------------------------------------
# Field 3 · must_have_concept_type_any
# ---------------------------------------------------------------------------


def test_must_have_concept_type_any_passes():
    res = _result(events=[_event(type="intention")])
    fix = _fixture({"must_have_concept_type_any": ["intention"]})
    assert check_invariants(fix, res) == []


def test_must_have_concept_type_any_fails_when_no_match():
    res = _result(events=[_event(type="event")])
    fix = _fixture({"must_have_concept_type_any": ["intention"]})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "must_have_concept_type_any" in violations[0]


# ---------------------------------------------------------------------------
# Field 4 · forbidden_descriptions_contain_none
# ---------------------------------------------------------------------------


def test_forbidden_descriptions_contain_none_passes_when_clean():
    res = _result(events=[_event(description="用户养了一只猫")])
    fix = _fixture({"forbidden_descriptions_contain_none": ["击败 persona"]})
    assert check_invariants(fix, res) == []


def test_forbidden_descriptions_contain_none_fails_when_phrase_present():
    res = _result(events=[_event(description="用户击败 persona 在游戏里")])
    fix = _fixture({"forbidden_descriptions_contain_none": ["击败 persona"]})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "forbidden_descriptions_contain_none" in violations[0]


# ---------------------------------------------------------------------------
# Field 5 · entity_count_eq + entity_count_max
# ---------------------------------------------------------------------------


def _entity(name: str, **overrides) -> dict:
    base = {
        "id": 1,
        "canonical_name": name,
        "kind": "person",
        "merge_status": "confirmed",
    }
    base.update(overrides)
    return base


def test_entity_count_eq_passes():
    res = _result(entities=[_entity("Mochi")])
    fix = _fixture({"entity_count_eq": 1})
    assert check_invariants(fix, res) == []


def test_entity_count_eq_fails_when_count_off():
    res = _result(entities=[_entity("Mochi"), _entity("Alex", id=2)])
    fix = _fixture({"entity_count_eq": 1})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "entity_count_eq" in violations[0]


def test_entity_count_max_passes_when_under_cap():
    res = _result(entities=[_entity("Mochi")])
    fix = _fixture({"entity_count_max": 2})
    assert check_invariants(fix, res) == []


def test_entity_count_max_fails_when_over_cap():
    res = _result(entities=[_entity("Mochi"), _entity("Alex", id=2)])
    fix = _fixture({"entity_count_max": 1})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "entity_count_max" in violations[0]


# ---------------------------------------------------------------------------
# Field 6 · entity_merge_status_eq
# ---------------------------------------------------------------------------


def test_entity_merge_status_eq_passes():
    res = _result(entities=[_entity("Mochi", merge_status="confirmed")])
    fix = _fixture({"entity_merge_status_eq": {"Mochi": "confirmed"}})
    assert check_invariants(fix, res) == []


def test_entity_merge_status_eq_fails_on_mismatch():
    res = _result(entities=[_entity("Mochi", merge_status="uncertain")])
    fix = _fixture({"entity_merge_status_eq": {"Mochi": "confirmed"}})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "entity_merge_status_eq" in violations[0]


def test_entity_merge_status_eq_fails_when_entity_missing():
    res = _result(entities=[])
    fix = _fixture({"entity_merge_status_eq": {"Mochi": "confirmed"}})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "entity_merge_status_eq" in violations[0]


# ---------------------------------------------------------------------------
# Field 7 · recall_message_count_eq
# ---------------------------------------------------------------------------


def test_recall_message_count_eq_passes():
    res = _result(recall_count=4)
    fix = _fixture({"recall_message_count_eq": 4})
    assert check_invariants(fix, res) == []


def test_recall_message_count_eq_fails_on_mismatch():
    res = _result(recall_count=3)
    fix = _fixture({"recall_message_count_eq": 4})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "recall_message_count_eq" in violations[0]


# ---------------------------------------------------------------------------
# Field 8 · core_block_count_unchanged
# ---------------------------------------------------------------------------


def test_core_block_count_unchanged_passes_when_identical():
    snap = {"PERSONA|_": "deadbeef"}
    res = _result(
        core_block_snapshot_before=snap,
        core_block_snapshot_after=dict(snap),
    )
    fix = _fixture({"core_block_count_unchanged": True})
    assert check_invariants(fix, res) == []


def test_core_block_count_unchanged_fails_when_content_drifts():
    res = _result(
        core_block_snapshot_before={"PERSONA|_": "aaaaaaaa"},
        core_block_snapshot_after={"PERSONA|_": "bbbbbbbb"},
    )
    fix = _fixture({"core_block_count_unchanged": True})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "core_block_count_unchanged" in violations[0]


# ---------------------------------------------------------------------------
# Field 9 · top_k_must_contain_descriptions_all
# ---------------------------------------------------------------------------


def test_top_k_must_contain_descriptions_all_passes():
    res = _result(
        retrieved=[
            {"id": 1, "description": "用户养了 Mochi", "relevance": 0.9},
            {"id": 2, "description": "用户喜欢爬山", "relevance": 0.7},
        ]
    )
    fix = _fixture(
        {
            "top_k_must_contain_descriptions_all": ["Mochi"],
            "top_k_for_check": 2,
        }
    )
    assert check_invariants(fix, res) == []


def test_top_k_must_contain_descriptions_all_fails_when_missing():
    res = _result(
        retrieved=[
            {"id": 1, "description": "用户喜欢做饭", "relevance": 0.9},
            {"id": 2, "description": "用户喜欢爬山", "relevance": 0.7},
        ]
    )
    fix = _fixture(
        {
            "top_k_must_contain_descriptions_all": ["Mochi"],
            "top_k_for_check": 2,
        }
    )
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "top_k_must_contain_descriptions_all" in violations[0]


# ---------------------------------------------------------------------------
# Field 10 · top_k_must_not_contain_descriptions_any
# ---------------------------------------------------------------------------


def test_top_k_must_not_contain_descriptions_any_passes():
    res = _result(
        retrieved=[
            {"id": 1, "description": "用户喜欢爬山", "relevance": 0.9},
            {"id": 2, "description": "用户喜欢做饭", "relevance": 0.7},
        ]
    )
    fix = _fixture(
        {
            "top_k_must_not_contain_descriptions_any": ["旧事件"],
            "top_k_for_check": 2,
        }
    )
    assert check_invariants(fix, res) == []


def test_top_k_must_not_contain_descriptions_any_fails_when_present():
    res = _result(
        retrieved=[
            {"id": 1, "description": "旧事件 14 天前", "relevance": 0.9},
            {"id": 2, "description": "用户喜欢做饭", "relevance": 0.7},
        ]
    )
    fix = _fixture(
        {
            "top_k_must_not_contain_descriptions_any": ["旧事件"],
            "top_k_for_check": 2,
        }
    )
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "top_k_must_not_contain_descriptions_any" in violations[0]


# ---------------------------------------------------------------------------
# Field 11 · episodic_state_mood_changed
# ---------------------------------------------------------------------------


def test_episodic_state_mood_changed_passes():
    res = _result(
        episodic_state_before={"mood": "neutral"},
        episodic_state_after={"mood": "grieving"},
    )
    fix = _fixture({"episodic_state_mood_changed": True})
    assert check_invariants(fix, res) == []


def test_episodic_state_mood_changed_fails_when_unchanged():
    res = _result(
        episodic_state_before={"mood": "neutral"},
        episodic_state_after={"mood": "neutral"},
    )
    fix = _fixture({"episodic_state_mood_changed": True})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "episodic_state_mood_changed" in violations[0]


# ---------------------------------------------------------------------------
# Field 12 · episodic_state_mood_unchanged
# ---------------------------------------------------------------------------


def test_episodic_state_mood_unchanged_passes():
    res = _result(
        episodic_state_before={"mood": "calm"},
        episodic_state_after={"mood": "calm"},
    )
    fix = _fixture({"episodic_state_mood_unchanged": True})
    assert check_invariants(fix, res) == []


def test_episodic_state_mood_unchanged_fails_when_drifted():
    res = _result(
        episodic_state_before={"mood": "calm"},
        episodic_state_after={"mood": "anxious"},
    )
    fix = _fixture({"episodic_state_mood_unchanged": True})
    violations = check_invariants(fix, res)
    assert len(violations) == 1
    assert "episodic_state_mood_unchanged" in violations[0]

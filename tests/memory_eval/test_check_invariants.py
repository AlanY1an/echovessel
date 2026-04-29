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

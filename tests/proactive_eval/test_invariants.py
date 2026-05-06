"""Invariant checker unit tests · pure-data · no LLM."""

from __future__ import annotations

from tests.memory_eval.schema import FixtureTurn
from tests.proactive_eval.invariants import check_invariants
from tests.proactive_eval.schema import (
    ClockSpec,
    ExpectEvent,
    ExpectPhase,
    FollowUpStage,
    PhaseEvalResult,
    ProactiveEvalResult,
    ProactiveExpect,
    ProactiveFixture,
)


def _fix(**kw) -> ProactiveFixture:
    base: dict = {
        "fixture_id": "x",
        "scenario": "x",
        "clock": ClockSpec(fixed_now="2026-05-05 12:00:00"),
        "turns": [FixtureTurn(role="user", content="hi")],
    }
    base.update(kw)
    return ProactiveFixture(**base)


def test_event_required_present_passes() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            event_extracted=[
                ExpectEvent(
                    description_contains=["Houston"],
                    follow_up_at_required=True,
                    advance_pre_hours_range=(12, 72),
                )
            ]
        )
    )
    res = ProactiveEvalResult(
        events=[
            {
                "description": "用户搬去 Houston 工作",
                "follow_up_at": "2026-05-10 12:00:00",
                "advance_pre_hours": 24,
                "advance_post_hours": 24,
                "estimated_arc_days": 14,
                "follow_up_hint": "搬家进展",
                "relational_tags": ["unresolved"],
                "event_time_start": "2026-05-10 12:00:00",
                "event_time_end": "2026-05-10 12:00:00",
            }
        ]
    )
    assert check_invariants(fix, res) == []


def test_event_required_missing_fails() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            event_extracted=[
                ExpectEvent(description_contains=["Houston"], follow_up_at_required=True)
            ]
        )
    )
    res = ProactiveEvalResult(
        events=[
            {
                "description": "用户搬去 Houston",
                "follow_up_at": None,
                "advance_pre_hours": None,
                "advance_post_hours": None,
                "estimated_arc_days": None,
                "follow_up_hint": None,
                "relational_tags": [],
                "event_time_start": None,
                "event_time_end": None,
            }
        ]
    )
    violations = check_invariants(fix, res)
    assert any("follow_up_at_required" in v for v in violations)


def test_event_forbidden_present_fails() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            event_extracted=[
                ExpectEvent(description_contains=["天气"], follow_up_at_forbidden=True)
            ]
        )
    )
    res = ProactiveEvalResult(
        events=[
            {
                "description": "今天天气真好",
                "follow_up_at": "2026-05-08",
                "advance_pre_hours": None,
                "advance_post_hours": None,
                "estimated_arc_days": None,
                "follow_up_hint": "x",
                "relational_tags": [],
                "event_time_start": None,
                "event_time_end": None,
            }
        ]
    )
    violations = check_invariants(fix, res)
    assert any("follow_up_at_forbidden" in v for v in violations)


def test_advance_range_violation() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            event_extracted=[
                ExpectEvent(description_contains=["interview"], advance_pre_hours_range=(12, 72))
            ]
        )
    )
    res = ProactiveEvalResult(
        events=[
            {
                "description": "interview",
                "advance_pre_hours": 4,
                "advance_post_hours": None,
                "estimated_arc_days": None,
                "follow_up_at": "x",
                "follow_up_hint": "x",
                "relational_tags": [],
                "event_time_start": None,
                "event_time_end": None,
            }
        ]
    )
    violations = check_invariants(fix, res)
    assert any("advance_pre_hours" in v for v in violations)


def test_phase_fire_passes_when_fire_row_exists() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            phases={"pre": ExpectPhase(fire_at_clock="2026-05-09 12:00:00", audit_action="fire")}
        )
    )
    res = ProactiveEvalResult(
        events=[],
        audit_per_phase={
            "pre": PhaseEvalResult(
                fire_at_clock="2026-05-09 12:00:00",
                audit_rows=[
                    {"phase": "pre", "action": "send", "send_ok": True, "suppress_reason": None}
                ],
                generated_messages=["x"],
            )
        },
    )
    assert check_invariants(fix, res) == []


def test_phase_fire_fails_when_skip_row() -> None:
    fix = _fix(
        expect=ProactiveExpect(phases={"pre": ExpectPhase(fire_at_clock="x", audit_action="fire")})
    )
    res = ProactiveEvalResult(
        events=[],
        audit_per_phase={
            "pre": PhaseEvalResult(
                fire_at_clock="x",
                audit_rows=[
                    {
                        "phase": "pre",
                        "action": "skip",
                        "send_ok": None,
                        "suppress_reason": "rate_limited",
                    }
                ],
                generated_messages=[],
            )
        },
    )
    violations = check_invariants(fix, res)
    assert any("phase pre" in v for v in violations)


def test_phase_skip_with_matching_reason_passes() -> None:
    fix = _fix(
        expect=ProactiveExpect(
            phases={
                "on": ExpectPhase(
                    fire_at_clock="x", audit_action="skip", suppress_reason="rate_limited"
                )
            }
        )
    )
    res = ProactiveEvalResult(
        events=[],
        audit_per_phase={
            "on": PhaseEvalResult(
                fire_at_clock="x",
                audit_rows=[
                    {
                        "phase": "on",
                        "action": "skip",
                        "suppress_reason": "rate_limited",
                        "send_ok": None,
                    }
                ],
                generated_messages=[],
            )
        },
    )
    assert check_invariants(fix, res) == []


def test_supersede_required_but_missing_fails() -> None:
    fix = _fix(
        follow_up_stage=FollowUpStage(
            after_clock="2026-05-11 18:00:00",
            turns=[FixtureTurn(role="user", content="完事了")],
            expect_supersede_event_description_contains=["完事"],
        )
    )
    res = ProactiveEvalResult(events=[], supersede_event=None)
    violations = check_invariants(fix, res)
    assert any("supersede" in v for v in violations)

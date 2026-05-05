"""Schema unit tests · pure-data round-trip · no LLM."""

from __future__ import annotations

from pathlib import Path

from tests.proactive_eval.schema import (
    ClockSpec,  # noqa: F401 — import smoke test
    ExpectEvent,  # noqa: F401
    ExpectPhase,  # noqa: F401
    FollowUpStage,  # noqa: F401
    PersonaProfileSpec,  # noqa: F401
    ProactiveDecisionSpec,  # noqa: F401
    ProactiveFixture,  # noqa: F401
    ProactiveSeed,  # noqa: F401
    ProactiveStateSpec,  # noqa: F401
    XfailSpec,  # noqa: F401
    discover_fixtures,
    load_fixture,
)


def test_minimal_yaml_loads(tmp_path: Path) -> None:
    yaml_text = """
fixture_id: t1
scenario: minimal
clock:
  fixed_now: "2026-05-05 12:00:00"
turns:
  - role: user
    content: hi
"""
    p = tmp_path / "t1.yaml"
    p.write_text(yaml_text)
    fix = load_fixture(p)
    assert fix.fixture_id == "t1"
    assert fix.in_flight_turn is False
    assert fix.expect.event_extracted == []
    assert fix.expect.phases == {}
    assert fix.seed.persona_profile is None
    assert fix.xfail is None


def test_full_yaml_loads(tmp_path: Path) -> None:
    yaml_text = """
fixture_id: t2
scenario: full
clock:
  fixed_now: "2026-05-05 12:00:00"
in_flight_turn: true
seed:
  persona_profile:
    style_summary: 陪伴
    quiet_hours: [23, 7]
    forbidden_topics: [前任]
  proactive_state:
    engagement_score: 0.2
  proactive_decisions:
    - trigger_type: follow_up
      action: send
      timestamp_offset_hours: -3.0
      phase: on
turns:
  - role: user
    content: hi
expect:
  event_extracted:
    - description_contains: [Houston]
      follow_up_at_required: true
      advance_pre_hours_range: [12, 72]
      relational_tags_any: [unresolved]
      follow_up_hint_min_chars: 4
  phases:
    pre:
      fire_at_clock: "2026-05-09 12:00:00"
      audit_action: fire
      judge_message_prompts:
        - Does the message mention Houston?
follow_up_stage:
  after_clock: "2026-05-10 18:00:00"
  turns:
    - role: user
      content: 搬家了
  expect_supersede_event_description_contains: [搬]
xfail:
  reason: PART F gap
"""
    p = tmp_path / "t2.yaml"
    p.write_text(yaml_text)
    fix = load_fixture(p)
    assert fix.in_flight_turn is True
    assert fix.seed.persona_profile is not None
    assert fix.seed.persona_profile.forbidden_topics == ["前任"]
    assert fix.seed.proactive_state.engagement_score == 0.2
    assert len(fix.seed.proactive_decisions) == 1
    assert fix.expect.event_extracted[0].advance_pre_hours_range == (12, 72)
    assert fix.expect.phases["pre"].audit_action == "fire"
    assert fix.follow_up_stage.after_clock == "2026-05-10 18:00:00"
    assert fix.xfail.reason == "PART F gap"


def test_discover_fixtures_returns_sorted(tmp_path: Path, monkeypatch) -> None:
    scripted = tmp_path / "scripted"
    scripted.mkdir()
    (scripted / "b.yaml").write_text(
        "fixture_id: b\nclock: {fixed_now: '2026-05-05 12:00:00'}\nturns: []\n"
    )
    (scripted / "a.yaml").write_text(
        "fixture_id: a\nclock: {fixed_now: '2026-05-05 12:00:00'}\nturns: []\n"
    )
    monkeypatch.setattr("tests.proactive_eval.schema.FIXTURE_ROOT", tmp_path)
    paths = discover_fixtures()
    assert [p.name for p in paths] == ["a.yaml", "b.yaml"]

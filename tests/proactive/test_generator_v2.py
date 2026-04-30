"""Tests for v3 phase-aware prompt builder in ``proactive/engines/generator``.

Stage 3.2 added ``PHASE_GUIDANCE`` (replaces v2's ``KIND_GUIDANCE``) and
``build_message_prompt`` (replaces v2's thread-driven assembly). These
tests pin the field-routing contract so future refactors don't silently
regress: ``follow_up_hint`` is the anchor, ``phase`` selects guidance,
``relational_tags`` + ``emotional_impact`` surface as context tags.
"""

from __future__ import annotations

from datetime import datetime

from echovessel.memory.models import ConceptNode, NodeType
from echovessel.proactive.core.models import PersonaProfile
from echovessel.proactive.engines.generator import (
    PHASE_GUIDANCE,
    build_message_prompt,
)


def _profile(style: str = "温柔且具体") -> PersonaProfile:
    return PersonaProfile(
        persona_id="p",
        style_summary=style,
        quiet_hours=[23, 7],
        forbidden_topics=[],
        voice_id=None,
        profile_generated_at=datetime(2026, 4, 1, 0, 0),
        profile_source="fallback_default",
    )


def _event(**overrides) -> ConceptNode:
    base: dict = {
        "id": 1,
        "persona_id": "p",
        "user_id": "u",
        "type": NodeType.EVENT,
        "description": "用户下周一面试",
        "follow_up_at": datetime(2026, 4, 27, 9, 0),
        "follow_up_hint": "周一面试",
        "advance_pre_hours": 24,
        "advance_post_hours": 24,
        "estimated_arc_days": 14,
        "relational_tags": ["commitment"],
        "emotional_impact": 4,
        "event_time_start": datetime(2026, 4, 27, 9, 0),
        "event_time_end": datetime(2026, 4, 27, 11, 0),
        "created_at": datetime(2026, 4, 20, 0, 0),
    }
    base.update(overrides)
    return ConceptNode(**base)


def test_phase_guidance_covers_all_canonical_phases():
    """The five canonical phases all map to non-empty guidance text."""
    for phase in ("pre", "on", "post", "check_1", "check_2", "check_3"):
        assert PHASE_GUIDANCE[phase], f"phase {phase!r} has empty guidance"


def test_build_message_prompt_includes_follow_up_hint():
    prompt = build_message_prompt(
        event=_event(follow_up_hint="周一面试"),
        phase="pre",
        profile=_profile(),
        memory_context="(empty)",
        now=datetime(2026, 4, 26, 9, 0),
    )
    assert "周一面试" in prompt
    assert "锚点" in prompt


def test_build_message_prompt_handles_missing_anchor():
    prompt = build_message_prompt(
        event=_event(follow_up_hint=None),
        phase="pre",
        profile=_profile(),
        memory_context="",
        now=datetime(2026, 4, 26, 9, 0),
    )
    assert "(无)" in prompt


def test_build_message_prompt_routes_phase_guidance():
    """Each phase's guidance string makes it into the prompt verbatim."""
    for phase, guidance in PHASE_GUIDANCE.items():
        prompt = build_message_prompt(
            event=_event(),
            phase=phase,
            profile=_profile(),
            memory_context="",
            now=datetime(2026, 4, 26, 9, 0),
        )
        assert guidance in prompt
        assert f"阶段({phase})" in prompt


def test_build_message_prompt_unknown_phase_falls_back_to_check_3():
    """``check_4`` / ``check_5`` etc. reuse the ``check_3`` restraint
    text rather than degrading to silence."""
    prompt = build_message_prompt(
        event=_event(),
        phase="check_4",
        profile=_profile(),
        memory_context="",
        now=datetime(2026, 4, 30, 9, 0),
    )
    assert PHASE_GUIDANCE["check_3"] in prompt


def test_build_message_prompt_surfaces_emotional_impact_and_tags():
    prompt = build_message_prompt(
        event=_event(emotional_impact=7, relational_tags=["commitment", "vulnerability"]),
        phase="post",
        profile=_profile(),
        memory_context="",
        now=datetime(2026, 4, 28, 9, 0),
    )
    assert "impact: 7" in prompt
    assert "commitment" in prompt
    assert "vulnerability" in prompt


def test_build_message_prompt_includes_style_summary_and_now():
    now = datetime(2026, 4, 28, 12, 30)
    prompt = build_message_prompt(
        event=_event(),
        phase="on",
        profile=_profile(style="温柔但不过分热情"),
        memory_context="recent_chat",
        now=now,
    )
    assert "温柔但不过分热情" in prompt
    assert "recent_chat" in prompt
    assert str(now) in prompt


def test_build_message_prompt_includes_event_time_window():
    prompt = build_message_prompt(
        event=_event(),
        phase="pre",
        profile=_profile(),
        memory_context="",
        now=datetime(2026, 4, 26, 9, 0),
    )
    assert "2026-04-27 09:00" in prompt
    assert "2026-04-27 11:00" in prompt


def test_build_message_prompt_handles_missing_event_times():
    prompt = build_message_prompt(
        event=_event(event_time_start=None, event_time_end=None),
        phase="pre",
        profile=_profile(),
        memory_context="",
        now=datetime(2026, 4, 26, 9, 0),
    )
    assert "原事件 ? - ?" in prompt

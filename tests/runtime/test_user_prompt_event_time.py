"""R4 · build_user_prompt renders event_time delta phrase.

Spec: develop-docs/initiatives/_active/2026-04-persona-6-layer-memory/
03-spec-event-time-anchor.md sub-task 6 + verification of the MBV
gate Case 7 ("4-19 said '下周 exam', 4-29 retrieve renders 'status=
active (3 days in)'").

These are pure render-layer tests — no DB, no embedding, no LLM. We
construct ConceptNode objects in-memory, hand them as fake "top
memories" to ``build_user_prompt`` together with a ``now`` anchor,
and assert on the output string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from echovessel.core.types import NodeType
from echovessel.memory.models import ConceptNode
from echovessel.runtime.interaction import build_user_prompt


@dataclass
class _FakeScored:
    """Minimal stand-in for memory.retrieve.ScoredMemory.

    ``build_user_prompt`` only reads ``.node`` off each candidate, so
    keeping a real dataclass field is the cleanest mock surface.
    """

    node: ConceptNode


def _event(
    *,
    description: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> _FakeScored:
    return _FakeScored(
        node=ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description=description,
            emotional_impact=-2,
            event_time_start=start,
            event_time_end=end,
        )
    )


def _thought(*, description: str) -> _FakeScored:
    return _FakeScored(
        node=ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            description=description,
            emotional_impact=0,
        )
    )


def test_event_with_event_time_renders_delta_phrase():
    """Case 7 MBV gate: 4-26~5-02 viewed on 4-29 must render
    'status=active (3 days in)' attached to the event line."""
    out = build_user_prompt(
        top_memories=[
            _event(
                description="用户提到下周有期末考",
                start=datetime(2026, 4, 26),
                end=datetime(2026, 5, 2),
            )
        ],
        recent_messages=[],
        user_message="考试怎么样了",
        now=datetime(2026, 4, 29),
    )
    assert "用户提到下周有期末考" in out
    assert "event 2026-04-26~2026-05-02" in out
    assert "status=active" in out
    assert "3 days in" in out


def test_atemporal_event_renders_no_delta_phrase():
    """Pure facts ("user is left-handed") must not carry a fake date."""
    out = build_user_prompt(
        top_memories=[_event(description="user likes cats")],
        recent_messages=[],
        user_message="hi",
        now=datetime(2026, 4, 29),
    )
    line = next(line for line in out.splitlines() if "user likes cats" in line)
    assert " · event " not in line
    assert "status=" not in line


def test_past_event_renders_days_ago():
    out = build_user_prompt(
        top_memories=[
            _event(
                description="用户提到上周末出去玩",
                start=datetime(2026, 4, 18),
                end=datetime(2026, 4, 19),
            )
        ],
        recent_messages=[],
        user_message="周末过得怎样",
        now=datetime(2026, 4, 29),
    )
    assert "status=past" in out
    assert "10 days ago" in out


def test_planned_event_renders_in_n_days():
    out = build_user_prompt(
        top_memories=[
            _event(
                description="用户下周一面试",
                start=datetime(2026, 5, 4),
                end=datetime(2026, 5, 4),
            )
        ],
        recent_messages=[],
        user_message="紧张吗",
        now=datetime(2026, 4, 29),
    )
    assert "status=planned" in out
    assert "in 5 days" in out


def test_now_none_renders_bare_description_for_back_compat():
    """Tests that don't care about R4 can pass ``now=None`` and get
    pre-Spec-3 behaviour — a clean event description with no delta."""
    out = build_user_prompt(
        top_memories=[
            _event(
                description="用户提到下周有期末考",
                start=datetime(2026, 4, 26),
                end=datetime(2026, 5, 2),
            )
        ],
        recent_messages=[],
        user_message="hi",
        now=None,
    )
    assert "用户提到下周有期末考" in out
    assert "status=" not in out
    assert " · event " not in out


def test_thought_lines_unchanged_by_event_time_path():
    """L4 thoughts MUST NOT carry an event_time delta. They have no
    date scope and the rendering code branches on type."""
    out = build_user_prompt(
        top_memories=[
            _thought(description="this person leans on humor when stressed"),
            _event(
                description="用户提到下周有期末考",
                start=datetime(2026, 4, 26),
                end=datetime(2026, 5, 2),
            ),
        ],
        recent_messages=[],
        user_message="hi",
        now=datetime(2026, 4, 29),
    )
    thought_line = next(line for line in out.splitlines() if "leans on humor" in line)
    assert "status=" not in thought_line
    assert " · event " not in thought_line


def test_event_section_header_renders_under_things_you_remember():
    """The R4 spec's exit criterion mentions persona uses the delta
    phrase to answer naturally. The events still live under the
    canonical L3 section header so the LLM knows what kind of memory
    they are."""
    out = build_user_prompt(
        top_memories=[
            _event(
                description="用户提到下周有期末考",
                start=datetime(2026, 4, 26),
                end=datetime(2026, 5, 2),
            )
        ],
        recent_messages=[],
        user_message="考试怎么样",
        now=datetime(2026, 4, 29),
    )
    assert "# Recent things you remember happened" in out

"""v0.5 prompt-section invariants (plan §2).

- ``# About yourself (private self-narrative)`` and ``# Relationship``
  must never appear in the system prompt.
- ``# How you see yourself lately`` must render when
  ``persona_thoughts`` are supplied to ``build_user_prompt`` /
  ``build_turn_user_prompt``.
- ``# About {canonical_name}`` must render exactly once per anchored
  entity that carries a non-empty description, and must be skipped
  silently when the description is empty.
"""

from __future__ import annotations

from datetime import datetime

from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory.models import ConceptNode, CoreBlock
from echovessel.runtime.turn.coordinator import (
    build_system_prompt,
    build_user_prompt,
)


def _persona_block() -> CoreBlock:
    return CoreBlock(
        persona_id="p",
        user_id=None,
        label=BlockLabel.PERSONA,
        content="你是温柔的朋友",
    )


def test_about_yourself_section_removed_from_system_prompt():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[_persona_block()],
    )
    assert "# About yourself" not in prompt
    assert "private self-narrative" not in prompt


def test_relationship_section_removed_from_system_prompt():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[_persona_block()],
    )
    assert "# Relationship" not in prompt


def test_how_you_see_yourself_lately_renders_persona_thoughts():
    thoughts = [
        ConceptNode(
            id=11,
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="persona",
            description="我最近更愿意先听对方说完再反应",
            created_at=datetime(2026, 4, 24, 10, 0, 0),
        )
    ]
    prompt = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hello",
        persona_thoughts=thoughts,
    )
    assert "# How you see yourself lately" in prompt
    assert "我最近更愿意先听对方说完再反应" in prompt


def test_how_you_see_yourself_lately_absent_when_no_persona_thoughts():
    prompt = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
    )
    assert "# How you see yourself lately" not in prompt


def test_about_entity_section_renders_when_description_present():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[_persona_block()],
        entity_descriptions=[
            ("黄逸扬", "Alan 在 SF 的室友,做算法的。"),
        ],
    )
    assert "# About 黄逸扬" in prompt
    assert "SF 的室友" in prompt


def test_about_entity_section_skips_empty_description():
    """Anchored entities with no description must NOT emit a header —
    a bare ``# About X`` block would waste prompt tokens and confuse
    the LLM."""
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[_persona_block()],
        entity_descriptions=[
            ("Scott", "   "),  # whitespace-only: treat as empty
            ("Mochi", ""),
        ],
    )
    assert "# About Scott" not in prompt
    assert "# About Mochi" not in prompt

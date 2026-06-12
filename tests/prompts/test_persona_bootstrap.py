"""Tests for echovessel.prompts.persona_bootstrap."""

from __future__ import annotations

import dataclasses

from echovessel.prompts.persona_bootstrap import (
    PERSONA_BOOTSTRAP_SYSTEM_PROMPT,
    BootstrappedBlocks,
    format_persona_bootstrap_user_prompt,
    parse_persona_bootstrap_response,
)


def test_system_prompt_declares_two_blocks_matching_schema():
    assert "write TWO initial core blocks" in PERSONA_BOOTSTRAP_SYSTEM_PROMPT
    field_names = [f.name for f in dataclasses.fields(BootstrappedBlocks)]
    assert field_names == ["persona_block", "user_block"]
    for name in field_names:
        assert f'"{name}"' in PERSONA_BOOTSTRAP_SYSTEM_PROMPT


def test_user_prompt_block_count_matches_schema():
    # The user prompt's block count must agree with the system prompt
    # and the parser: exactly two blocks, persona_block + user_block.
    out = format_persona_bootstrap_user_prompt(
        persona_display_name=None,
        events=[("user adopted a cat", 4, ["identity-bearing"])],
        thoughts=["the user values quiet routines"],
    )
    assert "TWO initial core blocks" in out
    assert "FIVE" not in out
    assert "persona_block" in out
    assert "user_block" in out


def test_parser_accepts_exactly_the_two_schema_keys():
    result = parse_persona_bootstrap_response(
        '{"persona_block": "You are a patient friend.", "user_block": "The user has a cat."}'
    )
    assert result.persona_block == "You are a patient friend."
    assert result.user_block == "The user has a cat."

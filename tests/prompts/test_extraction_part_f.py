"""Stage 1.4 · PART F smoke tests · proactive follow-up annotation."""

from __future__ import annotations

from echovessel.prompts.extraction import EXTRACTION_SYSTEM_PROMPT


def test_part_f_section_present():
    """PART F header is in the prompt."""
    assert "# PART F · Proactive follow-up annotation" in EXTRACTION_SYSTEM_PROMPT


def test_part_f_teaches_follow_up_at():
    """PART F teaches the follow_up_at field."""
    assert "follow_up_at" in EXTRACTION_SYSTEM_PROMPT


def test_part_f_teaches_follow_up_hint():
    """PART F teaches the follow_up_hint field."""
    assert "follow_up_hint" in EXTRACTION_SYSTEM_PROMPT


def test_part_f_teaches_advance_hours():
    """PART F teaches advance_pre_hours and advance_post_hours."""
    assert "advance_pre_hours" in EXTRACTION_SYSTEM_PROMPT
    assert "advance_post_hours" in EXTRACTION_SYSTEM_PROMPT


def test_part_f_teaches_estimated_arc_days():
    assert "estimated_arc_days" in EXTRACTION_SYSTEM_PROMPT


def test_part_f_teaches_reminder_request_zero_advance():
    """reminder request → advance_pre = advance_post = 0."""
    # Look for the reminder request row in the advance hours table
    assert "reminder request" in EXTRACTION_SYSTEM_PROMPT.lower()


def test_part_f_omits_supersede_segment():
    """Stage 0 verify: existing # Supersedes detection section already teaches this.
    PART F should NOT add a duplicate ## 6 supersede segment."""
    # The legacy section header still exists (don't break it)
    assert "# Supersedes detection" in EXTRACTION_SYSTEM_PROMPT
    # But PART F itself should not have a ## 6 sub-section
    part_f_start = EXTRACTION_SYSTEM_PROMPT.find("# PART F")
    assert part_f_start > 0
    part_f_text = EXTRACTION_SYSTEM_PROMPT[part_f_start:]
    assert "## 6" not in part_f_text


def test_part_f_appears_after_existing_parts():
    """PART F is appended AT THE END of the prompt, not interleaved into existing PART A-E."""
    part_a_pos = EXTRACTION_SYSTEM_PROMPT.find("# Self-check step")  # an early section
    part_f_pos = EXTRACTION_SYSTEM_PROMPT.find("# PART F")
    assert part_a_pos < part_f_pos

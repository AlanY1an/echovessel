"""Stage 1.4 · PART F smoke tests · proactive follow-up annotation."""

from __future__ import annotations

from datetime import datetime

from echovessel.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    _parse_event,
)


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


# ---------------------------------------------------------------------------
# Parser-side tests: confirm _parse_event reads the PART F fields off JSON
# and routes them onto RawExtractedEvent without crashing on missing/null
# entries (backward compat with pre-v3 prompts).
# ---------------------------------------------------------------------------


def _base_event_json(**overrides):
    base = {
        "description": "user has interview Monday 10am",
        "emotional_impact": 3,
        "emotion_tags": ["nervous"],
        "relational_tags": ["unresolved"],
        "subject": "user",
        "event_time": {
            "start": "2026-05-11T10:00:00",
            "end": "2026-05-11T10:00:00",
        },
    }
    base.update(overrides)
    return base


def test_parse_event_reads_follow_up_at_iso():
    """JSON with valid ISO datetime → RawExtractedEvent.follow_up_at is datetime."""
    raw = _base_event_json(follow_up_at="2026-05-11T10:00:00")
    ev = _parse_event(raw, index=0)
    assert isinstance(ev.follow_up_at, datetime)
    assert ev.follow_up_at == datetime(2026, 5, 11, 10, 0, 0)


def test_parse_event_handles_null_follow_up_at():
    """JSON with null → None (no error)."""
    raw = _base_event_json(follow_up_at=None)
    ev = _parse_event(raw, index=0)
    assert ev.follow_up_at is None


def test_parse_event_reads_advance_hours():
    """JSON with advance_pre_hours: 24 and advance_post_hours: 0 → both populated."""
    raw = _base_event_json(advance_pre_hours=24, advance_post_hours=0)
    ev = _parse_event(raw, index=0)
    assert ev.advance_pre_hours == 24
    assert ev.advance_post_hours == 0


def test_parse_event_reads_follow_up_hint():
    """JSON with hint string → field populated."""
    raw = _base_event_json(follow_up_hint="面试结果")
    ev = _parse_event(raw, index=0)
    assert ev.follow_up_hint == "面试结果"


def test_parse_event_reads_estimated_arc_days():
    """JSON with int → field populated."""
    raw = _base_event_json(estimated_arc_days=14)
    ev = _parse_event(raw, index=0)
    assert ev.estimated_arc_days == 14


def test_parse_event_omits_part_f_fields_gracefully():
    """JSON without any PART F keys → all 5 fields are None (backward compat)."""
    raw = _base_event_json()
    ev = _parse_event(raw, index=0)
    assert ev.follow_up_at is None
    assert ev.follow_up_hint is None
    assert ev.estimated_arc_days is None
    assert ev.advance_pre_hours is None
    assert ev.advance_post_hours is None


def test_parse_event_invalid_follow_up_at_falls_back_to_none():
    """Malformed ISO string → None (defensive coercion, no crash)."""
    raw = _base_event_json(follow_up_at="not-an-iso-date")
    ev = _parse_event(raw, index=0)
    assert ev.follow_up_at is None


def test_parse_event_empty_follow_up_hint_is_none():
    """Empty / whitespace hint → None."""
    raw = _base_event_json(follow_up_hint="   ")
    ev = _parse_event(raw, index=0)
    assert ev.follow_up_hint is None


def test_parse_event_non_int_advance_hours_is_none():
    """String / float-with-fraction advance hours → None (defensive)."""
    raw = _base_event_json(advance_pre_hours="lots", advance_post_hours=1.5)
    ev = _parse_event(raw, index=0)
    assert ev.advance_pre_hours is None
    assert ev.advance_post_hours is None

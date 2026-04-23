"""R4 · event_time anchor on the extraction prompt + parser.

Spec: develop-docs/initiatives/_active/2026-04-persona-6-layer-memory/
03-spec-event-time-anchor.md sub-tasks 1 + 2.

These tests are "stability guards" for the prompt text plus end-to-end
round-trip parser checks. They MUST keep the LLM's anchor wiring intact:
if the ``<<NOW>>`` placeholder gets stripped from the user prompt or the
``event_time`` JSON shape gets removed from the system prompt, the LLM
will silently fall back to its training-time sense of "now" and start
hallucinating absolute dates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from echovessel.core.types import EventTime
from echovessel.prompts.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractionParseError,
    format_extraction_user_prompt,
    parse_extraction_response,
)

# ---------------------------------------------------------------------------
# System prompt — stability guards for the time-binding section
# ---------------------------------------------------------------------------


def test_system_prompt_documents_now_anchor_placeholder():
    """The system prompt MUST tell the LLM that <<NOW>> is its anchor.

    If this token disappears the LLM has no way to know the user prompt
    will carry a CONTEXT TIMESTAMPS block — the whole R4 chain breaks.
    """
    assert "<<NOW>>" in EXTRACTION_SYSTEM_PROMPT


def test_system_prompt_describes_event_time_output_shape():
    """The system prompt MUST describe the event_time JSON shape."""
    assert "event_time" in EXTRACTION_SYSTEM_PROMPT
    assert '"start"' in EXTRACTION_SYSTEM_PROMPT
    assert '"end"' in EXTRACTION_SYSTEM_PROMPT


def test_system_prompt_says_atemporal_facts_emit_null():
    """Atemporal facts ("user likes cats") MUST emit `event_time: null`,
    not a fabricated date. Stability guard."""
    assert "atemporal" in EXTRACTION_SYSTEM_PROMPT.lower()
    # Either explicit "event_time: null" or "null" near event_time —
    # accept both phrasings; the key guard is that the prompt steers
    # the LLM AWAY from inventing dates for atemporal facts.
    text = EXTRACTION_SYSTEM_PROMPT
    assert "null" in text.lower()


def test_system_prompt_warns_against_dropping_timezone():
    """Timezone offset MUST survive into the JSON output. Stability guard."""
    assert "timezone" in EXTRACTION_SYSTEM_PROMPT.lower()
    assert "UTC" in EXTRACTION_SYSTEM_PROMPT  # "Do NOT shift to UTC"


# ---------------------------------------------------------------------------
# User prompt — anchor injection
# ---------------------------------------------------------------------------


def test_user_prompt_contains_now_anchor_line():
    out = format_extraction_user_prompt(
        session_id="s",
        started_at_iso="2026-04-19T12:00:00+08:00",
        closed_at_iso="2026-04-19T12:30:00+08:00",
        message_count=2,
        messages=[("12:00", "user", "下周有期末考")],
        now_iso="2026-04-19T12:00:00+08:00",
    )
    assert "CONTEXT TIMESTAMPS:" in out
    assert "<<NOW>>: 2026-04-19T12:00:00+08:00" in out


def test_user_prompt_now_iso_defaults_to_started_at_iso():
    """Legacy callers that don't pass `now_iso` still get an anchor —
    we fall back to ``started_at_iso``. Without this, every old call
    site would silently lose the R4 chain."""
    out = format_extraction_user_prompt(
        session_id="s",
        started_at_iso="2026-04-19T12:00:00+08:00",
        closed_at_iso="2026-04-19T12:30:00+08:00",
        message_count=1,
        messages=[("12:00", "user", "hi")],
    )
    assert "<<NOW>>: 2026-04-19T12:00:00+08:00" in out


# ---------------------------------------------------------------------------
# Round-trip parser
# ---------------------------------------------------------------------------


def _ok_event(event_time: dict | None) -> dict:
    return {
        "description": "用户提到下周有期末考",
        "emotional_impact": -2,
        "emotion_tags": ["anxiety"],
        "relational_tags": [],
        "event_time": event_time,
    }


def test_parse_event_time_round_trip_interval():
    payload = {
        "events": [
            _ok_event(
                {
                    "start": "2026-04-26T00:00:00+08:00",
                    "end": "2026-05-02T23:59:59+08:00",
                }
            )
        ],
        "self_check_notes": "ok",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    assert len(parsed.events) == 1
    et = parsed.events[0].event_time
    assert isinstance(et, EventTime)
    assert et.start == datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert et.end == datetime(2026, 5, 2, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))


def test_parse_event_time_atemporal_emits_none():
    payload = {
        "events": [_ok_event(None)],
        "self_check_notes": "",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    assert parsed.events[0].event_time is None


def test_parse_event_time_instant_when_end_missing():
    payload = {
        "events": [_ok_event({"start": "2026-04-19T12:34:56+08:00"})],
        "self_check_notes": "",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    et = parsed.events[0].event_time
    assert et is not None
    assert et.end is None


def test_parse_event_time_explicit_null_end_is_instant():
    payload = {
        "events": [_ok_event({"start": "2026-04-19T12:34:56+08:00", "end": None})],
        "self_check_notes": "",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    assert parsed.events[0].event_time is not None
    assert parsed.events[0].event_time.end is None


def test_parse_event_time_field_missing_treated_as_atemporal():
    """Backwards-compat: an extraction response that doesn't include
    event_time at all (older models, prompt regression) must NOT crash —
    treat absence as atemporal. The DB column is nullable too."""
    payload = {
        "events": [
            {
                "description": "user likes cats",
                "emotional_impact": 1,
                "emotion_tags": [],
                "relational_tags": [],
            }
        ],
        "self_check_notes": "",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    assert parsed.events[0].event_time is None


def test_parse_event_time_rejects_end_before_start():
    """Monotonic invariant guard: a malformed range is dropped at the
    parser layer rather than reaching the DB CHECK constraint."""
    payload = {
        "events": [
            _ok_event(
                {
                    "start": "2026-05-02T00:00:00+08:00",
                    "end": "2026-04-26T00:00:00+08:00",
                }
            )
        ],
        "self_check_notes": "",
    }
    with pytest.raises(ExtractionParseError):
        parse_extraction_response(json.dumps(payload))


def test_parse_event_time_rejects_non_object():
    payload = {
        "events": [_ok_event("2026-04-26")],  # type: ignore[arg-type]
        "self_check_notes": "",
    }
    with pytest.raises(ExtractionParseError):
        parse_extraction_response(json.dumps(payload))


def test_parse_event_time_rejects_invalid_iso():
    payload = {
        "events": [_ok_event({"start": "next monday"})],
        "self_check_notes": "",
    }
    with pytest.raises(ExtractionParseError):
        parse_extraction_response(json.dumps(payload))


def test_parse_event_time_preserves_timezone_offset():
    """The renderer downstream uses the user's local wall clock.
    UTC-shifting at parse time would make ``status=active`` flicker
    around midnight local."""
    payload = {
        "events": [
            _ok_event(
                {
                    "start": "2026-04-26T00:00:00+08:00",
                    "end": "2026-05-02T23:59:59+08:00",
                }
            )
        ],
        "self_check_notes": "",
    }
    parsed = parse_extraction_response(json.dumps(payload))
    et = parsed.events[0].event_time
    assert et is not None
    assert et.start.utcoffset() == timedelta(hours=8)
    assert et.end is not None
    assert et.end.utcoffset() == timedelta(hours=8)

"""R4 · derive_event_status + render_event_delta_phrase.

Spec: develop-docs/initiatives/_active/2026-04-persona-6-layer-memory/
03-spec-event-time-anchor.md sub-task 5.

Behaviour guarantee: rendering is at DAY precision. Hour-precision
makes the persona sound like a database cursor instead of a friend
remembering. Atemporal events render no delta at all so callers can
append unconditionally.
"""

from __future__ import annotations

from datetime import UTC, datetime

from echovessel.core.types import NodeType
from echovessel.memory.models import ConceptNode
from echovessel.memory.retrieve import (
    derive_event_status,
    render_event_delta_phrase,
)


def _node(*, start: datetime | None, end: datetime | None) -> ConceptNode:
    """Build a minimal ConceptNode in-memory (NOT inserted)."""
    return ConceptNode(
        persona_id="p_test",
        user_id="self",
        type=NodeType.EVENT,
        description="用户提到下周有期末考",
        emotional_impact=-2,
        event_time_start=start,
        event_time_end=end,
    )


# ---------------------------------------------------------------------------
# derive_event_status — 4 branches
# ---------------------------------------------------------------------------


def test_derive_status_atemporal_when_both_bounds_none():
    n = _node(start=None, end=None)
    assert derive_event_status(n, datetime(2026, 4, 29)) == "atemporal"


def test_derive_status_active_when_now_inside_window():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    assert derive_event_status(n, datetime(2026, 4, 29)) == "active"


def test_derive_status_planned_when_now_before_start():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    assert derive_event_status(n, datetime(2026, 4, 19)) == "planned"


def test_derive_status_past_when_now_after_end():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    assert derive_event_status(n, datetime(2026, 5, 10)) == "past"


def test_derive_status_instant_event_active_at_exact_moment():
    """Instant events (start == end) are 'active' at that exact moment,
    'past' the next day, 'planned' the day before."""
    moment = datetime(2026, 4, 26, 12, 0, 0)
    n = _node(start=moment, end=moment)
    assert derive_event_status(n, moment) == "active"
    assert derive_event_status(n, datetime(2026, 4, 27)) == "past"
    assert derive_event_status(n, datetime(2026, 4, 25)) == "planned"


def test_derive_status_handles_start_only():
    """A node with start but no end is treated as instant."""
    n = _node(start=datetime(2026, 4, 26, 12, 0), end=None)
    assert derive_event_status(n, datetime(2026, 4, 26, 12, 0)) == "active"
    assert derive_event_status(n, datetime(2026, 4, 28)) == "past"


def test_derive_status_handles_end_only():
    """A node with end but no start is treated as instant at end."""
    n = _node(start=None, end=datetime(2026, 4, 26, 12, 0))
    assert derive_event_status(n, datetime(2026, 4, 26, 12, 0)) == "active"
    assert derive_event_status(n, datetime(2026, 4, 28)) == "past"


# ---------------------------------------------------------------------------
# render_event_delta_phrase — output format
# ---------------------------------------------------------------------------


def test_render_delta_atemporal_returns_empty_string():
    n = _node(start=None, end=None)
    assert render_event_delta_phrase(n, datetime(2026, 4, 29)) == ""


def test_render_delta_active_with_days_in():
    """4-26~5-02 viewed on 4-29 → 3 days in."""
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 29))
    assert "event 2026-04-26~2026-05-02" in out
    assert "status=active" in out
    assert "3 days in" in out


def test_render_delta_active_just_started_on_first_day():
    """Day 0 should read as 'just started', not '0 days in'."""
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 26, 12, 0))
    assert "just started" in out
    assert "status=active" in out


def test_render_delta_active_one_day_in_uses_singular():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 27))
    assert "1 day in" in out


def test_render_delta_planned_with_days_until():
    """Renders ``in N days`` ahead of the start."""
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 19))
    assert "status=planned" in out
    assert "in 7 days" in out


def test_render_delta_planned_today_when_zero_days_off():
    n = _node(
        start=datetime(2026, 4, 26, 18, 0),
        end=datetime(2026, 4, 26, 18, 0),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 26, 9, 0))
    # start == end == today, but 9am < 6pm → still "planned"
    assert "status=planned" in out
    assert "today" in out


def test_render_delta_planned_in_one_day_uses_singular():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 25))
    assert "in 1 day" in out


def test_render_delta_past_with_days_ago():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 5, 10))
    assert "status=past" in out
    assert "8 days ago" in out


def test_render_delta_past_one_day_ago_uses_singular():
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 5, 3))
    assert "1 day ago" in out


def test_render_delta_single_day_event_renders_date_once():
    moment = datetime(2026, 4, 26)
    n = _node(start=moment, end=moment)
    out = render_event_delta_phrase(n, datetime(2026, 4, 28))
    # No tilde when start == end at day precision.
    assert "event 2026-04-26" in out
    assert "~" not in out


def test_render_delta_starts_with_separator_so_callers_can_concatenate():
    """The return string begins with ``" · "`` so a caller can write
    ``f"- {desc}{delta}"`` without checking for empty."""
    n = _node(
        start=datetime(2026, 4, 26),
        end=datetime(2026, 5, 2),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 29))
    assert out.startswith(" · ")


def test_render_delta_ignores_hour_precision_in_status_label():
    """Day-precision: a 6pm event "yesterday" reads as 1 day ago, not
    18 hours ago."""
    n = _node(
        start=datetime(2026, 4, 28, 18, 0),
        end=datetime(2026, 4, 28, 18, 0),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 29, 9, 0))
    assert "1 day ago" in out
    # Must NOT mention hours.
    assert "hour" not in out.lower()


def test_render_delta_handles_tz_aware_inputs():
    """Both bounds tz-aware (typical for production data) — the
    renderer must not crash and must still produce day labels."""
    tz_8 = UTC
    n = _node(
        start=datetime(2026, 4, 26, tzinfo=tz_8),
        end=datetime(2026, 5, 2, tzinfo=tz_8),
    )
    out = render_event_delta_phrase(n, datetime(2026, 4, 29, tzinfo=tz_8))
    assert "status=active" in out
    assert "3 days in" in out

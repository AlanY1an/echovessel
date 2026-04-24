"""Spec 5 plan §6.4 / decision 2 · day-bucket header rendering.

Two surfaces:

1. ``day_bucket_of(when, now)`` maps a recall timestamp to one of the
   five labels in ``DAY_BUCKET_ORDER``. Cutoffs are tight on purpose
   — coarser bands feel like channel cadence metadata, finer bands
   leak hour-precision into the prompt.

2. ``build_user_prompt`` groups the recent_messages window under
   ``## <Bucket>`` headers in OLDER → NEWER order (matching how a
   human reconstructs a memory). Empty buckets are skipped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from echovessel.core.types import MessageRole
from echovessel.memory.models import RecallMessage
from echovessel.runtime.interaction import (
    DAY_BUCKET_ORDER,
    build_user_prompt,
    day_bucket_of,
)

_NOW = datetime(2026, 4, 24, 14, 0, 0)


def _msg(*, when: datetime, content: str, role: MessageRole) -> RecallMessage:
    return RecallMessage(
        session_id="s",
        persona_id="p",
        user_id="self",
        channel_id="test",
        role=role,
        content=content,
        day=date.today(),
        token_count=len(content),
        created_at=when,
    )


# ---------------------------------------------------------------------------
# day_bucket_of · 5 cutoffs
# ---------------------------------------------------------------------------


def test_bucket_just_now_within_30_minutes():
    assert day_bucket_of(_NOW - timedelta(minutes=10), _NOW) == "Just now"
    assert day_bucket_of(_NOW - timedelta(minutes=29), _NOW) == "Just now"
    assert day_bucket_of(_NOW, _NOW) == "Just now"


def test_bucket_earlier_today_after_30_minutes_same_date():
    assert day_bucket_of(_NOW - timedelta(minutes=31), _NOW) == "Earlier today"
    assert day_bucket_of(_NOW - timedelta(hours=8), _NOW) == "Earlier today"


def test_bucket_yesterday_one_calendar_day_back():
    assert day_bucket_of(_NOW - timedelta(days=1), _NOW) == "Yesterday"
    # Boundary: very early yesterday is still "Yesterday"
    yesterday = (_NOW - timedelta(days=1)).replace(hour=0, minute=5)
    assert day_bucket_of(yesterday, _NOW) == "Yesterday"


def test_bucket_earlier_this_week_within_seven_days():
    assert day_bucket_of(_NOW - timedelta(days=2), _NOW) == "Earlier this week"
    assert day_bucket_of(_NOW - timedelta(days=6), _NOW) == "Earlier this week"


def test_bucket_older_beyond_seven_days():
    assert day_bucket_of(_NOW - timedelta(days=7), _NOW) == "Older"
    assert day_bucket_of(_NOW - timedelta(days=42), _NOW) == "Older"


def test_bucket_ordering_constant_is_older_to_newer():
    """The DAY_BUCKET_ORDER tuple must walk OLDER → NEWER. Reversing
    it would put 'Just now' at the top of the rendered conversation,
    which is the opposite of how transcripts read."""
    assert DAY_BUCKET_ORDER == (
        "Older",
        "Earlier this week",
        "Yesterday",
        "Earlier today",
        "Just now",
    )


# ---------------------------------------------------------------------------
# build_user_prompt · # Our recent conversation buckets
# ---------------------------------------------------------------------------


def test_recent_conversation_groups_by_bucket():
    msgs = [
        _msg(when=_NOW - timedelta(days=10), content="last month news", role=MessageRole.USER),
        _msg(when=_NOW - timedelta(days=1), content="yesterday news", role=MessageRole.USER),
        _msg(when=_NOW - timedelta(minutes=5), content="just messaged", role=MessageRole.USER),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=msgs,
        user_message="now",
        now=_NOW,
    )
    assert "## Older" in out
    assert "## Yesterday" in out
    assert "## Just now" in out


def test_recent_conversation_renders_buckets_in_older_to_newer_order():
    """Verify the actual lexical order in the prompt matches
    DAY_BUCKET_ORDER, regardless of message insertion order."""
    msgs = [
        _msg(when=_NOW - timedelta(minutes=5), content="just messaged", role=MessageRole.USER),
        _msg(when=_NOW - timedelta(days=10), content="last month news", role=MessageRole.USER),
        _msg(when=_NOW - timedelta(hours=4), content="lunch chat", role=MessageRole.USER),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=msgs,
        user_message="now",
        now=_NOW,
    )
    older_idx = out.index("## Older")
    today_idx = out.index("## Earlier today")
    just_now_idx = out.index("## Just now")
    assert older_idx < today_idx < just_now_idx


def test_recent_conversation_skips_empty_buckets():
    msgs = [
        _msg(when=_NOW - timedelta(minutes=5), content="x", role=MessageRole.USER),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=msgs,
        user_message="now",
        now=_NOW,
    )
    # Only "Just now" bucket should appear
    assert "## Just now" in out
    assert "## Yesterday" not in out
    assert "## Older" not in out


def test_recent_conversation_role_prefixes_unchanged():
    """Day bucketing must not regress the role-pronoun rendering
    (`them` / `me` instead of literal 'user' / 'persona')."""
    msgs = [
        _msg(when=_NOW - timedelta(minutes=5), content="hi there", role=MessageRole.USER),
        _msg(when=_NOW - timedelta(minutes=4), content="hello", role=MessageRole.PERSONA),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=msgs,
        user_message="now",
        now=_NOW,
    )
    assert "them: hi there" in out
    assert "me: hello" in out
    # Literal role names must NOT leak into the prompt.
    assert "user:" not in out
    assert "persona:" not in out


def test_recent_conversation_falls_back_flat_when_no_now():
    """``now=None`` keeps the legacy flat render — needed by tests
    that don't set up a clock."""
    msgs = [
        _msg(when=_NOW - timedelta(minutes=5), content="hi", role=MessageRole.USER),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=msgs,
        user_message="now",
        now=None,
    )
    assert "# Our recent conversation" in out
    assert "## " not in out  # No bucket headers
    assert "them: hi" in out


# ---------------------------------------------------------------------------
# # Recent sessions section · day-bucketed
# ---------------------------------------------------------------------------


def test_recent_sessions_section_renders_with_bucket_prefix():
    """``# Recent sessions`` (session_summary thoughts) shares the
    same bucket helper, but renders the bucket inline in `[Bucket]`
    form rather than as a `## ` header (one section, multi-bucket)."""
    from echovessel.core.types import NodeType
    from echovessel.memory.models import ConceptNode

    summaries = [
        ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            description="user shared grad school news",
            emotional_impact=4,
            emotion_tags=["session_summary"],
            source_session_id="s_old",
            created_at=_NOW - timedelta(days=2),
        ),
        ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            description="we caught up on apex",
            emotional_impact=2,
            emotion_tags=["session_summary"],
            source_session_id="s_new",
            created_at=_NOW - timedelta(hours=4),
        ),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        recent_session_summaries=summaries,
    )
    assert "# Recent sessions" in out
    assert "[Earlier this week] user shared grad school news" in out
    assert "[Earlier today] we caught up on apex" in out


def test_recent_sessions_omitted_when_empty():
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        recent_session_summaries=[],
    )
    assert "# Recent sessions" not in out

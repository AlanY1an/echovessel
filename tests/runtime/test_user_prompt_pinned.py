"""Spec 5 · # About {speaker} + # Promises you've made render paths.

Tests the rendering layer (build_user_prompt) plus the underlying
``_load_user_thoughts_force`` ranking. Both surfaces are covered:

1. ``retrieve(..., force_load_user_thoughts=N)`` returns up to N L4
   thoughts ranked by recency × importance — bypassing query
   similarity entirely. Already-returned ids in ``memories`` are
   excluded from ``pinned_thoughts`` to avoid duplicate render lines.

2. ``build_user_prompt(pinned_thoughts=..., speaker_display=...)``
   renders ``# About {speaker}`` between the existing thought
   section and the L3 events. Empty list → no header.

3. ``build_user_prompt(active_intentions=...)`` renders ``# Promises
   you've made`` with the R4 delta phrase appended per intention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.models import ConceptNode
from echovessel.memory.retrieve import retrieve
from echovessel.runtime.interaction import build_user_prompt

_NOW = datetime(2026, 4, 24, 14, 0, 0)


def _embed(_: str) -> list[float]:
    v = [0.0] * 384
    v[0] = 1.0
    return v


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Sage"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _thought(
    *,
    description: str,
    impact: int = 4,
    age_days: int = 0,
) -> ConceptNode:
    return ConceptNode(
        persona_id="p_test",
        user_id="self",
        type=NodeType.THOUGHT,
        description=description,
        emotional_impact=impact,
        created_at=_NOW - timedelta(days=age_days),
    )


# ---------------------------------------------------------------------------
# retrieve(force_load_user_thoughts=N)
# ---------------------------------------------------------------------------


def _query_embed(_: str) -> list[float]:
    """Vector orthogonal to the seeded thoughts so vector_search
    drops everything below the relevance floor — leaves
    ``pinned_thoughts`` as the ONLY surface that returns nodes."""
    v = [0.0] * 384
    v[200] = 1.0
    return v


def test_force_load_returns_top_n_by_recency_x_importance():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        # Three thoughts ranked by recency × importance — see
        # _load_user_thoughts_force docstring. Vector search will
        # not return any of them (orthogonal query vector) so the
        # only path that surfaces nodes is the force-load helper.
        recent_strong = _thought(description="strong recent", impact=8, age_days=0)
        ancient_strong = _thought(description="strong ancient", impact=8, age_days=60)
        recent_weak = _thought(description="weak recent", impact=1, age_days=0)
        for n in (recent_strong, ancient_strong, recent_weak):
            db.add(n)
        db.commit()
        for n in (recent_strong, ancient_strong, recent_weak):
            db.refresh(n)
            backend.insert_vector(n.id, _embed("seed"))

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="totally unrelated query",
            embed_fn=_query_embed,
            now=_NOW,
            force_load_user_thoughts=2,
        )
        ids = [t.id for t in result.pinned_thoughts]
        assert ids[0] == recent_strong.id  # winner
        # Second slot prefers the recent-weak over ancient-strong
        # because the 60d age decays past 4 half-lives.
        assert recent_weak.id in ids
        assert ancient_strong.id not in ids


def test_force_load_zero_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        t = _thought(description="x")
        db.add(t)
        db.commit()
        db.refresh(t)
        backend.insert_vector(t.id, _embed("x"))

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="q",
            embed_fn=_embed,
            now=_NOW,
        )
        assert result.pinned_thoughts == []


def test_force_load_excludes_already_returned():
    """If a thought is already in the rerank `memories` list the
    pinned section should not render it again (prevents duplicate
    bullet under # About {speaker})."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    with DbSession(engine) as db:
        _seed(db)
        same = _thought(description="strong recent", impact=8, age_days=0)
        another = _thought(description="another recent", impact=4, age_days=0)
        db.add(same)
        db.add(another)
        db.commit()
        db.refresh(same)
        db.refresh(another)
        # Same vector → both candidates for vector hit
        backend.insert_vector(same.id, _embed("q"))
        backend.insert_vector(another.id, _embed("q"))

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="anything",
            embed_fn=_embed,
            now=_NOW,
            force_load_user_thoughts=10,
        )
        memory_ids = {sm.node.id for sm in result.memories}
        pinned_ids = {t.id for t in result.pinned_thoughts}
        # No overlap
        assert memory_ids.isdisjoint(pinned_ids)


# ---------------------------------------------------------------------------
# # About {speaker} render
# ---------------------------------------------------------------------------


def test_about_section_rendered_with_speaker_display():
    pinned = [
        _thought(description="leans on humor when stressed"),
        _thought(description="codes Go for a living, new to React"),
    ]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        pinned_thoughts=pinned,
        speaker_display="Alan",
    )
    assert "# About Alan" in out
    assert "leans on humor when stressed" in out
    assert "codes Go for a living, new to React" in out


def test_about_section_omitted_when_no_pinned_thoughts():
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        pinned_thoughts=[],
        speaker_display="Alan",
    )
    assert "# About" not in out


def test_about_section_uses_them_when_no_speaker_display():
    pinned = [_thought(description="x")]
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        pinned_thoughts=pinned,
    )
    assert "# About them" in out


# ---------------------------------------------------------------------------
# # Promises you've made render
# ---------------------------------------------------------------------------


@dataclass
class _FakeIntention:
    description: str
    event_time_start: datetime | None
    event_time_end: datetime | None


def test_promises_section_rendered_with_delta_phrase():
    intention = _FakeIntention(
        description="你答应 Alan 周六晚上一起复盘 grad school",
        event_time_start=datetime(2026, 4, 26, 20, 0),
        event_time_end=datetime(2026, 4, 26, 22, 0),
    )
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        active_intentions=[intention],
    )
    assert "# Promises you've made" in out
    assert "周六晚上一起复盘" in out
    # Spec 3 day-precision delta phrase: 4-26 vs 4-24 → "in 2 days"
    assert "status=planned" in out
    assert "in 2 days" in out


def test_promises_section_omitted_when_empty():
    out = build_user_prompt(
        top_memories=[],
        recent_messages=[],
        user_message="hi",
        now=_NOW,
        active_intentions=[],
    )
    assert "# Promises" not in out


def test_about_section_renders_between_thoughts_and_events():
    """Section ordering (Spec 5 §6.4): # Recent thoughts → # About →
    # Recent things you remember happened. The render order matters
    because LLMs anchor on the most recent block they read; pinned
    thoughts must sit ABOVE per-query events."""

    @dataclass
    class _FakeScored:
        node: ConceptNode

    event_node = ConceptNode(
        persona_id="p_test",
        user_id="self",
        type=NodeType.EVENT,
        description="用户提到下周有期末考",
        emotional_impact=-2,
    )
    pinned = [_thought(description="this person is steady")]
    out = build_user_prompt(
        top_memories=[_FakeScored(node=event_node)],
        recent_messages=[],
        user_message="考试怎么样",
        now=_NOW,
        pinned_thoughts=pinned,
        speaker_display="Alan",
    )
    about_idx = out.index("# About Alan")
    events_idx = out.index("# Recent things you remember happened")
    assert about_idx < events_idx

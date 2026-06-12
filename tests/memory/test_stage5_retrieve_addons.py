"""Stage 5 add-ons · retrieve invariants and edge cases.

Four scenarios:

- **5.4** ``assemble_turn`` always renders the recent L2 message window
  in the user prompt, even when ``retrieve`` returns zero L3/L4 hits.
  This is the "you can always rely on the last N turns to ground the
  reply" guarantee.
- **5.5** Iron rule D4 — ``retrieve`` MUST NOT accept a ``channel_id``
  parameter, ever. Pin via ``inspect.signature`` so a future drive-by
  edit cannot silently add one.
- **5.6** Minimum-relevance floor drops orthogonal hits even when they
  carry high emotional impact (the "Over-recall mitigation" from
  ``docs/memory/eval-runs/2026-04-15``). Without the floor, a SHOCK
  event from an unrelated topic surfaces because of impact / relational
  bonus alone.
- **5.7** FTS fallback fires only when vector search returns fewer than
  ``fallback_threshold`` hits — when the index is healthy and returns
  enough events, the L2 fallback stays empty.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime

from sqlmodel import Session as DbSession

from echovessel.core.types import MessageRole, NodeType
from echovessel.memory import (
    ConceptNode as MemConceptNode,  # noqa: N814 — keep import side-effect even if unused
)
from echovessel.memory import (
    Persona,
    RecallMessage,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory import (
    Session as MemSession,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.retrieve import (
    DEFAULT_MIN_RELEVANCE,
    list_recall_messages,
    retrieve,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="P"))
    db.add(User(id="u", display_name="U"))
    db.commit()


def _unit_vec(idx: int, dim: int = 384) -> list[float]:
    """Single-axis unit vector — any two distinct indices are
    orthogonal (cosine distance 1.0), giving relevance 0.5 (below the
    default min_relevance floor of 0.55)."""

    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


def _make_session(db: DbSession, sid: str = "s_x") -> MemSession:
    sess = MemSession(
        id=sid,
        persona_id="p",
        user_id="u",
        channel_id="web",
    )
    db.add(sess)
    db.commit()
    return sess


def _add_event(
    db: DbSession,
    backend: SQLiteBackend,
    *,
    description: str,
    impact: int,
    embedding_axis: int,
    relational_tags: list[str] | None = None,
) -> MemConceptNode:
    node = MemConceptNode(
        persona_id="p",
        user_id="u",
        type=NodeType.EVENT,
        description=description,
        emotional_impact=impact,
        relational_tags=relational_tags or [],
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    backend.insert_vector(node.id, _unit_vec(embedding_axis))
    return node


# ---------------------------------------------------------------------------
# 5.5 · D4 invariant — retrieve has no channel_id parameter
# ---------------------------------------------------------------------------


def test_retrieve_signature_has_no_channel_id_parameter() -> None:
    """Iron rule D4: ``retrieve`` is the unified-persona doorway. A
    ``channel_id`` parameter on this function would let a careless
    caller filter retrieval per channel and silently break the
    cross-channel persona contract. Pin via ``inspect`` so any future
    drive-by parameter addition fails this test."""

    sig = inspect.signature(retrieve)
    assert "channel_id" not in sig.parameters, (
        "retrieve() must not accept a channel_id parameter — D4 forbids "
        "filtering memory by transport"
    )

    # list_recall_messages is the L2 timeline reader; the same
    # invariant applies — a "show me only web messages" filter would
    # let a UI accidentally narrow the recent window per channel and
    # break cross-channel continuity.
    sig2 = inspect.signature(list_recall_messages)
    assert "channel_id" not in sig2.parameters


# ---------------------------------------------------------------------------
# 5.6 · min_relevance floor drops orthogonal high-impact hits
# ---------------------------------------------------------------------------


def test_min_relevance_floor_drops_orthogonal_high_impact_event() -> None:
    """A SHOCK event whose embedding is orthogonal to the query (cosine
    similarity ≈ 0) should NOT surface just because it has high impact.
    The min_relevance floor cuts it BEFORE rerank's impact term gets a
    chance to push it to the top."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        # Orthogonal high-impact event (axis 100, very high impact).
        orthogonal = _add_event(
            db,
            backend,
            description="user disclosed loss of mother",
            impact=-9,
            embedding_axis=100,
        )
        # Aligned mild event (axis 5 == query axis).
        aligned = _add_event(
            db,
            backend,
            description="user mentioned their cat Mochi",
            impact=2,
            embedding_axis=5,
        )

        def embed(_q: str) -> list[float]:
            return _unit_vec(5)

        # With the default floor, only the aligned event passes.
        with_floor = retrieve(
            db=db,
            backend=backend,
            persona_id="p",
            user_id="u",
            query_text="how is the cat",
            embed_fn=embed,
            top_k=5,
        )
        ids = [m.node.id for m in with_floor.memories]
        assert aligned.id in ids
        assert orthogonal.id not in ids, (
            "orthogonal SHOCK event surfaced anyway — min_relevance "
            "floor is not protecting against over-recall"
        )

        # Drop the floor to 0 → both events pass; high-impact orthogonal
        # one ranks above the aligned mild one because impact dominates.
        no_floor = retrieve(
            db=db,
            backend=backend,
            persona_id="p",
            user_id="u",
            query_text="how is the cat",
            embed_fn=embed,
            top_k=5,
            min_relevance=0.0,
        )
        ids_no_floor = [m.node.id for m in no_floor.memories]
        assert orthogonal.id in ids_no_floor
        assert aligned.id in ids_no_floor

    # Default constant must stay above 0; otherwise the floor is
    # effectively disabled and the test above passes for the wrong
    # reason.
    assert DEFAULT_MIN_RELEVANCE > 0


# ---------------------------------------------------------------------------
# 5.7 · FTS fallback NOT triggered when vector returns enough hits
# ---------------------------------------------------------------------------


def test_fts_fallback_does_not_trigger_when_vector_returns_enough_hits() -> None:
    """The FTS L2 fallback exists for the empty-index case. When the
    vector index is healthy and returns >= ``fallback_threshold`` hits,
    fts_fallback must be empty — otherwise we'd double-bill latency
    and confuse the prompt with stale L2 chatter."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        # Seed several messages with the FTS-relevant keyword so that
        # FTS WOULD return hits if it were called.
        for i in range(5):
            db.add(
                RecallMessage(
                    session_id=sess.id,
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    role=MessageRole.USER,
                    content=f"Mochi diary entry {i}",
                    day=date.today(),
                )
            )
        db.commit()

        # And seed enough vector hits to satisfy fallback_threshold=3.
        for i in range(4):
            _add_event(
                db,
                backend,
                description=f"event {i}",
                impact=1,
                embedding_axis=i,
            )

        def embed(_q: str) -> list[float]:
            return _unit_vec(0)

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p",
            user_id="u",
            query_text="Mochi",
            embed_fn=embed,
            top_k=5,
            fallback_threshold=3,
            min_relevance=0.0,  # disable so all 4 vector hits survive
        )
    assert len(result.memories) >= 3
    assert result.fts_fallback == [], (
        "fts_fallback should stay empty when vector returns enough hits"
    )


# ---------------------------------------------------------------------------
# 5.4 · L2 recent window survives an empty retrieve result
# ---------------------------------------------------------------------------


def test_list_recall_messages_returns_recent_window_independent_of_retrieve() -> None:
    """``assemble_turn`` calls ``list_recall_messages`` independently of
    ``retrieve`` — the recent L2 window is always a part of the prompt
    so the persona has the literal last-N turns even when L3/L4
    retrieval finds nothing relevant.

    This test asserts at the memory layer (the runtime layer's wiring
    is covered separately by ``test_interaction.py``): with zero
    ConceptNodes in the DB, retrieve returns 0 memories AND
    list_recall_messages returns the messages we just wrote.
    """

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        for i in range(3):
            db.add(
                RecallMessage(
                    session_id=sess.id,
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=f"recent {i}",
                    day=date.today(),
                    created_at=datetime(2026, 4, 18, 10, i),
                )
            )
        db.commit()

        def embed(_q: str) -> list[float]:
            return _unit_vec(0)

        # No concept nodes → retrieve memories list is empty.
        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p",
            user_id="u",
            query_text="anything",
            embed_fn=embed,
            top_k=5,
            fallback_threshold=0,  # disable FTS so we test L2 reader path explicitly
        )
        assert result.memories == []

        # The L2 reader still hands back our recent messages — assemble_turn
        # uses this same call to populate the user-prompt window.
        recent = list_recall_messages(
            db, persona_id="p", user_id="u", limit=10, before=None
        )
    contents = [m.content for m in recent]
    assert "recent 0" in contents
    assert "recent 1" in contents
    assert "recent 2" in contents

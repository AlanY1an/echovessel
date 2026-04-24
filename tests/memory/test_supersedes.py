"""Spec 5 plan §6.2 step 2 · supersedes write + retrieve filter.

Validates that:

1. When extraction emits an event with ``superseded_event_ids=[old_id]``,
   consolidate writes ``superseded_by_id = new_node.id`` on the OLD
   ConceptNode (soft delete; row stays alive for any future history
   API but drops out of default retrieval).

2. ``retrieve()`` filters out superseded nodes by default — even when
   they would otherwise be the strongest vector match — so the persona
   never reads a stale fact ("用户每天三杯咖啡") after the user has
   updated it ("用户戒咖啡了").

3. Cross-scope, self-cycle, and missing-target supersedes are all
   defended at the consolidate write layer rather than reaching the
   DB.

Schema invariants (event_time monotonic, soft-delete columns) live in
``test_schema.py``; this file is purely about the supersedes lifecycle.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.models import ConceptNode
from echovessel.memory.retrieve import retrieve

_NOW = datetime(2026, 4, 24, 14, 0, 0)


def _seed_persona(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _seed_session(db: DbSession, sid: str = "s_supersede") -> Session:
    sess = Session(
        id=sid,
        persona_id="p_test",
        user_id="self",
        channel_id="test",
        status=SessionStatus.CLOSING,
        message_count=4,
        total_tokens=400,
    )
    db.add(sess)
    db.commit()
    return sess


def _add_messages(db: DbSession, sid: str, contents: list[str]) -> None:
    for i, c in enumerate(contents):
        db.add(
            RecallMessage(
                session_id=sid,
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                content=c,
                day=date.today(),
                token_count=len(c),
            )
        )
    db.commit()


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


# ---------------------------------------------------------------------------
# Consolidate write path
# ---------------------------------------------------------------------------


async def test_supersedes_marks_old_node_superseded_by_new():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)
        # Pre-existing event from an earlier session — the OLD belief.
        old = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户每天三杯咖啡",
            emotional_impact=2,
        )
        db.add(old)
        db.commit()
        db.refresh(old)
        old_id = old.id
        assert old_id is not None

        sess = _seed_session(db)
        _add_messages(db, sess.id, ["我戒咖啡了", "ok 收到"])

        async def extract(_msgs):
            return ExtractionResult(
                events=[
                    ExtractedEvent(
                        description="用户戒咖啡了",
                        emotional_impact=3,
                        superseded_event_ids=[old_id],
                    )
                ]
            )

        async def reflect(_n, _r):
            return []

        await consolidate_session(
            db,
            backend=backend,
            session=sess,
            extract_fn=extract,
            reflect_fn=reflect,
            embed_fn=_embed,
            now=_NOW,
        )

        # New node was inserted
        new = db.exec(
            select(ConceptNode).where(ConceptNode.description == "用户戒咖啡了")
        ).one()
        assert new.id is not None

        # Old node now points at the new one — soft delete.
        db.refresh(old)
        assert old.superseded_by_id == new.id
        assert old.deleted_at is None  # NOT a hard delete


async def test_supersedes_self_cycle_blocked():
    """A new node cannot supersede itself — would create a cycle."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)
        sess = _seed_session(db)
        _add_messages(db, sess.id, ["我戒咖啡了", "ok"])

        async def extract(_msgs):
            # superseded_event_ids list is RESOLVED post-hoc against the
            # new node's id — but the LLM cannot actually know the id
            # in advance. To test the self-cycle guard we pass an id
            # the writer KNOWS will collide; we use 1 because in a
            # fresh DB the new node will be assigned id=1 (the only
            # ConceptNode insert in the test).
            return ExtractionResult(
                events=[
                    ExtractedEvent(
                        description="用户戒咖啡了",
                        emotional_impact=3,
                        superseded_event_ids=[1],
                    )
                ]
            )

        async def reflect(_n, _r):
            return []

        await consolidate_session(
            db,
            backend=backend,
            session=sess,
            extract_fn=extract,
            reflect_fn=reflect,
            embed_fn=_embed,
            now=_NOW,
        )

        new = db.exec(
            select(ConceptNode).where(ConceptNode.description == "用户戒咖啡了")
        ).one()
        # Self-cycle blocked → superseded_by_id stays None.
        assert new.superseded_by_id is None


async def test_supersedes_missing_target_logs_and_continues():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)
        sess = _seed_session(db)
        _add_messages(db, sess.id, ["x", "y"])

        async def extract(_msgs):
            return ExtractionResult(
                events=[
                    ExtractedEvent(
                        description="新事实",
                        emotional_impact=1,
                        superseded_event_ids=[9999],
                    )
                ]
            )

        async def reflect(_n, _r):
            return []

        # Should NOT raise.
        result = await consolidate_session(
            db,
            backend=backend,
            session=sess,
            extract_fn=extract,
            reflect_fn=reflect,
            embed_fn=_embed,
            now=_NOW,
        )
        assert len(result.events_created) == 1


async def test_supersedes_cross_scope_skipped():
    """A node belonging to a different (persona, user) scope MUST NOT
    be supersede-targetable from another scope's session."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)
        # Add a second persona + an event under it.
        db.add(Persona(id="p_other", display_name="Other"))
        db.commit()
        foreign = ConceptNode(
            persona_id="p_other",
            user_id="self",
            type=NodeType.EVENT,
            description="某 other 事实",
            emotional_impact=1,
        )
        db.add(foreign)
        db.commit()
        db.refresh(foreign)

        sess = _seed_session(db)
        _add_messages(db, sess.id, ["x", "y"])

        async def extract(_msgs):
            return ExtractionResult(
                events=[
                    ExtractedEvent(
                        description="新事实",
                        emotional_impact=1,
                        superseded_event_ids=[foreign.id],
                    )
                ]
            )

        async def reflect(_n, _r):
            return []

        await consolidate_session(
            db,
            backend=backend,
            session=sess,
            extract_fn=extract,
            reflect_fn=reflect,
            embed_fn=_embed,
            now=_NOW,
        )

        db.refresh(foreign)
        # Cross-scope guard prevented the write.
        assert foreign.superseded_by_id is None


# ---------------------------------------------------------------------------
# Retrieve filter
# ---------------------------------------------------------------------------


def test_retrieve_filters_superseded_nodes_by_default():
    """The most basic R3 / R4 invariant: a superseded node must NEVER
    appear in default retrieve output, even if its embedding is the
    strongest match for the query."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)

        # Two sibling nodes — old + new — sharing the same embedding,
        # so vector_search ties; only the new one should reach output.
        old = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户每天三杯咖啡",
            emotional_impact=2,
        )
        new = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户戒咖啡了",
            emotional_impact=3,
        )
        db.add(old)
        db.add(new)
        db.commit()
        db.refresh(old)
        db.refresh(new)

        # Same embedding so both would surface in vector search
        backend.insert_vector(old.id, _embed("coffee"))
        backend.insert_vector(new.id, _embed("coffee"))

        # Mark the old one as superseded by the new one
        old.superseded_by_id = new.id
        db.add(old)
        db.commit()

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="coffee",
            embed_fn=lambda _: _embed("coffee"),
            top_k=10,
            now=_NOW,
        )
        ids = [sm.node.id for sm in result.memories]
        assert new.id in ids
        assert old.id not in ids


def test_force_load_user_thoughts_skips_superseded():
    """Same guard, force-load path — the pinned thoughts API must not
    surface superseded thoughts."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)

        live = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.THOUGHT,
            description="this person is steady through stress",
            emotional_impact=4,
        )
        retracted = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.THOUGHT,
            description="this person panics under stress",
            emotional_impact=4,
        )
        db.add(live)
        db.add(retracted)
        db.commit()
        db.refresh(live)
        db.refresh(retracted)
        retracted.superseded_by_id = live.id
        db.add(retracted)
        db.commit()

        backend.insert_vector(live.id, _embed("stress"))
        backend.insert_vector(retracted.id, _embed("stress"))

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="anything",
            embed_fn=lambda _: _embed("unrelated"),
            top_k=5,
            now=_NOW,
            force_load_user_thoughts=10,
        )
        ids = [t.id for t in result.pinned_thoughts]
        assert live.id in ids
        assert retracted.id not in ids

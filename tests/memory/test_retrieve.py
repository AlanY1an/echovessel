"""RETRIEVE pipeline tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory import (
    ConceptNode,
    CoreBlock,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.retrieve import (
    load_core_blocks,
    retrieve,
)


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _unit_vec(axis: int, dim: int = 384) -> list[float]:
    v = [0.0] * dim
    v[axis] = 1.0
    return v


# ---------------------------------------------------------------------------
# L1 loading
# ---------------------------------------------------------------------------


def test_load_core_blocks_returns_shared_and_per_user():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)

        db.add(
            CoreBlock(
                persona_id="p_test",
                user_id=None,
                label=BlockLabel.PERSONA,
                content="温柔的朋友",
            )
        )
        db.add(
            CoreBlock(
                persona_id="p_test",
                user_id="self",
                label=BlockLabel.USER,
                content="Alan 是软件工程师",
            )
        )
        db.add(
            CoreBlock(
                persona_id="p_test",
                user_id=None,
                label=BlockLabel.STYLE,
                content="别用'哈哈'开头",
            )
        )
        db.commit()

        blocks = load_core_blocks(db, "p_test", "self")
        labels = [b.label for b in blocks]
        assert BlockLabel.PERSONA in labels
        assert BlockLabel.USER in labels
        assert BlockLabel.STYLE in labels
        # Order: persona → user → style (v0.5 collapsed L1 to 3 labels)
        assert labels.index(BlockLabel.PERSONA) < labels.index(BlockLabel.USER)
        assert labels.index(BlockLabel.USER) < labels.index(BlockLabel.STYLE)


def test_load_core_blocks_excludes_other_users():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test"))
        db.add(User(id="alice", display_name="Alice"))
        db.add(User(id="bob", display_name="Bob"))
        db.commit()

        db.add(
            CoreBlock(
                persona_id="p_test",
                user_id="alice",
                label=BlockLabel.USER,
                content="alice fact",
            )
        )
        db.add(
            CoreBlock(
                persona_id="p_test",
                user_id="bob",
                label=BlockLabel.USER,
                content="bob fact",
            )
        )
        db.commit()

        alice_blocks = load_core_blocks(db, "p_test", "alice")
        assert len(alice_blocks) == 1
        assert alice_blocks[0].content == "alice fact"


# ---------------------------------------------------------------------------
# Retrieval — vector + rerank
# ---------------------------------------------------------------------------


def test_retrieve_returns_nearest_event_with_score():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

        e1 = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户喜欢爵士乐",
            emotional_impact=3,
            emotion_tags=["joy"],
        )
        e2 = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户周一加班",
            emotional_impact=-2,
        )
        db.add(e1)
        db.add(e2)
        db.commit()
        db.refresh(e1)
        db.refresh(e2)

        backend.insert_vector(e1.id, _unit_vec(0))
        backend.insert_vector(e2.id, _unit_vec(1))

        def embed_fn(_: str) -> list[float]:
            return _unit_vec(0)  # query matches e1

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="music",
            embed_fn=embed_fn,
            top_k=5,
        )

        assert len(result.memories) >= 1
        top = result.memories[0]
        assert top.node.id == e1.id
        assert top.relevance > 0.9  # unit vectors on same axis → distance = 0
        assert top.total > 0


def test_retrieve_scores_include_relational_bonus():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

        tagged = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户说他是单亲妈妈",
            emotional_impact=0,
            relational_tags=["identity-bearing"],
        )
        plain = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="今天午饭吃了拉面",
            emotional_impact=0,
            relational_tags=[],
        )
        db.add(tagged)
        db.add(plain)
        db.commit()
        db.refresh(tagged)
        db.refresh(plain)

        # Same vector for both — tie-breaker is relational_bonus
        backend.insert_vector(tagged.id, _unit_vec(0))
        backend.insert_vector(plain.id, _unit_vec(0))

        def embed_fn(_: str) -> list[float]:
            return _unit_vec(0)

        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="anything",
            embed_fn=embed_fn,
            top_k=5,
        )

        # The tagged node should score higher than the plain node
        tagged_score = next(m for m in result.memories if m.node.id == tagged.id).total
        plain_score = next(m for m in result.memories if m.node.id == plain.id).total
        assert tagged_score > plain_score


def test_retrieve_increments_access_count():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

        node = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户养了一只猫",
            emotional_impact=4,
        )
        db.add(node)
        db.commit()
        db.refresh(node)

        backend.insert_vector(node.id, _unit_vec(0))

        assert node.access_count == 0

        def embed_fn(_: str) -> list[float]:
            return _unit_vec(0)

        retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="cat",
            embed_fn=embed_fn,
            top_k=5,
        )

        db.refresh(node)
        assert node.access_count == 1


def test_retrieve_fts_fallback_triggers_when_no_vector_hits():
    """If L3/L4 returns 0 memories, the pipeline should fall back to L2 FTS."""
    from datetime import date

    from echovessel.core.types import MessageRole
    from echovessel.memory import RecallMessage, Session

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s_x",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
        )
        db.add(sess)
        db.commit()

        db.add(
            RecallMessage(
                session_id="s_x",
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER,
                content="Mochi 昨晚生病了",
                day=date.today(),
            )
        )
        db.commit()

        def embed_fn(_: str) -> list[float]:
            return _unit_vec(0)

        # No ConceptNodes yet → vector search returns empty → fallback to FTS
        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="Mochi",
            embed_fn=embed_fn,
            top_k=5,
        )

        assert len(result.memories) == 0
        assert len(result.fts_fallback) >= 1
        assert any("Mochi" in m.content for m in result.fts_fallback)


# ---------------------------------------------------------------------------
# list_recall_messages — pure L2 timeline query for UI pagination
# ---------------------------------------------------------------------------


def _seed_session(db: DbSession, session_id: str, channel_id: str = "test") -> None:
    from echovessel.memory import Session

    db.add(
        Session(
            id=session_id,
            persona_id="p_test",
            user_id="self",
            channel_id=channel_id,
        )
    )
    db.commit()


def _add_message(
    db: DbSession,
    *,
    session_id: str,
    channel_id: str,
    content: str,
    created_at: datetime,
    deleted_at: datetime | None = None,
) -> None:
    from datetime import date

    from echovessel.core.types import MessageRole
    from echovessel.memory import RecallMessage

    db.add(
        RecallMessage(
            session_id=session_id,
            persona_id="p_test",
            user_id="self",
            channel_id=channel_id,
            role=MessageRole.USER,
            content=content,
            day=date.today(),
            created_at=created_at,
            deleted_at=deleted_at,
        )
    )


def test_list_recall_messages_basic():
    """Returns up to `limit` messages for (persona, user), newest first."""
    from echovessel.memory import list_recall_messages

    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        _seed_session(db, "s_a")

        base = datetime(2026, 4, 10, 20, 0, 0)
        for i in range(10):
            _add_message(
                db,
                session_id="s_a",
                channel_id="test",
                content=f"msg {i}",
                created_at=base + timedelta(minutes=i),
            )
        db.commit()

        rows = list_recall_messages(db, "p_test", "self", limit=5)

        assert len(rows) == 5
        # Newest first: msg 9, 8, 7, 6, 5
        assert [r.content for r in rows] == [
            "msg 9",
            "msg 8",
            "msg 7",
            "msg 6",
            "msg 5",
        ]


def test_list_recall_messages_before_cursor():
    """`before` excludes rows with created_at >= cursor."""
    from echovessel.memory import list_recall_messages

    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        _seed_session(db, "s_a")

        base = datetime(2026, 4, 10, 20, 0, 0)
        for i in range(10):
            _add_message(
                db,
                session_id="s_a",
                channel_id="test",
                content=f"msg {i}",
                created_at=base + timedelta(minutes=i),
            )
        db.commit()

        cursor = base + timedelta(minutes=5)  # exclusive upper bound

        rows = list_recall_messages(
            db, "p_test", "self", limit=50, before=cursor
        )

        # Should return only msg 0..4, newest first
        assert [r.content for r in rows] == [
            "msg 4",
            "msg 3",
            "msg 2",
            "msg 1",
            "msg 0",
        ]


def test_list_recall_messages_excludes_deleted():
    """Soft-deleted messages must never surface through this API."""
    from echovessel.memory import list_recall_messages

    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        _seed_session(db, "s_a")

        base = datetime(2026, 4, 10, 20, 0, 0)
        deleted_at = datetime(2026, 4, 11, 0, 0, 0)

        for i in range(5):
            _add_message(
                db,
                session_id="s_a",
                channel_id="test",
                content=f"msg {i}",
                created_at=base + timedelta(minutes=i),
                deleted_at=deleted_at if i in (1, 3) else None,
            )
        db.commit()

        rows = list_recall_messages(db, "p_test", "self", limit=50)

        contents = [r.content for r in rows]
        assert len(contents) == 3
        assert "msg 1" not in contents
        assert "msg 3" not in contents
        assert set(contents) == {"msg 0", "msg 2", "msg 4"}


def test_list_recall_messages_unified_across_channels():
    """🚨 D4 guard test — this API must NEVER filter by channel_id.

    If anyone sneaks a WHERE channel_id=... into list_recall_messages, this
    test fails immediately. Do not loosen it. See DISCUSSION.md 2026-04-14 D4.
    """
    from echovessel.memory import list_recall_messages

    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        _seed_session(db, "s_web", channel_id="web")
        _seed_session(db, "s_dc", channel_id="discord:g123")

        base = datetime(2026, 4, 10, 20, 0, 0)
        # Interleave web and discord messages in time order
        order = [
            ("web", "web-0"),
            ("discord:g123", "dc-0"),
            ("web", "web-1"),
            ("discord:g123", "dc-1"),
            ("web", "web-2"),
            ("discord:g123", "dc-2"),
        ]
        for i, (ch, content) in enumerate(order):
            _add_message(
                db,
                session_id="s_web" if ch == "web" else "s_dc",
                channel_id=ch,
                content=content,
                created_at=base + timedelta(minutes=i),
            )
        db.commit()

        rows = list_recall_messages(db, "p_test", "self", limit=50)

        # MUST return all 6, regardless of channel
        assert len(rows) == 6
        channels = {r.channel_id for r in rows}
        assert channels == {"web", "discord:g123"}
        contents = {r.content for r in rows}
        assert contents == {
            "web-0",
            "web-1",
            "web-2",
            "dc-0",
            "dc-1",
            "dc-2",
        }


def test_list_recall_messages_limit_cap():
    """`limit` is hard-capped at 200 to prevent abusive queries."""
    from echovessel.memory import list_recall_messages

    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        _seed_session(db, "s_a")

        base = datetime(2026, 4, 10, 20, 0, 0)
        for i in range(250):
            _add_message(
                db,
                session_id="s_a",
                channel_id="test",
                content=f"msg {i}",
                created_at=base + timedelta(seconds=i),
            )
        db.commit()

        rows = list_recall_messages(db, "p_test", "self", limit=9999)

        assert len(rows) == 200

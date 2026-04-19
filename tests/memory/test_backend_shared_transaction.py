"""Backend shared-transaction regression tests.

Pin the fix where ``SQLiteBackend.insert_vector`` accepts an optional
``conn`` parameter so that ``consolidate_session`` can share the main
DbSession's transaction instead of opening an independent one.

The old behaviour: consolidate did
    db.add(node); db.flush()      # holds SQLite's single writer lock
    backend.insert_vector(...)     # opens new conn → deadlocks against self

After the fix, vec writes join the caller's transaction and commit
atomically together. These tests reproduce the exact two code paths
that touched the bug:

A · Extraction path(Stage B)· multiple events per session · each event
    triggers ``INSERT concept_nodes + INSERT concept_nodes_vec`` in the
    same transaction.

B · Reflection path(Stage E)· a SHOCK-qualifying event(|impact|≥8)
    forces reflect_fn to run · writes one or more L4 thoughts · each
    thought does the same combined insert. Hitting the deadlock here
    would strand the reflection pass but leave extracted events
    persisted — the old failure mode.

C · Fresh-close round trip · use the official ``_mark_closing`` API
    (not a hand-rolled UPDATE) to prove the end-to-end flow still works
    post-fix: open session → mark closing → worker picks up → extract +
    reflect → CLOSED, extracted=1.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

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
from echovessel.memory.consolidate import ExtractedEvent, ExtractedThought
from echovessel.memory.models import ConceptNode
from echovessel.memory.sessions import _mark_closing
from echovessel.runtime.consolidate_worker import ConsolidateWorker


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="P"))
    db.add(User(id="u", display_name="U"))
    db.commit()


def _file_db_engine():
    """Use a real on-disk SQLite file, not :memory:. The deadlock is
    a WAL single-writer issue that only shows up on real file DBs —
    :memory: databases don't exercise the same lock pathway.
    """
    tmpdir = tempfile.mkdtemp(prefix="echovessel-test-")
    db_path = Path(tmpdir) / "memory.db"
    engine = create_engine(db_path)
    create_all_tables(engine)
    return engine


def _add_session(engine, sid: str, *, messages: list[tuple[str, str]]) -> None:
    """Add a session in CLOSING state with the given (role, content) messages."""
    with DbSession(engine) as db:
        sess = Session(
            id=sid,
            persona_id="p",
            user_id="u",
            channel_id="t",
            status=SessionStatus.CLOSING,
            message_count=len(messages),
            total_tokens=sum(len(c) for _, c in messages),
        )
        db.add(sess)
        db.commit()
        for role, content in messages:
            db.add(
                RecallMessage(
                    session_id=sid,
                    persona_id="p",
                    user_id="u",
                    channel_id="t",
                    role=role,
                    content=content,
                    day=date.today(),
                    token_count=len(content),
                )
            )
        db.commit()


async def _noop_reflect(nodes, reason):
    return []


# ---------------------------------------------------------------------------
# A · Extraction path · multiple events commit without self-deadlock
# ---------------------------------------------------------------------------


async def test_extraction_multi_event_no_self_deadlock():
    """Three events written in one session — each one flushes a ConceptNode
    and then inserts into concept_nodes_vec. Before the backend-conn fix
    this would deadlock on the second or third vec insert. After the fix
    it runs clean.
    """
    engine = _file_db_engine()
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
    _add_session(
        engine,
        "s_multi",
        messages=[
            (MessageRole.USER, "今天跟妈妈吵了一架"),
            (MessageRole.PERSONA, "嗯嗯 你还好吗"),
            (MessageRole.USER, "后来去了公园散心 感觉好多了"),
            (MessageRole.PERSONA, "散步确实有效 很开心你找回平静了"),
            (MessageRole.USER, "晚上还是有点难受 但好多了"),
        ],
    )

    async def extractor(msgs):
        # Emit three separate events so the vec insert loop runs 3 times.
        return [
            ExtractedEvent(description="用户和母亲发生了争吵", emotional_impact=-4),
            ExtractedEvent(description="用户去公园散步调节情绪", emotional_impact=+2),
            ExtractedEvent(description="晚上情绪仍有起伏但已缓解", emotional_impact=-1),
        ]

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        initial_session_ids=("s_multi",),
    )

    processed = await worker.drain_once()
    assert processed == 1

    with DbSession(engine) as db:
        events = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_multi",
                    ConceptNode.type == NodeType.EVENT,
                )
            )
        )
        assert len(events) == 3, (
            f"expected 3 extracted events; got {len(events)}. "
            "If this is 0 or 1, the backend self-deadlock has regressed."
        )
        sess = db.get(Session, "s_multi")
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True

        # Verify each event has a corresponding vec row (the bug would
        # have left some events without their vectors).
        for e in events:
            row = db.connection().execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM concept_nodes_vec WHERE id = :id"
                ),
                {"id": e.id},
            ).scalar()
            assert row == 1, f"event {e.id} missing its vec row"


# ---------------------------------------------------------------------------
# B · Reflection path · SHOCK triggers thought writes, same lock path
# ---------------------------------------------------------------------------


async def test_reflection_shock_thought_writes_no_deadlock():
    """A single high-impact event (|impact| ≥ SHOCK_IMPACT_THRESHOLD=8)
    makes Stage E run reflect_fn. The returned thoughts go through the
    same ``db.flush() + backend.insert_vector`` pattern — the same
    self-deadlock window. This test proves thoughts also write cleanly
    after the backend-conn fix.
    """
    engine = _file_db_engine()
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
    _add_session(
        engine,
        "s_shock",
        messages=[
            (MessageRole.USER, "我妈走了"),
            (MessageRole.PERSONA, "我很难过..."),
            (MessageRole.USER, "还是接受不了"),
            (MessageRole.PERSONA, "慢慢来 陪着你"),
        ],
    )

    async def extractor(msgs):
        return [
            ExtractedEvent(
                description="用户的母亲去世",
                emotional_impact=-10,  # triggers SHOCK
                emotion_tags=["grief"],
            ),
        ]

    async def reflect(nodes, reason):
        # Return 2 thoughts — both must write vec rows cleanly.
        return [
            ExtractedThought(
                description="用户正在经历丧亲之痛，情感上会非常脆弱",
                emotional_impact=-8,
                emotion_tags=["grief"],
                filling=[n.id for n in nodes],
            ),
            ExtractedThought(
                description="陪伴是此刻最重要的支持",
                emotional_impact=-2,
                filling=[n.id for n in nodes],
            ),
        ]

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=reflect,
        embed_fn=_embed,
        initial_session_ids=("s_shock",),
    )

    processed = await worker.drain_once()
    assert processed == 1

    with DbSession(engine) as db:
        events = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_shock",
                    ConceptNode.type == NodeType.EVENT,
                )
            )
        )
        thoughts = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.type == NodeType.THOUGHT,
                )
            )
        )
        assert len(events) == 1, f"expected 1 event, got {len(events)}"
        assert len(thoughts) == 2, (
            f"expected 2 thoughts, got {len(thoughts)}. "
            "If this is 0, reflection's vec inserts deadlocked."
        )

        # Every thought must have a corresponding vec row.
        for t in thoughts:
            row = db.connection().execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM concept_nodes_vec WHERE id = :id"
                ),
                {"id": t.id},
            ).scalar()
            assert row == 1, f"thought {t.id} missing its vec row"


# ---------------------------------------------------------------------------
# C · Official closing API round trip (not hand-rolled UPDATE)
# ---------------------------------------------------------------------------


async def test_fresh_close_via_official_api_end_to_end():
    """Simulate the production closing flow exactly:

      1. Session starts in OPEN state
      2. ``_mark_closing(session, trigger='idle')`` transitions to CLOSING
      3. Worker catches it on next poll
      4. Full consolidate runs: extract + (maybe reflect) + mark CLOSED

    This catches any regression that the retry-tests would miss because
    they SQL-flipped sessions into CLOSING rather than using the real
    API path.
    """
    engine = _file_db_engine()
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

    # Open session directly (simulating what ingest_message would create).
    sid = "s_fresh"
    with DbSession(engine) as db:
        sess = Session(
            id=sid,
            persona_id="p",
            user_id="u",
            channel_id="t",
            status=SessionStatus.OPEN,
            message_count=3,
            total_tokens=60,
        )
        db.add(sess)
        db.commit()
        for i, c in enumerate(
            ["今天去爬山了", "风景很美", "走了快五小时累死我"]
        ):
            db.add(
                RecallMessage(
                    session_id=sid,
                    persona_id="p",
                    user_id="u",
                    channel_id="t",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=c,
                    day=date.today(),
                    token_count=len(c),
                )
            )
        db.commit()

    # Use the official API to transition OPEN → CLOSING.
    with DbSession(engine) as db:
        sess = db.get(Session, sid)
        assert sess is not None
        assert sess.status == SessionStatus.OPEN
        _mark_closing(sess, trigger="idle")
        db.add(sess)
        db.commit()
        assert sess.status == SessionStatus.CLOSING
        assert sess.close_trigger == "idle"
        assert sess.closed_at is not None  # _mark_closing sets this

    async def extractor(msgs):
        return [
            ExtractedEvent(
                description="用户完成了一次爬山活动",
                emotional_impact=+3,
            )
        ]

    def _db():
        return DbSession(engine)

    worker = ConsolidateWorker(
        db_factory=_db,
        backend=backend,
        extract_fn=extractor,
        reflect_fn=_noop_reflect,
        embed_fn=_embed,
        # No initial_session_ids — let _poll_closing_sessions find it
        # naturally (this is the real production path).
    )

    processed = await worker.drain_once()
    assert processed == 1, "worker did not discover the CLOSING session via poll"

    with DbSession(engine) as db:
        sess = db.get(Session, sid)
        assert sess is not None
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True
        assert sess.closed_at is not None  # preserved from _mark_closing

        events = list(
            db.exec(
                select(ConceptNode).where(ConceptNode.source_session_id == sid)
            )
        )
        assert len(events) == 1

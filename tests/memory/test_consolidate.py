"""CONSOLIDATE pipeline tests.

LLM callables are mocked — memory module must not depend on real providers.

ExtractFn / ReflectFn are async callables (spec §6.4), so these tests use
async def + pytest-asyncio's `asyncio_mode = "auto"` from pyproject.toml.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    ConceptNodeFilling,
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    SHOCK_IMPACT_THRESHOLD,
    ExtractedEvent,
    ExtractedThought,
    ExtractionResult,
    consolidate_session,
    is_trivial,
)
from echovessel.memory.models import ConceptNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _make_session(
    db: DbSession, *, status: SessionStatus = SessionStatus.CLOSING
) -> Session:
    sess = Session(
        id="s_test",
        persona_id="p_test",
        user_id="self",
        channel_id="test",
        status=status,
        message_count=5,
        total_tokens=500,
    )
    db.add(sess)
    db.commit()
    return sess


def _add_messages(db: DbSession, session_id: str, contents: list[str]) -> None:
    for i, c in enumerate(contents):
        db.add(
            RecallMessage(
                session_id=session_id,
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


def _deterministic_embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _make_async_extractor(events: list[ExtractedEvent], *, track: list | None = None):
    async def _extract(msgs):
        if track is not None:
            track.append(len(msgs))
        return ExtractionResult(events=list(events))

    return _extract


def _make_async_reflector(thoughts: list[ExtractedThought], *, track: list | None = None):
    async def _reflect(nodes, reason):
        if track is not None:
            track.append(reason)
        # Fill `filling` with the input ids so DB links get created.
        out: list[ExtractedThought] = []
        for t in thoughts:
            if not t.filling:
                out.append(
                    ExtractedThought(
                        description=t.description,
                        emotional_impact=t.emotional_impact,
                        emotion_tags=list(t.emotion_tags),
                        relational_tags=list(t.relational_tags),
                        filling=[n.id for n in nodes],
                    )
                )
            else:
                out.append(t)
        return out

    return _reflect


async def _empty_extract(_msgs):
    return ExtractionResult()


async def _empty_reflect(_nodes, _reason):
    return []


# ---------------------------------------------------------------------------
# Trivial skip
# ---------------------------------------------------------------------------


def test_is_trivial_short_session_without_emotion():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s_t",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=2,
            total_tokens=80,
        )
        db.add(sess)
        db.commit()

        msgs = [
            RecallMessage(
                session_id="s_t",
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER,
                content="在吗",
                day=date.today(),
            )
        ]
        assert is_trivial(sess, msgs) is True


def test_is_trivial_false_when_strong_emotion_present():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s_t",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=1,
            total_tokens=50,
        )
        db.add(sess)
        db.commit()

        msgs = [
            RecallMessage(
                session_id="s_t",
                persona_id="p_test",
                user_id="self",
                channel_id="test",
                role=MessageRole.USER,
                content="我妈上个月走了",
                day=date.today(),
            )
        ]
        assert is_trivial(sess, msgs) is False


async def test_consolidate_trivial_session_marks_closed_without_extraction():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = Session(
            id="s_t",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=1,
            total_tokens=40,
        )
        db.add(sess)
        db.commit()
        _add_messages(db, "s_t", ["在吗"])

        extract_called: list[int] = []
        extractor = _make_async_extractor([], track=extract_called)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
        )

        assert result.skipped is True
        assert result.events_created == []
        assert extract_called == [], "Trivial session must skip extraction entirely"

        db.refresh(sess)
        assert sess.status == SessionStatus.CLOSED
        assert sess.trivial is True


# ---------------------------------------------------------------------------
# Extraction happy path
# ---------------------------------------------------------------------------


async def test_consolidate_normal_session_creates_events():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            [
                "我家 Mochi 今天又调皮了",
                "哈哈，它最近精力很旺",
                "昨天把我早上六点吵醒",
                "我已经完全拿它没办法了",
                "但还是很爱它",
            ],
        )

        extractor = _make_async_extractor(
            [
                ExtractedEvent(
                    description="用户有只叫 Mochi 的猫，很宠它",
                    emotional_impact=4,
                    emotion_tags=["joy", "pet"],
                    relational_tags=["identity-bearing"],
                )
            ]
        )

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
        )

        assert result.skipped is False
        assert len(result.events_created) == 1
        event = result.events_created[0]
        assert event.emotional_impact == 4
        assert event.emotion_tags == ["joy", "pet"]
        assert event.source_session_id == "s_test"

        db.refresh(sess)
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True


# ---------------------------------------------------------------------------
# SHOCK reflection
# ---------------------------------------------------------------------------


async def test_shock_event_triggers_reflection():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            [
                "其实我想说我爸两年前走了",
                "我一直没告诉任何人",
                "今天不知道为什么想说出来",
                "觉得有点轻一些了",
                "谢谢你听",
            ],
        )

        extractor = _make_async_extractor(
            [
                ExtractedEvent(
                    description="用户分享父亲两年前去世",
                    emotional_impact=-9,
                    emotion_tags=["grief", "bereavement"],
                    relational_tags=["identity-bearing", "vulnerability"],
                )
            ]
        )

        reasons: list[str] = []

        async def reflector(nodes, reason):
            reasons.append(reason)
            return [
                ExtractedThought(
                    description="Alan 把最重的事压了很久才敢说出来",
                    emotional_impact=-5,
                    emotion_tags=["vulnerability-window"],
                    relational_tags=["identity-bearing"],
                    filling=[
                        n.id
                        for n in nodes
                        if n.emotional_impact <= -SHOCK_IMPACT_THRESHOLD
                    ],
                )
            ]

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=reflector,
            embed_fn=_deterministic_embed,
        )

        assert result.reflection_reason == "shock"
        assert len(result.thoughts_created) == 1
        assert reasons == ["shock"]

        # Filling link should exist
        thought = result.thoughts_created[0]
        links = list(
            db.exec(
                select(ConceptNodeFilling).where(
                    ConceptNodeFilling.parent_id == thought.id
                )
            )
        )
        assert len(links) == 1


# ---------------------------------------------------------------------------
# TIMER reflection
# ---------------------------------------------------------------------------


async def test_timer_reflection_runs_when_no_prior_thought_in_24h():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            ["今天工作挺忙的", "老板又催项目了", "还好同事给力", "下班去吃了火锅", "挺开心"],
        )

        extractor = _make_async_extractor(
            [
                ExtractedEvent(
                    description="用户今天工作被催但下班吃了火锅心情变好",
                    emotional_impact=2,
                    emotion_tags=["mild-stress", "joy"],
                )
            ]
        )

        reasons: list[str] = []

        async def reflector(nodes, reason):
            reasons.append(reason)
            return [
                ExtractedThought(
                    description="Alan 的日常压力有明显的自我调节能力",
                    emotional_impact=2,
                    relational_tags=["pattern"],
                    filling=[n.id for n in nodes],
                )
            ]

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=reflector,
            embed_fn=_deterministic_embed,
        )

        assert result.reflection_reason == "timer"
        assert reasons == ["timer"]


async def test_timer_does_not_fire_when_recent_reflection_exists():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

        prior_thought = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.THOUGHT,
            description="prior reflection",
            emotional_impact=0,
        )
        db.add(prior_thought)
        db.commit()

        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            ["小事一桩", "今天还行", "没啥特别的", "就这样吧", "晚安"],
        )

        extractor = _make_async_extractor(
            [
                ExtractedEvent(
                    description="平淡的一天",
                    emotional_impact=1,
                )
            ]
        )

        reasons: list[str] = []

        async def reflector(nodes, reason):
            reasons.append(reason)
            return [ExtractedThought(description="", emotional_impact=0)]

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=reflector,
            embed_fn=_deterministic_embed,
        )

        assert result.reflection_reason is None
        assert reasons == []


# ---------------------------------------------------------------------------
# Hard gate
# ---------------------------------------------------------------------------


async def test_reflection_hard_gate_blocks_beyond_3_per_24h():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)

        for i in range(3):
            t = ConceptNode(
                persona_id="p_test",
                user_id="self",
                type=NodeType.THOUGHT,
                description=f"prior thought {i}",
                emotional_impact=0,
            )
            db.add(t)
        db.commit()

        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            [
                "我妈上个月走了我一直没说",
                "每天都在硬撑",
                "今晚实在忍不住告诉你",
                "你是第一个知道的",
                "谢谢你不评判我",
            ],
        )

        extractor = _make_async_extractor(
            [
                ExtractedEvent(
                    description="母亲去世未曾告人，今晚首次坦白",
                    emotional_impact=-9,
                )
            ]
        )

        reasons: list[str] = []

        async def reflector(nodes, reason):
            reasons.append(reason)
            return [
                ExtractedThought(description="should not be reached", emotional_impact=0)
            ]

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=reflector,
            embed_fn=_deterministic_embed,
        )

        assert len(result.events_created) == 1
        assert reasons == [], "Hard gate should prevent 4th reflection in 24h"
        assert result.reflection_reason is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_already_closed_session_is_noop():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db, status=SessionStatus.CLOSED)

        called: list[int] = []
        extractor = _make_async_extractor([], track=called)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
        )

        assert result.skipped is True
        assert called == []


# ---------------------------------------------------------------------------
# Retry safety (P0 fix — 2026-04-consolidate-retry-safety)
# ---------------------------------------------------------------------------


async def test_consolidate_skips_extraction_when_already_extracted():
    """If a prior attempt committed the extraction phase, a retry must
    load those events instead of re-running the extractor.

    This is the positive side of the retry-safety P0 fix — proves the
    skip-B branch loads persisted events and feeds them to reflection
    without ever invoking ``extract_fn``.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        # Simulate a session where B already ran on a prior attempt:
        # events are persisted and extracted_events flag is set, but
        # reflection never finished so the session is still CLOSING.
        sess = Session(
            id="s_resume",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=5,
            total_tokens=500,
            extracted_events=True,
        )
        db.add(sess)
        db.commit()
        _add_messages(db, "s_resume", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        pre_existing = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="pre-existing event from prior attempt",
            emotional_impact=5,
            source_session_id="s_resume",
        )
        db.add(pre_existing)
        db.commit()
        db.refresh(pre_existing)

        async def must_not_be_called(_msgs):
            raise AssertionError(
                "extract_fn must not be called when extracted_events=True"
            )

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=must_not_be_called,
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
        )

        # No new events were created (the existing one was loaded from
        # the DB, not re-extracted).
        events = db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == "s_resume",
                ConceptNode.type == NodeType.EVENT,
            )
        ).all()
        assert len(events) == 1
        assert events[0].id == pre_existing.id

        # Session is now fully closed after F.
        assert result.skipped is False
        assert sess.status == SessionStatus.CLOSED
        assert sess.extracted is True


async def test_consolidate_sets_extracted_events_flag_before_reflection():
    """The ``extracted_events`` flag must be persisted in the same commit
    as the B-phase events, so a reflection failure (even a raise) leaves
    the session in a state where the next retry will skip B.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(
            db,
            "s_test",
            ["我今天心情糟透了", "我爸走了", "我缓不过来", "真的很难", "整个人不对"],
        )

        async def extractor(_msgs):
            return ExtractionResult(
                events=[
                    ExtractedEvent(
                        description="user lost father suddenly",
                        # Forces SHOCK trigger → reflection runs
                        emotional_impact=9,
                        emotion_tags=["grief"],
                    )
                ]
            )

        async def exploding_reflect(_nodes, _reason):
            raise RuntimeError("reflection blew up mid-flight")

        with pytest.raises(RuntimeError, match="reflection blew up"):
            await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=extractor,
                reflect_fn=exploding_reflect,
                embed_fn=_deterministic_embed,
            )

    # Re-open the DB so we read the persisted state, not the in-memory
    # transaction that the failed call was using.
    with DbSession(engine) as db:
        saved = db.get(Session, "s_test")
        assert saved is not None
        assert saved.extracted_events is True, (
            "flag must persist across reflection failure so retry skips B"
        )
        assert saved.extracted_events_at is not None
        # F never ran.
        assert saved.status == SessionStatus.CLOSING
        assert saved.extracted is False

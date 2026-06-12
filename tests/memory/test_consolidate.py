"""CONSOLIDATE pipeline tests.

LLM callables are mocked — memory module must not depend on real providers.

ExtractFn / ReflectFn are async callables (spec §6.4), so these tests use
async def + pytest-asyncio's `asyncio_mode = "auto"` from pyproject.toml.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import EventTime, MessageRole, NodeType, SessionStatus
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
    ExtractedEntity,
    ExtractedEvent,
    ExtractedSessionMoodSignal,
    ExtractedThought,
    ExtractionResult,
    consolidate_session,
    is_trivial,
)
from echovessel.memory.consolidate.phase_bce import (
    _count_reflections_24h,
    _is_timer_due,
)
from echovessel.memory.models import ConceptNode, ConceptNodeEntity

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


# ---------------------------------------------------------------------------
# Resume bookkeeping beyond phase B — entities / summary / mood / reflection
# ---------------------------------------------------------------------------


def _summary_nodes(db: DbSession, session_id: str) -> list[ConceptNode]:
    return list(
        db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == session_id,
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.emotion_tags.like('%"session_summary"%'),  # type: ignore[union-attr]
            )
        )
    )


def _rich_extraction_result() -> ExtractionResult:
    return ExtractionResult(
        events=[
            ExtractedEvent(
                description="用户说 Scott 升职了，自己也很受触动",
                emotional_impact=9,
                emotion_tags=["joy"],
            )
        ],
        mentioned_entities=[
            ExtractedEntity(canonical_name="Scott", aliases=[], kind="person", in_events=[0])
        ],
        session_mood_signal=ExtractedSessionMoodSignal(
            mood="warm-open", energy=7, last_user_signal="warm"
        ),
        session_summary="聊了 Scott 升职的事，气氛很好",
    )


async def test_retry_after_reflect_failure_keeps_one_set_of_derived_writes():
    """A reflect failure after the extraction commit must not lose any
    derived write on retry: the retried run ends with exactly one event,
    one set of junctions, one session_summary node, one mood update, and
    one reflection thought.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, "s_test", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        async def extractor(_msgs):
            return _rich_extraction_result()

        async def exploding_reflect(_nodes, _reason):
            raise RuntimeError("reflection outage")

        with pytest.raises(RuntimeError, match="reflection outage"):
            await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=extractor,
                reflect_fn=exploding_reflect,
                embed_fn=_deterministic_embed,
            )

    with DbSession(engine) as db:
        sess_again = db.get(Session, "s_test")
        assert sess_again is not None

        async def must_not_extract(_msgs):
            raise AssertionError("extract_fn must not run on resume")

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess_again,
            extract_fn=must_not_extract,
            reflect_fn=_make_async_reflector(
                [ExtractedThought(description="Scott 的事对用户意义很大", emotional_impact=4)]
            ),
            embed_fn=_deterministic_embed,
        )
        assert result.reflection_reason == "shock"

    with DbSession(engine) as db:
        events = db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == "s_test",
                ConceptNode.type == NodeType.EVENT.value,
            )
        ).all()
        assert len(events) == 1

        junctions = db.exec(select(ConceptNodeEntity)).all()
        assert len(junctions) == 1
        assert junctions[0].node_id == events[0].id

        assert len(_summary_nodes(db, "s_test")) == 1

        persona = db.get(Persona, "p_test")
        assert persona is not None
        assert persona.episodic_state["mood"] == "warm-open", "mood update must survive the retry"

        thoughts = db.exec(
            select(ConceptNode).where(
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.emotion_tags.not_like('%"session_summary"%'),  # type: ignore[union-attr]
            )
        ).all()
        assert len(thoughts) == 1

        sess_done = db.get(Session, "s_test")
        assert sess_done is not None
        assert sess_done.status == SessionStatus.CLOSED
        assert sess_done.reflected is True
        assert sess_done.extraction_payload is None


async def test_resume_rehydrates_extraction_payload_for_derived_writes():
    """A retry where the prior attempt committed only events + payload
    (e.g. the entity-phase commit failed) must rebuild the junctions,
    the session_summary node, and the mood update from the payload —
    without calling the extraction LLM again.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        pre_existing = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="用户说 Scott 升职了",
            emotional_impact=4,
            source_session_id="s_resume",
        )
        db.add(pre_existing)
        db.commit()
        db.refresh(pre_existing)

        payload = json.dumps(
            {
                "event_node_ids": {"0": pre_existing.id},
                "mentioned_entities": [
                    {
                        "canonical_name": "Scott",
                        "aliases": [],
                        "kind": "person",
                        "in_events": [0],
                    }
                ],
                "entity_clarification": None,
                "session_mood_signal": {
                    "mood": "warm-open",
                    "energy": 6,
                    "last_user_signal": "warm",
                },
                "session_summary": "聊了 Scott 升职的事",
            }
        )
        sess = Session(
            id="s_resume",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=5,
            total_tokens=500,
            extracted_events=True,
            extraction_payload=payload,
        )
        db.add(sess)
        db.commit()
        _add_messages(db, "s_resume", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        async def must_not_extract(_msgs):
            raise AssertionError("extract_fn must not run on resume")

        await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=must_not_extract,
            reflect_fn=_empty_reflect,
            embed_fn=_deterministic_embed,
        )

        junctions = db.exec(select(ConceptNodeEntity)).all()
        assert len(junctions) == 1
        assert junctions[0].node_id == pre_existing.id

        assert len(_summary_nodes(db, "s_resume")) == 1

        persona = db.get(Persona, "p_test")
        assert persona is not None
        assert persona.episodic_state["mood"] == "warm-open"

        db.refresh(sess)
        assert sess.status == SessionStatus.CLOSED
        assert sess.extraction_payload is None


async def test_resume_includes_intention_nodes_from_prior_attempt():
    """When extraction produced both a plain EVENT and an INTENTION
    (subject='persona' + event_time) and reflection failed transiently,
    the retry must rehydrate both node types: the intention drives SHOCK
    detection, reaches the reflector, stays wired to its entity junction,
    and appears exactly once in ``events_created``.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    extraction = ExtractionResult(
        events=[
            ExtractedEvent(
                description="Scott 说下周要交申请材料",
                emotional_impact=2,
            ),
            ExtractedEvent(
                description="我答应明早九点提醒 Scott 交材料",
                emotional_impact=SHOCK_IMPACT_THRESHOLD,
                subject="persona",
                event_time=EventTime(start=datetime(2026, 6, 13, 9, 0)),
            ),
        ],
        mentioned_entities=[
            ExtractedEntity(canonical_name="Scott", aliases=[], kind="person", in_events=[0, 1])
        ],
    )

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, "s_test", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        async def extractor(_msgs):
            return extraction

        async def exploding_reflect(_nodes, _reason):
            raise RuntimeError("reflection outage")

        with pytest.raises(RuntimeError, match="reflection outage"):
            await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=extractor,
                reflect_fn=exploding_reflect,
                embed_fn=_deterministic_embed,
            )

    with DbSession(engine) as db:
        sess_again = db.get(Session, "s_test")
        assert sess_again is not None

        async def must_not_extract(_msgs):
            raise AssertionError("extract_fn must not run on resume")

        reflect_input_ids: list[list[int]] = []

        async def reflector(nodes, reason):
            reflect_input_ids.append([n.id for n in nodes])
            return [
                ExtractedThought(
                    description="这个承诺对她很重要",
                    emotional_impact=4,
                    filling=[n.id for n in nodes],
                )
            ]

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess_again,
            extract_fn=must_not_extract,
            reflect_fn=reflector,
            embed_fn=_deterministic_embed,
        )

        assert result.reflection_reason == "shock"
        assert len(result.events_created) == 2
        intentions = [
            n
            for n in result.events_created
            if getattr(n.type, "value", n.type) == NodeType.INTENTION.value
        ]
        assert len(intentions) == 1, "rehydrated set must include the INTENTION exactly once"
        assert reflect_input_ids and intentions[0].id in reflect_input_ids[0]

    with DbSession(engine) as db:
        intention_rows = db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == "s_test",
                ConceptNode.type == NodeType.INTENTION.value,
            )
        ).all()
        assert len(intention_rows) == 1
        event_rows = db.exec(
            select(ConceptNode).where(
                ConceptNode.source_session_id == "s_test",
                ConceptNode.type == NodeType.EVENT.value,
            )
        ).all()
        assert len(event_rows) == 1

        junctions = db.exec(select(ConceptNodeEntity)).all()
        assert {j.node_id for j in junctions} == {event_rows[0].id, intention_rows[0].id}

        sess_done = db.get(Session, "s_test")
        assert sess_done is not None
        assert sess_done.status == SessionStatus.CLOSED


async def test_resume_skips_reflection_when_already_reflected():
    """A failure between the reflection commit and the close commit must
    not re-run reflection on retry — the ``reflected`` flag committed with
    the thoughts makes the retry skip phase E entirely.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        # Prior attempt: events + payload + thoughts all committed, but the
        # close commit never landed.
        shock = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="重大事件",
            emotional_impact=-9,
            source_session_id="s_reflected",
        )
        prior_thought = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.THOUGHT,
            description="prior reflection from the first attempt",
            emotional_impact=-4,
        )
        db.add(shock)
        db.add(prior_thought)
        sess = Session(
            id="s_reflected",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=5,
            total_tokens=500,
            extracted_events=True,
            reflected=True,
        )
        db.add(sess)
        db.commit()
        _add_messages(db, "s_reflected", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        async def must_not_extract(_msgs):
            raise AssertionError("extract_fn must not run on resume")

        async def must_not_reflect(_nodes, _reason):
            raise AssertionError("reflect_fn must not run again after the reflection commit")

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=must_not_extract,
            reflect_fn=must_not_reflect,
            embed_fn=_deterministic_embed,
        )
        assert result.reflection_reason is None

        thoughts = db.exec(
            select(ConceptNode).where(ConceptNode.type == NodeType.THOUGHT.value)
        ).all()
        assert len(thoughts) == 1, "retry must not add a second reflection"

        db.refresh(sess)
        assert sess.status == SessionStatus.CLOSED


# ---------------------------------------------------------------------------
# Reflection gating ignores session_summary thoughts
# ---------------------------------------------------------------------------


def test_reflection_gates_ignore_session_summary_thoughts():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        _seed(db)
        for i in range(3):
            db.add(
                ConceptNode(
                    persona_id="p_test",
                    user_id="self",
                    type=NodeType.THOUGHT,
                    description=f"session summary {i}",
                    emotional_impact=0,
                    emotion_tags=["session_summary"],
                )
            )
        db.commit()

        now = datetime.now()
        assert _count_reflections_24h(db, "p_test", "self", now) == 0
        assert _is_timer_due(db, "p_test", "self", now) is True

        db.add(
            ConceptNode(
                persona_id="p_test",
                user_id="self",
                type=NodeType.THOUGHT,
                description="real reflection",
                emotional_impact=2,
            )
        )
        db.commit()
        assert _count_reflections_24h(db, "p_test", "self", now) == 1
        assert _is_timer_due(db, "p_test", "self", now) is False


async def test_timer_fires_even_when_extraction_emits_session_summary():
    """The session_summary thought committed during phase B must not be
    mistaken for a fresh reflection by the TIMER gate in the same run."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, "s_test", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        async def extractor(_msgs):
            return ExtractionResult(
                events=[ExtractedEvent(description="平常的一天", emotional_impact=2)],
                session_summary="今天聊了日常",
            )

        reasons: list[str] = []

        async def reflector(nodes, reason):
            reasons.append(reason)
            return [
                ExtractedThought(
                    description="日常也值得记下",
                    emotional_impact=1,
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
        assert len(_summary_nodes(db, "s_test")) == 1


# ---------------------------------------------------------------------------
# Embedding runs off the event loop
# ---------------------------------------------------------------------------


async def test_node_embeddings_run_off_the_event_loop():
    """The embed callable is sync (sentence-transformers); the vector
    writes for events, the session summary, and reflection thoughts must
    run it via a worker thread, never on the loop thread."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    calls: list[tuple[str, bool]] = []

    def recording_embed(text: str) -> list[float]:
        calls.append((text, threading.current_thread() is threading.main_thread()))
        return _deterministic_embed(text)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, "s_test", ["a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40])

        event_text = "用户说今天发生了一件大事"
        summary_text = "聊了一件大事"
        thought_text = "这件事值得记住"

        async def extractor(_msgs):
            return ExtractionResult(
                events=[ExtractedEvent(description=event_text, emotional_impact=9)],
                session_summary=summary_text,
            )

        async def reflector(nodes, _reason):
            return [
                ExtractedThought(
                    description=thought_text,
                    emotional_impact=3,
                    filling=[n.id for n in nodes],
                )
            ]

        await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=extractor,
            reflect_fn=reflector,
            embed_fn=recording_embed,
        )

    on_main_by_text: dict[str, list[bool]] = {}
    for text, on_main in calls:
        on_main_by_text.setdefault(text, []).append(on_main)

    assert on_main_by_text[summary_text] == [False]
    assert on_main_by_text[thought_text] == [False]
    # The event description is also embedded by the mention-dedup helper;
    # the node-vector embed itself must come from a worker thread.
    assert False in on_main_by_text[event_text]

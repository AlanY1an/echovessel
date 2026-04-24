"""Guard test: the extraction LLM's ``entity.in_events`` is best-effort,
and over-linking has been observed in practice — a session that mentions
Scott in chat + extracts an unrelated "next-week exam" event would have
``in_events=[scott_idx, exam_idx]`` on the Scott entity.

``_consolidate_entities`` must refuse the exam-event junction row so
alias-anchor retrieval for "Scott" does not surface the exam event and
poison the prompt. Accept only junctions where the entity's
``canonical_name`` or any of its aliases appears literally in the event's
``description`` text.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, SessionStatus
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
from echovessel.memory.models import ConceptNodeEntity
from echovessel.prompts.extraction import RawExtractedEntity


def _deterministic_embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


@pytest.mark.asyncio
async def test_entity_link_rejects_events_without_matching_surface_form():
    """When extraction claims an entity appears in N events, only events
    whose ``description`` literally contains canonical_name or an alias
    should get a junction row. The rest must be silently dropped.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test"))
        db.add(User(id="self", display_name="Alan"))
        sess = Session(
            id="s_entity_guard",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=4,
            total_tokens=200,
        )
        db.add(sess)
        db.commit()

        for i, c in enumerate(
            [
                "我昨天和 Scott 吃饭了 他压力挺大",
                "嗯 Scott 最近看起来累",
                "对了我下周有期末考试",
                "有点紧张",
            ]
        ):
            db.add(
                RecallMessage(
                    session_id=sess.id,
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

        # Extraction LLM output: two events + one entity that over-reaches.
        # Event 0 literally mentions Scott.
        # Event 1 is the unrelated exam event, does NOT mention Scott/黄逸扬.
        # Entity over-reaches and claims in_events=[0, 1].
        extraction = ExtractionResult(
            events=[
                ExtractedEvent(
                    description="用户跟 Scott 吃饭 · Scott 最近压力挺大",
                    emotional_impact=3,
                    emotion_tags=["concern"],
                    relational_tags=[],
                ),
                ExtractedEvent(
                    description="用户提到下周期末考试 · 有点紧张",
                    emotional_impact=4,
                    emotion_tags=["anticipation"],
                    relational_tags=[],
                ),
            ],
            mentioned_entities=[
                RawExtractedEntity(
                    canonical_name="黄逸扬",
                    aliases=["Scott"],
                    kind="person",
                    in_events=[0, 1],
                )
            ],
        )

        async def _extract(_msgs):
            return extraction

        async def _reflect(_nodes, _reason):
            return []

        await consolidate_session(
            db,
            backend,
            session=sess,
            extract_fn=_extract,
            reflect_fn=_reflect,
            embed_fn=_deterministic_embed,
        )

        # Now the post-condition we care about.
        junctions = list(db.exec(select(ConceptNodeEntity)))
        assert len(junctions) == 1, (
            f"expected exactly 1 junction (event-0-linked), "
            f"got {len(junctions)}: {[(j.node_id, j.entity_id) for j in junctions]}"
        )
        # And that one junction points at the event whose description
        # literally contains "Scott".
        from echovessel.memory.models import ConceptNode

        linked_node = db.exec(
            select(ConceptNode).where(ConceptNode.id == junctions[0].node_id)
        ).first()
        assert linked_node is not None
        assert "Scott" in (linked_node.description or "")
        assert "期末考试" not in (linked_node.description or "")


@pytest.mark.asyncio
async def test_entity_link_alias_substring_match_is_sufficient():
    """Canonical name missing from description but an alias present → accept.
    Proves the defense doesn't accidentally reject valid cross-alias writes.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p_test", display_name="Test"))
        db.add(User(id="self", display_name="Alan"))
        sess = Session(
            id="s_alias_ok",
            persona_id="p_test",
            user_id="self",
            channel_id="test",
            status=SessionStatus.CLOSING,
            message_count=4,
            total_tokens=250,
        )
        db.add(sess)
        for i, c in enumerate(
            [
                "Scott 想换工作",
                "他考虑好久了",
                "听说他找的方向挺明确的",
                "希望顺利",
            ]
        ):
            db.add(
                RecallMessage(
                    session_id=sess.id,
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

        extraction = ExtractionResult(
            events=[
                ExtractedEvent(
                    # Description uses alias "Scott", not canonical "黄逸扬".
                    description="Scott 跟用户说他想换工作",
                    emotional_impact=2,
                    emotion_tags=["concern"],
                    relational_tags=[],
                )
            ],
            mentioned_entities=[
                RawExtractedEntity(
                    canonical_name="黄逸扬",
                    aliases=["Scott", "Yiyang"],
                    kind="person",
                    in_events=[0],
                )
            ],
        )

        async def _extract(_msgs):
            return extraction

        async def _reflect(_nodes, _reason):
            return []

        await consolidate_session(
            db,
            backend,
            session=sess,
            extract_fn=_extract,
            reflect_fn=_reflect,
            embed_fn=_deterministic_embed,
        )

        junctions = list(db.exec(select(ConceptNodeEntity)))
        assert len(junctions) == 1

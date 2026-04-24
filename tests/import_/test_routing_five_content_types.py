"""All 5 memory content_types get dispatched correctly + self_block side path."""

from __future__ import annotations

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import BlockLabel, NodeType
from echovessel.import_.models import Chunk, ContentItem
from echovessel.import_.routing import dispatch_item, translate_llm_write
from echovessel.memory.models import ConceptNode, CoreBlockAppend


def _chunk_with(content: str) -> Chunk:
    return Chunk(chunk_index=0, total_chunks=1, content=content, source_label="test")


def test_persona_traits_dispatch(db_session_factory, engine):
    item = ContentItem(
        content_type="persona_traits",
        payload={
            "persona_id": "p_test",
            "user_id": "self",
            "content": "她很怕鬼但对她爱的人极其坚定",
        },
    )
    with db_session_factory() as db:
        result, new_ids = dispatch_item(item, db=db, source="hash-1")
    assert result.content_type == "persona_traits"
    assert new_ids == []
    with DbSession(engine) as db:
        appends = list(db.exec(select(CoreBlockAppend)))
        assert len(appends) == 1
        assert appends[0].label == BlockLabel.PERSONA.value


def test_user_identity_facts_dispatch(db_session_factory, engine):
    item = ContentItem(
        content_type="user_identity_facts",
        payload={
            "persona_id": "p_test",
            "user_id": "self",
            "content": "用户是数据科学家",
            "category": "work",
        },
    )
    with db_session_factory() as db:
        result, new_ids = dispatch_item(item, db=db, source="hash-2")
    assert result.content_type == "user_identity_facts"
    with DbSession(engine) as db:
        appends = list(db.exec(select(CoreBlockAppend)))
        assert any(a.label == BlockLabel.USER.value for a in appends)


def test_user_events_dispatch_creates_event_node(db_session_factory, engine):
    item = ContentItem(
        content_type="user_events",
        payload={
            "persona_id": "p_test",
            "user_id": "self",
            "events": [
                {
                    "description": "Mochi 去世那天",
                    "emotional_impact": -7,
                    "emotion_tags": ["grief"],
                    "relational_tags": ["unresolved"],
                }
            ],
        },
    )
    with db_session_factory() as db:
        result, new_ids = dispatch_item(item, db=db, source="hash-3")
    assert result.content_type == "user_events"
    assert len(new_ids) == 1
    with DbSession(engine) as db:
        nodes = list(db.exec(select(ConceptNode)))
        assert len(nodes) == 1
        assert nodes[0].type == NodeType.EVENT.value
        assert nodes[0].imported_from == "hash-3"


def test_user_reflections_dispatch_creates_thought_node(
    db_session_factory, engine
):
    item = ContentItem(
        content_type="user_reflections",
        payload={
            "persona_id": "p_test",
            "user_id": "self",
            "thoughts": [
                {
                    "description": "我总是在退一步",
                    "emotional_impact": 0,
                    "emotion_tags": [],
                    "relational_tags": [],
                }
            ],
        },
    )
    with db_session_factory() as db:
        result, new_ids = dispatch_item(item, db=db, source="hash-4")
    assert result.content_type == "user_reflections"
    assert len(new_ids) == 1
    with DbSession(engine) as db:
        nodes = list(db.exec(select(ConceptNode)))
        assert len(nodes) == 1
        assert nodes[0].type == NodeType.THOUGHT.value


def test_relationship_facts_target_is_rejected(db_session_factory):
    """v0.5 · L1.relationship_block was deleted from LEGAL_LLM_TARGETS.

    A pipeline that still emits the legacy target now sees a hard
    ValueError from :func:`translate_llm_write` instead of a silent
    write — CLAUDE.md ``no backcompat shims`` rule.
    """
    chunk = _chunk_with("Alan 是她男友这一句话用作 evidence")
    raw = {
        "target": "L1.relationship_block",
        "content": "Alan 是她男友",
        "person_label": "Alan",
        "confidence": 0.9,
        "evidence_quote": "Alan 是她男友这一句话用作 evidence",
    }
    import pytest

    with pytest.raises(ValueError, match="unknown target"):
        translate_llm_write(
            raw, chunk=chunk, persona_id="p_test", user_id="self"
        )


def test_self_block_target_is_rejected(db_session_factory):
    """v0.5 · L1.self_block was deleted from LEGAL_LLM_TARGETS too.

    Persona self-narrative now lives on L4.thought[subject='persona']
    via slow_cycle, so the import-time target is gone. Same hard fail
    as the relationship case.
    """
    chunk = _chunk_with("我容易在半夜醒来然后想太多这是一句话")
    raw = {
        "target": "L1.self_block",
        "content": "我容易在半夜醒来然后想太多",
        "confidence": 0.9,
        "evidence_quote": "我容易在半夜醒来然后想太多",
    }
    import pytest

    with pytest.raises(ValueError, match="unknown target"):
        translate_llm_write(
            raw, chunk=chunk, persona_id="p_test", user_id="self"
        )

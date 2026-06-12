"""LIKE-fallback path of :func:`search_concept_nodes`.

The fallback engages whenever any token is shorter than the FTS5
trigram floor (3 chars) — common for CJK ("下雨") and ASCII ("AI")
alike. Multi-token queries must require EVERY token to appear in the
description, mirroring the implicit-AND semantics of the FTS5 path.
"""

from __future__ import annotations

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.models import ConceptNode
from echovessel.memory.retrieve import search_concept_nodes


def _build_engine():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()
    return engine


def _seed_node(engine, description: str) -> int:
    with DbSession(engine) as db:
        row = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description=description,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return int(row.id)


def test_multi_token_query_requires_all_tokens():
    """ "AI 编程心得" must narrow to nodes containing BOTH terms — a node
    mentioning only "AI" is not a hit, and the total reflects that."""
    engine = _build_engine()
    target_id = _seed_node(engine, "整理了一篇 AI 编程心得，记录踩坑过程")
    _seed_node(engine, "写完了本周的 AI 周报")
    _seed_node(engine, "编程心得分享会改到了下周")

    with DbSession(engine) as db:
        hits, total = search_concept_nodes(db, "p", "self", query_text="AI 编程心得")

    assert total == 1
    assert [h.node.id for h in hits] == [target_id]


def test_multi_token_snippet_highlights_longest_token():
    engine = _build_engine()
    _seed_node(engine, "整理了一篇 AI 编程心得，记录踩坑过程")

    with DbSession(engine) as db:
        hits, total = search_concept_nodes(db, "p", "self", query_text="AI 编程心得")

    assert total == 1
    assert "<b>编程心得</b>" in hits[0].snippet


def test_single_short_token_still_matches():
    engine = _build_engine()
    target_id = _seed_node(engine, "今天下雨了，心情低落")
    _seed_node(engine, "阳光明媚的下午")

    with DbSession(engine) as db:
        hits, total = search_concept_nodes(db, "p", "self", query_text="下雨")

    assert total == 1
    assert hits[0].node.id == target_id
    assert "<b>下雨</b>" in hits[0].snippet


def test_quotes_only_query_matches_nothing():
    """A query that reduces to zero tokens (only quote characters) must
    not degrade into a match-everything scan."""
    engine = _build_engine()
    _seed_node(engine, "quiet afternoon")

    with DbSession(engine) as db:
        hits, total = search_concept_nodes(db, "p", "self", query_text='""')

    assert hits == []
    assert total == 0

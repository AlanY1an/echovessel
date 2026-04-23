"""L5 entity-anchor rerank bonus (plan §6.3) — Case 8 Scott/黄逸扬.

When a query text contains a known entity alias, concept nodes linked
to that entity through the L3↔L5 junction get bumped above the
relevance floor so vector-only misses (e.g. cross-language aliases)
still surface.
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
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeEntity,
    Entity,
    EntityAlias,
)
from echovessel.memory.retrieve import (
    ENTITY_ANCHOR_BONUS_VALUE,
    find_query_entities,
    get_nodes_linked_to_entities,
    retrieve,
)


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _unit_embed(slot: int) -> list[float]:
    v = [0.0] * 384
    v[slot % 384] = 1.0
    return v


def _orthogonal_embed(_text: str) -> list[float]:
    # Any fixed vector distinct from the node's vector → orthogonal by
    # construction. Forces relevance ≈ 0.293 < min_relevance floor.
    return _unit_embed(0)


def test_find_query_entities_matches_alias_token():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    with DbSession(engine) as db:
        db.add(
            Entity(
                id=1,
                persona_id="p",
                user_id="self",
                canonical_name="黄逸扬",
                kind="person",
                merge_status="confirmed",
            )
        )
        db.add(EntityAlias(alias="黄逸扬", entity_id=1))
        db.add(EntityAlias(alias="Scott", entity_id=1))
        db.commit()

    with DbSession(engine) as db:
        matched = find_query_entities(
            db, "Scott 最近怎么样", persona_id="p", user_id="self"
        )
        assert matched == [1]

        # CJK alias also matches via the raw-text fallback.
        matched_zh = find_query_entities(
            db, "关于黄逸扬的事", persona_id="p", user_id="self"
        )
        assert matched_zh == [1]

        # No match at all.
        assert find_query_entities(
            db, "今天天气不错", persona_id="p", user_id="self"
        ) == []


def test_query_alias_bumps_junction_linked_nodes_into_top_k():
    """Case 8 smoke: user asks about Scott, but the event description
    uses only 黄逸扬. Without entity anchor, vector distance is
    orthogonal and the relevance floor drops it. With anchor, the
    node surfaces because of the junction link."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    # Seed the L5 entity + aliases + the L3 event + junction link.
    with DbSession(engine) as db:
        db.add(
            Entity(
                id=1,
                persona_id="p",
                user_id="self",
                canonical_name="黄逸扬",
                kind="person",
                merge_status="confirmed",
            )
        )
        db.add(EntityAlias(alias="黄逸扬", entity_id=1))
        db.add(EntityAlias(alias="Scott", entity_id=1))

        ev = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="黄逸扬 is starting a startup with 3 friends",
            emotional_impact=3,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)

        db.add(ConceptNodeEntity(node_id=ev.id, entity_id=1))
        db.commit()
        event_id = ev.id

    # Vector at slot 123; the query embedder returns orthogonal slot 0.
    backend.insert_vector(event_id, _unit_embed(123))

    with DbSession(engine) as db:
        result = retrieve(
            db,
            backend,
            persona_id="p",
            user_id="self",
            query_text="Scott 最近怎么样",
            embed_fn=_orthogonal_embed,
            top_k=5,
        )

    hits = [sm.node.id for sm in result.memories]
    assert event_id in hits, (
        f"entity-anchored node should surface despite orthogonal vector; got {hits}"
    )

    anchored = next(sm for sm in result.memories if sm.node.id == event_id)
    assert anchored.entity_anchor_bonus == ENTITY_ANCHOR_BONUS_VALUE


def test_query_without_alias_does_not_apply_bonus():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    with DbSession(engine) as db:
        ev = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="user went hiking in Yosemite",
            emotional_impact=2,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        event_id = ev.id
    backend.insert_vector(event_id, _unit_embed(50))

    # No entity seeded → no alias to match.
    with DbSession(engine) as db:
        matched = find_query_entities(
            db, "did they hike last weekend?", persona_id="p", user_id="self"
        )
        assert matched == []

        # The unrelated junction-less node should NOT get a bonus in retrieve.
        result = retrieve(
            db,
            backend,
            persona_id="p",
            user_id="self",
            query_text="did they hike last weekend?",
            embed_fn=lambda t: _unit_embed(50),  # exact match → relevance 1
            top_k=5,
        )
        assert all(sm.entity_anchor_bonus == 0.0 for sm in result.memories)


def test_get_nodes_linked_to_entities_empty_input():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        assert get_nodes_linked_to_entities(db, []) == set()

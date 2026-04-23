"""``_render_entity_disambiguation_hint`` injects the Level-3 ask-user
prompt text when a query alias-matches an entity whose
``merge_status='uncertain'`` and ``merge_target_id`` points at a known
other entity (plan §6.3.1).

Confirmed / disambiguated entities never produce a hint.
"""

from __future__ import annotations

from sqlmodel import Session as DbSession

from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.models import Entity, EntityAlias
from echovessel.runtime.interaction import _render_entity_disambiguation_hint


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def test_uncertain_entity_produces_ask_hint():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)

    with DbSession(engine) as db:
        # Entity 1: the known "黄逸扬" (target of uncertain merge).
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
        db.add(EntityAlias(alias="Yiyang", entity_id=1))

        # Entity 2: new "Scott" tagged uncertain, candidate merge_target = 1.
        db.add(
            Entity(
                id=2,
                persona_id="p",
                user_id="self",
                canonical_name="Scott",
                kind="person",
                merge_status="uncertain",
                merge_target_id=1,
            )
        )
        db.add(EntityAlias(alias="Scott", entity_id=2))
        db.commit()

    with DbSession(engine) as db:
        hint = _render_entity_disambiguation_hint(
            db, query_text="Scott 最近怎么样", persona_id="p", user_id="self"
        )

    assert hint, "Expected a non-empty hint for uncertain entity"
    assert "# Entity disambiguation pending" in hint
    assert "Scott" in hint
    assert "黄逸扬" in hint
    # Hint must not hardcode the exact phrasing — only describe the
    # ambiguity and ask the LLM to raise it naturally.
    assert "clarify with the user" in hint
    assert "natural moment" in hint.lower()


def test_confirmed_entity_has_no_hint():
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
        hint = _render_entity_disambiguation_hint(
            db, query_text="Scott 最近怎么样", persona_id="p", user_id="self"
        )

    assert hint == ""


def test_no_alias_match_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db:
        hint = _render_entity_disambiguation_hint(
            db, query_text="random text", persona_id="p", user_id="self"
        )
    assert hint == ""


def test_uncertain_with_missing_target_skipped():
    """If merge_target_id is None, we can't describe the candidate —
    skip rather than emitting half a sentence."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db:
        db.add(
            Entity(
                id=1,
                persona_id="p",
                user_id="self",
                canonical_name="Scott",
                kind="person",
                merge_status="uncertain",
                merge_target_id=None,
            )
        )
        db.add(EntityAlias(alias="Scott", entity_id=1))
        db.commit()

    with DbSession(engine) as db:
        hint = _render_entity_disambiguation_hint(
            db, query_text="Scott 最近怎么样", persona_id="p", user_id="self"
        )
    assert hint == ""

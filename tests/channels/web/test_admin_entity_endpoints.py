"""v0.5 hotfix · admin Persona Social Graph + Reflection endpoints.

Backs the Spec 2 (Worker B) frontend with seven cases:

1. ``GET /api/admin/memory/entities`` returns ``{entities: []}`` when
   nothing is seeded.
2. The same endpoint returns aliases + ``linked_events_count`` +
   ``last_mentioned_at`` for a populated row.
3. ``PATCH /api/admin/memory/entities/{id}`` writes a new
   description AND flips ``owner_override=true`` server-side.
4. ``POST /api/admin/memory/entities`` manually creates an entity
   with ``merge_status='confirmed'`` and ``owner_override=true``
   when a description is supplied.
5. ``POST /api/admin/memory/entities/{id}/merge`` calls the existing
   ``apply_entity_clarification(same=True)`` path and the loser is
   soft-deleted.
6. ``POST /api/admin/memory/entities/{id}/confirm-separate`` calls
   ``apply_entity_clarification(same=False)`` and any leftover
   uncertain row promotes to ``'confirmed'``.
7. ``GET /api/admin/memory/thoughts?subject=persona`` returns only
   ``subject='persona'`` thoughts and includes the new ``subject``
   / ``filling_event_ids`` / ``source`` fields on the serialized
   payload.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.routes.admin import build_admin_router
from echovessel.core.types import NodeType
from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeEntity,
    ConceptNodeFilling,
    Entity,
    EntityAlias,
    Persona,
    User,
)

# ---------------------------------------------------------------------------
# Test rig (shared with the rest of tests/channels/web)
# ---------------------------------------------------------------------------


class _Registry:
    def register(self, _ch: Any) -> None: ...
    def iter_channels(self):
        return iter([])


class _Cfg:
    class _Runtime:
        data_dir = "/tmp"

    runtime = _Runtime()


class _Persona:
    id = "admin-test"
    display_name = "admin-test"
    voice_enabled = False
    voice_id = None


class _Ctx:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.registry = _Registry()
        self.config = _Cfg()
        self.persona = _Persona()
        self.config_path = None


class _Runtime:
    def __init__(self, engine) -> None:
        self.ctx = _Ctx(engine)

    def _atomic_write_config_field(self, *args, **kwargs) -> None: ...


def _build() -> tuple[Any, TestClient]:
    tmp = Path(tempfile.mkdtemp())
    engine = create_engine(tmp / "entity_endpoints.db")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="admin-test", display_name="admin-test"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()
    rt = _Runtime(engine)
    app = FastAPI()
    app.include_router(build_admin_router(runtime=rt))
    return engine, TestClient(app)


def _seed_entity(
    engine,
    *,
    canonical: str,
    kind: str = "person",
    aliases: tuple[str, ...] = (),
    description: str | None = None,
    merge_status: str = "confirmed",
    merge_target_id: int | None = None,
) -> int:
    with DbSession(engine) as db:
        ent = Entity(
            persona_id="admin-test",
            user_id="self",
            canonical_name=canonical,
            kind=kind,
            description=description,
            merge_status=merge_status,
            merge_target_id=merge_target_id,
        )
        db.add(ent)
        db.commit()
        db.refresh(ent)
        for alias in aliases:
            db.add(EntityAlias(alias=alias, entity_id=ent.id))
        db.add(EntityAlias(alias=canonical, entity_id=ent.id))
        db.commit()
        return ent.id


def _seed_event_linked_to(
    engine, entity_id: int, *, description: str, when: datetime
) -> int:
    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id="admin-test",
            user_id="self",
            type=NodeType.EVENT,
            description=description,
            emotional_impact=2,
            created_at=when,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        db.add(ConceptNodeEntity(node_id=node.id, entity_id=entity_id))
        db.commit()
        return node.id


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_list_entities_empty():
    _, client = _build()
    with client:
        resp = client.get("/api/admin/memory/entities")
    assert resp.status_code == 200
    assert resp.json() == {"entities": []}


def test_list_entities_with_aliases_and_linked_count():
    engine, client = _build()
    huang_id = _seed_entity(
        engine, canonical="黄逸扬", aliases=("Scott",), description="室友"
    )
    when = datetime(2026, 4, 24, 12, 0, 0)
    _seed_event_linked_to(
        engine, huang_id, description="黄逸扬 来 SF 看望", when=when
    )
    _seed_event_linked_to(
        engine,
        huang_id,
        description="Scott 升职了",
        when=when - timedelta(days=2),
    )

    with client:
        resp = client.get("/api/admin/memory/entities")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entities"]) == 1
    row = body["entities"][0]
    assert row["canonical_name"] == "黄逸扬"
    assert row["description"] == "室友"
    assert row["owner_override"] is False  # default for seeded rows
    assert row["linked_events_count"] == 2
    assert row["last_mentioned_at"] is not None
    # Aliases are sorted; canonical_name is also seeded as an alias.
    assert set(row["aliases"]) == {"Scott", "黄逸扬"}


def test_patch_description_sets_owner_override_true():
    engine, client = _build()
    ent_id = _seed_entity(engine, canonical="温冉", description="Scott 的女朋友")

    with client:
        resp = client.patch(
            f"/api/admin/memory/entities/{ent_id}",
            json={"description": "Scott 的女朋友 · 在 SF 工作"},
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["description"] == "Scott 的女朋友 · 在 SF 工作"
    assert payload["owner_override"] is True

    with DbSession(engine) as db:
        row = db.get(Entity, ent_id)
    assert row is not None
    assert row.owner_override is True
    assert row.description == "Scott 的女朋友 · 在 SF 工作"


def test_post_create_manual_entity():
    engine, client = _build()
    with client:
        resp = client.post(
            "/api/admin/memory/entities",
            json={
                "canonical_name": "Mochi",
                "kind": "pet",
                "description": "用户 2020 年领养的黑猫",
                "aliases": ["小黑", "毛球"],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_name"] == "Mochi"
    assert body["kind"] == "pet"
    assert body["merge_status"] == "confirmed"
    # description supplied → owner_override flips True (slow_cycle
    # synthesizer skips this row).
    assert body["owner_override"] is True
    assert set(body["aliases"]) == {"Mochi", "小黑", "毛球"}

    with DbSession(engine) as db:
        rows = list(db.exec(EntityAlias.__table__.select())).copy()
    aliases = {r.alias for r in rows}
    assert {"Mochi", "小黑", "毛球"}.issubset(aliases)


def test_merge_arbitration_calls_clarification_with_same_true():
    engine, client = _build()
    # Seed Scott (uncertain merge candidate pointing at 黄逸扬).
    huang_id = _seed_entity(engine, canonical="黄逸扬", aliases=("Yiyang",))
    scott_id = _seed_entity(
        engine,
        canonical="Scott",
        merge_status="uncertain",
        merge_target_id=huang_id,
    )

    with client:
        resp = client.post(
            f"/api/admin/memory/entities/{scott_id}/merge",
            json={"target_id": huang_id},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "merged_into": huang_id}

    with DbSession(engine) as db:
        scott = db.get(Entity, scott_id)
        huang = db.get(Entity, huang_id)
    # apply_entity_clarification soft-deletes the loser (Scott was
    # uncertain) and promotes the winner to 'confirmed'.
    assert scott is not None and scott.deleted_at is not None
    assert huang is not None and huang.merge_status == "confirmed"
    assert huang.merge_target_id is None


def test_separate_arbitration_calls_clarification_with_same_false():
    engine, client = _build()
    huang_id = _seed_entity(engine, canonical="黄逸扬")
    # Pretend "Scott" was tentatively suggested as the same person
    # (uncertain). Owner says they're different.
    scott_id = _seed_entity(
        engine,
        canonical="Scott",
        merge_status="uncertain",
        merge_target_id=huang_id,
    )

    with client:
        resp = client.post(
            f"/api/admin/memory/entities/{scott_id}/confirm-separate",
            json={"other_id": huang_id},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    with DbSession(engine) as db:
        scott = db.get(Entity, scott_id)
        huang = db.get(Entity, huang_id)
    assert scott is not None
    # apply_entity_clarification(same=False) flips the uncertain side
    # to 'disambiguated'; the route then promotes any leftover
    # 'uncertain' rows to 'confirmed'. After this call:
    #   - Scott (formerly uncertain) → 'disambiguated' (clarification
    #     codepath wins; the post-call sweep only targets rows that
    #     are STILL 'uncertain').
    #   - 黄逸扬 was 'confirmed' to begin with → unchanged.
    assert scott.merge_status == "disambiguated"
    assert huang is not None
    assert huang.merge_status == "confirmed"


def test_thoughts_subject_filter_returns_only_persona():
    engine, client = _build()
    # Seed two persona-side thoughts, one user-side. Only the
    # persona-side ones should come back when the route filters on
    # ``subject=persona`` — and the response items must carry the new
    # serializer fields (subject / source / filling_event_ids).
    when = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        # First seed a backing event so filling_event_ids has something
        # plausible to point at.
        event = ConceptNode(
            persona_id="admin-test",
            user_id="self",
            type=NodeType.EVENT,
            description="event the persona will reflect on",
            created_at=when - timedelta(hours=1),
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_id = event.id

        persona_thought_a = ConceptNode(
            persona_id="admin-test",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="persona",
            description="我最近更愿意先听对方说完",
            created_at=when,
            # source_session_id intentionally None → 'slow_tick'
        )
        persona_thought_b = ConceptNode(
            persona_id="admin-test",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="persona",
            description="另一条 persona 反思",
            created_at=when - timedelta(minutes=10),
            source_session_id="some-session",  # → 'reflection'
        )
        user_thought = ConceptNode(
            persona_id="admin-test",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="user",
            description="对用户的看法 · 不该出现在 persona filter 里",
            created_at=when - timedelta(minutes=5),
        )
        db.add_all([persona_thought_a, persona_thought_b, user_thought])
        db.commit()
        db.refresh(persona_thought_a)
        db.refresh(persona_thought_b)
        # Filling chain on persona_thought_a so the serializer has
        # something to surface.
        db.add(
            ConceptNodeFilling(
                parent_id=persona_thought_a.id, child_id=event_id
            )
        )
        db.commit()

    with client:
        resp = client.get(
            "/api/admin/memory/thoughts", params={"subject": "persona"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "persona"
    descs = [item["description"] for item in body["items"]]
    assert "对用户的看法 · 不该出现在 persona filter 里" not in descs
    assert "我最近更愿意先听对方说完" in descs
    assert "另一条 persona 反思" in descs

    # Schema additions from the v0.5 hotfix serializer.
    by_desc = {it["description"]: it for it in body["items"]}
    a = by_desc["我最近更愿意先听对方说完"]
    assert a["subject"] == "persona"
    assert a["source"] == "slow_tick"  # source_session_id is None
    assert a["filling_event_ids"] == [event_id]

    b = by_desc["另一条 persona 反思"]
    assert b["subject"] == "persona"
    assert b["source"] == "reflection"  # source_session_id present
    assert b["filling_event_ids"] == []

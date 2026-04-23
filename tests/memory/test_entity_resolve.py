"""L5 ``resolve_entity`` three-level dedup tests (plan decision 4).

Covers the four resolution paths:

    Level 1 — alias exact match
    Level 2 — embedding cosine > 0.85 (auto-merge)
    Level 2 — 0.65 < cosine < 0.85 (uncertain, new row w/ merge_target_id)
    Level 3 — no candidate → new entity

Plus idempotency: running resolve_entity twice for the same input must
yield the same ``entities.id`` and must not leak duplicate aliases.
"""

from __future__ import annotations

import math

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.entities import (
    EMBEDDING_MERGE_THRESHOLD_HIGH,
    EMBEDDING_MERGE_THRESHOLD_LOW,
    resolve_entity,
)
from echovessel.memory.models import Entity, EntityAlias


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _make_unit_embed(vector_slot: int) -> list[float]:
    """Build a 384-dim unit-norm vector pointing at ``vector_slot``."""
    v = [0.0] * 384
    v[vector_slot % 384] = 1.0
    return v


def _cosine_target_embed(base_slot: int, cosine: float) -> list[float]:
    """Build a 384-dim unit-norm vector whose true cosine similarity with
    the unit vector at ``base_slot`` equals ``cosine``.

    Uses a two-component mix: cos*e_base + sin*e_other.
    Note: sqlite-vec returns L2 distance, which ``entities._distance_to_cosine``
    maps via ``1 - d/2``. For unit-norm vectors, that recovered score is
    NOT the true cosine — it is ``1 - sqrt(2 - 2c) / 2``. Use
    :func:`_recovered_sim_for` to convert when asserting thresholds.
    """
    v = [0.0] * 384
    sin = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    v[base_slot % 384] = cosine
    v[(base_slot + 10) % 384] = sin
    return v


def _recovered_sim_for(true_cosine: float) -> float:
    """Return what ``entities._distance_to_cosine`` will yield for two
    unit-norm vectors whose true cosine is ``true_cosine``."""
    d = math.sqrt(max(0.0, 2.0 - 2.0 * true_cosine))
    return max(0.0, min(1.0, 1.0 - d / 2.0))


def test_level1_alias_exact_match_returns_existing_entity():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    # Seed one entity with an alias.
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
        entity_id, path = resolve_entity(
            db,
            backend,
            lambda t: _make_unit_embed(0),
            persona_id="p",
            user_id="self",
            canonical_name="Scott",  # known alias
            aliases=["Yiyang"],
            kind="person",
        )

        assert path == "alias_match"
        assert entity_id == 1

        # The new alias should now be attached to the matched entity.
        aliases = {
            row.alias
            for row in db.exec(
                select(EntityAlias).where(EntityAlias.entity_id == 1)
            )
        }
        assert aliases == {"Scott", "黄逸扬", "Yiyang"}


def test_level2_high_cosine_auto_merges():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    base_embed = _make_unit_embed(5)
    # True cosine 0.97 → recovered sim ≈ 0.877, above the HIGH threshold.
    new_embed = _cosine_target_embed(5, cosine=0.97)
    assert _recovered_sim_for(0.97) > EMBEDDING_MERGE_THRESHOLD_HIGH

    # Seed the existing entity + its vector.
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
        db.commit()
    backend.insert_entity_vector(1, base_embed)

    def _embed(text: str) -> list[float]:
        return new_embed

    with DbSession(engine) as db:
        entity_id, path = resolve_entity(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            canonical_name="Scott",
            aliases=[],
            kind="person",
        )
        assert path == "embedding_high"
        assert entity_id == 1

        aliases = {
            row.alias
            for row in db.exec(
                select(EntityAlias).where(EntityAlias.entity_id == 1)
            )
        }
        assert "Scott" in aliases

        # No new entity row should have been created.
        rows = list(db.exec(select(Entity)))
        assert len(rows) == 1
        assert rows[0].merge_status == "confirmed"


def test_level2_mid_cosine_creates_uncertain_candidate():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    base_embed = _make_unit_embed(5)
    # True cosine 0.75 → recovered sim ≈ 0.647 — straddles the LOW bound
    # in (LOW, HIGH) once we account for the distance conversion.
    new_embed = _cosine_target_embed(5, cosine=0.85)
    sim = _recovered_sim_for(0.85)
    assert EMBEDDING_MERGE_THRESHOLD_LOW < sim < EMBEDDING_MERGE_THRESHOLD_HIGH

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
        db.commit()
    backend.insert_entity_vector(1, base_embed)

    def _embed(text: str) -> list[float]:
        return new_embed

    with DbSession(engine) as db:
        entity_id, path = resolve_entity(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            canonical_name="Scott",
            aliases=[],
            kind="person",
        )
        assert path == "embedding_low"
        assert entity_id != 1

        new_entity = db.exec(select(Entity).where(Entity.id == entity_id)).one()
        assert new_entity.merge_status == "uncertain"
        assert new_entity.merge_target_id == 1


def test_level3_far_cosine_creates_new_confirmed_entity():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    base_embed = _make_unit_embed(5)
    # True cosine 0.1 → recovered sim ≈ 0.329, well below LOW.
    new_embed = _cosine_target_embed(5, cosine=0.10)
    assert _recovered_sim_for(0.10) < EMBEDDING_MERGE_THRESHOLD_LOW

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
        db.commit()
    backend.insert_entity_vector(1, base_embed)

    with DbSession(engine) as db:
        entity_id, path = resolve_entity(
            db,
            backend,
            lambda t: new_embed,
            persona_id="p",
            user_id="self",
            canonical_name="UnrelatedPerson",
            aliases=[],
            kind="person",
        )
        assert path == "new"
        assert entity_id != 1

        new_entity = db.exec(select(Entity).where(Entity.id == entity_id)).one()
        assert new_entity.merge_status == "confirmed"
        assert new_entity.merge_target_id is None


def test_resolve_entity_is_idempotent():
    """Running the same resolve_entity call twice yields the same entity_id
    and does not create duplicate alias rows."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    def _embed(text: str) -> list[float]:
        return _make_unit_embed(7)

    with DbSession(engine) as db:
        id_1, path_1 = resolve_entity(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            canonical_name="黄逸扬",
            aliases=["Scott"],
            kind="person",
        )
        assert path_1 == "new"

        id_2, path_2 = resolve_entity(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            canonical_name="黄逸扬",
            aliases=["Scott"],
            kind="person",
        )
        # Alias match on either "黄逸扬" or "Scott" returns the same row.
        assert path_2 == "alias_match"
        assert id_2 == id_1

        # Exactly one Entity row, exactly two alias rows.
        entities = list(db.exec(select(Entity)))
        aliases = list(db.exec(select(EntityAlias)))
        assert len(entities) == 1
        assert {a.alias for a in aliases} == {"黄逸扬", "Scott"}


def test_thresholds_are_reasonable_bounds():
    """Sanity check the constants — dogfood can dial them but not invert."""
    assert 0 < EMBEDDING_MERGE_THRESHOLD_LOW < EMBEDDING_MERGE_THRESHOLD_HIGH < 1.0

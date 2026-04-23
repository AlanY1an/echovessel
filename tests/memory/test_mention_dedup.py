"""Mention dedup (plan §6.2 step 3) — reject near-duplicate L3 events.

When extraction produces an event whose vector matches an existing L3
node above ``MENTION_DEDUP_COSINE_THRESHOLD`` within
``MENTION_DEDUP_WINDOW_DAYS``, consolidate bumps ``mention_count`` +
appends ``source_turn_ids`` rather than inserting a duplicate node.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.entities import detect_mention_dedup
from echovessel.memory.models import ConceptNode


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="x"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _unit_vec(slot: int) -> list[float]:
    v = [0.0] * 384
    v[slot % 384] = 1.0
    return v


def _cosine_vec(base_slot: int, cosine: float) -> list[float]:
    v = [0.0] * 384
    v[base_slot % 384] = cosine
    v[(base_slot + 10) % 384] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return v


def test_close_existing_event_matches_and_is_returned():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    now = datetime(2026, 4, 23, 12, 0, 0)
    existing_desc = "user mentioned Mochi getting sick"
    # Seed an existing event + its vector with a recent created_at.
    with DbSession(engine) as db:
        ev = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description=existing_desc,
            emotional_impact=2,
            created_at=now - timedelta(days=5),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        existing_id = ev.id
    backend.insert_vector(existing_id, _unit_vec(11))

    # Build an embedder that returns a cosine~0.98 match (recovered sim > 0.85).
    def _embed(text: str) -> list[float]:
        return _cosine_vec(11, cosine=0.99)

    with DbSession(engine) as db:
        matches = detect_mention_dedup(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            new_event_descriptions=["user again talks about Mochi being sick"],
            now=now,
        )
    assert matches == {0: existing_id}


def test_old_match_outside_window_does_not_dedup():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    now = datetime(2026, 4, 23, 12, 0, 0)
    # Same shape as above but created_at is 60d ago → outside 30d window.
    with DbSession(engine) as db:
        ev = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="stale event",
            emotional_impact=1,
            created_at=now - timedelta(days=60),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        existing_id = ev.id
    backend.insert_vector(existing_id, _unit_vec(7))

    def _embed(text: str) -> list[float]:
        return _cosine_vec(7, cosine=0.99)

    with DbSession(engine) as db:
        matches = detect_mention_dedup(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            new_event_descriptions=["same topic resurfaced months later"],
            now=now,
        )
    assert matches == {}


def test_low_cosine_does_not_dedup():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)

    now = datetime(2026, 4, 23, 12, 0, 0)
    with DbSession(engine) as db:
        ev = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EVENT,
            description="user had a fun hike",
            emotional_impact=2,
            created_at=now - timedelta(days=2),
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        existing_id = ev.id
    backend.insert_vector(existing_id, _unit_vec(33))

    # Completely unrelated direction — recovered sim well below 0.85.
    def _embed(text: str) -> list[float]:
        return _cosine_vec(33, cosine=0.20)

    with DbSession(engine) as db:
        matches = detect_mention_dedup(
            db,
            backend,
            _embed,
            persona_id="p",
            user_id="self",
            new_event_descriptions=["user cooked a new recipe"],
            now=now,
        )
    assert matches == {}

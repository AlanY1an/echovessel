"""``memory.retrieve.load_persona_thoughts_force`` coverage (v0.5).

The helper powers the ``# How you see yourself lately`` user-prompt
section that replaced the deleted L1.self block. Three cases:

1. Happy path: returns top-N subject='persona' thoughts by recency.
2. ``exclude_ids`` honors the rerank dedup — caller-supplied ids are
   dropped from the result.
3. ``subject='user'`` thoughts are NOT picked up (this helper is
   persona-only; the sibling ``_load_user_thoughts_force`` is the
   user-side one).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    ConceptNode,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.retrieve import load_persona_thoughts_force


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="Luna"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def _add_thought(
    db: DbSession,
    *,
    description: str,
    subject: str,
    created_at: datetime,
) -> int:
    node = ConceptNode(
        persona_id="p",
        user_id="self",
        type=NodeType.THOUGHT,
        subject=subject,
        description=description,
        emotional_impact=0,
        created_at=created_at,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node.id


def test_returns_top_n_by_recency():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    base = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        _seed(db)
        ids = [
            _add_thought(
                db,
                description=f"persona reflection {i}",
                subject="persona",
                created_at=base - timedelta(days=10 - i),
            )
            for i in range(6)
        ]
        rows = load_persona_thoughts_force(
            db, persona_id="p", user_id="self", top_n=3
        )
    # Most recent 3 are the last three inserted (highest i).
    assert [n.id for n in rows] == [ids[5], ids[4], ids[3]]


def test_exclude_ids_removes_already_returned():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    base = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        _seed(db)
        ids = [
            _add_thought(
                db,
                description=f"persona note {i}",
                subject="persona",
                created_at=base - timedelta(days=5 - i),
            )
            for i in range(5)
        ]
        # Pretend the rerank already surfaced the two most recent.
        rows = load_persona_thoughts_force(
            db,
            persona_id="p",
            user_id="self",
            top_n=3,
            exclude_ids={ids[4], ids[3]},
        )
    assert [n.id for n in rows] == [ids[2], ids[1], ids[0]]


def test_user_subject_thoughts_are_not_returned():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    base = datetime(2026, 4, 24, 12, 0, 0)
    with DbSession(engine) as db:
        _seed(db)
        user_id_ = _add_thought(
            db,
            description="about the user · should NOT be returned",
            subject="user",
            created_at=base,
        )
        persona_id_ = _add_thought(
            db,
            description="the persona's own reflection · should be returned",
            subject="persona",
            created_at=base - timedelta(hours=1),
        )
        rows = load_persona_thoughts_force(
            db, persona_id="p", user_id="self", top_n=10
        )
    returned = {n.id for n in rows}
    assert user_id_ not in returned
    assert persona_id_ in returned

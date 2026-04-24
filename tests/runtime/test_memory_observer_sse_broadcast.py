"""Spec 3 · RuntimeMemoryObserver SSE broadcasts for memory writes.

Covers the 5 new hooks wired up in spec-3:

- ``on_event_created``                      → ``memory.event.created``
- ``on_thought_created``                    → ``memory.thought.created`` (source tag preserved)
- ``on_entity_confirmed``                   → ``memory.entity.confirmed``
- ``on_entity_confirmed`` (uncertain)       → skipped (plan §3.1 — admin-only)
- ``on_entity_description_updated``         → ``memory.entity.description_updated``

Also asserts backward compatibility of the prior-round ``on_mood_updated``
broadcast so the Web frontend's existing subscription keeps working.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory.db import create_all_tables, create_engine
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    Entity,
    Persona,
    User,
)
from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.memory_observers import RuntimeMemoryObserver


class _FakeChannel:
    def __init__(self, channel_id: str = "web") -> None:
        self.channel_id = channel_id
        self.calls: list[tuple[str, dict]] = []

    async def push_sse(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


async def _drain_loop() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def _seed_engine():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    with DbSession(engine) as db:
        db.add(Persona(id="p1", display_name="P"))
        db.add(User(id="self", display_name="Self"))
        db.commit()
    return engine


def _make_observer(engine=None) -> tuple[RuntimeMemoryObserver, _FakeChannel]:
    registry = ChannelRegistry()
    channel = _FakeChannel()
    registry.register(channel)
    loop = asyncio.get_event_loop()
    observer = RuntimeMemoryObserver(registry=registry, loop=loop, engine=engine)
    return observer, channel


async def test_on_event_created_broadcasts() -> None:
    observer, channel = _make_observer()
    event = ConceptNode(
        id=42,
        persona_id="p1",
        user_id="self",
        type=NodeType.EVENT,
        description="用户说下周要去温哥华",
        emotional_impact=3,
        source_session_id="sess-1",
        created_at=datetime(2026, 4, 24, 10, 0, 0),
    )

    observer.on_event_created(event)
    await _drain_loop()

    assert len(channel.calls) == 1
    topic, payload = channel.calls[0]
    assert topic == "memory.event.created"
    assert payload["event_id"] == 42
    assert payload["description"] == "用户说下周要去温哥华"
    assert payload["emotional_impact"] == 3
    assert payload["session_id"] == "sess-1"
    assert payload["persona_id"] == "p1"
    assert payload["user_id"] == "self"
    assert payload["created_at"].startswith("2026-04-24")


async def test_on_thought_created_broadcasts_with_source_tag() -> None:
    engine = _seed_engine()
    observer, channel = _make_observer(engine=engine)

    # Seed an event + thought + filling so the observer can populate
    # filling_event_ids from the DB.
    with DbSession(engine) as db:
        src = ConceptNode(
            persona_id="p1",
            user_id="self",
            type=NodeType.EVENT,
            description="用户在紧张",
        )
        db.add(src)
        db.commit()
        db.refresh(src)
        th = ConceptNode(
            persona_id="p1",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="persona",
            description="我注意到用户最近很紧张",
            source_session_id="sess-2",
        )
        db.add(th)
        db.commit()
        db.refresh(th)
        db.add(ConceptNodeFilling(parent_id=th.id, child_id=src.id))
        db.commit()
        thought_id = th.id
        event_id = src.id

    # Reload the row so the relationships are populated.
    with DbSession(engine) as db:
        th_loaded = db.get(ConceptNode, thought_id)
        assert th_loaded is not None

        observer.on_thought_created(th_loaded, "slow_tick")

    await _drain_loop()

    assert len(channel.calls) == 1
    topic, payload = channel.calls[0]
    assert topic == "memory.thought.created"
    assert payload["thought_id"] == thought_id
    assert payload["source"] == "slow_tick"
    assert payload["type"] == "thought"
    assert payload["subject"] == "persona"
    assert payload["session_id"] == "sess-2"
    assert payload["filling_event_ids"] == [event_id]


async def test_on_thought_created_reflection_source_preserved() -> None:
    observer, channel = _make_observer()
    th = ConceptNode(
        id=7,
        persona_id="p1",
        user_id="self",
        type=NodeType.THOUGHT,
        description="reflection output",
        source_session_id=None,
    )

    observer.on_thought_created(th, "reflection")
    await _drain_loop()

    assert channel.calls[0][1]["source"] == "reflection"


async def test_on_entity_confirmed_broadcasts() -> None:
    observer, channel = _make_observer()
    entity = Entity(
        id=11,
        persona_id="p1",
        user_id="self",
        canonical_name="温冉",
        kind="person",
        description=None,
        merge_status="confirmed",
        created_at=datetime(2026, 4, 24, 12, 0, 0),
    )

    observer.on_entity_confirmed(entity)
    await _drain_loop()

    assert len(channel.calls) == 1
    topic, payload = channel.calls[0]
    assert topic == "memory.entity.confirmed"
    assert payload == {
        "entity_id": 11,
        "persona_id": "p1",
        "user_id": "self",
        "canonical_name": "温冉",
        "kind": "person",
        "merge_status": "confirmed",
        "created_at": "2026-04-24T12:00:00",
    }


async def test_on_entity_confirmed_skips_uncertain() -> None:
    observer, channel = _make_observer()
    entity = Entity(
        id=12,
        persona_id="p1",
        user_id="self",
        canonical_name="Scott",
        kind="person",
        merge_status="uncertain",
        merge_target_id=5,
    )

    observer.on_entity_confirmed(entity)
    await _drain_loop()

    # Uncertain entity = admin-only; Timeline must never learn about it.
    assert channel.calls == []


async def test_on_entity_description_updated_broadcasts() -> None:
    observer, channel = _make_observer()
    entity = Entity(
        id=21,
        persona_id="p1",
        user_id="self",
        canonical_name="温冉",
        kind="person",
        description="温冉是用户的女朋友,数学系 PhD 在读。",
        merge_status="confirmed",
        updated_at=datetime(2026, 4, 24, 13, 0, 0),
    )

    observer.on_entity_description_updated(entity, "slow_tick")
    await _drain_loop()

    assert len(channel.calls) == 1
    topic, payload = channel.calls[0]
    assert topic == "memory.entity.description_updated"
    assert payload["entity_id"] == 21
    assert payload["canonical_name"] == "温冉"
    assert payload["description"].startswith("温冉是")
    assert payload["source"] == "slow_tick"
    assert payload["updated_at"].startswith("2026-04-24")


async def test_on_entity_description_owner_source_preserved() -> None:
    observer, channel = _make_observer()
    entity = Entity(
        id=22,
        persona_id="p1",
        user_id="self",
        canonical_name="Mochi",
        kind="pet",
        description="owner override",
        merge_status="confirmed",
    )

    observer.on_entity_description_updated(entity, "owner")
    await _drain_loop()

    assert channel.calls[0][1]["source"] == "owner"


async def test_on_mood_updated_backward_compat() -> None:
    """Verify the existing `chat.mood.update` topic still fires unchanged —
    the frontend's current subscriber shouldn't regress with the new hooks
    bolted on."""
    observer, channel = _make_observer()

    observer.on_mood_updated(
        persona_id="p1", user_id="self", new_mood_text="愿意慢慢听"
    )
    await _drain_loop()

    assert channel.calls == [
        (
            "chat.mood.update",
            {"persona_id": "p1", "user_id": "self", "mood_summary": "愿意慢慢听"},
        )
    ]


async def test_observer_tolerates_missing_engine() -> None:
    """Observer must not crash when engine is None — filling_event_ids
    degrades to [] and session counts to None, but broadcast still fires."""
    observer, channel = _make_observer(engine=None)
    th = ConceptNode(
        id=99,
        persona_id="p1",
        user_id="self",
        type=NodeType.THOUGHT,
        description="no engine path",
    )
    observer.on_thought_created(th, "reflection")
    await _drain_loop()

    assert len(channel.calls) == 1
    assert channel.calls[0][1]["filling_event_ids"] == []

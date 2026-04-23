"""Observer dispatch on memory writes.

Contract (round3 tracker §2.2 + review M2/M3):

- `ingest_message` fires `on_message_ingested` after commit
- `import_content(user_events)` / `bulk_create_events` fires
  `on_event_created` for each new event
- `import_content(user_reflections)` / `bulk_create_thoughts` fires
  `on_thought_created`
- `import_content(persona_traits)` / `append_to_core_block` fires
  `on_core_block_appended`
- Exceptions raised from observer hooks MUST NOT roll back the memory
  write — the row stays committed, the error is logged, the caller
  sees a normal successful return
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole
from echovessel.memory import (
    ConceptNode,
    CoreBlockAppend,
    EventInput,
    NullObserver,
    Persona,
    RecallMessage,
    ThoughtInput,
    User,
    bulk_create_events,
    bulk_create_thoughts,
    create_all_tables,
    create_engine,
    import_content,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import Session as SessionRow
from echovessel.memory.sessions import mark_session_closing


class SpyObserver:
    """Records every observer callback so tests can assert on counts
    and identity of the objects."""

    def __init__(self) -> None:
        self.messages: list[RecallMessage] = []
        self.events: list[ConceptNode] = []
        self.thoughts: list[ConceptNode] = []
        self.core_block_appends: list[CoreBlockAppend] = []

    def on_message_ingested(self, msg: RecallMessage) -> None:
        self.messages.append(msg)

    def on_event_created(self, event: ConceptNode) -> None:
        self.events.append(event)

    def on_thought_created(self, thought: ConceptNode) -> None:
        self.thoughts.append(thought)

    def on_core_block_appended(self, append: CoreBlockAppend) -> None:
        self.core_block_appends.append(append)


class ExplodingObserver:
    """Every hook raises. The memory writes must still succeed."""

    def on_message_ingested(self, msg: RecallMessage) -> None:
        raise RuntimeError("boom-ingest")

    def on_event_created(self, event: ConceptNode) -> None:
        raise RuntimeError("boom-event")

    def on_thought_created(self, thought: ConceptNode) -> None:
        raise RuntimeError("boom-thought")

    def on_core_block_appended(self, append: CoreBlockAppend) -> None:
        raise RuntimeError("boom-append")


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


def test_null_observer_is_noop():
    """NullObserver is the default fallback — it should satisfy the
    Protocol structurally and do nothing."""
    observer = NullObserver()
    # Structural check: each method exists and is callable
    assert callable(observer.on_message_ingested)
    assert callable(observer.on_event_created)
    assert callable(observer.on_thought_created)
    assert callable(observer.on_core_block_appended)


def test_spy_observer_on_ingest():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = SpyObserver()
    with DbSession(engine) as db:
        _seed(db)
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "hello",
            observer=observer,
        )
        ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.PERSONA,
            "hi back",
            observer=observer,
        )

        # Assert inside the DbSession so ORM attribute access still works
        assert len(observer.messages) == 2
        assert observer.messages[0].content == "hello"
        assert observer.messages[1].content == "hi back"


def test_spy_observer_on_bulk_events_and_thoughts():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = SpyObserver()
    with DbSession(engine) as db:
        _seed(db)
        bulk_create_events(
            db,
            events=[
                EventInput(
                    persona_id="p_test",
                    user_id="self",
                    description="a",
                    imported_from="hash-e",
                ),
                EventInput(
                    persona_id="p_test",
                    user_id="self",
                    description="b",
                    imported_from="hash-e",
                ),
            ],
            observer=observer,
        )
        bulk_create_thoughts(
            db,
            thoughts=[
                ThoughtInput(
                    persona_id="p_test",
                    user_id="self",
                    description="reflection",
                    imported_from="hash-t",
                ),
            ],
            observer=observer,
        )

        assert len(observer.events) == 2
        assert len(observer.thoughts) == 1
        assert observer.events[0].description == "a"
        assert observer.thoughts[0].description == "reflection"


def test_spy_observer_on_core_block_append():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = SpyObserver()
    with DbSession(engine) as db:
        _seed(db)
        import_content(
            db,
            source="h",
            content_type="persona_traits",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "她温柔",
            },
            observer=observer,
        )

    assert len(observer.core_block_appends) == 1
    assert observer.core_block_appends[0].label == "persona"


@pytest.mark.asyncio
async def test_spy_observer_on_consolidate_event():
    """consolidate_session fires on_event_created for each event it
    extracts. Review R2: still per-session, so 1 session → 1 extract_fn
    call → N observer callbacks."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    observer = SpyObserver()

    async def extract_fn(_messages):
        return ExtractionResult(
            events=[
            ExtractedEvent(description="ev1", emotional_impact=2),
            ExtractedEvent(description="ev2", emotional_impact=3),
        ],
        )

    async def reflect_fn(_nodes, _reason):
        return []

    def zero_embed(_t):
        return [0.0] * 384

    with DbSession(engine) as db:
        _seed(db)
        base = datetime(2026, 4, 16, 10, 0, 0)
        for i in range(3):
            ingest_message(
                db,
                "p_test",
                "self",
                "web",
                MessageRole.USER,
                f"msg {i}",
                now=base + timedelta(seconds=i),
            )
        session = db.exec(select(SessionRow)).one()
        mark_session_closing(
            db, session, trigger="idle", now=base + timedelta(minutes=35)
        )
        db.commit()
        db.refresh(session)

        await consolidate_session(
            db,
            backend,
            session,
            extract_fn,
            reflect_fn,
            zero_embed,
            observer=observer,
        )

        assert len(observer.events) == 2
        assert observer.events[0].description == "ev1"


def test_exploding_observer_does_not_block_ingest():
    """Review M2/M3: observer exceptions MUST be swallowed. The commit
    succeeds, the row is queryable afterwards."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = ExplodingObserver()
    with DbSession(engine) as db:
        _seed(db)
        result = ingest_message(
            db,
            "p_test",
            "self",
            "web",
            MessageRole.USER,
            "survives",
            observer=observer,
        )
        # No exception bubbled up
        assert result.message.id is not None

        # And the row is really there
        rows = list(db.exec(select(RecallMessage)))
        assert len(rows) == 1
        assert rows[0].content == "survives"


def test_exploding_observer_does_not_block_bulk_events():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = ExplodingObserver()
    with DbSession(engine) as db:
        _seed(db)
        ids = bulk_create_events(
            db,
            events=[
                EventInput(
                    persona_id="p_test",
                    user_id="self",
                    description="boom-tolerant",
                    imported_from="hash-z",
                )
            ],
            observer=observer,
        )
        assert len(ids) == 1
        assert (
            db.exec(select(ConceptNode)).one().description == "boom-tolerant"
        )


def test_exploding_observer_does_not_block_core_block_append():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    observer = ExplodingObserver()
    with DbSession(engine) as db:
        _seed(db)
        result = import_content(
            db,
            source="hash-core",
            content_type="persona_traits",
            payload={
                "persona_id": "p_test",
                "user_id": "self",
                "content": "durable trait",
            },
            observer=observer,
        )
        assert len(result.core_block_append_ids) == 1
        appends = list(db.exec(select(CoreBlockAppend)))
        assert len(appends) == 1

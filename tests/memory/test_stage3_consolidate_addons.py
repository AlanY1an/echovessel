"""Stage 3 add-ons · consolidate edge cases the existing suite skipped.

Four tests covering the failure / recovery paths:

- **3.6** ``make_extract_fn`` returns an empty list when the LLM emits
  malformed JSON, instead of letting the parser exception surface and
  marking the entire session FAILED. Sessions with garbage extractor
  output close cleanly with zero events.
- **3.7** Bad ``relational_tags`` are filtered at the parser layer when
  the data flows through ``make_extract_fn`` end-to-end (regression
  guard around the prompts → memory adapter).
- **3.10** When ``backend.insert_vector`` raises mid-event, the
  surrounding consolidate transaction must roll back: zero events
  land in ``concept_nodes``, and the resume flag is NOT set.
- **3.11** A session previously marked FAILED can be retried by
  flipping its status back to CLOSING + resetting ``extracted=False``;
  the worker picks it up via ``initial_session_ids`` and consolidates
  it normally.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import func
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import MessageRole, NodeType, SessionStatus
from echovessel.memory import (
    Persona,
    RecallMessage,
    Session,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.models import ConceptNode
from echovessel.runtime.llm import StubProvider
from echovessel.runtime.loops.consolidate_worker import ConsolidateWorker
from echovessel.runtime.prompts_wiring import make_extract_fn

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="P"))
    db.add(User(id="u", display_name="U"))
    db.commit()


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _make_session(db: DbSession, *, status: SessionStatus = SessionStatus.CLOSING) -> Session:
    sess = Session(
        id="s_test",
        persona_id="p",
        user_id="u",
        channel_id="web",
        status=status,
        message_count=4,
        total_tokens=400,
    )
    db.add(sess)
    db.commit()
    return sess


def _add_messages(db: DbSession, sid: str, n: int) -> None:
    for i in range(n):
        db.add(
            RecallMessage(
                session_id=sid,
                persona_id="p",
                user_id="u",
                channel_id="web",
                role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                content=f"a real message {i} with enough words",
                day=date.today(),
                token_count=80,
            )
        )
    db.commit()


def _make_msg() -> RecallMessage:
    return RecallMessage(
        session_id="s_test",
        persona_id="p",
        user_id="u",
        channel_id="web",
        role=MessageRole.USER,
        content="anything",
        day=date.today(),
        token_count=10,
    )


# ---------------------------------------------------------------------------
# 3.6 · malformed LLM JSON → make_extract_fn returns []
# ---------------------------------------------------------------------------


async def test_make_extract_fn_returns_empty_list_on_malformed_llm_output() -> None:
    """The wrapper around ``parse_extraction_response`` must catch
    ``ExtractionParseError`` and degrade to ``[]``, so a single bad LLM
    response does not propagate up and mark the session FAILED.
    """

    llm = StubProvider(fallback="this is definitely not JSON")
    extract_fn = make_extract_fn(llm)

    result = await extract_fn([_make_msg()])

    assert result.events == []


async def test_make_extract_fn_returns_empty_on_top_level_array() -> None:
    """Wrong top-level shape (array instead of object) is also a parse
    failure that must degrade silently."""

    llm = StubProvider(fallback=json.dumps([{"description": "x"}]))
    extract_fn = make_extract_fn(llm)

    result = await extract_fn([_make_msg()])
    assert result.events == []


# ---------------------------------------------------------------------------
# 3.7 · invalid relational_tag dropped at parse layer (round-trip)
# ---------------------------------------------------------------------------


async def test_make_extract_fn_drops_invalid_relational_tags_in_round_trip() -> None:
    """End-to-end: a well-formed LLM response with one valid + one
    invalid relational_tag must reach ``make_extract_fn`` callers as a
    single ``ExtractedEvent`` whose ``relational_tags`` contains only
    the valid one. Bad tags do not block the event."""

    payload = {
        "self_check_notes": "ok",
        "events": [
            {
                "description": "user disclosed something",
                "emotional_impact": -3,
                "emotion_tags": ["fatigue"],
                "relational_tags": ["identity-bearing", "made-up-tag"],
            }
        ],
    }
    llm = StubProvider(fallback=json.dumps(payload))
    extract_fn = make_extract_fn(llm)

    result = await extract_fn([_make_msg()])

    assert len(result.events) == 1
    assert result.events[0].relational_tags == ["identity-bearing"]
    assert result.events[0].emotional_impact == -3


# ---------------------------------------------------------------------------
# 3.10 · vector insert mid-event raises → transaction rolls back
# ---------------------------------------------------------------------------


class _ExplodingBackend(SQLiteBackend):
    """SQLiteBackend that raises on the second ``insert_vector`` call.

    Used to prove that a partial commit during the events-write loop
    leaves the DB clean — no half-state with one event in
    ``concept_nodes`` and the resume flag set on the session.
    """

    def __init__(self, engine, fail_after: int = 1) -> None:
        super().__init__(engine)
        self._fail_after = fail_after
        self.calls = 0

    def insert_vector(self, concept_node_id: int, embedding) -> None:  # type: ignore[override]
        self.calls += 1
        if self.calls > self._fail_after:
            raise RuntimeError("simulated vector store outage")
        super().insert_vector(concept_node_id, embedding)


@pytest.mark.xfail(
    reason=(
        "consolidate is not atomic across the events loop · backend."
        "insert_vector opens its own engine.begin() connection which "
        "auto-commits, so a mid-loop failure can leave one concept_nodes "
        "row plus one concept_nodes_vec row visible while the session "
        "still has extracted_events=False. A retry would then duplicate "
        "that event. Fix is a separate PR — design choice between "
        "(a) wrapping vector writes in the SQLAlchemy transaction or "
        "(b) writing all events in one commit, then vectors after."
    ),
    strict=True,
)
async def test_consolidate_atomic_when_vector_insert_raises_mid_event() -> None:
    """Two extracted events; ``insert_vector`` raises on the second.
    The whole session-level commit must NOT happen, and a fresh DB
    inspection must show zero events plus the session still in
    CLOSING with ``extracted_events=False``."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    exploder = _ExplodingBackend(engine, fail_after=1)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        async def _extract(_msgs):
            return ExtractionResult(
                events=[
                ExtractedEvent(description="event A", emotional_impact=-2),
                ExtractedEvent(description="event B", emotional_impact=2),
            ],
            )

        async def _reflect(_nodes, _reason):
            return []

        try:
            await consolidate_session(
                db=db,
                backend=exploder,
                session=sess,
                extract_fn=_extract,
                reflect_fn=_reflect,
                embed_fn=_embed,
            )
        except RuntimeError as e:
            assert "simulated vector store outage" in str(e)
        else:
            raise AssertionError("expected the explosion to propagate")

    with DbSession(engine) as db:
        event_count = db.exec(
            select(func.count())
            .select_from(ConceptNode)
            .where(ConceptNode.type == NodeType.EVENT.value)
        ).one()
        sess_after = db.get(Session, "s_test")

    # The transaction never committed → no events and the resume flag
    # is still False. Worker would retry the whole extract on next pass.
    assert event_count == 0
    assert sess_after is not None
    assert sess_after.extracted_events is False
    assert sess_after.extracted is False
    assert sess_after.status == SessionStatus.CLOSING


# ---------------------------------------------------------------------------
# 3.11 · failed session can be manually retried
# ---------------------------------------------------------------------------


async def test_failed_session_can_be_retried_after_status_flip() -> None:
    """Operator unwedge path: a session left in FAILED state can be
    nudged back to CLOSING (reset ``extracted`` / ``extracted_events``)
    and re-run by feeding its id to a fresh worker via
    ``initial_session_ids``. This is the workflow that recovers the
    real failed session from production after the WAL fix lands."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        # Session that previously failed.
        db.add(
            Session(
                id="s_unwedge",
                persona_id="p",
                user_id="u",
                channel_id="web",
                status=SessionStatus.FAILED,
                message_count=4,
                total_tokens=400,
                close_trigger="catchup|failed:database is locked",
                extracted=False,
                extracted_events=False,
            )
        )
        for i in range(4):
            db.add(
                RecallMessage(
                    session_id="s_unwedge",
                    persona_id="p",
                    user_id="u",
                    channel_id="web",
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.PERSONA,
                    content=f"unwedge-{i}",
                    day=date.today(),
                    token_count=80,
                )
            )
        db.commit()

    # Operator flips it back to CLOSING.
    with DbSession(engine) as db:
        sess = db.get(Session, "s_unwedge")
        assert sess is not None
        sess.status = SessionStatus.CLOSING
        sess.close_trigger = (sess.close_trigger or "") + "|retry"
        db.add(sess)
        db.commit()

    async def _extract(_msgs):
        return ExtractionResult(events=[ExtractedEvent(description="unwedged event", emotional_impact=2)])

    async def _reflect(_nodes, _reason):
        return []

    worker = ConsolidateWorker(
        db_factory=lambda: DbSession(engine),
        backend=backend,
        extract_fn=_extract,
        reflect_fn=_reflect,
        embed_fn=_embed,
        initial_session_ids=("s_unwedge",),
    )
    processed = await worker.drain_once()

    assert processed >= 1
    with DbSession(engine) as db:
        sess_done = db.get(Session, "s_unwedge")
        events = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_unwedge"
                )
            )
        )
    assert sess_done is not None
    assert sess_done.status == SessionStatus.CLOSED
    assert sess_done.extracted is True
    assert len(events) == 1
    assert events[0].description == "unwedged event"
    # Audit trail of why it was retried survives.
    assert "retry" in (sess_done.close_trigger or "")

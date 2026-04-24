"""Stage 4 · Reflection (L3 → L4) behaviour.

The reflect path was never directly covered by a unit test before this
file. Six scenarios:

- **4.1 SHOCK trigger** — extracting an event with ``|impact|>=8`` runs
  ``reflect_fn`` for that session.
- **4.2 TIMER trigger** — when no thought exists in the last 24h, the
  next extraction runs ``reflect_fn`` even with no SHOCK.
- **4.3 24h hard gate** — once 3 thoughts exist in the last 24h, even a
  SHOCK does not trigger another reflection.
- **4.4 filling chain** — every thought lands with
  ``concept_node_filling`` rows pointing at the events the reflector
  cited; the rows survive across the commit boundary.
- **4.5 orphaned filling** — soft-deleting a source event does NOT
  delete the thought; it flips the matching ``filling.orphaned`` flag
  so audit / forgetting-rights flows still see the link.
- **4.6 reflect crash leaves events replayable** — when ``reflect_fn``
  raises, the events written before the reflect step survive and a
  retry of the same session does NOT duplicate them (the
  ``extracted_events`` resume flag is honoured on the second pass).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
    ExtractedThought,
    ExtractionResult,
    consolidate_session,
)
from echovessel.memory.forget import DeletionChoice, delete_concept_node
from echovessel.memory.models import ConceptNode, ConceptNodeFilling

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(db: DbSession) -> None:
    db.add(Persona(id="p", display_name="P"))
    db.add(User(id="u", display_name="U"))
    db.commit()


def _embed(text: str) -> list[float]:
    v = [0.0] * 384
    v[hash(text) % 384] = 1.0
    return v


def _make_session(db: DbSession) -> Session:
    sess = Session(
        id="s_test",
        persona_id="p",
        user_id="u",
        channel_id="web",
        status=SessionStatus.CLOSING,
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
                content=f"msg-{i} but with enough words to count",
                day=date.today(),
                token_count=80,
            )
        )
    db.commit()


def _seed_existing_thought(
    db: DbSession, *, created_at: datetime, impact: int = 3
) -> ConceptNode:
    th = ConceptNode(
        persona_id="p",
        user_id="u",
        type=NodeType.THOUGHT,
        description=f"prior thought @ {created_at.isoformat()}",
        emotional_impact=impact,
        created_at=created_at,
    )
    db.add(th)
    db.commit()
    db.refresh(th)
    return th


def _make_extract(events: list[ExtractedEvent]):
    async def _fn(_msgs):
        return ExtractionResult(events=list(events))

    return _fn


def _make_reflect(thoughts: list[ExtractedThought], *, calls: list | None = None):
    async def _fn(nodes, reason):
        if calls is not None:
            calls.append((reason, [n.id for n in nodes]))
        out: list[ExtractedThought] = []
        for t in thoughts:
            out.append(
                ExtractedThought(
                    description=t.description,
                    emotional_impact=t.emotional_impact,
                    emotion_tags=list(t.emotion_tags),
                    relational_tags=list(t.relational_tags),
                    filling=t.filling or [n.id for n in nodes],
                )
            )
        return out

    return _fn


# ---------------------------------------------------------------------------
# 4.1 SHOCK trigger
# ---------------------------------------------------------------------------


async def test_shock_event_triggers_reflection() -> None:
    """A single extracted event with |impact|>=8 makes consolidate call
    reflect_fn for that session."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        reflect_calls: list = []
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [
                    ExtractedEvent(
                        description="user disclosed a major loss",
                        emotional_impact=-9,
                    )
                ]
            ),
            reflect_fn=_make_reflect(
                [
                    ExtractedThought(
                        description="user is grieving a death",
                        emotional_impact=-7,
                    )
                ],
                calls=reflect_calls,
            ),
            embed_fn=_embed,
        )

    assert result.reflection_reason == "shock"
    assert len(result.thoughts_created) == 1
    assert len(reflect_calls) == 1
    assert reflect_calls[0][0] == "shock"


# ---------------------------------------------------------------------------
# 4.2 TIMER trigger
# ---------------------------------------------------------------------------


async def test_timer_trigger_fires_when_no_recent_thought() -> None:
    """No prior thought in the last 24h → consolidate runs reflect_fn
    even though the extracted events are mild (no SHOCK)."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    now = datetime(2026, 4, 18, 12, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        # Old thought 30h ago — past the TIMER cutoff.
        _seed_existing_thought(db, created_at=now - timedelta(hours=30))
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        reflect_calls: list = []
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [ExtractedEvent(description="mild moment", emotional_impact=2)]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="weekly summary", emotional_impact=0)],
                calls=reflect_calls,
            ),
            embed_fn=_embed,
            now=now,
        )

    assert result.reflection_reason == "timer"
    assert len(reflect_calls) == 1
    assert reflect_calls[0][0] == "timer"


async def test_timer_trigger_skipped_when_recent_thought_exists() -> None:
    """A thought within the last 24h suppresses TIMER for this session.
    Since no SHOCK is present, consolidate must NOT call reflect."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    now = datetime(2026, 4, 18, 12, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        # Thought from 6h ago — well inside the 24h window.
        _seed_existing_thought(db, created_at=now - timedelta(hours=6))
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        reflect_calls: list = []
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [ExtractedEvent(description="mild moment", emotional_impact=2)]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="should not run", emotional_impact=0)],
                calls=reflect_calls,
            ),
            embed_fn=_embed,
            now=now,
        )

    assert result.reflection_reason is None
    assert reflect_calls == []
    assert result.thoughts_created == []


# ---------------------------------------------------------------------------
# 4.3 24h hard gate
# ---------------------------------------------------------------------------


async def test_hard_gate_blocks_reflection_after_three_thoughts_in_24h() -> None:
    """Even a SHOCK event must not trigger reflection if three thoughts
    already landed in the last 24h. The session still reaches CLOSED;
    only the thought write is suppressed."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    now = datetime(2026, 4, 18, 12, 0, 0)

    with DbSession(engine) as db:
        _seed(db)
        # Three thoughts already written today.
        for offset in (1, 5, 10):
            _seed_existing_thought(db, created_at=now - timedelta(hours=offset))

        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        reflect_calls: list = []
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [
                    ExtractedEvent(
                        description="another major disclosure",
                        emotional_impact=-9,
                    )
                ]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="blocked", emotional_impact=0)],
                calls=reflect_calls,
            ),
            embed_fn=_embed,
            now=now,
            reflection_hard_limit_24h=3,
        )

    assert result.reflection_reason is None  # gated, not "shock"
    assert reflect_calls == []
    assert result.session.status == SessionStatus.CLOSED


# ---------------------------------------------------------------------------
# 4.4 Filling chain landed correctly
# ---------------------------------------------------------------------------


async def test_thought_writes_filling_chain_pointing_to_source_events() -> None:
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [
                    ExtractedEvent(description="user disclosed loss", emotional_impact=-9),
                    ExtractedEvent(description="user mentioned mochi", emotional_impact=2),
                ]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="user is going through grief", emotional_impact=-5)]
            ),
            embed_fn=_embed,
        )

        # Capture ids while the session is still open — accessing .id
        # on a detached row triggers a refresh that needs the bound
        # session.
        assert len(result.thoughts_created) == 1
        thought_id = result.thoughts_created[0].id
        event_ids = {e.id for e in result.events_created}

    with DbSession(engine) as db:
        rows = list(
            db.exec(
                select(ConceptNodeFilling).where(
                    ConceptNodeFilling.parent_id == thought_id
                )
            )
        )
    # Reflector said "use everything"; both events become filling rows.
    assert len(rows) == 2
    assert all(r.orphaned is False for r in rows)
    child_ids = {r.child_id for r in rows}
    assert child_ids == event_ids


# ---------------------------------------------------------------------------
# 4.5 Orphaned filling on event delete
# ---------------------------------------------------------------------------


async def test_event_delete_orphans_filling_but_keeps_thought() -> None:
    """Deleting a source event must NOT delete the thought.

    Per the architecture's forgetting-rights design, the user can delete
    raw / event memories while keeping the higher-level thought; the
    filling row simply gets marked ``orphaned=True`` so the lineage
    audit trail remains queryable.
    """

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess,
            extract_fn=_make_extract(
                [
                    ExtractedEvent(description="event A", emotional_impact=-9),
                    ExtractedEvent(description="event B", emotional_impact=2),
                ]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="grief shape", emotional_impact=-4)]
            ),
            embed_fn=_embed,
        )

        # Capture ids while the session is still open.
        thought_id = result.thoughts_created[0].id
        victim_id = result.events_created[0].id

    # Forget event A while keeping the thought (orphan choice).
    with DbSession(engine) as db:
        delete_concept_node(
            db, node_id=victim_id, choice=DeletionChoice.ORPHAN
        )

    with DbSession(engine) as db:
        # Thought is still present.
        thought = db.get(ConceptNode, thought_id)
        assert thought is not None
        assert thought.deleted_at is None

        # Filling row for the deleted event is now orphaned.
        rows = list(
            db.exec(
                select(ConceptNodeFilling).where(
                    ConceptNodeFilling.parent_id == thought_id
                )
            )
        )
        by_child = {r.child_id: r for r in rows}
        assert by_child[victim_id].orphaned is True
        assert by_child[victim_id].orphaned_at is not None


# ---------------------------------------------------------------------------
# 4.6 Reflect crash leaves events replayable
# ---------------------------------------------------------------------------


class _ReflectError(RuntimeError):
    pass


async def test_reflect_failure_leaves_events_intact_and_resumable() -> None:
    """If reflect_fn raises after extract has committed events, those
    events must still be in the DB. A second consolidate pass on the
    same session must skip extraction (resume flag), retry reflect,
    and NOT duplicate the events."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed(db)
        sess = _make_session(db)
        _add_messages(db, sess.id, 4)

        async def _crash_reflect(_nodes, _reason):
            raise _ReflectError("simulated reflect outage")

        # First pass: extract commits, reflect raises.
        try:
            await consolidate_session(
                db=db,
                backend=backend,
                session=sess,
                extract_fn=_make_extract(
                    [ExtractedEvent(description="vivid event", emotional_impact=-9)]
                ),
                reflect_fn=_crash_reflect,
                embed_fn=_embed,
            )
        except _ReflectError:
            pass
        else:
            raise AssertionError("expected _ReflectError to propagate")

    # Events must already be in the DB and the resume flag set.
    with DbSession(engine) as db:
        events = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_test",
                    ConceptNode.type == NodeType.EVENT.value,
                )
            )
        )
    assert len(events) == 1
    with DbSession(engine) as db:
        sess_after = db.get(Session, "s_test")
        assert sess_after is not None
        # extracted_events was set inside the extract commit; status
        # never advanced to CLOSED because reflect crashed before the
        # final commit.
        assert sess_after.extracted_events is True
        assert sess_after.extracted is False
        assert sess_after.status == SessionStatus.CLOSING

    # Second pass: same session, this time reflect succeeds. Events
    # must NOT be duplicated.
    with DbSession(engine) as db:
        sess_again = db.get(Session, "s_test")
        assert sess_again is not None
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=sess_again,
            extract_fn=_make_extract(
                # Different event content; the resume flag must mean
                # extract is skipped entirely so this is never called.
                [ExtractedEvent(description="WRONG", emotional_impact=0)]
            ),
            reflect_fn=_make_reflect(
                [ExtractedThought(description="grief shape", emotional_impact=-4)]
            ),
            embed_fn=_embed,
        )

    assert len(result.thoughts_created) == 1
    with DbSession(engine) as db:
        events_after = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.source_session_id == "s_test",
                    ConceptNode.type == NodeType.EVENT.value,
                )
            )
        )
        sess_done = db.get(Session, "s_test")
        thought_count = db.exec(
            select(func.count())
            .select_from(ConceptNode)
            .where(
                ConceptNode.persona_id == "p",
                ConceptNode.type == NodeType.THOUGHT.value,
            )
        ).one()
    # No duplicate events; the original "vivid event" survived and the
    # second "WRONG" never landed.
    assert len(events_after) == 1
    assert events_after[0].description == "vivid event"
    assert thought_count == 1
    assert sess_done is not None
    assert sess_done.status == SessionStatus.CLOSED
    assert sess_done.extracted is True

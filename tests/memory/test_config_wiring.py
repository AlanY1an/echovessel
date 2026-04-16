"""Tests for 4 previously-hardcoded thresholds that are now configurable.

These verify:

- ``retrieve.retrieve(relational_bonus_weight=...)`` threads the weight
  into ``_score_node`` rather than using the module-level constant
- ``consolidate.is_trivial(trivial_message_count=...,
  trivial_token_count=...)`` honours the call-site overrides
- ``consolidate.consolidate_session(reflection_hard_limit_24h=...)``
  honours its override through the reflection gate

Each field has two checks: default value preserves the historical
behaviour, and a custom value visibly changes the outcome.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType, SessionStatus
from echovessel.memory import (
    ConceptNode,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractedThought,
    consolidate_session,
    is_trivial,
)
from echovessel.memory.models import RecallMessage, Session
from echovessel.memory.retrieve import retrieve

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _unit_vec(axis: int, dim: int = 384) -> list[float]:
    v = [0.0] * dim
    v[axis] = 1.0
    return v


def _seed_persona(db: DbSession) -> None:
    db.add(Persona(id="p_test", display_name="Test"))
    db.add(User(id="self", display_name="Alan"))
    db.commit()


# ---------------------------------------------------------------------------
# retrieve · relational_bonus_weight
# ---------------------------------------------------------------------------


def _build_retrieve_fixture():
    """Two nodes sharing the same vector: one relationally-tagged, one plain.
    The tagged node wins the tie-breaker only via relational_bonus."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)

    with DbSession(engine) as db:
        _seed_persona(db)
        tagged = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="关系相关事件",
            emotional_impact=0,
            relational_tags=["identity-bearing"],
        )
        plain = ConceptNode(
            persona_id="p_test",
            user_id="self",
            type=NodeType.EVENT,
            description="普通事件",
            emotional_impact=0,
            relational_tags=[],
        )
        db.add(tagged)
        db.add(plain)
        db.commit()
        db.refresh(tagged)
        db.refresh(plain)
        backend.insert_vector(tagged.id, _unit_vec(0))
        backend.insert_vector(plain.id, _unit_vec(0))

    return engine, backend


def _embed_axis0(_: str) -> list[float]:
    return _unit_vec(0)


def test_retrieve_default_relational_bonus_weight_preserves_behaviour() -> None:
    engine, backend = _build_retrieve_fixture()
    with DbSession(engine) as db:
        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="q",
            embed_fn=_embed_axis0,
            top_k=5,
        )
    tagged = next(m for m in result.memories if m.relational_bonus > 0)
    plain = next(m for m in result.memories if m.relational_bonus == 0)
    # Default weight is 1.0; tagged beats plain by the bonus term.
    assert tagged.total > plain.total


def test_retrieve_zero_relational_bonus_weight_eliminates_tie_breaker() -> None:
    engine, backend = _build_retrieve_fixture()
    with DbSession(engine) as db:
        result = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="q",
            embed_fn=_embed_axis0,
            top_k=5,
            relational_bonus_weight=0.0,
        )
    tagged = next(m for m in result.memories if m.relational_bonus > 0)
    plain = next(m for m in result.memories if m.relational_bonus == 0)
    # With weight=0, the two nodes score identically.
    assert tagged.total == plain.total


def test_retrieve_inflated_relational_bonus_weight_widens_gap() -> None:
    engine, backend = _build_retrieve_fixture()
    with DbSession(engine) as db:
        small = retrieve(
            db=db,
            backend=backend,
            persona_id="p_test",
            user_id="self",
            query_text="q",
            embed_fn=_embed_axis0,
            top_k=5,
            relational_bonus_weight=1.0,
        )
        # Fresh engine for the second call — retrieve increments access_count
        # and commits, which would otherwise change the recency/recency
        # components between calls in subtle ways.
    engine2, backend2 = _build_retrieve_fixture()
    with DbSession(engine2) as db2:
        large = retrieve(
            db=db2,
            backend=backend2,
            persona_id="p_test",
            user_id="self",
            query_text="q",
            embed_fn=_embed_axis0,
            top_k=5,
            relational_bonus_weight=5.0,
        )

    def _gap(res) -> float:
        t = next(m for m in res.memories if m.relational_bonus > 0)
        p = next(m for m in res.memories if m.relational_bonus == 0)
        return t.total - p.total

    assert _gap(large) > _gap(small)


# ---------------------------------------------------------------------------
# consolidate.is_trivial · trivial_message_count / trivial_token_count
# ---------------------------------------------------------------------------


def _fake_session_and_messages(
    *, message_count: int, total_tokens: int
) -> tuple[Session, list[RecallMessage]]:
    session = Session(
        id="s_trivial",
        persona_id="p_test",
        user_id="self",
        channel_id="web",
        status=SessionStatus.CLOSING,
        message_count=message_count,
        total_tokens=total_tokens,
    )
    # Benign content so _has_strong_emotion returns False. These are
    # never persisted (is_trivial only reads .content), so the `day`
    # NOT NULL constraint does not apply — but we set it defensively.
    today = date.today()
    messages = [
        RecallMessage(
            id=i + 1,
            persona_id="p_test",
            user_id="self",
            channel_id="web",
            session_id="s_trivial",
            role="user",
            content="hi",
            token_count=5,
            day=today,
        )
        for i in range(message_count)
    ]
    return session, messages


def test_is_trivial_default_message_count_preserves_behaviour() -> None:
    session, messages = _fake_session_and_messages(message_count=2, total_tokens=50)
    # Default TRIVIAL_MESSAGE_COUNT == 3; message_count=2 is below threshold.
    assert is_trivial(session, messages) is True


def test_is_trivial_lower_message_count_flips_verdict() -> None:
    session, messages = _fake_session_and_messages(message_count=2, total_tokens=50)
    # With threshold dropped to 2, message_count >= threshold, so not trivial.
    assert (
        is_trivial(
            session, messages, trivial_message_count=2, trivial_token_count=50_000
        )
        is False
    )


def test_is_trivial_default_token_count_preserves_behaviour() -> None:
    session, messages = _fake_session_and_messages(message_count=1, total_tokens=199)
    # Default TRIVIAL_TOKEN_COUNT == 200; 199 < 200, so trivial.
    assert is_trivial(session, messages) is True


def test_is_trivial_lower_token_count_flips_verdict() -> None:
    session, messages = _fake_session_and_messages(message_count=1, total_tokens=199)
    # With threshold dropped to 100, 199 >= 100, so not trivial.
    assert (
        is_trivial(
            session, messages, trivial_message_count=50, trivial_token_count=100
        )
        is False
    )


# ---------------------------------------------------------------------------
# consolidate_session · reflection_hard_limit_24h
# ---------------------------------------------------------------------------


async def _extract_one_shock(messages):
    return [
        ExtractedEvent(
            description="shock event",
            emotional_impact=9,  # triggers SHOCK reflection
            emotion_tags=[],
            relational_tags=[],
        )
    ]


async def _reflect_one(events, reason):
    return [
        ExtractedThought(
            description=f"reflection({reason})",
            emotional_impact=0,
        )
    ]


def _embed_static(_: str) -> list[float]:
    return _unit_vec(1)


def _seed_session_with_messages(db: DbSession, session_id: str) -> Session:
    _seed_persona(db)
    session = Session(
        id=session_id,
        persona_id="p_test",
        user_id="self",
        channel_id="web",
        status=SessionStatus.CLOSING,
        message_count=5,
        total_tokens=500,
    )
    db.add(session)
    db.commit()
    # Non-trivial session: 5 messages with benign content.
    today = date.today()
    for i in range(5):
        db.add(
            RecallMessage(
                persona_id="p_test",
                user_id="self",
                channel_id="web",
                session_id=session_id,
                role="user",
                content=f"message {i}",
                token_count=100,
                day=today,
            )
        )
    db.commit()
    db.refresh(session)
    return session


def _seed_n_recent_thoughts(db: DbSession, *, count: int, now: datetime) -> None:
    """Write `count` THOUGHT nodes inside the last 24h so the hard gate counts them."""

    for i in range(count):
        db.add(
            ConceptNode(
                persona_id="p_test",
                user_id="self",
                type=NodeType.THOUGHT,
                description=f"prior thought {i}",
                emotional_impact=0,
                created_at=now - timedelta(hours=i + 1),
            )
        )
    db.commit()


async def test_consolidate_session_default_reflection_hard_limit_gates_at_3() -> None:
    """Default REFLECTION_HARD_LIMIT_24H == 3. With 3 prior thoughts in the
    last 24h, the hard gate fires — no new reflection is produced even
    though the session contains a SHOCK event."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    now = datetime(2026, 4, 15, 12, 0, 0)

    with DbSession(engine) as db:
        session = _seed_session_with_messages(db, "s_default_gate")
        _seed_n_recent_thoughts(db, count=3, now=now)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=session,
            extract_fn=_extract_one_shock,
            reflect_fn=_reflect_one,
            embed_fn=_embed_static,
            now=now,
        )
    assert result.reflection_reason is None
    assert result.thoughts_created == []


async def test_consolidate_session_raised_reflection_hard_limit_lets_reflection_fire() -> None:
    """With hard limit raised to 10, 3 prior thoughts fall under the gate
    and the SHOCK reflection executes normally."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    now = datetime(2026, 4, 15, 12, 0, 0)

    with DbSession(engine) as db:
        session = _seed_session_with_messages(db, "s_raised_gate")
        _seed_n_recent_thoughts(db, count=3, now=now)

        result = await consolidate_session(
            db=db,
            backend=backend,
            session=session,
            extract_fn=_extract_one_shock,
            reflect_fn=_reflect_one,
            embed_fn=_embed_static,
            now=now,
            reflection_hard_limit_24h=10,
        )
    assert result.reflection_reason == "shock"
    assert len(result.thoughts_created) == 1


async def test_consolidate_session_lowered_reflection_hard_limit_gates_immediately() -> None:
    """With hard limit lowered to 0, even the first reflection in a 24h
    window is blocked."""

    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    now = datetime(2026, 4, 15, 12, 0, 0)

    with DbSession(engine) as db:
        session = _seed_session_with_messages(db, "s_lowered_gate")
        # Zero prior thoughts; default would allow the reflection through.
        result = await consolidate_session(
            db=db,
            backend=backend,
            session=session,
            extract_fn=_extract_one_shock,
            reflect_fn=_reflect_one,
            embed_fn=_embed_static,
            now=now,
            reflection_hard_limit_24h=0,
        )
    assert result.reflection_reason is None
    assert result.thoughts_created == []

"""Fast-loop expectation check tests (Spec 6 · plan §7.6).

``check_pending_expectations`` is the embedding-only matcher run
during ``assemble_turn``. No LLM call on this path. It operates on
the pending-expectation list assemble_turn already loaded for the
``# You've been expecting`` section, and caches expectation
embeddings keyed by node id (descriptions are immutable once
created).
"""

from __future__ import annotations

import threading
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
from echovessel.memory.models import ConceptNode
from echovessel.runtime.turn import coordinator
from echovessel.runtime.turn.coordinator import (
    EXPECTATION_MATCH_COSINE_THRESHOLD,
    IncomingMessage,
    IncomingTurn,
    TurnContext,
    assemble_turn,
    check_pending_expectations,
)
from echovessel.runtime.turn.prompt_assembly import (
    _load_pending_expectations,
    build_system_prompt,
)


def _seed(engine) -> None:
    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="Luna"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()


def _add_expectation(
    engine,
    *,
    description: str,
    due_at: datetime | None = None,
) -> int:
    with DbSession(engine) as db:
        node = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.EXPECTATION,
            subject="persona",
            description=description,
            emotional_impact=1,
            event_time_end=due_at,
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node.id


def _keyword_embed(keyword: str) -> list[float]:
    """Deterministic 'embedding' that returns a unit vector keyed on
    the first occurrence of one of a small keyword set. Lets us test
    cosine similarity without pulling in sentence-transformers.
    """
    keywords = ["grad school", "recipe", "family", "dog"]
    v = [0.0] * 384

    def _fn(text: str) -> list[float]:
        out = v.copy()
        for i, kw in enumerate(keywords):
            if kw in text.lower():
                out[i] = 1.0
                return out
        # Unknown text → noise slot
        out[len(keywords)] = 1.0
        return out

    return _fn  # type: ignore[return-value]


def _pending(db: DbSession, *, now: datetime) -> list[ConceptNode]:
    return _load_pending_expectations(db, persona_id="p", user_id="self", now=now)


def test_match_above_threshold_returns_fulfilled():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_expectation(
        engine,
        description="alan will update on grad school next week",
        due_at=datetime.now() + timedelta(days=7),
    )
    with DbSession(engine) as db:
        matches = check_pending_expectations(
            _pending(db, now=datetime.now()),
            user_message_text="I finally finished my grad school applications!",
            embed_fn=_keyword_embed(""),
        )
    assert len(matches) == 1
    exp, status = matches[0]
    assert status == "fulfilled"
    assert "grad school" in exp.description


def test_no_match_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_expectation(
        engine,
        description="alan will update on grad school next week",
        due_at=datetime.now() + timedelta(days=7),
    )
    with DbSession(engine) as db:
        matches = check_pending_expectations(
            _pending(db, now=datetime.now()),
            user_message_text="today I walked my dog",
            embed_fn=_keyword_embed(""),
        )
    assert matches == []


def test_expired_expectation_skipped():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    past_due = datetime(2026, 1, 1)
    _add_expectation(
        engine,
        description="alan will update on grad school next week",
        due_at=past_due,
    )
    with DbSession(engine) as db:
        matches = check_pending_expectations(
            _pending(db, now=datetime(2026, 4, 24)),
            user_message_text="grad school update",
            embed_fn=_keyword_embed(""),
        )
    assert matches == []


def test_threshold_is_configurable():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    _add_expectation(
        engine,
        description="recipe cooked tomorrow",
    )

    def weak_embed(text: str) -> list[float]:
        # Every text gets the same unit vector so cosine is exactly 1.0.
        v = [0.0] * 384
        v[0] = 1.0
        return v

    with DbSession(engine) as db:
        # Very high threshold: matches still pass because cosine == 1.0.
        matches = check_pending_expectations(
            _pending(db, now=datetime.now()),
            user_message_text="anything",
            embed_fn=weak_embed,
            threshold=0.99,
        )
        assert len(matches) == 1


def test_empty_user_message_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db:
        matches = check_pending_expectations(
            _pending(db, now=datetime.now()),
            user_message_text="",
            embed_fn=_keyword_embed(""),
        )
    assert matches == []


def test_embedding_cache_skips_reembedding_expectations():
    """Expectation descriptions are immutable — a populated cache means
    the second check embeds only the user message, not the expectation.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    desc = "alan will update on grad school next week"
    _add_expectation(engine, description=desc, due_at=datetime.now() + timedelta(days=7))

    counts: dict[str, int] = {}
    inner = _keyword_embed("")

    def counting_embed(text: str) -> list[float]:
        counts[text] = counts.get(text, 0) + 1
        return inner(text)

    cache: dict[int, list[float]] = {}
    with DbSession(engine) as db:
        pending = _pending(db, now=datetime.now())
        for _ in range(2):
            matches = check_pending_expectations(
                pending,
                user_message_text="grad school news!",
                embed_fn=counting_embed,
                embedding_cache=cache,
            )
            assert len(matches) == 1

    assert counts[desc] == 1
    assert counts["grad school news!"] == 2


def test_embedding_cache_evicts_ids_no_longer_pending():
    cache = {999: [1.0] * 4}
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    exp_id = _add_expectation(
        engine,
        description="family dinner planned",
        due_at=datetime.now() + timedelta(days=7),
    )
    with DbSession(engine) as db:
        check_pending_expectations(
            _pending(db, now=datetime.now()),
            user_message_text="family dinner went great",
            embed_fn=_keyword_embed(""),
            embedding_cache=cache,
        )
    assert 999 not in cache
    assert exp_id in cache


async def test_assemble_turn_embeds_expectation_off_loop_and_once():
    """Across two turns with the same pending expectation, the
    expectation description is embedded exactly once (cache hit on the
    second turn) and that embed runs off the event-loop thread.
    """
    engine = create_engine(":memory:")
    create_all_tables(engine)
    backend = SQLiteBackend(engine)
    _seed(engine)
    desc = "alan will update on grad school next week"
    _add_expectation(engine, description=desc, due_at=datetime(2026, 5, 1))

    coordinator._EXPECTATION_EMBED_CACHE.clear()
    main_thread = threading.get_ident()
    embed_calls: list[tuple[str, int]] = []
    inner = _keyword_embed("")

    def recording_embed(text: str) -> list[float]:
        embed_calls.append((text, threading.get_ident()))
        return inner(text)

    from echovessel.runtime.llm import StubProvider

    with DbSession(engine) as db:
        ctx = TurnContext(
            persona_id="p",
            persona_display_name="Luna",
            db=db,
            backend=backend,
            embed_fn=recording_embed,
        )
        for i in range(2):
            turn = IncomingTurn.from_single_message(
                IncomingMessage(
                    channel_id="web",
                    user_id="self",
                    content=f"hello again {i}",
                    received_at=datetime(2026, 4, 24, 9, i, 0),
                ),
                turn_id=f"t-exp-{i}",
            )
            result = await assemble_turn(ctx, turn, StubProvider(fallback="hi"))
            assert not result.skipped

    desc_embeds = [c for c in embed_calls if c[0] == desc]
    assert len(desc_embeds) == 1
    assert desc_embeds[0][1] != main_thread

    coordinator._EXPECTATION_EMBED_CACHE.clear()


def test_system_prompt_includes_expectation_match_hint():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    exp_id = _add_expectation(engine, description="alan shares grad school deadline")
    with DbSession(engine) as db:
        node = db.get(ConceptNode, exp_id)
        prompt = build_system_prompt(
            persona_display_name="Luna",
            core_blocks=[],
            expectation_matches=[(node, "fulfilled")],
        )
    assert "# Expectation match" in prompt
    assert "alan shares grad school deadline" in prompt
    assert "acknowledge" in prompt.lower()


def test_system_prompt_no_expectation_match_no_hint():
    prompt = build_system_prompt(
        persona_display_name="Luna",
        core_blocks=[],
        expectation_matches=None,
    )
    assert "# Expectation match" not in prompt


def test_threshold_constant_is_reasonable_bound():
    assert 0.0 < EXPECTATION_MATCH_COSINE_THRESHOLD < 1.0

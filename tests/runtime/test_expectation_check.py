"""Fast-loop expectation check tests (Spec 6 · plan §7.6).

``check_pending_expectations`` is the embedding-only matcher run
during ``assemble_turn``. No LLM call on this path.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.models import ConceptNode
from echovessel.runtime.turn.coordinator import (
    EXPECTATION_MATCH_COSINE_THRESHOLD,
    check_pending_expectations,
)
from echovessel.runtime.turn.prompt_assembly import build_system_prompt


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
            db,
            persona_id="p",
            user_id="self",
            user_message_text="I finally finished my grad school applications!",
            embed_fn=_keyword_embed(""),
            now=datetime.now(),
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
            db,
            persona_id="p",
            user_id="self",
            user_message_text="today I walked my dog",
            embed_fn=_keyword_embed(""),
            now=datetime.now(),
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
            db,
            persona_id="p",
            user_id="self",
            user_message_text="grad school update",
            embed_fn=_keyword_embed(""),
            now=datetime(2026, 4, 24),
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
            db,
            persona_id="p",
            user_id="self",
            user_message_text="anything",
            embed_fn=weak_embed,
            now=datetime.now(),
            threshold=0.99,
        )
        assert len(matches) == 1


def test_empty_user_message_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    with DbSession(engine) as db:
        matches = check_pending_expectations(
            db,
            persona_id="p",
            user_id="self",
            user_message_text="",
            embed_fn=_keyword_embed(""),
            now=datetime.now(),
        )
    assert matches == []


def test_system_prompt_includes_expectation_match_hint():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    exp_id = _add_expectation(
        engine, description="alan shares grad school deadline"
    )
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

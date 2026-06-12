"""Impact decay + reinforcement dampening in the rerank formula.

The impact term decays with a slow half-life down to a floor, and
access_count / mention_count reinforcement raises that floor. Covers:

1. Age-0 invariance — a fresh node's decay factor is exactly 1.0 and
   its total is unchanged by reinforcement signals.
2. Calibration — a year-old node with no signals bottoms out at the
   floor; access_count=20 lifts it back to most of its weight; very
   large signals saturate the floor at 1.0.
3. Ordering — an old high-impact event ranks below a fresh,
   more-relevant node even though the undecayed arithmetic of the same
   pair puts the old peak on top: the decay term is load-bearing.
4. Force-load — ``_load_user_thoughts_force`` ranks a frequently
   accessed old thought above an unaccessed same-age peer, so
   repeatedly-confirmed user facts stay in the pinned top-N.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session as DbSession

from echovessel.core.types import NodeType
from echovessel.memory import (
    ConceptNode,
    Persona,
    User,
    create_all_tables,
    create_engine,
)
from echovessel.memory.retrieve.core import _load_user_thoughts_force
from echovessel.memory.retrieve.scoring import (
    IMPACT_DECAY_FLOOR,
    WEIGHT_IMPACT,
    WEIGHT_RECENCY,
    WEIGHT_RELEVANCE,
    ScoredMemory,
    _impact_decay_factor,
    _impact_score,
    _score_node,
)

NOW = datetime(2026, 6, 1, 12, 0, 0)


def _node(
    *,
    age_days: float,
    impact: int,
    access_count: int = 0,
    mention_count: int = 1,
    description: str = "x",
) -> ConceptNode:
    return ConceptNode(
        persona_id="p",
        user_id="self",
        type=NodeType.EVENT,
        description=description,
        emotional_impact=impact,
        access_count=access_count,
        mention_count=mention_count,
        created_at=NOW - timedelta(days=age_days),
    )


# ---------------------------------------------------------------------------
# Age-0 invariance
# ---------------------------------------------------------------------------


def test_age_zero_factor_is_one_regardless_of_signals():
    assert _impact_decay_factor(_node(age_days=0, impact=9), NOW) == 1.0
    assert (
        _impact_decay_factor(_node(age_days=0, impact=9, access_count=50, mention_count=7), NOW)
        == 1.0
    )


def test_age_zero_total_unchanged_by_signals():
    plain = _score_node(_node(age_days=0, impact=5), 0.4, NOW)
    reinforced = _score_node(_node(age_days=0, impact=5, access_count=50), 0.4, NOW)
    assert plain.total == reinforced.total
    assert plain.impact == _impact_score(5)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_year_old_no_signals_bottoms_at_floor():
    node = _node(age_days=365, impact=9)
    assert _impact_decay_factor(node, NOW) == pytest.approx(IMPACT_DECAY_FLOOR)
    sm = _score_node(node, 2.0, NOW)
    # impact component as ranked: 9/10 normalized × the floor
    assert sm.impact == pytest.approx(0.9 * IMPACT_DECAY_FLOOR)


def test_year_old_with_20_accesses_keeps_most_weight():
    factor = _impact_decay_factor(_node(age_days=365, impact=9, access_count=20), NOW)
    assert 0.7 < factor < 1.0


def test_mention_count_baseline_of_one_is_not_reinforcement():
    # Every node is born with mention_count=1; that alone must not lift
    # the floor above the no-signal level.
    plain = _impact_decay_factor(_node(age_days=365, impact=9, mention_count=1), NOW)
    assert plain == pytest.approx(IMPACT_DECAY_FLOOR)
    re_mentioned = _impact_decay_factor(_node(age_days=365, impact=9, mention_count=5), NOW)
    assert re_mentioned > plain


def test_reinforcement_floor_saturates_at_one():
    factor = _impact_decay_factor(_node(age_days=365, impact=9, access_count=10_000), NOW)
    assert factor == 1.0


# ---------------------------------------------------------------------------
# Ordering at scale
# ---------------------------------------------------------------------------


def test_old_peak_ranks_below_fresh_relevant_node():
    old_peak = _node(age_days=365, impact=9, description="peak event a year back")
    fresh = _node(age_days=0, impact=1, description="what we talked about today")

    old_sm = _score_node(old_peak, 0.6, NOW)  # relevance 0.7
    fresh_sm = _score_node(fresh, 0.3, NOW)  # relevance 0.85

    def undecayed(sm: ScoredMemory) -> float:
        return (
            WEIGHT_RECENCY * sm.recency
            + WEIGHT_RELEVANCE * sm.relevance
            + WEIGHT_IMPACT * _impact_score(sm.node.emotional_impact)
        )

    # With a full-strength impact term the old peak wins this pair...
    assert undecayed(old_sm) > undecayed(fresh_sm)
    # ...the decayed impact term ranks the fresh, more relevant node first.
    assert fresh_sm.total > old_sm.total


# ---------------------------------------------------------------------------
# Force-load user thoughts share the dampened impact term
# ---------------------------------------------------------------------------


def test_force_load_ranks_reinforced_thought_above_unaccessed_peer():
    engine = create_engine(":memory:")
    create_all_tables(engine)

    with DbSession(engine) as db:
        db.add(Persona(id="p", display_name="Luna"))
        db.add(User(id="self", display_name="Alan"))
        db.commit()

        created = NOW - timedelta(days=200)
        reinforced = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="user",
            description="retired teacher, cat named Mochi",
            emotional_impact=4,
            access_count=30,
            created_at=created,
        )
        stale = ConceptNode(
            persona_id="p",
            user_id="self",
            type=NodeType.THOUGHT,
            subject="user",
            description="mentioned a podcast once",
            emotional_impact=4,
            access_count=0,
            created_at=created,
        )
        db.add(reinforced)
        db.add(stale)
        db.commit()
        db.refresh(reinforced)
        db.refresh(stale)

        rows = _load_user_thoughts_force(
            db,
            persona_id="p",
            user_id="self",
            limit=2,
            now=NOW,
        )

        assert [n.id for n in rows] == [reinforced.id, stale.id]

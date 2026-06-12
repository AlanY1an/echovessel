"""Scoring weights, decay constants, and per-candidate score functions.

The retrieve pipeline ranks candidate concept nodes with

    score = WEIGHT_RECENCY     * recency
          + WEIGHT_RELEVANCE   * relevance
          + WEIGHT_IMPACT      * impact          (impact decays slowly; see
                                                  ``_impact_decay_factor``)
          + relational_bonus_w * relational_bonus
          + WEIGHT_ENTITY_ANCHOR * entity_anchor_bonus

This module owns the weights and the score helpers + ``_score_node``
so tuning happens in one place. Architecture v0.3 §3.2 + §4.14 + plan §6.3.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from echovessel.memory.models import ConceptNode

# Scoring weights (architecture v0.3 §3.2 + §4.14)
WEIGHT_RECENCY = 0.5
WEIGHT_RELEVANCE = 3.0
WEIGHT_IMPACT = 2.0
WEIGHT_RELATIONAL_BONUS = 1.0
RELATIONAL_BONUS_VALUE = 0.5

# L5 · entity_anchor bonus (plan §6.3). If the query string contains a
# known entity alias, concept nodes linked to that entity through the
# L3↔L5 junction get an extra score bump — the Scott/黄逸扬 case (plan
# Case 8) exists specifically because vector-only search misses cross-
# language alias hits. Weight is applied against a fixed bonus value so
# the extra score is either on (+WEIGHT_ENTITY_ANCHOR) or off (0).
WEIGHT_ENTITY_ANCHOR = 1.5
ENTITY_ANCHOR_BONUS_VALUE = 1.0

# Recency half-life: how much weight a memory from N days ago retains.
# Architecture uses positional decay 0.99^i; we use time-based for stability
# across varying session densities. 14 days half-life is a reasonable default.
RECENCY_HALF_LIFE_DAYS = 14

# Impact decay: the impact term loses weight slowly with age so a store
# full of old peak events can't permanently crowd fresh relevant
# material out of the prompt. Much slower than recency (emotional
# weight fades slower than topical freshness), with a floor: a year-old
# impact-9 event still contributes ~30% of its full term — old trauma
# still matters, just not at full strength.
IMPACT_HALF_LIFE_DAYS = 90
IMPACT_DECAY_FLOOR = 0.3

# Reinforcement raises the effective decay floor: a node that keeps
# being retrieved (access_count) or re-mentioned at consolidate time
# (mention_count) is a durable fact, not a one-off peak, and should
# wash out slower. log1p keeps the gain sub-linear; the floor caps at
# 1.0, so a heavily-reinforced node simply stops decaying. mention_count
# is born at 1, so only re-mentions count as reinforcement. At gain
# 0.15, ~20 signals lift the floor to ≈0.76 and ~105 saturate it.
IMPACT_REINFORCEMENT_GAIN = 0.15

# Default minimum relevance floor applied at rerank time.
#
# `_relevance_score(distance)` maps sqlite-vec's cosine distance
# ([0, 2]: 0 = identical, 1 = orthogonal, 2 = opposite) to a relevance
# in [0, 1] via `1 - d/2`, so a strictly-orthogonal candidate scores
# exactly 0.5. The floor sits just above that at **0.55** — tight
# enough to drop candidates with zero directional overlap, loose
# enough to keep anything with cosine similarity ≥ 0.1 toward the
# query. Without the floor, orthogonal candidates flow through rerank,
# where the impact + relational_bonus tie-breakers consistently
# promote high-|impact| peak events for completely unrelated queries —
# the root of the Over-recall MVP miss documented in
# `docs/memory/eval-runs/2026-04-15-baseline-nogit.md` §6.
#
# With a real sentence-transformers embedder this floor rarely fires
# because natural language rarely hits exact-zero overlap; it is
# principally a stub-embedder safety net in the eval harness.
DEFAULT_MIN_RELEVANCE = 0.55


@dataclass(slots=True)
class ScoredMemory:
    """A single retrieved memory with its individual score components."""

    node: ConceptNode
    recency: float
    relevance: float
    # Dampened: normalized |impact| × decay factor (``_impact_decay_factor``),
    # i.e. exactly the value the rerank total weights.
    impact: float
    relational_bonus: float
    total: float
    # L5 · non-zero when the node is linked to an entity whose alias
    # appeared in the query text. ``ENTITY_ANCHOR_BONUS_VALUE`` when
    # present, 0 otherwise — stored unweighted so logs / tests can
    # inspect the raw signal separately from the rerank weight.
    entity_anchor_bonus: float = 0.0


def _recency_score(created_at: datetime, now: datetime) -> float:
    """Exponential decay by time difference. Returns [0, 1]."""
    days = max((now - created_at).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)


def _relevance_score(distance: float) -> float:
    """Convert a cosine distance to a similarity in [0, 1].

    sqlite-vec returns cosine distance in [0, 2] (0 = identical, 1 = orthogonal,
    2 = opposite). We map it to [1, 0] via 1 - d/2, clamped.
    """
    similarity = 1.0 - (distance / 2.0)
    return max(0.0, min(1.0, similarity))


def _impact_score(emotional_impact: int) -> float:
    """|impact| normalized to [0, 1]."""
    return min(abs(emotional_impact) / 10.0, 1.0)


def _impact_decay_factor(node: ConceptNode, now: datetime) -> float:
    """Slow exponential decay for the impact term, floored by reinforcement.

    Returns (0, 1]; exactly 1.0 at age 0 regardless of signals, so a
    fresh node always carries its full impact.
    """
    days = max((now - node.created_at).total_seconds() / 86400.0, 0.0)
    decay = 0.5 ** (days / IMPACT_HALF_LIFE_DAYS)
    reinforcement = node.access_count + max(node.mention_count - 1, 0)
    floor = min(1.0, IMPACT_DECAY_FLOOR + IMPACT_REINFORCEMENT_GAIN * math.log1p(reinforcement))
    return max(decay, floor)


def _dampened_impact_score(node: ConceptNode, now: datetime) -> float:
    """The impact term exactly as ranked: normalized |impact| × decay factor.

    Shared by ``_score_node`` and the force-load user-thoughts ranking
    in ``retrieve.core`` so the two formulas can't diverge.
    """
    return _impact_score(node.emotional_impact) * _impact_decay_factor(node, now)


def _relational_bonus(node: ConceptNode) -> float:
    return RELATIONAL_BONUS_VALUE if node.relational_tags else 0.0


def _score_node(
    node: ConceptNode,
    distance: float,
    now: datetime,
    *,
    relational_bonus_weight: float = WEIGHT_RELATIONAL_BONUS,
    entity_anchored: bool = False,
) -> ScoredMemory:
    recency = _recency_score(node.created_at, now)
    relevance = _relevance_score(distance)
    impact = _dampened_impact_score(node, now)
    rel_bonus = _relational_bonus(node)
    anchor_bonus = ENTITY_ANCHOR_BONUS_VALUE if entity_anchored else 0.0

    total = (
        WEIGHT_RECENCY * recency
        + WEIGHT_RELEVANCE * relevance
        + WEIGHT_IMPACT * impact
        + relational_bonus_weight * rel_bonus
        + WEIGHT_ENTITY_ANCHOR * anchor_bonus
    )
    return ScoredMemory(
        node=node,
        recency=recency,
        relevance=relevance,
        impact=impact,
        relational_bonus=rel_bonus,
        total=total,
        entity_anchor_bonus=anchor_bonus,
    )

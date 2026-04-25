"""Scoring weights, decay constants, and per-candidate score functions.

The retrieve pipeline ranks candidate concept nodes with

    score = WEIGHT_RECENCY     * recency
          + WEIGHT_RELEVANCE   * relevance
          + WEIGHT_IMPACT      * impact
          + relational_bonus_w * relational_bonus
          + WEIGHT_ENTITY_ANCHOR * entity_anchor_bonus

This module owns the weights and the four score helpers + ``_score_node``
so tuning happens in one place. Architecture v0.3 §3.2 + §4.14 + plan §6.3.
"""

from __future__ import annotations

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

# Default minimum relevance floor applied at rerank time.
#
# `_relevance_score(distance)` maps sqlite-vec's distance output to a
# relevance in [0, 1]. The older docstring on `_relevance_score` labels
# the metric "cosine distance" but `vec0` virtual tables use L2 distance
# by default, so for unit-norm embeddings the orthogonal case is
# `||u - v|| = sqrt(2) ≈ 1.414` and `relevance = 1 - 1.414/2 ≈ 0.293`,
# while partial overlap (cos=0.5) gives `||u - v|| = 1` and relevance =
# 0.5. (Identical and opposite endpoints still match the docstring.)
#
# Given that, the floor sits at **0.4** — tight enough to drop truly
# orthogonal candidates (~0.293) but loose enough to keep events that
# share a single dimension with the query (~0.5). Without the floor,
# strictly-orthogonal candidates flow through rerank, where the impact
# + relational_bonus tie-breakers consistently promote high-|impact|
# peak events for completely unrelated queries — the root of the
# Over-recall MVP miss documented in
# `docs/memory/eval-runs/2026-04-15-baseline-nogit.md` §6.
#
# With a real sentence-transformers embedder this floor rarely fires
# because natural language rarely hits exact-zero overlap; it is
# principally a stub-embedder safety net in the eval harness, but the
# math is the same for any embedder whose orthogonal case lands near
# distance=sqrt(2).
DEFAULT_MIN_RELEVANCE = 0.4


@dataclass(slots=True)
class ScoredMemory:
    """A single retrieved memory with its individual score components."""

    node: ConceptNode
    recency: float
    relevance: float
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
    impact = _impact_score(node.emotional_impact)
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

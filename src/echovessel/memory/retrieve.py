"""RETRIEVE pipeline — what goes into the prompt before each response.

Per architecture v0.3 §3.2:

    1. L1 (core blocks) always in prompt, unconditional
    2. Unified L3+L4 query on concept_nodes, filtered by type IN (event, thought)
    3. Rerank with score = 0.5*recency + 3*relevance + 2*impact + 1*relational_bonus
    4. Top-K returned
    5. Optional session expansion via L2 JOIN when an event needs context
    6. L2 FTS fallback when L3/L4 returns too few hits or an explicit query

Accepts an `embed_fn` callable so the memory module stays decoupled from
any specific embedding provider.

---

🚨 铁律 · Memory retrieval NEVER filters by channel_id · DISCUSSION.md D4 🚨

This entire file must not contain any `WHERE channel_id = ...` clause —
not in vector_search, not in FTS fallback, not in session context expansion,
not in L1 loading. A real human in a group chat still remembers every
private conversation; memory knows everything. Deciding what to VOICE in a
given channel is the job of Interaction Policy (the output layer), not this
module.

Adding a channel filter here would:
  - make persona "forget" in one channel what it knew in another
  - break the "single psyche across channels" contract
  - require the whole retrieval stack to be rewritten when group chat lands
  - undo the reason single_psyche_A was chosen in the first place

Code review red flag: if any retrieve diff introduces a channel filter,
reject it and refer to docs/DISCUSSION.md 2026-04-14 D4.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    CoreBlock,
    RecallMessage,
)

# Scoring weights (architecture v0.3 §3.2 + §4.14)
WEIGHT_RECENCY = 0.5
WEIGHT_RELEVANCE = 3.0
WEIGHT_IMPACT = 2.0
WEIGHT_RELATIONAL_BONUS = 1.0
RELATIONAL_BONUS_VALUE = 0.5

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


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScoredMemory:
    """A single retrieved memory with its individual score components."""

    node: ConceptNode
    recency: float
    relevance: float
    impact: float
    relational_bonus: float
    total: float


@dataclass(slots=True)
class RetrievalResult:
    """Full return of the retrieve pipeline."""

    core_blocks: list[CoreBlock]
    memories: list[ScoredMemory]
    # Context messages from L2, if any were expanded around hit events
    context_messages: list[RecallMessage]
    # L2 FTS fallback hits (if triggered)
    fts_fallback: list[RecallMessage]


# ---------------------------------------------------------------------------
# Enum normalization helpers
# ---------------------------------------------------------------------------


def _type_str(node: ConceptNode) -> str:
    """ConceptNode.type may come back as the enum or as a plain string
    depending on whether it was hydrated from DB or built in Python.
    Normalize both to the string value."""
    t = node.type
    return getattr(t, "value", t)


# ---------------------------------------------------------------------------
# L1 · Core blocks loading
# ---------------------------------------------------------------------------


def load_core_blocks(
    db: DbSession, persona_id: str, user_id: str
) -> list[CoreBlock]:
    """Load every core block that belongs to (persona_id, user_id).

    Returns both shared blocks (user_id NULL) and per-user blocks for this user.
    Ordered for prompt injection: persona -> self -> user -> relationship -> mood.
    """
    order = ["persona", "self", "user", "relationship", "mood"]

    stmt = select(CoreBlock).where(
        CoreBlock.persona_id == persona_id,
        CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
        # shared OR this user's per-user blocks
        (CoreBlock.user_id.is_(None)) | (CoreBlock.user_id == user_id),  # type: ignore[union-attr]
    )
    blocks = list(db.exec(stmt))

    def _label_str(b: CoreBlock) -> str:
        # Columns typed as String store enum values as plain strings at load
        # time, but the Python-side field is still annotated as the enum.
        # Normalize both cases.
        label = b.label
        return getattr(label, "value", label)

    blocks.sort(
        key=lambda b: order.index(_label_str(b)) if _label_str(b) in order else 99
    )
    return blocks


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


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
) -> ScoredMemory:
    recency = _recency_score(node.created_at, now)
    relevance = _relevance_score(distance)
    impact = _impact_score(node.emotional_impact)
    rel_bonus = _relational_bonus(node)

    total = (
        WEIGHT_RECENCY * recency
        + WEIGHT_RELEVANCE * relevance
        + WEIGHT_IMPACT * impact
        + relational_bonus_weight * rel_bonus
    )
    return ScoredMemory(
        node=node,
        recency=recency,
        relevance=relevance,
        impact=impact,
        relational_bonus=rel_bonus,
        total=total,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def retrieve(
    db: DbSession,
    backend: StorageBackend,
    persona_id: str,
    user_id: str,
    query_text: str,
    embed_fn: Callable[[str], list[float]],
    top_k: int = 10,
    now: datetime | None = None,
    fallback_threshold: int = 3,
    expand_session_context: bool = True,
    context_window: int = 3,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    relational_bonus_weight: float = WEIGHT_RELATIONAL_BONUS,
) -> RetrievalResult:
    """Full RETRIEVE pipeline per architecture v0.3 §3.2.

    Args:
        db: SQLModel session.
        backend: StorageBackend for vector + FTS.
        persona_id / user_id: Scope of retrieval.
        query_text: The current user message or query.
        embed_fn: Function that turns text into a 384-dim vector.
        top_k: Max ConceptNodes to return.
        now: Override current time for deterministic tests.
        fallback_threshold: If L3/L4 returns fewer than this, trigger L2 FTS.
        expand_session_context: If True, pull surrounding L2 messages for event hits.
        context_window: Number of neighbours (each side) for session expansion.
        min_relevance: Drop candidates whose `relevance` score (see
            `_relevance_score`) is strictly below this threshold BEFORE
            rerank orders them. Default 0.55 filters out strictly-orthogonal
            matches (relevance == 0.5), which is the MVP Over-recall
            mitigation documented in
            `docs/memory/eval-runs/2026-04-15-baseline-*.md` §6. Set to
            0.0 to restore pre-fix behaviour (all candidates kept).
        relational_bonus_weight: Weight applied to the relational-bonus
            term in the rerank formula. Default matches the module-level
            `WEIGHT_RELATIONAL_BONUS` constant (1.0). Runtime threads
            this from `cfg.memory.relational_bonus_weight`; tests can
            dial it up/down to bias retrieval toward or away from
            relationally-tagged events.
    """
    now = now or datetime.now()

    # Step 1: L1
    core_blocks = load_core_blocks(db, persona_id, user_id)

    # Step 2: Vector search on concept_nodes via backend
    query_vec = embed_fn(query_text)
    hits = backend.vector_search(
        query_embedding=query_vec,
        persona_id=persona_id,
        user_id=user_id,
        types=(NodeType.EVENT.value, NodeType.THOUGHT.value),
        top_k=max(top_k * 4, 40),
    )

    # Step 3: Load the full nodes and rerank
    if hits:
        node_ids = [h.concept_node_id for h in hits]
        distance_by_id = {h.concept_node_id: h.distance for h in hits}

        nodes = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.id.in_(node_ids),  # type: ignore[union-attr]
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
        scored = [
            _score_node(
                n,
                distance_by_id[n.id],
                now,
                relational_bonus_weight=relational_bonus_weight,
            )
            for n in nodes
        ]
        # Drop candidates whose relevance is below the floor. This is the
        # Over-recall mitigation: strictly-orthogonal matches collapse to
        # relevance 0.5 and, without this filter, the rerank tie-breakers
        # (impact + relational_bonus) always pick high-impact peak events
        # and surface them for unrelated queries. See the docstring on
        # `min_relevance` and the 2026-04-16 EVAL-overrecall diagnosis.
        scored = [sm for sm in scored if sm.relevance >= min_relevance]
        scored.sort(key=lambda s: -s.total)
        top_memories = scored[:top_k]
    else:
        top_memories = []

    # Step 4: access_count bookkeeping (+1 for each hit we actually return)
    for sm in top_memories:
        sm.node.access_count += 1
        sm.node.last_accessed_at = now
        db.add(sm.node)
    if top_memories:
        db.commit()

    # Step 5: Session expansion — for each event hit, pull neighbours from L2
    context_messages: list[RecallMessage] = []
    if expand_session_context and top_memories:
        context_messages = _expand_session_context(db, top_memories, context_window)

    # Step 6: L2 FTS fallback if the vector index itself came up empty.
    #
    # Note: we compare against the RAW vector-hit count (`hits`), not the
    # post-rerank `top_memories` count. The min_relevance filter's job is
    # to drop truly-irrelevant candidates; if the filter legitimately
    # leaves us with 0-2 memories because only 0-2 candidates passed the
    # relevance floor, that is the correct answer, not a signal that FTS
    # should take over. FTS should only rescue the case where sqlite-vec
    # returned nothing at all (e.g. empty index). See the 2026-04-16
    # Over-recall fix notes in `docs/memory/eval-runs/`.
    fts_fallback: list[RecallMessage] = []
    if len(hits) < fallback_threshold:
        fts_hits = backend.fts_search(
            query_text=query_text,
            persona_id=persona_id,
            user_id=user_id,
            top_k=fallback_threshold,
        )
        if fts_hits:
            hit_ids = [h.recall_message_id for h in fts_hits]
            fts_fallback = list(
                db.exec(
                    select(RecallMessage).where(
                        RecallMessage.id.in_(hit_ids),  # type: ignore[union-attr]
                        RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                )
            )

    return RetrievalResult(
        core_blocks=core_blocks,
        memories=top_memories,
        context_messages=context_messages,
        fts_fallback=fts_fallback,
    )


# ---------------------------------------------------------------------------
# Session context expansion
# ---------------------------------------------------------------------------


def list_recall_messages(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    limit: int = 50,
    before: datetime | None = None,
) -> list[RecallMessage]:
    """Pure L2 timeline query for UI pagination.

    Returns recall messages for (persona_id, user_id) ordered by created_at
    DESC, excluding soft-deleted rows. If ``before`` is given, only returns
    messages with ``created_at < before`` (cursor pagination).

    🚨 BY DESIGN, this API does NOT accept a channel_id parameter. It returns
    a unified timeline across all channels per DISCUSSION.md 2026-04-14 D4
    and D-SPEC-4 in docs/channels/01-spec-v0.1.md. Web UI filters via the
    ``channel_id`` field on each returned row if it wants a per-channel view
    — that is a frontend concern, not a memory concern.

    This is a ground-truth L2 read, NOT part of the retrieve pipeline. It
    does not touch scoring, rerank, vector search, or FTS. It is a plain SQL
    timeline query consumed by the web channel's /api/history endpoint and
    by runtime's interaction layer when it needs the recent conversation
    window for prompt assembly.

    Args:
        db: SQLModel session.
        persona_id: Whose timeline.
        user_id: For which user (MVP: always "self").
        limit: Max rows returned, hard-capped at 200 to prevent abusive
            queries.
        before: Cursor; only rows with ``created_at < before`` are returned.
            None means "start from newest".

    Returns:
        list[RecallMessage] in DESCENDING created_at order (newest first).
    """
    limit = max(1, min(limit, 200))

    stmt = (
        select(RecallMessage)
        .where(
            RecallMessage.persona_id == persona_id,
            RecallMessage.user_id == user_id,
            RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(RecallMessage.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(RecallMessage.created_at < before)

    return list(db.exec(stmt).all())


def list_concept_nodes(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    node_type: NodeType,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[ConceptNode], int]:
    """Pure ConceptNode timeline query for the admin Memory tabs.

    Returns ``(rows, total_count)`` for ``(persona_id, user_id, node_type)``
    ordered by ``created_at`` DESC, excluding soft-deleted rows. The
    admin Events / Thoughts tabs use this to render a paginated list
    with the server-side total — total is computed separately so the
    UI can show "showing X of Y" without bringing every row over the
    wire.

    🚨 Same iron rule as :func:`list_recall_messages`: this function
    does NOT accept a channel_id parameter. There is no transport
    filtering at the memory layer.

    Args:
        db: SQLModel session.
        persona_id: Whose timeline.
        user_id: For which user (MVP: always "self").
        node_type: ``NodeType.EVENT`` for the Events tab,
            ``NodeType.THOUGHT`` for the Thoughts tab.
        limit: Max rows returned, hard-capped at 100.
        offset: Number of rows to skip from the head of the DESC order
            (i.e. "page through older entries"). Negative values are
            clamped to 0.

    Returns:
        ``(rows, total)`` where ``rows`` is a list of ConceptNode in
        DESCENDING created_at order and ``total`` is the unfiltered
        non-deleted count for the same persona/user/node_type triple.
    """

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    base_filters = (
        ConceptNode.persona_id == persona_id,
        ConceptNode.user_id == user_id,
        ConceptNode.type == node_type,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )

    page_stmt = (
        select(ConceptNode)
        .where(*base_filters)
        .order_by(ConceptNode.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    rows = list(db.exec(page_stmt).all())

    from sqlmodel import func as _func

    total_stmt = (
        select(_func.count())
        .select_from(ConceptNode)
        .where(*base_filters)
    )
    total = int(db.exec(total_stmt).one() or 0)

    return rows, total


def _expand_session_context(
    db: DbSession,
    memories: list[ScoredMemory],
    window: int,
) -> list[RecallMessage]:
    """For each event hit with a source session, grab ±window messages
    around the event's source. Returns deduplicated messages in created_at
    order.
    """
    if not memories:
        return []

    session_ids = {
        sm.node.source_session_id
        for sm in memories
        if _type_str(sm.node) == NodeType.EVENT.value
        and sm.node.source_session_id is not None
    }
    if not session_ids:
        return []

    # Naive approach: for each session, pull the first (2 * window + 1) messages.
    # A more sophisticated version would anchor to a specific moment, but we
    # don't store message anchors on L3 events yet.
    seen: set[int] = set()
    out: list[RecallMessage] = []
    for sid in session_ids:
        stmt = (
            select(RecallMessage)
            .where(
                RecallMessage.session_id == sid,
                RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(RecallMessage.created_at)
            .limit(2 * window + 1)
        )
        for msg in db.exec(stmt):
            if msg.id not in seen:
                seen.add(msg.id)
                out.append(msg)
    out.sort(key=lambda m: m.created_at)
    return out

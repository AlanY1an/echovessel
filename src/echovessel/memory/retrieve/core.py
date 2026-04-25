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

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeEntity,
    CoreBlock,
    Entity,
    EntityAlias,
    RecallMessage,
)

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

# Regex for splitting the query into candidate alias tokens. Alias
# matching is case-sensitive exact (plan decision 4). We deliberately
# split on non-word characters across Unicode so CJK runs stay intact
# — ``re.split(r"\W+", "Scott黄逸扬")`` under ``re.UNICODE`` drops the
# CJK chars; the simpler rule here keeps every contiguous run that is
# NOT ASCII whitespace / common punctuation.
_QUERY_TOKEN_SEPARATORS = re.compile(r"[\s,.?!;:()\[\]{}<>/\"'\\]+")

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
    # L5 · non-zero when the node is linked to an entity whose alias
    # appeared in the query text. ``ENTITY_ANCHOR_BONUS_VALUE`` when
    # present, 0 otherwise — stored unweighted so logs / tests can
    # inspect the raw signal separately from the rerank weight.
    entity_anchor_bonus: float = 0.0


@dataclass(slots=True)
class RetrievalResult:
    """Full return of the retrieve pipeline."""

    core_blocks: list[CoreBlock]
    memories: list[ScoredMemory]
    # Context messages from L2, if any were expanded around hit events
    context_messages: list[RecallMessage]
    # L2 FTS fallback hits (if triggered)
    fts_fallback: list[RecallMessage]
    # Spec 5 · plan §6.3 force-load. Top-N L4 thoughts of the current
    # speaker, ranked by recency × importance — bypasses query
    # similarity so the persona always carries some background
    # awareness of who the speaker is, even when the current message
    # has no obvious topical anchor. Empty when ``retrieve()`` was
    # called with the default ``force_load_user_thoughts=0``.
    pinned_thoughts: list[ConceptNode] = field(default_factory=list)
    # v0.5 · plan §2.1 force-load for L4.thought[subject='persona'].
    # Sibling of ``pinned_thoughts`` but surfaces the persona's own
    # recent reflections so the ``# How you see yourself lately``
    # user-prompt section can replace the v0.4 ``# About yourself``
    # L1.self block (now deleted). Empty when the caller did not pass
    # ``force_load_persona_thoughts``.
    persona_thoughts: list[ConceptNode] = field(default_factory=list)


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


def load_core_blocks(db: DbSession, persona_id: str, user_id: str) -> list[CoreBlock]:
    """Load every core block that belongs to (persona_id, user_id).

    Returns both shared blocks (user_id NULL) and per-user blocks for this
    user. Ordered for prompt injection: persona -> user -> style. v0.5
    dropped ``self`` and ``relationship`` (plan §1); the legacy order is
    no longer in this list, and any leftover row carrying one of those
    labels is filtered by ``deleted_at IS NOT NULL`` upstream.
    """
    order = ["persona", "user", "style"]

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

    blocks.sort(key=lambda b: order.index(_label_str(b)) if _label_str(b) in order else 99)
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


def find_query_entities(
    db: DbSession,
    query_text: str,
    *,
    persona_id: str,
    user_id: str,
) -> list[int]:
    """Return entity ids whose alias exactly matches a token in ``query_text``.

    Alias matching is case-sensitive (plan decision 4 — normalisation
    is a v2 concern). Tokens are split on ASCII whitespace + common
    punctuation; the full ``query_text`` is also tried as a single
    token so multi-character aliases that cross token boundaries
    ("黄逸扬") still match a query like "Scott 最近怎么样" where the
    alias sits inside a whitespace-delimited fragment.

    Soft-deleted entities are skipped. An empty query or one whose
    tokens match no aliases returns ``[]``.
    """
    if not query_text or not query_text.strip():
        return []

    candidates = {tok for tok in _QUERY_TOKEN_SEPARATORS.split(query_text) if tok}
    # Also try every substring occurrence of stored aliases inside the
    # raw query (case-sensitive). Scans every alias for this scope once
    # — small on a personal deployment, and the alternative (trigram
    # scan across all aliases) would need its own index. If alias count
    # ever grows, the right move is a trigram index keyed on
    # (persona_id, user_id), not more Python loops.
    alias_rows = db.exec(
        select(EntityAlias, Entity)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(
            Entity.persona_id == persona_id,
            Entity.user_id == user_id,
            Entity.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()

    matched: set[int] = set()
    for alias, ent in alias_rows:
        if ent.id is None:
            continue
        if alias.alias in candidates or alias.alias in query_text:
            matched.add(ent.id)
    return list(matched)


def get_nodes_linked_to_entities(db: DbSession, entity_ids: list[int]) -> set[int]:
    """Return the set of ConceptNode ids junction-linked to any of
    ``entity_ids``. Empty input → empty set.
    """
    if not entity_ids:
        return set()
    rows = db.exec(
        select(ConceptNodeEntity.node_id).where(
            ConceptNodeEntity.entity_id.in_(entity_ids)  # type: ignore[union-attr]
        )
    ).all()
    return {int(r) for r in rows if r is not None}


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
    force_load_user_thoughts: int = 0,
    force_load_persona_thoughts: int = 0,
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

    # L5 · Entity anchor pre-compute. Resolve every alias that appears
    # in the query to its entity id, then collect the set of concept
    # nodes junction-linked to any of those entities. Used below to
    # bump rerank score for those nodes.
    query_entity_ids = find_query_entities(db, query_text, persona_id=persona_id, user_id=user_id)
    entity_anchored_node_ids = get_nodes_linked_to_entities(db, query_entity_ids)

    # Step 3: Load the full nodes and rerank. We UNION the vector hits
    # with any entity-anchored node ids so the Scott/黄逸扬 case still
    # surfaces nodes whose description does not contain the query
    # string (plan case 8). Vector distance for an entity-anchored node
    # that did NOT come from vector search is left at the orthogonal
    # default so the anchor bonus is the only thing driving it above
    # the relevance floor.
    anchored_only_ids = entity_anchored_node_ids - {h.concept_node_id for h in hits}
    if hits or anchored_only_ids:
        node_ids = [h.concept_node_id for h in hits] + list(anchored_only_ids)
        distance_by_id = {h.concept_node_id: h.distance for h in hits}

        nodes = list(
            db.exec(
                select(ConceptNode).where(
                    ConceptNode.id.in_(node_ids),  # type: ignore[union-attr]
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                    # Spec 5 · plan §6.2 step 2. Soft-deleted via supersede:
                    # never surface a node a newer one has replaced.
                    ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                )
            )
        )
        scored = [
            _score_node(
                n,
                # Anchored-only nodes use the orthogonal distance
                # sentinel so they clear the min_relevance floor only
                # via the anchor bonus path below.
                distance_by_id.get(n.id, 2.0),
                now,
                relational_bonus_weight=relational_bonus_weight,
                entity_anchored=(n.id in entity_anchored_node_ids),
            )
            for n in nodes
        ]
        # Drop candidates whose relevance is below the floor UNLESS the
        # entity anchor is active for them. The anchor is exactly the
        # escape hatch for cross-language alias recall where the
        # embedder sees zero overlap.
        scored = [
            sm for sm in scored if sm.relevance >= min_relevance or sm.entity_anchor_bonus > 0
        ]
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

    # Spec 5 · plan §6.3 force-load. Bypasses query similarity entirely
    # — we want the persona to ALWAYS know who it's talking to even
    # when the current message is "?" or "嗯". Default kwarg=0 leaves
    # the field empty for callers that don't care.
    already_returned = {sm.node.id for sm in top_memories if sm.node.id is not None}
    pinned_thoughts: list[ConceptNode] = []
    if force_load_user_thoughts > 0:
        # Drop ids that already appear in the rerank result so the
        # caller doesn't render the same thought twice.
        pinned_thoughts = _load_user_thoughts_force(
            db,
            persona_id=persona_id,
            user_id=user_id,
            limit=force_load_user_thoughts,
            now=now,
            exclude_ids=already_returned,
        )

    # v0.5 · plan §2.1 force-load persona's own reflections. Same
    # dedup treatment: exclude anything already surfaced by the
    # primary rerank so ``# How you see yourself lately`` and the main
    # memory list never render the same thought twice.
    persona_thoughts: list[ConceptNode] = []
    if force_load_persona_thoughts > 0:
        persona_thoughts = load_persona_thoughts_force(
            db,
            persona_id=persona_id,
            user_id=user_id,
            top_n=force_load_persona_thoughts,
            exclude_ids=already_returned,
        )

    return RetrievalResult(
        core_blocks=core_blocks,
        memories=top_memories,
        context_messages=context_messages,
        fts_fallback=fts_fallback,
        pinned_thoughts=pinned_thoughts,
        persona_thoughts=persona_thoughts,
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


@dataclass(slots=True)
class ConceptSearchHit:
    """One row from :func:`search_concept_nodes`.

    ``snippet`` is the FTS5 ``snippet()`` output for ``description`` —
    a short HTML fragment with ``<b>`` tags around the matched terms.
    Front-end is responsible for sanitising / rendering it (the only
    HTML we emit is ``<b>``, see Worker θ tracker).
    """

    node: ConceptNode
    snippet: str
    rank: float


def search_concept_nodes(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    query_text: str,
    node_types: tuple[NodeType, ...] | None = None,
    tag: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[ConceptSearchHit], int]:
    """FTS5 search over ``ConceptNode.description`` with optional filters.

    Returns ``(hits, total)``. ``total`` is the unfiltered match count
    so the admin search UI can render "showing X of Y".

    The query string is sanitised by the same
    ``_sanitize_fts5_query`` helper :class:`SQLiteBackend` uses for L2
    FTS — special characters (AND / OR / NEAR / parens / colons …)
    are wrapped in double quotes and treated as literals. An empty
    query returns ``([], 0)`` rather than running an unbounded scan.

    ``tag`` matches ``emotion_tags`` OR ``relational_tags`` JSON
    arrays. Worker θ scope: a single tag string, exact match (``LIKE
    '%"<tag>"%'`` against the JSON serialisation). The DB schema
    doesn't have a tag-junction table; if richer queries are ever
    needed, that's the time to introduce one.
    """

    from sqlalchemy import text as _text
    from sqlmodel import func as _func

    if not query_text or not query_text.strip():
        return [], 0

    # Reuse the proven helper — keep the sanitisation identical between
    # L2 and concept search so users see the same query-syntax behaviour.
    from echovessel.memory.backends.sqlite import SQLiteBackend

    safe_query = SQLiteBackend._sanitize_fts5_query(query_text)
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # SQLite trigram tokenizer can't match terms shorter than 3
    # characters — common case for CJK queries like "下雨" (2 chars).
    # When any token is too short, fall back to a plain ``LIKE`` scan
    # over ``concept_nodes.description``. Snippets are still produced,
    # just hand-rolled instead of via FTS5's ``snippet()``.
    stripped = query_text.strip()
    tokens = [t for t in stripped.replace('"', " ").split() if t]
    use_like_fallback = any(len(t) < 3 for t in tokens) or not tokens

    type_values: tuple[str, ...] = tuple(t.value for t in node_types) if node_types else ()

    type_clause = ""
    if type_values:
        # Build ``cn.type IN (:t0, :t1, ...)`` dynamically — the IN tuple
        # length is unknown so we expand at format time.
        placeholders = ", ".join(f":type_{i}" for i in range(len(type_values)))
        type_clause = f"AND cn.type IN ({placeholders})"

    tag_clause = ""
    if tag:
        # JSON arrays are stored as plain TEXT in SQLite. Match either
        # emotion_tags OR relational_tags containing the literal tag.
        # We wrap the search in quotes so partial overlaps don't match
        # (e.g. searching for "joy" won't match "joyful").
        tag_clause = "AND (cn.emotion_tags LIKE :tag_pat OR cn.relational_tags LIKE :tag_pat)"

    if use_like_fallback:
        # LIKE path — stable across CJK / short queries, no FTS5 trigram
        # length floor. Snippet is hand-rolled in
        # :func:`_build_like_snippet` below.
        like_pattern = f"%{tokens[0]}%" if tokens else "%"
        page_sql = _text(
            f"""
            SELECT cn.id, cn.description
            FROM concept_nodes AS cn
            WHERE cn.description LIKE :like_pat
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            ORDER BY cn.created_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        count_sql = _text(
            f"""
            SELECT COUNT(*)
            FROM concept_nodes AS cn
            WHERE cn.description LIKE :like_pat
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            """
        )
        params: dict = {
            "like_pat": like_pattern,
            "persona_id": persona_id,
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        }
    else:
        page_sql = _text(
            f"""
            SELECT
                cn.id,
                snippet(concept_nodes_fts, 0, '<b>', '</b>', '…', 16) AS snip,
                fts.rank
            FROM concept_nodes_fts AS fts
            JOIN concept_nodes AS cn ON cn.id = fts.rowid
            WHERE concept_nodes_fts MATCH :q
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            ORDER BY fts.rank
            LIMIT :limit OFFSET :offset
            """
        )
        count_sql = _text(
            f"""
            SELECT COUNT(*)
            FROM concept_nodes_fts AS fts
            JOIN concept_nodes AS cn ON cn.id = fts.rowid
            WHERE concept_nodes_fts MATCH :q
              AND cn.persona_id = :persona_id
              AND cn.user_id = :user_id
              AND cn.deleted_at IS NULL
              {type_clause}
              {tag_clause}
            """
        )
        params = {
            "q": safe_query,
            "persona_id": persona_id,
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        }

    for i, v in enumerate(type_values):
        params[f"type_{i}"] = v
    if tag:
        params["tag_pat"] = f'%"{tag}"%'

    page_rows = db.exec(page_sql, params=params).all()  # type: ignore[arg-type]
    count_row = db.exec(count_sql, params=params).first()  # type: ignore[arg-type]
    total = int(count_row[0]) if count_row else 0

    if not page_rows:
        return [], total

    # Hydrate ConceptNode rows in one query, preserving FTS rank order.
    node_ids = [r[0] for r in page_rows]
    node_map = {
        n.id: n
        for n in db.exec(
            select(ConceptNode).where(
                ConceptNode.id.in_(node_ids),  # type: ignore[union-attr]
            )
        )
    }

    hits: list[ConceptSearchHit] = []
    for row in page_rows:
        node = node_map.get(row[0])
        if node is None or node.deleted_at is not None:
            continue
        if use_like_fallback:
            snippet = _build_like_snippet(str(row[1] or ""), tokens[0] if tokens else "")
            rank = 0.0
        else:
            snippet = str(row[1] or "")
            rank = float(row[2])
        hits.append(ConceptSearchHit(node=node, snippet=snippet, rank=rank))

    # Suppress unused symbol warning — keep the helper around for future
    # consumers that want raw counts.
    _ = _func
    return hits, total


def _build_like_snippet(description: str, term: str, *, window: int = 28) -> str:
    """Hand-roll an FTS5-like snippet for the LIKE-fallback path.

    Surrounds the first occurrence of ``term`` with ``<b>…</b>`` and
    truncates with ``…`` on either side so the result fits one line in
    the admin search UI. Term comparison is case-insensitive on ASCII
    but exact for non-ASCII (Python ``str.lower`` over CJK is identity,
    which matches what users expect — searching "下雨" should not match
    a different unicode-equivalent form).
    """

    if not term or not description:
        return description
    haystack_lower = description.lower()
    term_lower = term.lower()
    idx = haystack_lower.find(term_lower)
    if idx < 0:
        return description

    end = idx + len(term)
    left = max(0, idx - window)
    right = min(len(description), end + window)

    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(description) else ""
    head = description[left:idx]
    match = description[idx:end]
    tail = description[end:right]
    return f"{prefix}{head}<b>{match}</b>{tail}{suffix}"


def list_concept_nodes(
    db: DbSession,
    persona_id: str,
    user_id: str,
    *,
    node_type: NodeType,
    limit: int = 20,
    offset: int = 0,
    subject: str | None = None,
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
        subject: v0.5 hotfix · optional filter on ``ConceptNode.subject``
            (``'user'`` / ``'persona'`` / ``'shared'``). ``None`` keeps
            the legacy behaviour (no filter). Used by the admin
            Persona tab to surface only ``subject='persona'`` thoughts
            for the Reflection section.

    Returns:
        ``(rows, total)`` where ``rows`` is a list of ConceptNode in
        DESCENDING created_at order and ``total`` is the unfiltered
        non-deleted count for the same persona/user/node_type triple.
    """

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    base_filters: tuple = (
        ConceptNode.persona_id == persona_id,
        ConceptNode.user_id == user_id,
        ConceptNode.type == node_type,
        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    if subject is not None:
        base_filters = (*base_filters, ConceptNode.subject == subject)

    page_stmt = (
        select(ConceptNode)
        .where(*base_filters)
        .order_by(ConceptNode.created_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    rows = list(db.exec(page_stmt).all())

    from sqlmodel import func as _func

    total_stmt = select(_func.count()).select_from(ConceptNode).where(*base_filters)
    total = int(db.exec(total_stmt).one() or 0)

    return rows, total


# ---------------------------------------------------------------------------
# Spec 5 · force-load user thoughts (plan §6.3)
# ---------------------------------------------------------------------------


def _load_user_thoughts_force(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    limit: int,
    now: datetime,
    exclude_ids: set[int] | None = None,
) -> list[ConceptNode]:
    """Top-N L4 thoughts for ``user_id``, ranked by recency × importance.

    Bypasses query similarity entirely — this is the force-load path
    that lives behind ``# About {speaker}`` in the user prompt. The
    intention is "who is this person, regardless of what they just
    typed". Filters: not soft-deleted, not superseded, optionally
    excludes ids already surfaced by the main retrieve rerank.

    v0.5 · this helper explicitly targets ``subject != 'persona'`` so
    it doesn't shadow :func:`load_persona_thoughts_force`, which
    surfaces the persona's own reflections (now written by slow_cycle
    instead of appended to the deleted L1.self block).
    """
    if limit <= 0:
        return []

    rows = list(
        db.exec(
            select(ConceptNode).where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.subject != "persona",
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    if exclude_ids:
        rows = [n for n in rows if n.id not in exclude_ids]

    def _score(n: ConceptNode) -> float:
        return _recency_score(n.created_at, now) * _impact_score(n.emotional_impact)

    rows.sort(key=_score, reverse=True)
    return rows[:limit]


def load_persona_thoughts_force(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    top_n: int = 5,
    exclude_ids: set[int] | None = None,
) -> list[ConceptNode]:
    """Return top-N ``subject='persona'`` thoughts by recency.

    v0.5 · plan §2.1. Sibling of :func:`_load_user_thoughts_force` but
    surfaces the persona's own introspection (written by slow_cycle /
    reflection fast-loop, never by owner). Powers the
    ``# How you see yourself lately`` user-prompt section that
    replaced the v0.4 ``# About yourself`` L1.self block.

    Ranking is strictly recency (not recency × importance like the
    user-thoughts sibling) because a persona-authored reflection
    rarely carries a reliable emotional_impact magnitude — the LLM
    writes them mostly in neutral tone, so impact would collapse the
    score down to zero for every row.

    ``exclude_ids`` should carry any ids already surfaced by the
    primary rerank so the caller doesn't render the same thought
    twice. Soft-deleted / superseded rows are always filtered.
    """
    if top_n <= 0:
        return []

    stmt = (
        select(ConceptNode)
        .where(
            ConceptNode.persona_id == persona_id,
            ConceptNode.user_id == user_id,
            ConceptNode.type == NodeType.THOUGHT.value,
            ConceptNode.subject == "persona",
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ConceptNode.created_at.desc())  # type: ignore[attr-defined]
        .limit(top_n * 2)
    )
    nodes = list(db.exec(stmt))
    if exclude_ids:
        nodes = [n for n in nodes if n.id not in exclude_ids]
    return nodes[:top_n]


# ---------------------------------------------------------------------------
# Event time anchor (R4 · plan §6.3 status derivation)
# ---------------------------------------------------------------------------


def derive_event_status(node: ConceptNode, now: datetime) -> str:
    """Status of an L3 event with respect to a reference moment.

    Output is one of: ``'past'``, ``'active'``, ``'planned'``,
    ``'atemporal'``. Atemporal applies when the node has no time bounds
    at all — a fact like "user likes cats" has no status delta. When
    only one of start/end is set, the missing bound is treated as the
    other (instant event).
    """
    start = node.event_time_start
    end = node.event_time_end
    if start is None and end is None:
        return "atemporal"
    if start is None:
        start = end
    if end is None:
        end = start
    if end < now:
        return "past"
    if start > now:
        return "planned"
    return "active"


def render_event_delta_phrase(node: ConceptNode, now: datetime) -> str:
    """Human-readable delta clause for ``# Things you remember`` rendering.

    Day-precision intentionally — hour-precision reads as a database
    cursor, not a friend remembering. Returns the empty string for
    atemporal events so the caller can append it unconditionally.

    Format: ``" · event YYYY-MM-DD~YYYY-MM-DD · status=X (N days …)"``.
    Single-day events render the date once.
    """
    status = derive_event_status(node, now)
    if status == "atemporal":
        return ""

    start = node.event_time_start or node.event_time_end
    end = node.event_time_end or node.event_time_start
    if start is None or end is None:
        return ""

    start_d = start.date()
    end_d = end.date()
    today = now.date()
    when = start_d.isoformat() if start_d == end_d else f"{start_d.isoformat()}~{end_d.isoformat()}"

    if status == "past":
        days_ago = (today - end_d).days
        suffix = (
            "today" if days_ago == 0 else "1 day ago" if days_ago == 1 else f"{days_ago} days ago"
        )
        return f" · event {when} · status=past ({suffix})"
    if status == "planned":
        days_until = (start_d - today).days
        suffix = (
            "today"
            if days_until == 0
            else "in 1 day"
            if days_until == 1
            else f"in {days_until} days"
        )
        return f" · event {when} · status=planned ({suffix})"
    # active
    days_in = max((today - start_d).days, 0)
    suffix = (
        "just started" if days_in == 0 else "1 day in" if days_in == 1 else f"{days_in} days in"
    )
    return f" · event {when} · status=active ({suffix})"


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
        if _type_str(sm.node) == NodeType.EVENT.value and sm.node.source_session_id is not None
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

"""L5 · Entity resolution with three-level dedup (R5 · plan decision 4).

Resolution flow per extracted entity:

    Level 1 — alias exact match
        Look up every surface name (canonical + aliases) in
        ``entity_aliases``. Any hit within this (persona, user) reuses the
        matched entity and merges in any new aliases we haven't seen.

    Level 2 — canonical-embedding cosine similarity
        Embed ``canonical_name . description``, search ``entities_vec``
        for the nearest neighbour in this (persona, user).

            cosine > 0.85   → auto-merge (status='confirmed')
            0.65 < cosine   → create a NEW entity with
                              ``merge_status='uncertain'`` and
                              ``merge_target_id`` pointing at the
                              near-miss candidate. The ask-user prompt
                              hint (plan §6.3.1) surfaces the ambiguity
                              at retrieve time.
            else            → Level 3

    Level 3 — brand-new entity
        Insert with ``merge_status='confirmed'``.

Thresholds live as module constants so dogfood can dial them up/down
without a code review. Alias matching is CASE-SENSITIVE exact equality
— normalisation (lowercase / whitespace collapse / transliteration) is
deferred to a v2 dogfood round.

This module has no LLM imports. The embed function is injected by the
caller (runtime constructs it from sentence-transformers; consolidate
threads it through).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeEntity,
    Entity,
    EntityAlias,
)
from echovessel.memory.observers import ENTITY_DESCRIPTION_SOURCES, _fire_lifecycle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds (plan §6.2 Level 2 · decision 4)
# ---------------------------------------------------------------------------

# Cosine above this → auto-merge (Level 2 high confidence).
EMBEDDING_MERGE_THRESHOLD_HIGH: float = 0.85

# Cosine above this but below HIGH → create new entity marked 'uncertain'
# and surface the ambiguity to the user (Level 3 ask-user).
EMBEDDING_MERGE_THRESHOLD_LOW: float = 0.65

# How many nearest candidates to pull from entities_vec before picking the
# best match. Small number — dedup is per-session so we only need a
# handful of plausible hits.
VECTOR_CANDIDATE_K: int = 5


# Mention dedup (plan §6.2 step 3): if a newly-extracted event matches an
# existing event above this cosine AND was observed in the last
# MENTION_DEDUP_WINDOW_DAYS, bump mention_count on the existing node
# instead of inserting a duplicate.
MENTION_DEDUP_COSINE_THRESHOLD: float = 0.85
MENTION_DEDUP_WINDOW_DAYS: int = 30
MENTION_DEDUP_CANDIDATE_K: int = 3


# ---------------------------------------------------------------------------
# Data classes (return types)
# ---------------------------------------------------------------------------

# Resolution path labels — caller uses these for logging / metrics only.
ResolutionPath = str
"""One of: 'alias_match' | 'embedding_high' | 'embedding_low' | 'new'."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_entity(
    db: DbSession,
    backend: StorageBackend,
    embed_fn: Callable[[str], list[float]],
    *,
    persona_id: str,
    user_id: str,
    canonical_name: str,
    aliases: list[str] | None = None,
    kind: str = "person",
    description: str | None = None,
) -> tuple[int, ResolutionPath]:
    """Resolve one extracted entity to an ``entities.id``.

    Returns a tuple of (entity_id, resolution_path). The caller is
    expected to wrap this in its own unit-of-work transaction — we
    commit internally because new aliases must be visible to the next
    resolve_entity call in the same session (otherwise a session that
    mentions "Scott" twice would create two rows).

    The three-level fallback logic is spelled out in the module
    docstring. Level 1 and the "new entity" branch always leave the
    entity with ``merge_status='confirmed'``. Level 2 mid-confidence
    creates a new entity but marks it ``'uncertain'`` + stores the
    candidate ``merge_target_id`` so the ask-user prompt hint can
    surface it in a future turn.
    """
    alias_list = [a for a in (aliases or []) if a and a != canonical_name]
    all_names = [canonical_name] + alias_list

    # ------------------------------------------------------------------
    # Level 1: alias exact match (case-sensitive · plan decision 4)
    # ------------------------------------------------------------------
    for name in all_names:
        matched = _find_entity_by_alias(db, alias=name, persona_id=persona_id, user_id=user_id)
        if matched is not None:
            _add_aliases(db, entity_id=matched.id, new_aliases=all_names)
            _fire_lifecycle("on_entity_confirmed", matched)
            return matched.id, "alias_match"

    # ------------------------------------------------------------------
    # Level 2: canonical-embedding cosine search
    # ------------------------------------------------------------------
    embed_input = canonical_name
    if description:
        embed_input = f"{canonical_name}. {description}"
    new_embedding = embed_fn(embed_input)

    candidates = backend.vec_search_entities(
        query_embedding=new_embedding,
        persona_id=persona_id,
        user_id=user_id,
        top_k=VECTOR_CANDIDATE_K,
    )
    best_candidate_id: int | None = None
    best_cosine: float = -1.0
    for cand_id, distance in candidates:
        cosine = _distance_to_cosine(distance)
        if cosine > best_cosine:
            best_cosine = cosine
            best_candidate_id = cand_id

    if best_candidate_id is not None and best_cosine > EMBEDDING_MERGE_THRESHOLD_HIGH:
        # High confidence — merge into existing, add new aliases.
        _add_aliases(db, entity_id=best_candidate_id, new_aliases=all_names)
        matched = db.get(Entity, best_candidate_id)
        if matched is not None:
            _fire_lifecycle("on_entity_confirmed", matched)
        return best_candidate_id, "embedding_high"

    if best_candidate_id is not None and best_cosine > EMBEDDING_MERGE_THRESHOLD_LOW:
        # Mid-confidence — create new entity but flag ambiguity.
        entity = _create_entity(
            db,
            persona_id=persona_id,
            user_id=user_id,
            canonical_name=canonical_name,
            kind=kind,
            description=description,
            merge_status="uncertain",
            merge_target_id=best_candidate_id,
        )
        backend.insert_entity_vector(entity.id, new_embedding, conn=db.connection())
        _add_aliases(db, entity_id=entity.id, new_aliases=all_names)
        _fire_lifecycle("on_entity_confirmed", entity)
        return entity.id, "embedding_low"

    # ------------------------------------------------------------------
    # Level 3 / default: brand-new entity.
    # ------------------------------------------------------------------
    entity = _create_entity(
        db,
        persona_id=persona_id,
        user_id=user_id,
        canonical_name=canonical_name,
        kind=kind,
        description=description,
        merge_status="confirmed",
        merge_target_id=None,
    )
    backend.insert_entity_vector(entity.id, new_embedding, conn=db.connection())
    _add_aliases(db, entity_id=entity.id, new_aliases=all_names)
    _fire_lifecycle("on_entity_confirmed", entity)
    return entity.id, "new"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_entity_by_alias(
    db: DbSession, *, alias: str, persona_id: str, user_id: str
) -> Entity | None:
    """Find the entity whose alias exactly matches ``alias`` in scope.

    Composite PK (alias, entity_id) lets the same alias point at more
    than one entity during an ambiguous period — we pick the first
    confirmed match for this (persona, user). Soft-deleted entities are
    excluded.
    """
    stmt = (
        select(Entity)
        .join(EntityAlias, EntityAlias.entity_id == Entity.id)
        .where(
            EntityAlias.alias == alias,
            Entity.persona_id == persona_id,
            Entity.user_id == user_id,
            Entity.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    )
    return db.exec(stmt).first()


def _add_aliases(db: DbSession, *, entity_id: int, new_aliases: list[str]) -> None:
    """Append new aliases to an entity, skipping duplicates.

    Commits so the rows are visible to the next ``resolve_entity`` in
    the same consolidate batch. INSERT OR IGNORE semantics are
    implemented client-side because SQLModel's unique-insert helper
    doesn't compose cleanly with a composite PK.
    """
    unique = {a for a in new_aliases if a}
    if not unique:
        return

    existing = {
        row.alias
        for row in db.exec(
            select(EntityAlias).where(
                EntityAlias.entity_id == entity_id,
                EntityAlias.alias.in_(list(unique)),  # type: ignore[union-attr]
            )
        )
    }
    for alias in unique - existing:
        db.add(EntityAlias(alias=alias, entity_id=entity_id))
    db.commit()


def _create_entity(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    canonical_name: str,
    kind: str,
    description: str | None,
    merge_status: str,
    merge_target_id: int | None,
) -> Entity:
    """Insert a new Entity row and commit so the id is stable."""
    entity = Entity(
        persona_id=persona_id,
        user_id=user_id,
        canonical_name=canonical_name,
        kind=kind,
        description=description,
        merge_status=merge_status,
        merge_target_id=merge_target_id,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def _distance_to_cosine(distance: float) -> float:
    """Convert an sqlite-vec distance to a cosine similarity in [0, 1].

    sqlite-vec's vec0 virtual table returns L2 distance by default for
    unit-normed embeddings, which maps to cosine via ``1 - d/2``. This
    matches the convention already used by
    ``echovessel.memory.retrieve._relevance_score`` — keep them in
    lockstep so the threshold values in plan decision 4 have the same
    meaning across files.
    """
    return max(0.0, min(1.0, 1.0 - (distance / 2.0)))


def detect_mention_dedup(
    db: DbSession,
    backend: StorageBackend,
    embed_fn: Callable[[str], list[float]],
    *,
    persona_id: str,
    user_id: str,
    new_event_descriptions: list[str],
    now: datetime,
    cosine_threshold: float = MENTION_DEDUP_COSINE_THRESHOLD,
    days_window: int = MENTION_DEDUP_WINDOW_DAYS,
) -> dict[int, int]:
    """Match each new event description against recent existing events.

    Returns a mapping ``{new_event_index → existing_concept_node_id}`` —
    only indices that found a match are present. Caller uses this to
    decide whether to INSERT a new row or to increment ``mention_count``
    on the matched row.

    The window is anchored on ``now`` (not event_time) because
    mention_count tracks how often the persona heard about this fact
    recently, not when the fact occurred. A 30-day window keeps the
    dedup from collapsing the user's "same job" mentions across years.
    """
    matches: dict[int, int] = {}
    if not new_event_descriptions:
        return matches
    cutoff = now - timedelta(days=days_window)

    for new_idx, desc in enumerate(new_event_descriptions):
        if not desc:
            continue
        embedding = embed_fn(desc)
        candidates = backend.vector_search(
            query_embedding=embedding,
            persona_id=persona_id,
            user_id=user_id,
            types=(NodeType.EVENT.value,),
            top_k=MENTION_DEDUP_CANDIDATE_K,
        )
        for hit in candidates:
            cosine = _distance_to_cosine(hit.distance)
            if cosine < cosine_threshold:
                break
            node = db.exec(
                select(ConceptNode).where(
                    ConceptNode.id == hit.concept_node_id,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            ).one_or_none()
            if node is None:
                continue
            if node.created_at < cutoff:
                continue
            matches[new_idx] = node.id
            break
    return matches


def add_concept_entity_link(db: DbSession, *, node_id: int, entity_id: int) -> None:
    """Create a ConceptNodeEntity junction row if it doesn't already exist.

    The composite PK (node_id, entity_id) means a duplicate INSERT would
    raise; we look first. Repeated mentions of the same entity in the
    same event hit this path — one junction row, not N.
    """
    existing = db.exec(
        select(ConceptNodeEntity).where(
            ConceptNodeEntity.node_id == node_id,
            ConceptNodeEntity.entity_id == entity_id,
        )
    ).first()
    if existing is not None:
        return
    db.add(ConceptNodeEntity(node_id=node_id, entity_id=entity_id))


def apply_entity_clarification(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    canonical_a: str,
    canonical_b: str,
    same: bool,
) -> tuple[int | None, str]:
    """Resolve a user-stated entity clarification (plan §6.3.1 · decision 4 L3).

    When the user confirms A and B are the same person, the entity
    whose ``merge_status='uncertain'`` is merged into the other one:
    its aliases migrate, the winner keeps canonical_name and
    merge_status flips to ``'confirmed'``.

    When the user says they are different people, the uncertain entity
    is split out — merge_status → ``'disambiguated'``, merge_target_id
    cleared.

    Returns (affected_entity_id, outcome) where outcome is one of
    'merged' | 'disambiguated' | 'noop'. 'noop' covers ambiguous
    clarifications (both entities are 'confirmed' already, or neither
    canonical matched any entity row) — safe to ignore.
    """
    by_canonical = {
        e.canonical_name: e
        for e in db.exec(
            select(Entity).where(
                Entity.persona_id == persona_id,
                Entity.user_id == user_id,
                Entity.canonical_name.in_([canonical_a, canonical_b]),  # type: ignore[union-attr]
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    }
    a = by_canonical.get(canonical_a)
    b = by_canonical.get(canonical_b)
    if a is None or b is None:
        log.info(
            "entity_clarification: unknown canonical(s) %r / %r — skipping",
            canonical_a,
            canonical_b,
        )
        return None, "noop"

    # Pick the uncertain one (loser); if both confirmed, nothing to
    # clarify. If both uncertain (rare), drop the one with the newer
    # created_at so the first-seen canonical wins.
    uncertain = [e for e in (a, b) if e.merge_status == "uncertain"]
    if not uncertain:
        return None, "noop"
    if len(uncertain) == 2:
        uncertain.sort(key=lambda e: e.created_at, reverse=True)
    loser = uncertain[0]
    winner = b if loser.id == a.id else a

    if same:
        # Merge loser → winner: migrate aliases, soft-delete loser.
        loser_aliases = [
            row.alias
            for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == loser.id))
        ]
        _add_aliases(
            db,
            entity_id=winner.id,
            new_aliases=loser_aliases + [loser.canonical_name],
        )
        # Migrate concept_node_entities junction rows from loser → winner
        # so retrieve still finds them. Skip rows that would collide.
        winner_links = {
            r.node_id
            for r in db.exec(
                select(ConceptNodeEntity).where(ConceptNodeEntity.entity_id == winner.id)
            )
        }
        loser_links = list(
            db.exec(select(ConceptNodeEntity).where(ConceptNodeEntity.entity_id == loser.id))
        )
        for link in loser_links:
            if link.node_id in winner_links:
                db.delete(link)
            else:
                link.entity_id = winner.id
                db.add(link)
        # Remove loser's alias rows so they don't ambiguously resolve
        # back to the soft-deleted row on Level 1 lookups.
        for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == loser.id)):
            db.delete(row)
        loser.deleted_at = datetime.now()
        winner.merge_status = "confirmed"
        winner.merge_target_id = None
        db.add(winner)
        db.add(loser)
        db.commit()
        db.refresh(winner)
        _fire_lifecycle("on_entity_confirmed", winner)
        return winner.id, "merged"

    # Not the same — split cleanly.
    loser.merge_status = "disambiguated"
    loser.merge_target_id = None
    db.add(loser)
    db.commit()
    return loser.id, "disambiguated"


def update_entity_description(
    db: DbSession,
    *,
    entity_id: int,
    description: str,
    source: str,
) -> Entity | None:
    """Write `description` to an Entity row, commit, fire hook.

    `source` is one of `ENTITY_DESCRIPTION_SOURCES` — `'slow_tick'` for
    the slow_cycle synthesizer (plan §2.2 · triggers when
    `linked_events_count` crosses threshold) or `'owner'` for operator
    writes via `PATCH /api/admin/memory/entities/{id}`.

    Returns the refreshed Entity, or None if the id does not exist or
    is soft-deleted. The observer hook fires only on a successful write.
    """
    if source not in ENTITY_DESCRIPTION_SOURCES:
        raise ValueError(
            f"update_entity_description: source must be one of "
            f"{ENTITY_DESCRIPTION_SOURCES!r}, got {source!r}"
        )
    entity = db.get(Entity, entity_id)
    if entity is None or entity.deleted_at is not None:
        return None
    entity.description = description
    db.add(entity)
    db.commit()
    db.refresh(entity)
    _fire_lifecycle("on_entity_description_updated", entity, source)
    return entity


__all__ = [
    "EMBEDDING_MERGE_THRESHOLD_HIGH",
    "EMBEDDING_MERGE_THRESHOLD_LOW",
    "MENTION_DEDUP_COSINE_THRESHOLD",
    "MENTION_DEDUP_WINDOW_DAYS",
    "VECTOR_CANDIDATE_K",
    "add_concept_entity_link",
    "apply_entity_clarification",
    "detect_mention_dedup",
    "resolve_entity",
    "update_entity_description",
]

"""Phases B / C / E machinery — extraction-side helpers + reflection gates.

This module owns the smaller helpers that ``consolidate_session``
calls into:

- Phase B (extraction): ``_fallback_source_turn_id`` derives a soft
  provenance hint when the LLM omits one; ``_consolidate_entities``
  resolves L5 entities and wires the L3↔L5 junction with the
  defensive surface-form filter.
- Phase C (SHOCK): the threshold constant ``SHOCK_IMPACT_THRESHOLD``
  lives here so SHOCK detection in ``consolidate_session`` reads as
  a single-line ``abs(impact) >= SHOCK_IMPACT_THRESHOLD`` check.
- Phase D (TIMER) gating: ``_is_timer_due`` and
  ``_count_reflections_24h``.
- Phase E (reflection): ``_load_reflection_inputs`` gathers candidate
  events; the ``REFLECTION_HARD_LIMIT_24H`` cap lives here.

The orchestration (which phase fires when, sequencing of writes,
session state transitions) stays in ``consolidate.core`` —
``consolidate_session`` is the only call site for everything below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import NodeType
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    RecallMessage,
    Session,
)

if TYPE_CHECKING:
    from echovessel.memory.consolidate.core import EmbedFn, ExtractionResult

log = logging.getLogger(__name__)


# SHOCK reflection threshold (single event |impact| >= this)
SHOCK_IMPACT_THRESHOLD = 8

# TIMER reflection cadence
TIMER_REFLECTION_HOURS = 24

# Hard gate: no more than this many reflections per rolling 24h window
REFLECTION_HARD_LIMIT_24H = 3


def _count_reflections_24h(db: DbSession, persona_id: str, user_id: str, now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    rows = list(
        db.exec(
            select(ConceptNode).where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.created_at > cutoff,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    return len(rows)


def _is_timer_due(db: DbSession, persona_id: str, user_id: str, now: datetime) -> bool:
    cutoff = now - timedelta(hours=TIMER_REFLECTION_HOURS)
    last = db.exec(
        select(ConceptNode)
        .where(
            ConceptNode.persona_id == persona_id,
            ConceptNode.user_id == user_id,
            ConceptNode.type == NodeType.THOUGHT.value,
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ConceptNode.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).one_or_none()

    if last is None:
        # No prior reflection — allow TIMER on first extraction, but only
        # if there's a reasonable window of events to reflect on. Architecture
        # says TIMER = "every 24h or so", so for the very first session we
        # still allow it.
        return True
    return last.created_at < cutoff


def _fallback_source_turn_id(messages: list[RecallMessage]) -> str | None:
    """Return the turn_id of the latest user message in `messages` that has one.

    Used when the extraction prompt emits an event without a
    `source_turn_id` hint. Review R2 says extraction is per-session, so
    any single turn is a coarse approximation — we pick the most recent
    one as a "centre of gravity" for downstream audit queries. If no
    user message has a turn_id (legacy data / tests that construct
    RecallMessages without one), return None — that's fine because
    `source_turn_id` is nullable.
    """

    def _role_str(msg: RecallMessage) -> str:
        r = msg.role
        return getattr(r, "value", r)

    for msg in reversed(messages):
        if msg.turn_id and _role_str(msg) == "user":
            return msg.turn_id
    # Fall back to any message with a turn_id (persona reply will share
    # turn_id with the user turn it answered, so this still yields a
    # reasonable anchor).
    for msg in reversed(messages):
        if msg.turn_id:
            return msg.turn_id
    return None


def _load_reflection_inputs(
    db: DbSession, persona_id: str, user_id: str, now: datetime
) -> list[ConceptNode]:
    """Gather the events the reflector should consider.

    MVP: recent ~10 events from the last 24h. v1.x can add priority by impact.
    """
    cutoff = now - timedelta(hours=24)
    stmt = (
        select(ConceptNode)
        .where(
            ConceptNode.persona_id == persona_id,
            ConceptNode.user_id == user_id,
            ConceptNode.type == NodeType.EVENT.value,
            ConceptNode.created_at > cutoff,
            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(ConceptNode.created_at.desc())  # type: ignore[union-attr]
        .limit(10)
    )
    return list(db.exec(stmt))


def _consolidate_entities(
    db: DbSession,
    backend: StorageBackend,
    embed_fn: EmbedFn,
    *,
    session: Session,
    extraction_output: ExtractionResult,
    event_by_ext_idx: dict[int, ConceptNode],
    junction_writes_out: list[dict] | None = None,
    junction_rejects_out: list[dict] | None = None,
    entities_resolved_out: list[dict] | None = None,
) -> None:
    """Resolve every mentioned entity + wire L3↔L5 junctions + apply
    any user-stated entity clarification. Kept as a helper so the B
    phase stays readable and so merge conflicts with other specs touching
    consolidate stay local to this function.

    Optional ``*_out`` lists receive observability rows — the dev-mode
    consolidate tracer passes in pre-allocated lists so phase_b can
    render the "which junctions did we write, which did we reject, and
    why?" breakdown in the drawer. Passing ``None`` (the default) makes
    the helper behave identically to its pre-Spec-4 form.
    """
    from echovessel.memory.entities import (
        add_concept_entity_link,
        apply_entity_clarification,
        resolve_entity,
    )

    entity_id_by_ext_idx: dict[int, int] = {}
    for ent_idx, ext_ent in enumerate(extraction_output.mentioned_entities):
        try:
            entity_id, _path = resolve_entity(
                db,
                backend,
                embed_fn,
                persona_id=session.persona_id,
                user_id=session.user_id,
                canonical_name=ext_ent.canonical_name,
                aliases=ext_ent.aliases,
                kind=ext_ent.kind,
            )
        except Exception as e:  # noqa: BLE001
            # Resolution failures (embedding NaN, bad schema data) must
            # not abort the whole consolidate run — events have already
            # committed. Log and drop this entity from junction wiring.
            log.warning(
                "resolve_entity failed for %r (session %s): %s",
                ext_ent.canonical_name,
                session.id,
                e,
            )
            continue
        entity_id_by_ext_idx[ent_idx] = entity_id
        if entities_resolved_out is not None:
            entities_resolved_out.append(
                {
                    "canonical_name": ext_ent.canonical_name,
                    "entity_id": entity_id,
                    "dedup_path": _path,
                }
            )

    # Junction rows: one per (event_id, entity_id) pair.
    #
    # Defensive filter: the extraction LLM has been observed to over-link
    # — when a session mentions Scott in chat but one of the extracted
    # events is about an unrelated exam, the LLM would sometimes emit
    # ``in_events=[exam_event_idx]`` for the Scott entity. A wrong link
    # then poisons alias-anchor retrieval: asking "how's Scott" pulls in
    # the exam event and the reply drifts into "Scott is busy studying"
    # even though Scott is not in that event at all. We enforce the rule
    # the prompt now asks for: the entity's canonical_name or at least
    # one of its aliases must appear LITERALLY in the event's description
    # text before we accept the junction row.
    for ent_idx, ext_ent in enumerate(extraction_output.mentioned_entities):
        entity_id = entity_id_by_ext_idx.get(ent_idx)
        if entity_id is None:
            continue
        surface_forms = [ext_ent.canonical_name, *ext_ent.aliases]
        for ev_idx in ext_ent.in_events:
            node = event_by_ext_idx.get(ev_idx)
            if node is None or node.id is None:
                continue
            description = node.description or ""
            if not any(form and form in description for form in surface_forms):
                log.info(
                    "entity-link rejected: %r not in event %s description %r "
                    "(session %s, prompt under-constrained entity.in_events)",
                    ext_ent.canonical_name,
                    node.id,
                    description[:60],
                    session.id,
                )
                if junction_rejects_out is not None:
                    junction_rejects_out.append(
                        {
                            "node_id": node.id,
                            "entity_id": entity_id,
                            "canonical_name": ext_ent.canonical_name,
                            "reason": "surface_form_not_in_description",
                        }
                    )
                continue
            add_concept_entity_link(db, node_id=node.id, entity_id=entity_id)
            if junction_writes_out is not None:
                junction_writes_out.append(
                    {
                        "node_id": node.id,
                        "entity_id": entity_id,
                        "canonical_name": ext_ent.canonical_name,
                    }
                )
    db.commit()

    # User-stated clarification from this session (plan §6.3.1). Safe to
    # run after junction writes — merge/disambiguate only re-points
    # existing junction rows.
    clarification = extraction_output.entity_clarification
    if clarification is not None:
        try:
            apply_entity_clarification(
                db,
                persona_id=session.persona_id,
                user_id=session.user_id,
                canonical_a=clarification.canonical_a,
                canonical_b=clarification.canonical_b,
                same=clarification.same,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "apply_entity_clarification failed (session %s, %r vs %r): %s",
                session.id,
                clarification.canonical_a,
                clarification.canonical_b,
                e,
            )

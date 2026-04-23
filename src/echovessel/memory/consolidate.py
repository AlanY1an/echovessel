"""CONSOLIDATE pipeline — what happens when a session closes.

Per architecture v0.3 §3.3:

    A. Trivial judgement (messages<3 AND tokens<200 AND no strong-emotion keywords)
    B. Extraction (small-model single prompt with self-check)
    C. SHOCK reflection (|emotional_impact| >= 8 in any just-extracted event)
    D. TIMER reflection (> 24h since last reflection)
    E. Reflection execution (hard-gated: max 3 reflections per 24h)
    F. Session status -> 'closed'

This module does NOT call LLMs directly. LLM access is injected via
`ExtractFn`, `ReflectFn`, and `EmbedFn` callables so the memory module
stays decoupled from the LLM providers that live in `runtime/llm/`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import EventTime, NodeType, SessionStatus
from echovessel.memory.backend import StorageBackend
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    RecallMessage,
    Session,
)
from echovessel.memory.observers import MemoryEventObserver
from echovessel.memory.sessions import (
    drain_and_fire_pending_lifecycle_events,
    track_pending_session_closed,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (MVP defaults per architecture v0.3)
# ---------------------------------------------------------------------------


# Trivial skip thresholds
TRIVIAL_MESSAGE_COUNT = 3
TRIVIAL_TOKEN_COUNT = 200

# Strong-emotion keyword override for the trivial skip rule.
# Architecture §3.3 Part A calls this out explicitly: even trivial sessions
# must be extracted if they contain high-emotion signals, so that Proactive
# Policy can see "user sent one sad message at midnight and went silent".
# MVP is a small hardcoded Chinese+English list; v1.x can expand or use a
# lightweight classifier.
STRONG_EMOTION_KEYWORDS: tuple[str, ...] = (
    # Bereavement / loss
    "走了",
    "去世",
    "死了",
    "离世",
    "葬礼",
    "没了",
    "died",
    "passed away",
    "funeral",
    # Crisis
    "撑不住",
    "不想活",
    "活不下去",
    "自杀",
    "崩溃",
    "can't go on",
    "suicide",
    "breakdown",
    # Major milestones
    "分手",
    "离婚",
    "被裁",
    "breakup",
    "divorce",
    "fired",
)

# SHOCK reflection threshold (single event |impact| >= this)
SHOCK_IMPACT_THRESHOLD = 8

# TIMER reflection cadence
TIMER_REFLECTION_HOURS = 24

# Hard gate: no more than this many reflections per rolling 24h window
REFLECTION_HARD_LIMIT_24H = 3


# ---------------------------------------------------------------------------
# Callable protocol types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExtractedEvent:
    """Output of the extraction callable for a single event."""

    description: str
    emotional_impact: int
    emotion_tags: list[str] = field(default_factory=list)
    relational_tags: list[str] = field(default_factory=list)
    # v0.3 · optional soft provenance hint emitted by the extraction
    # prompt when it believes a given event is anchored in one specific
    # user turn within the session. Per review R2 this is purely a
    # tracking field — extraction remains per-session, not per-turn.
    source_turn_id: str | None = None
    # v0.4 · R4 time-binding. Resolved absolute window for the event,
    # carried from the prompt-layer ExtractedEvent. None means the
    # event is atemporal ("user likes cats") or the LLM declined to
    # resolve. consolidate writes start/end into ConceptNode columns.
    event_time: EventTime | None = None


@dataclass(slots=True)
class ExtractedEntity:
    """Third-party entity the user mentioned in the session (L5 · R5).

    Mirrors the prompts-layer ``RawExtractedEntity`` with memory-layer
    typing. ``in_events`` holds indices into the sibling ``events`` list;
    consolidate uses these to build the L3↔L5 junction rows.
    """

    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    kind: str = "person"
    in_events: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ExtractedEntityClarification:
    """User-stated resolution of an entity ambiguity (plan §6.3.1).

    Consolidate uses this to flip ``entities.merge_status`` from
    'uncertain' to 'confirmed' (``same=True`` → merge) or 'disambiguated'
    (``same=False`` → split and clear the candidate merge target).
    """

    canonical_a: str
    canonical_b: str
    same: bool


@dataclass(slots=True)
class ExtractedSessionMoodSignal:
    """L6 · persona-side mood snapshot (plan §5.3).

    Wraps the ``session_mood_signal`` the extraction LLM emits
    alongside events. Consolidate feeds this to
    :func:`echovessel.memory.episodic.update_episodic_state` before
    marking the session CLOSED — same single LLM call, no extra
    round-trip.
    """

    mood: str
    energy: int
    last_user_signal: str | None = None


@dataclass(slots=True)
class ExtractionResult:
    """Full output of one extraction LLM call.

    Wraps the event list together with L5 outputs (``mentioned_entities``,
    ``entity_clarification``) and the L6 mood snapshot
    (``session_mood_signal``) so extraction can emit everything in a
    single round trip. Callers that only care about events can ignore
    the other fields; tests can build an ``ExtractionResult(events=[...])``
    with defaults for everything else.
    """

    events: list[ExtractedEvent] = field(default_factory=list)
    mentioned_entities: list[ExtractedEntity] = field(default_factory=list)
    entity_clarification: ExtractedEntityClarification | None = None
    session_mood_signal: ExtractedSessionMoodSignal | None = None


@dataclass(slots=True)
class ExtractedThought:
    """Output of the reflection callable for a single thought."""

    description: str
    emotional_impact: int
    emotion_tags: list[str] = field(default_factory=list)
    relational_tags: list[str] = field(default_factory=list)
    # IDs of the ConceptNodes that this thought was generated from.
    # The reflect runner will create concept_node_filling rows for these.
    filling: list[int] = field(default_factory=list)
    # v0.3 · optional soft provenance hint for reflection output. Same
    # semantics as ExtractedEvent.source_turn_id.
    source_turn_id: str | None = None


# The injected LLM-facing callables. ExtractFn / ReflectFn are ASYNC because
# Runtime's LLM provider is async and owns the single asyncio event loop
# (docs/runtime/01-spec-v0.1.md §6.4 + §14 decision #1). Runtime constructs
# these closures and passes them into consolidate_session().
#
# Extraction reads a batch of raw messages, returns a structured result
# that bundles events with L5 side-outputs (mentioned_entities, optional
# entity_clarification). Keeping everything in one return value lets the
# prompt-layer emit all five sections in a single LLM round trip —
# prompts_wiring translates that into this shape.
ExtractFn = Callable[[list[RecallMessage]], Awaitable["ExtractionResult"]]

# Reflection reads recent ConceptNodes (events + prior thoughts) plus a
# reason string ('timer' or 'shock'), returns zero or more thoughts.
ReflectFn = Callable[[list[ConceptNode], str], Awaitable[list[ExtractedThought]]]

# Embedder turns text into a 384-dim vector. The memory module never
# imports sentence-transformers or anthropic directly. Kept SYNC because
# sentence-transformers itself is sync; runtime wraps it in asyncio.to_thread
# if the caller cares about blocking the loop.
EmbedFn = Callable[[str], list[float]]


# ---------------------------------------------------------------------------
# Trivial skip
# ---------------------------------------------------------------------------


def _has_strong_emotion(messages: list[RecallMessage]) -> bool:
    """Return True if any message contains a strong-emotion keyword."""
    for m in messages:
        content_lower = m.content.lower()
        for kw in STRONG_EMOTION_KEYWORDS:
            if kw.lower() in content_lower:
                return True
    return False


def is_trivial(
    session: Session,
    messages: list[RecallMessage],
    *,
    trivial_message_count: int = TRIVIAL_MESSAGE_COUNT,
    trivial_token_count: int = TRIVIAL_TOKEN_COUNT,
) -> bool:
    """Decide whether to skip extraction for this session.

    Returns True iff the session is below the message/token thresholds AND
    contains no strong-emotion keywords. Strong emotion always forces
    extraction even when the session is tiny (e.g. a single late-night line).

    The two threshold arguments default to the module-level constants so
    existing callers are behaviour-preserving. Runtime threads them from
    ``cfg.consolidate.trivial_message_count`` /
    ``cfg.consolidate.trivial_token_count`` via
    :class:`echovessel.runtime.consolidate_worker.ConsolidateWorker`.
    """
    if session.message_count >= trivial_message_count:
        return False
    if session.total_tokens >= trivial_token_count:
        return False
    return not _has_strong_emotion(messages)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConsolidateResult:
    session: Session
    skipped: bool
    events_created: list[ConceptNode]
    thoughts_created: list[ConceptNode]
    reflection_reason: str | None  # 'shock' | 'timer' | None


async def consolidate_session(
    db: DbSession,
    backend: StorageBackend,
    session: Session,
    extract_fn: ExtractFn,
    reflect_fn: ReflectFn,
    embed_fn: EmbedFn,
    now: datetime | None = None,
    *,
    observer: MemoryEventObserver | None = None,
    trivial_message_count: int = TRIVIAL_MESSAGE_COUNT,
    trivial_token_count: int = TRIVIAL_TOKEN_COUNT,
    reflection_hard_limit_24h: int = REFLECTION_HARD_LIMIT_24H,
) -> ConsolidateResult:
    """Run the full CONSOLIDATE pipeline on a session in 'closing' state.

    This is the only entry point for extracting events and producing
    reflections. It is safe to call on already-processed sessions (it will
    return a skipped=True result without side effects).

    v0.3: `observer` receives per-write notifications (on_event_created /
    on_thought_created) after each ConceptNode commits. Review R2 is
    enforced here: extraction stays per-session (one LLM call per
    session), `source_turn_id` on each emitted event/thought is purely a
    soft hint carried from the extraction prompt — it does NOT split
    extraction into per-turn groups.
    """
    now = now or datetime.now()

    if session.status == SessionStatus.CLOSED:
        return ConsolidateResult(
            session=session,
            skipped=True,
            events_created=[],
            thoughts_created=[],
            reflection_reason=None,
        )

    # Resume-point guard: if a prior attempt already committed the
    # extraction phase but failed before F (e.g. transient reflection
    # error), skip B on this run and load the persisted events for the
    # downstream SHOCK / reflection stages. See
    # `develop-docs/initiatives/_active/2026-04-consolidate-retry-safety/`.
    skip_extraction = session.extracted_events

    # --- Load messages --------------------------------------------------
    messages = list(
        db.exec(
            select(RecallMessage)
            .where(
                RecallMessage.session_id == session.id,
                RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(RecallMessage.created_at)
        )
    )

    # --- A. Trivial skip ------------------------------------------------
    # Only re-evaluate trivial on a fresh run; if extracted_events is
    # already set, the prior attempt decided this session was NOT trivial.
    if not skip_extraction and is_trivial(
        session,
        messages,
        trivial_message_count=trivial_message_count,
        trivial_token_count=trivial_token_count,
    ):
        session.status = SessionStatus.CLOSED
        session.trivial = True
        session.extracted = True
        session.extracted_at = now
        db.add(session)
        db.commit()
        db.refresh(session)
        # Round 4: fire `on_session_closed` strictly after the commit
        # that transitioned status → CLOSED.
        track_pending_session_closed(session)
        drain_and_fire_pending_lifecycle_events()
        return ConsolidateResult(
            session=session,
            skipped=True,
            events_created=[],
            thoughts_created=[],
            reflection_reason=None,
        )

    # --- B. Extraction --------------------------------------------------
    created_events: list[ConceptNode] = []
    extraction_output: ExtractionResult | None = None
    if skip_extraction:
        # Load already-committed events from the prior attempt. No LLM
        # call, no new rows, no new vectors — just rehydrate what B
        # already wrote so stages C / D / E can see them.
        created_events = list(
            db.exec(
                select(ConceptNode)
                .where(
                    ConceptNode.source_session_id == session.id,
                    ConceptNode.type == NodeType.EVENT,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(ConceptNode.created_at)
            )
        )
    else:
        if messages:
            extraction_output = await extract_fn(messages)
        else:
            extraction_output = ExtractionResult()
        extracted_events = extraction_output.events

        # L5 · mention dedup: match each new event description against
        # recent (<=30d) existing events; matches bump mention_count
        # instead of inserting a duplicate node (plan §6.2 step 3).
        from echovessel.memory.entities import detect_mention_dedup

        dedup_matches = detect_mention_dedup(
            db,
            backend,
            embed_fn,
            persona_id=session.persona_id,
            user_id=session.user_id,
            new_event_descriptions=[e.description for e in extracted_events],
            now=now,
        )

        # For entity-junction wiring we need the ConceptNode behind each
        # extracted-event index, whether it was freshly inserted or
        # matched a prior mention.
        event_by_ext_idx: dict[int, ConceptNode] = {}

        for ev_idx, ev in enumerate(extracted_events):
            # Review R2: per-session extraction is preserved. `source_turn_id`
            # is an OPTIONAL soft hint from the LLM — if missing, fall back
            # to the last user turn in the session that has a turn_id, so
            # downstream audit ("what turn did this come from?") still has
            # something to point at. If no message in the session has a
            # turn_id (e.g. legacy data), leave it None.
            effective_source_turn_id = ev.source_turn_id or _fallback_source_turn_id(messages)

            matched_node_id = dedup_matches.get(ev_idx)
            if matched_node_id is not None:
                existing = db.exec(
                    select(ConceptNode).where(ConceptNode.id == matched_node_id)
                ).one()
                existing.mention_count = (existing.mention_count or 0) + 1
                existing.last_accessed_at = now
                if effective_source_turn_id and effective_source_turn_id not in (
                    existing.source_turn_ids or []
                ):
                    existing.source_turn_ids = list(existing.source_turn_ids or []) + [
                        effective_source_turn_id
                    ]
                db.add(existing)
                event_by_ext_idx[ev_idx] = existing
                continue

            # R4 · resolved absolute window if the LLM produced one;
            # both bounds nullable independently. The `event_time_start
            # <= event_time_end` invariant is enforced by the parser
            # AND by a DB CHECK constraint on concept_nodes.
            event_time_start = ev.event_time.start if ev.event_time else None
            event_time_end = ev.event_time.end if ev.event_time else None
            node = ConceptNode(
                persona_id=session.persona_id,
                user_id=session.user_id,
                type=NodeType.EVENT,
                description=ev.description,
                emotional_impact=ev.emotional_impact,
                emotion_tags=ev.emotion_tags,
                relational_tags=ev.relational_tags,
                source_session_id=session.id,
                source_turn_id=effective_source_turn_id,
                source_turn_ids=([effective_source_turn_id] if effective_source_turn_id else []),
                event_time_start=event_time_start,
                event_time_end=event_time_end,
            )
            db.add(node)
            db.flush()
            created_events.append(node)
            event_by_ext_idx[ev_idx] = node

            # Embed + index into the vector table, joining the current
            # transaction so we don't deadlock against our own flushed
            # INSERT on concept_nodes (SQLite has a single writer).
            vec = embed_fn(ev.description)
            backend.insert_vector(node.id, vec, conn=db.connection())

        # Atomic: events + the resume-point flag commit together. If this
        # commit fails, neither the nodes nor the flag persist, and the
        # next retry re-enters this branch cleanly.
        session.extracted_events = True
        session.extracted_events_at = now
        db.add(session)
        db.commit()
        for n in created_events:
            db.refresh(n)

        # Post-commit observer notifications for created events — only on
        # the fresh-extraction path; the skip branch already fired these
        # on the prior attempt.
        if observer is not None and created_events:
            for n in created_events:
                try:
                    observer.on_event_created(n)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "observer.on_event_created raised (event id=%s): %s",
                        n.id,
                        e,
                    )

        # L5 · write_entities + junction (plan §6.2 step 1 · decision 4).
        # Happens post-event-commit so every ConceptNode.id we reference
        # in the junction is already persisted. A commit failure in the
        # entity branch leaves events intact — junction is rebuildable
        # from extraction output on the next run.
        if extraction_output.mentioned_entities or (
            extraction_output.entity_clarification is not None
        ):
            _consolidate_entities(
                db,
                backend,
                embed_fn,
                session=session,
                extraction_output=extraction_output,
                event_by_ext_idx=event_by_ext_idx,
            )

    # --- C. SHOCK trigger ----------------------------------------------
    shock_event: ConceptNode | None = None
    for n in created_events:
        if abs(n.emotional_impact) >= SHOCK_IMPACT_THRESHOLD:
            shock_event = n
            break

    # --- D. TIMER trigger ----------------------------------------------
    timer_due = _is_timer_due(db, session.persona_id, session.user_id, now)

    reflection_reason: str | None = None
    created_thoughts: list[ConceptNode] = []

    # --- E. Reflection execution (hard gate) ---------------------------
    should_reflect = shock_event is not None or timer_due
    if should_reflect:
        recent_count_24h = _count_reflections_24h(db, session.persona_id, session.user_id, now)
        if recent_count_24h >= reflection_hard_limit_24h:
            # Hard gate hit; skip reflection but still mark session closed.
            pass
        else:
            reason = "shock" if shock_event is not None else "timer"
            reflection_reason = reason

            # Gather inputs: recent events in the last 24h (plus the shock
            # event if present, to guarantee it's in the input).
            reflection_inputs = _load_reflection_inputs(
                db, session.persona_id, session.user_id, now
            )
            if shock_event is not None and shock_event not in reflection_inputs:
                reflection_inputs.insert(0, shock_event)

            if reflection_inputs:
                extracted_thoughts = await reflect_fn(reflection_inputs, reason)
                for th in extracted_thoughts:
                    thought = ConceptNode(
                        persona_id=session.persona_id,
                        user_id=session.user_id,
                        type=NodeType.THOUGHT,
                        description=th.description,
                        emotional_impact=th.emotional_impact,
                        emotion_tags=th.emotion_tags,
                        relational_tags=th.relational_tags,
                        source_turn_id=th.source_turn_id,
                    )
                    db.add(thought)
                    db.flush()
                    created_thoughts.append(thought)

                    # Embed thought — join the current transaction
                    # (see note in the event branch above).
                    vec = embed_fn(th.description)
                    backend.insert_vector(thought.id, vec, conn=db.connection())

                    # Filling links
                    for child_id in th.filling:
                        link = ConceptNodeFilling(parent_id=thought.id, child_id=child_id)
                        db.add(link)
                db.commit()
                for t in created_thoughts:
                    db.refresh(t)

                # Post-commit observer notifications for thoughts
                if observer is not None:
                    for t in created_thoughts:
                        try:
                            observer.on_thought_created(t)
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "observer.on_thought_created raised (thought id=%s): %s",
                                t.id,
                                e,
                            )

    # --- F-pre. L6 episodic_state update (plan §6.2 step 6) -------------
    # Reuse the extraction LLM's ``session_mood_signal`` output — zero
    # extra round-trips. Best-effort: a write failure here is logged
    # but must not block the session-close transition.
    if extraction_output is not None and extraction_output.session_mood_signal is not None:
        from echovessel.memory.episodic import update_episodic_state

        signal = extraction_output.session_mood_signal
        try:
            update_episodic_state(
                db,
                persona_id=session.persona_id,
                signal={
                    "mood": signal.mood,
                    "energy": signal.energy,
                    "last_user_signal": signal.last_user_signal,
                },
                now=now,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "update_episodic_state failed (session %s): %s",
                session.id,
                e,
            )

    # --- F. Mark session closed ----------------------------------------
    session.status = SessionStatus.CLOSED
    session.extracted = True
    session.extracted_at = now
    db.add(session)
    db.commit()
    db.refresh(session)

    # Round 4: fire `on_session_closed` strictly after the commit that
    # transitioned status → CLOSED. Mirrors the trivial-skip branch
    # above (§ A).
    track_pending_session_closed(session)
    drain_and_fire_pending_lifecycle_events()

    return ConsolidateResult(
        session=session,
        skipped=False,
        events_created=created_events,
        thoughts_created=created_thoughts,
        reflection_reason=reflection_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
) -> None:
    """Resolve every mentioned entity + wire L3↔L5 junctions + apply
    any user-stated entity clarification. Kept as a helper so the B
    phase stays readable and so merge conflicts with other specs touching
    consolidate stay local to this function.
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

    # Junction rows: one per (event_id, entity_id) pair.
    for ent_idx, ext_ent in enumerate(extraction_output.mentioned_entities):
        entity_id = entity_id_by_ext_idx.get(ent_idx)
        if entity_id is None:
            continue
        for ev_idx in ext_ent.in_events:
            node = event_by_ext_idx.get(ev_idx)
            if node is None or node.id is None:
                continue
            add_concept_entity_link(db, node_id=node.id, entity_id=entity_id)
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

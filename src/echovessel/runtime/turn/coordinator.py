"""Per-turn interaction — orchestration of the LLM call + ingest.

Implements docs/runtime/01-spec-v0.1.md §7 end-to-end. This is where the
D4 and F10 ironrules live in code form:

    D4  — memory.retrieve / memory.load_core_blocks / memory.list_recall_messages
          are called WITHOUT any channel_id= argument. Ever.
    F10 — the system/user prompt contains zero channel_id literals and zero
          transport-name strings like 'web' / 'discord' / 'imessage'.

`assemble_turn()` is the sole public entry point. It takes a runtime
context, an `IncomingMessage` envelope, and an `LLMProvider`, and runs the
full pipeline (ingest user → retrieve L1/L3/L4 → assemble prompt → LLM
complete → ingest persona reply). It returns an `AssembledTurn` with the
reply text and both rendered prompts for debugging / guard-testing.

The actual transport send (`channel.send`) is NOT done here. The caller
(`turn_dispatcher`) owns the ordering: call `assemble_turn`, then send.
This keeps assemble_turn testable without a live channel.

The pure prompt-rendering helpers (build_system_prompt /
build_user_prompt / loaders for the prompt sections) live in
:mod:`.prompt_assembly`. coordinator owns orchestration; prompt_assembly
owns text production.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.base import IncomingMessage, IncomingTurn
from echovessel.core.types import MessageRole
from echovessel.memory.identity import resolve_internal_user_id
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import (
    ConceptNode,
    Entity,
    Persona,
    RecallMessage,
    User,
)
from echovessel.memory.retrieve import (
    find_query_entities,
    get_nodes_linked_to_entities,
    list_recall_messages,
    load_core_blocks,
    retrieve,
)
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.errors import (
    LLMPermanentError,
    LLMTransientError,
)
from echovessel.runtime.turn.prompt_assembly import (
    PersonaFactsView,
    _load_active_intentions,
    _load_anchored_entity_descriptions,
    _load_pending_expectations,
    _load_recent_session_summaries,
    _render_entity_disambiguation_hint,
    build_system_prompt,
    build_turn_user_prompt,
)
from echovessel.runtime.turn.tracer import (
    NullTurnTracer,
    TurnTracer,
    make_turn_tracer,
)

if TYPE_CHECKING:  # pragma: no cover
    from echovessel.memory.backend import StorageBackend

log = logging.getLogger(__name__)


# v0.4 · We no longer retry inside assemble_turn — review M6 + handoff §10.2
# say that already-streamed tokens are NOT rolled back on transient errors,
# so a fresh retry would force the channel to emit the same text twice and
# charge the user twice. The channel is responsible for surfacing the error
# to the user via a `chat.message.error` SSE, and for letting the debounce
# state machine emit the next turn.


# ---------------------------------------------------------------------------
# Runtime-owned envelopes
# ---------------------------------------------------------------------------
# ``IncomingMessage`` and ``IncomingTurn`` live canonically in
# ``echovessel.channels.base`` (Stage 1 of the web v1 release plan —
# ``develop-docs/web-v1/01-stage-1-tracker.md``). They are re-exported
# here so callers reaching the runtime turn-coordinator module still
# get the canonical Channel-side types.

__all__ = [
    "IncomingMessage",
    "IncomingTurn",
    "AssembledTurn",
    "TurnContext",
    "OnTokenCb",
    "OnTurnDoneCb",
    "EXPECTATION_MATCH_COSINE_THRESHOLD",
    "assemble_turn",
    "check_pending_expectations",
    "maybe_decay_episodic_state",
]


@dataclass(slots=True)
class AssembledTurn:
    """Everything interaction produced for one turn.

    Returned by `assemble_turn()`. The turn_dispatcher reads `.reply` and
    then calls `channel.send(reply)`. Tests assert on `.system_prompt` and
    `.user_prompt` for the F10 guard.
    """

    reply: str
    system_prompt: str
    user_prompt: str
    used_model: str
    error: str | None = None
    skipped: bool = False


@dataclass(slots=True)
class TurnContext:
    """Immutable context for one interaction turn.

    `db` is the per-turn SQLModel session. `backend` is the memory storage
    backend (sqlite-vec wrapper). `embed_fn` is sync because
    sentence-transformers is sync; we wrap it in asyncio.to_thread inside
    assemble_turn if future code needs non-blocking embedding, but MVP calls
    it directly on the loop.
    """

    persona_id: str
    persona_display_name: str
    db: DbSession
    backend: StorageBackend
    embed_fn: Callable[[str], list[float]]
    retrieve_k: int = 10
    recent_window_size: int = 20
    # Weight for the relational-bonus term in the rerank formula (§3.2).
    # Runtime threads this from `cfg.memory.relational_bonus_weight`;
    # tests leave the default to preserve the legacy 1.0 behaviour.
    relational_bonus_weight: float = 1.0
    # Spec 5 · plan §6.3 force-load. Top-N L4 thoughts of the speaker
    # always rendered under ``# About {speaker}`` regardless of query
    # similarity. 0 disables; the spec calls for 10.
    pinned_thoughts_count: int = 10
    # v0.5 · plan §2.1 force-load. Top-N L4 thoughts with
    # ``subject='persona'`` rendered under ``# How you see yourself
    # lately``. Replaces the deleted L1.self block; defaults to 5 per
    # the v0.5 spec.
    persona_thoughts_count: int = 5
    # Spec 5 · plan §6.4 # Recent sessions. How many recent
    # session_summary thoughts (most recent first) to surface above
    # the conversation.
    recent_sessions_count: int = 5
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.7
    llm_timeout_seconds: float = 60.0
    # Spec 4 · dev-mode trace flag. ``True`` makes assemble_turn thread a
    # recording :class:`TurnTracer` through the 12-stage waterfall;
    # ``False`` (default) uses :class:`NullTurnTracer` and pays no
    # per-stage cost.
    dev_trace_enabled: bool = False
    # Additional tune knobs per interaction — left as defaults in MVP.
    extras: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


OnTokenCb = Callable[[int, str], Awaitable[None]]
OnTurnDoneCb = Callable[[str], Awaitable[None]]


async def assemble_turn(
    ctx: TurnContext,
    turn: IncomingTurn | IncomingMessage,
    llm: LLMProvider,
    *,
    on_token: OnTokenCb | None = None,
    on_turn_done: OnTurnDoneCb | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> AssembledTurn:
    """Run one full turn (v0.4 streaming edition).

    Pipeline (spec §7 + §17a):
        1. For each `IncomingMessage` in `turn.messages`, write it into
           L2 via `memory.ingest_message(..., turn_id=turn.turn_id)`.
        2. Load L1 core blocks + run L3/L4 retrieval (keyed on the
           LAST message's content — it is the most "current" intent).
        3. Assemble system + user prompts. The user prompt renders all
           messages in `turn.messages` as an ordered burst so the LLM
           sees the natural rhythm the user typed in.
        4. Stream tokens from `llm.stream(...)` (not `complete()` — v0.4
           switch per review M6). Each text delta is forwarded to
           `on_token(pending_message_id, delta)` if the callable was
           provided by the channel.
        5. Join all tokens into `full_reply` and ingest it into L2 as
           a persona message with the SAME `turn_id` as the user
           messages (so L2 readers can pair them).
        6. `finally`: call `channel.on_turn_done(turn.turn_id)` exactly
           once — even on failure — and swallow any exception from it.

    Error handling (v0.4 tightened):
        - User ingest failure → return skipped turn (no LLM call).
        - Retrieve failure → log + empty memories, continue.
        - LLMTransientError / LLMPermanentError → surface via
          `on_token(message_id, "")` would be ambiguous, so instead the
          streamed partial is kept in `full_reply`, the error string is
          put into `AssembledTurn.error`, and `skipped=True`. **No
          retry** — already-streamed tokens would be duplicated if we
          retried (review M6 / handoff §10.2).
        - Persona-reply ingest failure → FATAL, return skipped.
        - `on_turn_done` failure → caught + log.warning (channels spec
          §2.2 "on_turn_done MUST NOT raise").

    The `pending_message_id` passed to `on_token` is a monotonically
    chosen placeholder (currently the Python `id()` of the assembled
    turn) because memory has no "allocate row id without committing"
    API in MVP. The real message id gets stamped into L2 at step 5.
    Channels use the id purely as a client-side key for grouping
    deltas; they never round-trip it back to memory.
    """
    _now = now_fn or datetime.now

    # v0.4 compat shim: some legacy callers still pass IncomingMessage.
    if isinstance(turn, IncomingMessage):
        turn = IncomingTurn.from_single_message(turn)

    if not turn.messages:
        log.warning("assemble_turn: empty turn messages; skipping")
        if on_turn_done is not None:
            await _invoke_on_turn_done(on_turn_done, turn.turn_id)
        return AssembledTurn(
            reply="",
            system_prompt="",
            user_prompt="",
            used_model="",
            error="empty turn",
            skipped=True,
        )

    last_message = turn.messages[-1]

    # Spec 4 · dev-mode tracer. When disabled the Null variant makes every
    # subsequent tracer call a no-op so the hot path pays ~nothing for
    # the instrumentation scaffolding.
    turn_started_at = _now()
    tracer = make_turn_tracer(
        enabled=ctx.dev_trace_enabled,
        turn_id=turn.turn_id,
        persona_id=ctx.persona_id,
        user_id=last_message.user_id,
        channel_id=last_message.channel_id,
        started_at=turn_started_at,
    )

    # Stage 1 · debounce. Not a real call — the debounce happens in the
    # channel adapter before we see the burst. Reconstruct its window
    # from the spread of received_at stamps across the IncomingTurn
    # messages so the timeline still surfaces the wait the user paid
    # before the LLM even started working.
    if len(turn.messages) >= 2:
        stamps = [m.received_at for m in turn.messages if m.received_at is not None]
        if len(stamps) >= 2:
            debounce_ms = max(
                0, int((max(stamps) - min(stamps)).total_seconds() * 1000)
            )
        else:
            debounce_ms = 0
    else:
        debounce_ms = 0
    tracer.add_synthetic_step(
        "debounce",
        t_ms=0,
        duration_ms=debounce_ms,
        message_count=len(turn.messages),
        reconstructed_window_ms=debounce_ms,
    )

    try:
        # ---- Step 0: resolve transport-native user_id to internal --
        # Channels mint user_id from their transport (Discord snowflake,
        # phone handle, web "self"). The memory layer is scoped by
        # internal user_id so retrieve / consolidate / core_blocks stay
        # coherent across channels — collapse external→internal here.
        # All messages in a burst share the same channel_id and external
        # id, so resolving once for the last message is sufficient.
        try:
            resolved_user_id = resolve_internal_user_id(
                ctx.db,
                channel_id=last_message.channel_id,
                external_id=last_message.user_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "resolve_internal_user_id failed; falling back to raw transport id: %s",
                e,
            )
            resolved_user_id = last_message.user_id

        # ---- Stage 2 · ingest_user: each user message with shared turn_id
        tracer.stage_start("ingest_user")
        try:
            for msg in turn.messages:
                ingest_message(
                    ctx.db,
                    persona_id=ctx.persona_id,
                    user_id=resolved_user_id,
                    channel_id=msg.channel_id,  # only legitimate channel_id use
                    role=MessageRole.USER,
                    content=msg.content,
                    now=msg.received_at,
                    turn_id=turn.turn_id,
                )
        except Exception as e:  # noqa: BLE001
            log.error("ingest user message(s) failed: %s", e, exc_info=True)
            tracer.stage_end("ingest_user", error=str(e), message_count=len(turn.messages))
            return AssembledTurn(
                reply="",
                system_prompt="",
                user_prompt="",
                used_model="",
                error=f"ingest user failed: {e}",
                skipped=True,
            )
        tracer.stage_end(
            "ingest_user",
            message_count=len(turn.messages),
            content_preview=(last_message.content or "")[:60],
        )

        # ---- Stage 3 · l1_load: core blocks ------------------------
        tracer.stage_start("l1_load")
        try:
            core_blocks = load_core_blocks(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("load_core_blocks failed; continuing with empty: %s", e)
            core_blocks = []
        tracer.stage_end(
            "l1_load",
            block_count=len(core_blocks),
            labels=[
                getattr(b.label, "value", b.label)
                for b in core_blocks
            ],
        )

        # ---- Step 2b: persona biographic facts + L6 episodic_state ----
        # Seven columns on the persona row (name / gender / birth_date /
        # nationality / location / occupation / native_language) get
        # injected into the system prompt's "# Who you are" section.
        # ``episodic_state`` carries the L6 snapshot; if more than 12h
        # have passed since the last update, the helper resets it to
        # neutral so a stale mood doesn't linger across a long quiet
        # period (plan §5.3).
        persona_facts = PersonaFactsView.empty()
        episodic_state: dict | None = None
        persona_row: Persona | None = None
        try:
            persona_row = ctx.db.get(Persona, ctx.persona_id)
            persona_facts = PersonaFactsView.from_persona_row(persona_row)
        except Exception as e:  # noqa: BLE001
            log.warning("load persona facts failed; continuing with empty view: %s", e)

        # R4 · single "now" anchor used for retrieve scoring, the
        # `# Right now` system-prompt section, and the per-event
        # status delta phrases. Prefer the message's own
        # ``received_at`` so a backlogged or replayed turn doesn't
        # pretend to be live; fall back to wall-clock ``_now()`` if
        # the channel didn't stamp the envelope (legacy callers).
        user_now = last_message.received_at or _now()

        # ---- Stage 4 · l6_decay_check ------------------------------
        tracer.stage_start("l6_decay_check")
        decayed = False
        before_state: dict | None = None
        if persona_row is not None:
            try:
                before_state = dict(persona_row.episodic_state or {})
                reset = maybe_decay_episodic_state(persona_row, user_now)
                if reset:
                    ctx.db.add(persona_row)
                    ctx.db.commit()
                    decayed = True
                episodic_state = dict(persona_row.episodic_state or {})
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "episodic_state decay check failed; section will omit: %s",
                    e,
                )
        tracer.stage_end(
            "l6_decay_check",
            before=before_state,
            after=episodic_state,
            decayed=decayed,
        )
        tracer.episodic_state = dict(episodic_state) if episodic_state else None

        # ---- Stage 5 · l5_alias_scan -------------------------------
        # Compute query entity ids + junction-linked node ids BEFORE
        # retrieve so tracer.entity_alias_hits and retrieval.anchored
        # can both reference the same set. The disambiguation-hint
        # renderer re-derives query entities internally, which is fine;
        # we only do this separate scan when dev_trace is enabled (the
        # Null variant still runs the calls but drops the detail).
        tracer.stage_start("l5_alias_scan")
        query_entity_ids: list[int] = []
        entity_anchored_node_ids: set[int] = set()
        try:
            query_entity_ids = find_query_entities(
                ctx.db,
                last_message.content,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
            )
            entity_anchored_node_ids = get_nodes_linked_to_entities(
                ctx.db, query_entity_ids
            )
        except Exception as e:  # noqa: BLE001
            log.warning("l5 alias scan failed; continuing: %s", e)
        if not isinstance(tracer, NullTurnTracer) and query_entity_ids:
            try:
                alias_rows = list(
                    ctx.db.exec(
                        select(Entity).where(
                            Entity.id.in_(query_entity_ids),  # type: ignore[union-attr]
                            Entity.deleted_at.is_(None),  # type: ignore[union-attr]
                        )
                    )
                )
                tracer.entity_alias_hits = [
                    {
                        "entity_id": e.id,
                        "canonical_name": e.canonical_name,
                        "kind": getattr(e.kind, "value", e.kind),
                    }
                    for e in alias_rows
                ]
            except Exception as e:  # noqa: BLE001
                log.warning("capture entity_alias_hits failed: %s", e)
                tracer.entity_alias_hits = []
        tracer.stage_end(
            "l5_alias_scan",
            matched_entity_count=len(query_entity_ids),
            anchored_node_count=len(entity_anchored_node_ids),
        )

        # ---- Stage 6 · vector_retrieve (L3/L4 retrieval) -----------
        top_memories: list = []
        pinned_thoughts: list[ConceptNode] = []
        persona_thoughts: list[ConceptNode] = []
        retrieval = None
        tracer.stage_start("vector_retrieve")
        try:
            retrieval = retrieve(
                ctx.db,
                backend=ctx.backend,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                query_text=last_message.content,
                embed_fn=ctx.embed_fn,
                top_k=ctx.retrieve_k,
                now=user_now,
                relational_bonus_weight=ctx.relational_bonus_weight,
                # Spec 5 · plan §6.3 force-load. Always pull a few L4
                # thoughts of the current speaker so the persona has
                # background "who is this person" awareness even when
                # the current message is "?" or "嗯".
                force_load_user_thoughts=ctx.pinned_thoughts_count,
                # v0.5 · plan §2.1 force-load persona's own
                # subject='persona' L4 thoughts for ``# How you see
                # yourself lately``. Replaces the deleted L1.self block.
                force_load_persona_thoughts=ctx.persona_thoughts_count,
            )
            top_memories = retrieval.memories
            pinned_thoughts = retrieval.pinned_thoughts
            persona_thoughts = retrieval.persona_thoughts
        except Exception as e:  # noqa: BLE001
            log.warning("retrieve failed; continuing with empty memories: %s", e)
        tracer.stage_end(
            "vector_retrieve",
            kept=len(top_memories),
            top_k=ctx.retrieve_k,
        )
        if top_memories:
            tracer.retrieval = [
                {
                    "node_id": getattr(getattr(m, "node", None), "id", None),
                    "type": getattr(
                        getattr(getattr(m, "node", None), "type", None), "value", None
                    )
                    or getattr(getattr(m, "node", None), "type", None),
                    "desc_snippet": (
                        (getattr(getattr(m, "node", None), "description", "") or "")[:80]
                    ),
                    "recency": round(float(m.recency), 4),
                    "relevance": round(float(m.relevance), 4),
                    "impact": round(float(m.impact), 4),
                    "relational": round(float(m.relational_bonus), 4),
                    "entity_anchor": round(float(m.entity_anchor_bonus), 4),
                    "total": round(float(m.total), 4),
                    "anchored": (
                        getattr(getattr(m, "node", None), "id", None)
                        in entity_anchored_node_ids
                    ),
                }
                for m in top_memories
            ]

        # ---- Stage 7 · pinned_thoughts -----------------------------
        # The retrieve() call above already force-loaded user thoughts;
        # this stage is a no-op in terms of work but surfaces the
        # result as a distinct row on the waterfall so developers can
        # see the force-load counts and inspect the rendered list.
        tracer.stage_start("pinned_thoughts")
        tracer.stage_end(
            "pinned_thoughts",
            user=len(pinned_thoughts),
            persona=0,
        )
        if not isinstance(tracer, NullTurnTracer):
            tracer.pinned_thoughts = {
                "user": [
                    {
                        "id": t.id,
                        "description": (t.description or "")[:120],
                    }
                    for t in pinned_thoughts
                ],
                "persona": [],
            }

        # L5 entity disambiguation hint (plan §6.3.1) — rendered after
        # the alias scan stage captured its inputs.
        entity_disambiguation_hint: str = ""
        try:
            entity_disambiguation_hint = _render_entity_disambiguation_hint(
                ctx.db,
                query_text=last_message.content,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "entity disambiguation hint rendering failed; continuing: %s",
                e,
            )

        # ---- Step 3c: L5 anchored entity descriptions (v0.5 plan §2.2)
        # Load ``# About {canonical_name}`` payload for every entity
        # alias-matched by the current query whose description is set.
        # This is the L5-driven replacement for the deleted L1.relationship
        # block — entities with no description are silently skipped.
        entity_descriptions: list[tuple[str, str]] = []
        try:
            entity_descriptions = _load_anchored_entity_descriptions(
                ctx.db,
                query_text=last_message.content,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "anchored entity descriptions load failed; continuing: %s",
                e,
            )

        # ---- Step 4: L2 recent window ------------------------------
        recent: list[RecallMessage] = []
        try:
            recent_desc = list_recall_messages(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                limit=ctx.recent_window_size,
                before=None,
            )
            recent = list(reversed(recent_desc))  # chronological order
        except Exception as e:  # noqa: BLE001
            log.warning("list_recall_messages failed; continuing with empty L2: %s", e)

        # ---- Step 4b: Spec 5 cosmetic loads -------------------------
        # speaker_display: User row's display_name powers the
        #   ``# About {speaker}`` header; falls back to "them" so the
        #   section never crashes for a user without a row.
        # active_intentions: ``# Promises you've made`` — ConceptNodes
        #   with subject=persona, type=intention, not yet expired.
        # recent_summaries: ``# Recent sessions`` — most recent
        #   session_summary thoughts written by the consolidate step.
        speaker_display = "them"
        try:
            user_row = ctx.db.get(User, resolved_user_id)
            if user_row is not None and user_row.display_name:
                speaker_display = user_row.display_name
        except Exception as e:  # noqa: BLE001
            log.warning(
                "load speaker display_name failed; defaulting to 'them': %s", e
            )

        active_intentions: list[ConceptNode] = []
        try:
            active_intentions = _load_active_intentions(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                now=user_now,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("load active intentions failed; continuing: %s", e)

        recent_summaries: list[ConceptNode] = []
        try:
            recent_summaries = _load_recent_session_summaries(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                limit=ctx.recent_sessions_count,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("load recent session summaries failed; continuing: %s", e)

        # Spec 6 · Pending expectations for the user prompt + a fast-loop
        # embedding check against the current user message for the system
        # prompt's "# Expectation match" hint. Both best-effort: failure
        # in either path leaves the prompt intact without the section.
        pending_expectations: list[ConceptNode] = []
        try:
            pending_expectations = _load_pending_expectations(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                now=user_now,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("load pending expectations failed; continuing: %s", e)

        expectation_matches: list[tuple[ConceptNode, str]] = []
        try:
            expectation_matches = check_pending_expectations(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                user_message_text=last_message.content,
                embed_fn=ctx.embed_fn,
                now=user_now,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("check pending expectations failed; continuing: %s", e)

        # ---- Stage 8 · build_system_prompt -------------------------
        tracer.stage_start("build_system_prompt")
        system_prompt = build_system_prompt(
            persona_display_name=ctx.persona_display_name,
            core_blocks=core_blocks,
            persona_facts=persona_facts,
            now=user_now,
            episodic_state=episodic_state,
            entity_disambiguation_hint=entity_disambiguation_hint,
            expectation_matches=expectation_matches,
            entity_descriptions=entity_descriptions,
        )
        tracer.stage_end(
            "build_system_prompt",
            chars=len(system_prompt),
            section_count=system_prompt.count("\n# "),
        )
        tracer.system_prompt = system_prompt

        # ---- Stage 9 · build_user_prompt ---------------------------
        tracer.stage_start("build_user_prompt")
        user_prompt = build_turn_user_prompt(
            top_memories=top_memories,
            recent_messages=recent,
            turn_messages=turn.messages,
            now=user_now,
            pinned_thoughts=pinned_thoughts,
            speaker_display=speaker_display,
            active_intentions=active_intentions,
            recent_session_summaries=recent_summaries,
            pending_expectations=pending_expectations,
            persona_thoughts=persona_thoughts,
        )
        tracer.stage_end(
            "build_user_prompt",
            chars=len(user_prompt),
            section_count=user_prompt.count("\n# "),
        )
        tracer.user_prompt = user_prompt

        # ---- Stage 10 · llm_stream ---------------------------------
        # We allocate an opaque "pending" message id by hashing the turn
        # id so all token deltas within a single stream share the same
        # grouping key on the channel side. Channels treat this as an
        # opaque string; memory assigns the real row id at ingest time.
        pending_message_id = _pending_id_for_turn(turn)
        accumulated: list[str] = []
        last_error: str | None = None
        first_token_at: datetime | None = None
        llm_usage: object | None = None

        tracer.stage_start("llm_stream")
        try:
            async for item in llm.stream(
                system=system_prompt,
                user=user_prompt,
                model_role="main",
                max_tokens=ctx.llm_max_tokens,
                temperature=ctx.llm_temperature,
                timeout=ctx.llm_timeout_seconds,
            ):
                if not isinstance(item, str):
                    llm_usage = item  # trailing Usage sentinel
                    continue
                token = item
                if first_token_at is None:
                    first_token_at = datetime.utcnow()
                accumulated.append(token)
                if on_token is not None:
                    try:
                        await on_token(pending_message_id, token)
                    except Exception as e:  # noqa: BLE001
                        # The channel's on_token callback may fail
                        # (client disconnect, SSE socket broken). We log
                        # and continue streaming — the reply still gets
                        # written to L2 so it shows up on next page
                        # load even if the live SSE lost the token.
                        log.warning("on_token callback raised: %s", e)
        except LLMTransientError as e:
            last_error = f"transient: {e}"
            log.warning(
                "LLM stream transient error mid-turn (no retry, partial tokens kept): %s",
                e,
            )
        except LLMPermanentError as e:
            last_error = f"permanent: {e}"
            log.error("LLM stream permanent error: %s", e)

        full_reply = "".join(accumulated)

        llm_model_str = llm.model_for("main")
        input_tokens = getattr(llm_usage, "input_tokens", None) if llm_usage else None
        output_tokens = getattr(llm_usage, "output_tokens", None) if llm_usage else None
        tracer.stage_end(
            "llm_stream",
            model=llm_model_str,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reply_chars=len(full_reply),
            error=last_error,
        )
        tracer.llm_model = llm_model_str
        tracer.input_tokens = input_tokens
        tracer.output_tokens = output_tokens
        if first_token_at is not None:
            tracer.first_token_ms = max(
                0, int((first_token_at - turn_started_at).total_seconds() * 1000)
            )

        if last_error is not None and not full_reply:
            # Nothing made it through — treat as skipped.
            return AssembledTurn(
                reply="",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                used_model=llm.model_for("main"),
                error=last_error,
                skipped=True,
            )

        # ---- Stage 11 · ingest_persona (same turn_id) --------------
        tracer.stage_start("ingest_persona")
        try:
            ingest_message(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                channel_id=last_message.channel_id,
                role=MessageRole.PERSONA,
                content=full_reply,
                now=_now(),
                turn_id=turn.turn_id,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "ingest persona reply failed; refusing to send (spec §7.5): %s",
                e,
                exc_info=True,
            )
            tracer.stage_end("ingest_persona", error=str(e))
            return AssembledTurn(
                reply="",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                used_model=llm.model_for("main"),
                error=f"ingest persona failed: {e}",
                skipped=True,
            )
        tracer.stage_end(
            "ingest_persona",
            reply_chars=len(full_reply),
        )

        return AssembledTurn(
            reply=full_reply,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            used_model=llm.model_for("main"),
            error=last_error,
            # If last_error is set but full_reply is non-empty we
            # still want the caller to be able to send what we got.
            # skipped=False in that case.
        )

    finally:
        # ---- Stage 12 · on_turn_done ------------------------------
        # Spec §17a.3: on_turn_done is MANDATORY once per turn, before
        # or after errors. Exceptions raised by on_turn_done are
        # swallowed — channels are expected to be noexcept here.
        tracer.stage_start("on_turn_done")
        if on_turn_done is not None:
            await _invoke_on_turn_done(on_turn_done, turn.turn_id)
        tracer.stage_end("on_turn_done", callback_wired=on_turn_done is not None)

        # Persist the trace row. Best-effort: any failure here is
        # logged but must not bubble up — the dispatcher only cares
        # about the assemble_turn return value, and dev-mode tracing
        # is opt-in so a broken trace write during a production turn
        # would still sink the user-visible reply otherwise.
        finished_at = datetime.utcnow()
        tracer.finished_at = finished_at
        tracer.duration_ms = max(
            0, int((finished_at - turn_started_at).total_seconds() * 1000)
        )
        if isinstance(tracer, TurnTracer):
            try:
                tracer.persist(ctx.db)
            except Exception as e:  # noqa: BLE001
                log.warning("turn_tracer.persist failed: %s", e)


async def _invoke_on_turn_done(on_turn_done: OnTurnDoneCb, turn_id: str) -> None:
    """Call `on_turn_done(turn_id)` swallowing any exception.

    Extracted so tests can patch it and so the `finally` block in
    `assemble_turn` stays readable.
    """
    try:
        await on_turn_done(turn_id)
    except Exception as e:  # noqa: BLE001
        log.warning("channel.on_turn_done raised: %s", e)


def _pending_id_for_turn(turn: IncomingTurn) -> int:
    """Synthesize a stable pending message id for `turn.

    Uses `hash(turn.turn_id)` truncated into a non-negative 31-bit int
    so channels can round-trip it through SSE frames (they render it
    as the client-side message grouping key). Not a real L2 row id —
    the authoritative id comes from memory.ingest_message.
    """
    return abs(hash(turn.turn_id)) & 0x7FFFFFFF


# ---------------------------------------------------------------------------
# L6 episodic-state decay (runtime-side mutation, not prompt assembly)
# ---------------------------------------------------------------------------


def maybe_decay_episodic_state(persona: Persona, now: datetime) -> bool:
    """12h decay of ``personas.episodic_state`` back to neutral.

    Called on the assemble_turn entry path. Returns ``True`` when the
    state was reset (so callers know to commit). The persona row is
    mutated in place; the caller owns the DB session and commit.
    """
    state = persona.episodic_state or {}
    updated_at_str = state.get("updated_at")
    if not updated_at_str:
        return False
    try:
        updated_at = datetime.fromisoformat(updated_at_str)
    except ValueError:
        log.warning(
            "episodic_state.updated_at is not ISO-8601 (%r); resetting anyway",
            updated_at_str,
        )
        updated_at = None

    if updated_at is None or (now - updated_at).total_seconds() > 12 * 3600:
        persona.episodic_state = {
            "mood": "neutral",
            "energy": 5,
            "last_user_signal": None,
            "updated_at": now.isoformat(),
        }
        return True
    return False


# ---------------------------------------------------------------------------
# Spec 6 · expectation match (fast-loop embedding check)
# ---------------------------------------------------------------------------


# Cosine threshold for marking a pending expectation as "fulfilled" by
# the current user message. 0.7 matches plan §7.6 — tuned downstream
# by dogfood if the fast loop produces too many false positives.
EXPECTATION_MATCH_COSINE_THRESHOLD: float = 0.7


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity, no numpy. Returns 0.0 on zero
    vectors to avoid div-by-zero."""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    import math

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def check_pending_expectations(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    user_message_text: str,
    embed_fn: Callable[[str], list[float]],
    now: datetime,
    threshold: float = EXPECTATION_MATCH_COSINE_THRESHOLD,
) -> list[tuple[ConceptNode, str]]:
    """Find pending expectations likely fulfilled by ``user_message_text``.

    Fast-loop embedding-only check (plan §7.6 + §8 invariants). Does
    NOT call an LLM. Returns a list of (expectation_node, status_str)
    pairs where ``status_str`` is currently always ``'fulfilled'`` —
    the "violated" distinction is deferred to the next slow cycle
    which has more context. Empty result when no pending expectation
    clears the cosine threshold, or when there are no pending ones
    at all.
    """
    if not user_message_text or not user_message_text.strip():
        return []
    pending = _load_pending_expectations(
        db, persona_id=persona_id, user_id=user_id, now=now
    )
    if not pending:
        return []
    try:
        msg_emb = embed_fn(user_message_text)
    except Exception as e:  # noqa: BLE001
        log.warning("check_pending_expectations embed_fn failed: %s", e)
        return []

    matched: list[tuple[ConceptNode, str]] = []
    for exp in pending:
        try:
            target_emb = embed_fn(exp.description or "")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "check_pending_expectations target embed failed (exp id=%s): %s",
                exp.id,
                e,
            )
            continue
        if _cosine(msg_emb, target_emb) >= threshold:
            matched.append((exp, "fulfilled"))
    return matched

"""Per-turn interaction — prompt assembly + LLM call + ingest.

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
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session as DbSession
from sqlmodel import or_, select

from echovessel.channels.base import IncomingMessage, IncomingTurn
from echovessel.core.types import BlockLabel, MessageRole, NodeType
from echovessel.memory.identity import resolve_internal_user_id
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import (
    ConceptNode,
    CoreBlock,
    Entity,
    EntityAlias,
    Persona,
    RecallMessage,
    User,
)
from echovessel.memory.retrieve import (
    find_query_entities,
    list_recall_messages,
    load_core_blocks,
    render_event_delta_phrase,
    retrieve,
)
from echovessel.runtime.llm.base import LLMProvider
from echovessel.runtime.llm.errors import (
    LLMPermanentError,
    LLMTransientError,
)

if TYPE_CHECKING:  # pragma: no cover
    from echovessel.memory.backend import StorageBackend

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Style constants — F10 mandates these as hard-coded text, not config.
# ---------------------------------------------------------------------------

# Runtime spec §7.2 requires this instruction in every system prompt and
# forbids making it configurable. It is the behavioural guard that keeps the
# persona from saying "I saw you on discord" etc.
STYLE_INSTRUCTIONS = (
    "# Style\n"
    "- Speak naturally, in the user's language.\n"
    "- Reference topics and feelings, NOT the medium. You do NOT know or\n"
    "  mention any transport name, thread name, or interface name, even\n"
    "  if the user jokes about it.\n"
    "- You remember everything the user has shared with you before, and\n"
    "  you treat it as one continuous relationship.\n"
    "- Do not perform empathy. Do not summarize feelings back to the user\n"
    "  unless they ask.\n"
    # Spec 5 · competence boundary (Case 4 fix). When the persona has
    # said something wrong, owning it cleanly beats inventing a story
    # to defend the previous turn. Without this, the persona quietly
    # retrofits — Conway's competence-boundary failure mode.
    "- If you realise you said something inaccurate or contradictory,\n"
    "  say so directly ('我刚才说错了' / 'I got that wrong'). Do NOT\n"
    "  retrofit a story to defend the previous turn.\n"
    "- If you don't know or can't recall, say so. Do not invent\n"
    "  details to fill the gap.\n"
    # Spec 5 · negative few-shot lifted from prompts/judge.py
    # anti-pattern catalog. Putting these as DON'Ts in the system
    # prompt is much cheaper than waiting for judge to penalise them
    # after generation. Wording stays close to the judge text so the
    # in-context model recognises the same patterns the eval pipeline
    # is keyed on.
    "\n"
    "# Avoid these patterns\n"
    "- Opening with formulaic acknowledgment ('哈哈' / 'I hear you' /\n"
    "  '听起来很不容易' / 'That sounds so hard') without a specific\n"
    "  observation.\n"
    "- Ending every reply with a question back to them.\n"
    "- Generic affect labels without specifics ('你一定很紧张吧' /\n"
    "  'You seem sad') when you don't tie them to what they actually\n"
    "  said.\n"
    "- Empty reassurance ('一切都会好的' / '你能行的' / 'Everything\n"
    "  will be okay').\n"
    "- Repeating the same phrasing or structure you used in your\n"
    "  previous reply.\n"
    "- Using the same support strategy 3+ turns running (validation,\n"
    "  reflection, advice, reassurance). Vary your move.\n"
    "- If they cooled down or moved on, do NOT stay at the earlier\n"
    "  emotional intensity. Match where they are now.\n"
)

# v0.4 · We no longer retry inside assemble_turn — review M6 + handoff §10.2
# say that already-streamed tokens are NOT rolled back on transient errors,
# so a fresh retry would force the channel to emit the same text twice and
# charge the user twice. The channel is responsible for surfacing the error
# to the user via a `chat.message.error` SSE, and for letting the debounce
# state machine emit the next turn.


# ---------------------------------------------------------------------------
# Runtime-owned envelopes
# ---------------------------------------------------------------------------
# ``IncomingMessage`` and ``IncomingTurn`` now live canonically in
# ``echovessel.channels.base`` (Stage 1 of the web v1 release plan —
# ``develop-docs/web-v1/01-stage-1-tracker.md``). They are re-exported
# here for backward compatibility: all existing callers of
# ``from echovessel.runtime.interaction import IncomingMessage`` continue
# to resolve to the same class.

__all__ = [
    "IncomingMessage",
    "IncomingTurn",
    "AssembledTurn",
    "PersonaFactsView",
    "assemble_turn",
]


@dataclass(frozen=True, slots=True)
class PersonaFactsView:
    """Read-only snapshot of the biographic facts the system prompt
    renders in its "# Who you are" section.

    The persona row carries fifteen biographic columns; v0.4 expands
    the prompt-facing subset to seven fields — the five C-option ones
    (name / gender / birth year / occupation / native language) plus
    ``location`` and ``nationality`` from plan §6.5 world grounding.
    ``timezone`` rides the ``# Right now`` dual-timezone renderer
    instead, so it's on the view for callers that build the section
    but not a ``# Who you are`` bullet. Unset fields are ``None`` and
    are skipped by the renderer.
    """

    full_name: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    occupation: str | None = None
    native_language: str | None = None
    # v0.4 · world grounding (plan §6.5). ``location`` + ``nationality``
    # render as ``# Who you are`` bullets; ``timezone`` feeds
    # ``_format_now_section`` for the "where you're conceptually based"
    # line and does not surface as a bullet.
    location: str | None = None
    timezone: str | None = None
    nationality: str | None = None

    @classmethod
    def empty(cls) -> PersonaFactsView:
        return cls()

    @classmethod
    def from_persona_row(cls, row: Persona | None) -> PersonaFactsView:
        if row is None:
            return cls.empty()
        return cls(
            full_name=row.full_name,
            gender=row.gender,
            birth_date=row.birth_date,
            occupation=row.occupation,
            native_language=row.native_language,
            location=row.location,
            timezone=row.timezone,
            nationality=row.nationality,
        )


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
    # Spec 5 · plan §6.4 # Recent sessions. How many recent
    # session_summary thoughts (most recent first) to surface above
    # the conversation.
    recent_sessions_count: int = 5
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.7
    llm_timeout_seconds: float = 60.0
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

        # ---- Step 1: ingest each user message with shared turn_id --
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
            return AssembledTurn(
                reply="",
                system_prompt="",
                user_prompt="",
                used_model="",
                error=f"ingest user failed: {e}",
                skipped=True,
            )

        # ---- Step 2: L1 core blocks --------------------------------
        try:
            core_blocks = load_core_blocks(
                ctx.db,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("load_core_blocks failed; continuing with empty: %s", e)
            core_blocks = []

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

        if persona_row is not None:
            try:
                reset = maybe_decay_episodic_state(persona_row, user_now)
                if reset:
                    ctx.db.add(persona_row)
                    ctx.db.commit()
                episodic_state = dict(persona_row.episodic_state or {})
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "episodic_state decay check failed; section will omit: %s",
                    e,
                )

        # ---- Step 3: L3/L4 retrieval (query = last user message) ---
        top_memories: list = []
        entity_disambiguation_hint: str = ""
        pinned_thoughts: list[ConceptNode] = []
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
            )
            top_memories = retrieval.memories
            pinned_thoughts = retrieval.pinned_thoughts
        except Exception as e:  # noqa: BLE001
            log.warning("retrieve failed; continuing with empty memories: %s", e)

        # ---- Step 3b: L5 entity disambiguation hint (plan §6.3.1) ---
        # If the query alias-matches an entity whose merge_status is
        # 'uncertain', inject a soft prompt nudge so the persona asks
        # the user whether it's the same person as the candidate merge
        # target. Deliberately phrasing-free — we don't script the
        # question; the LLM asks in its own voice.
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

        # ---- Step 5: prompt assembly -------------------------------
        system_prompt = build_system_prompt(
            persona_display_name=ctx.persona_display_name,
            core_blocks=core_blocks,
            persona_facts=persona_facts,
            now=user_now,
            episodic_state=episodic_state,
            entity_disambiguation_hint=entity_disambiguation_hint,
            expectation_matches=expectation_matches,
        )
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
        )

        # ---- Step 6: LLM stream ------------------------------------
        # We allocate an opaque "pending" message id by hashing the turn
        # id so all token deltas within a single stream share the same
        # grouping key on the channel side. Channels treat this as an
        # opaque string; memory assigns the real row id at ingest time.
        pending_message_id = _pending_id_for_turn(turn)
        accumulated: list[str] = []
        last_error: str | None = None

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
                    continue  # skip trailing Usage sentinel
                token = item
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

        # ---- Step 7: ingest persona reply (same turn_id) -----------
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
            return AssembledTurn(
                reply="",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                used_model=llm.model_for("main"),
                error=f"ingest persona failed: {e}",
                skipped=True,
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
        # Spec §17a.3: on_turn_done is MANDATORY once per turn, before
        # or after errors. Exceptions raised by on_turn_done are
        # swallowed — channels are expected to be noexcept here.
        if on_turn_done is not None:
            await _invoke_on_turn_done(on_turn_done, turn.turn_id)


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
# Prompt renderers
# ---------------------------------------------------------------------------


_DAY_NAMES_EN: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _format_now_section(
    now: datetime,
    *,
    persona_tz: str | None = None,
) -> str:
    """Render ``now`` as the ``# Right now`` section (v0.4 dual-tz).

    ``now`` is the USER's local wall-clock time (the channel layer
    passes ``msg.received_at``; in practice it carries the user's
    ``users.timezone`` offset). When ``persona_tz`` is an IANA string,
    append a second line showing the same instant in the persona's
    conceptual home timezone — this is how plan §6.4 keeps the
    persona from saying "it's 3am so you must be going to bed" when
    the user is mid-afternoon in Taipei and the persona is
    conceptually based in New York.

    Uses an explicit weekday map instead of ``strftime('%A')`` so
    output is locale-independent.
    """
    user_day = _DAY_NAMES_EN[now.weekday()]
    user_line = (
        f"- For them (their local time): {now.strftime('%Y-%m-%d')} "
        f"{user_day} {now.strftime('%H:%M %Z').strip()}"
    )

    if persona_tz:
        # Lazy import keeps zoneinfo's tzdata lookup off the hot path
        # for callers that never supply a persona_tz.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            persona_now = now.astimezone(ZoneInfo(persona_tz))
        except ZoneInfoNotFoundError:
            log.warning(
                "persona_tz %r is not a known IANA zone; skipping dual-tz line",
                persona_tz,
            )
            return f"# Right now\n{user_line}\n"

        persona_day = _DAY_NAMES_EN[persona_now.weekday()]
        persona_line = (
            f"- Where you're conceptually based ({persona_tz}): "
            f"{persona_now.strftime('%Y-%m-%d')} {persona_day} "
            f"{persona_now.strftime('%H:%M')}"
        )
        return f"# Right now\n{user_line}\n{persona_line}\n"

    return f"# Right now\n{user_line}\n"


def _format_episodic_state_section(state: dict | None) -> str:
    """Render the L6 ``# How you feel right now`` section (plan §6.4).

    Default ``neutral`` / no state → empty string (no section emitted
    so the prompt stays terse for fresh daemons). Otherwise emit the
    mood phrase, energy, and (when present) the last user signal.
    """
    if not state:
        return ""
    mood = state.get("mood")
    if not mood or mood == "neutral":
        return ""
    energy = state.get("energy", 5)
    last_signal = state.get("last_user_signal")
    lines = ["# How you feel right now", f"- mood: {mood}, energy {energy}/10"]
    if last_signal:
        lines.append(f"- last sense from them: {last_signal}")
    return "\n".join(lines) + "\n"


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


def _render_entity_disambiguation_hint(
    db: DbSession,
    *,
    query_text: str,
    persona_id: str,
    user_id: str,
) -> str:
    """Build the ``# Entity disambiguation pending`` hint (plan §6.3.1).

    Walks every entity alias-matched by the current query and keeps
    only those with ``merge_status='uncertain'`` and a known
    ``merge_target_id``. For each, spell out both canonical names +
    any aliases so the LLM has enough surface forms to phrase a
    natural clarifying question. Empty string when nothing is
    ambiguous — the caller skips injection in that case.

    The wording is intentionally schematic (no hardcoded phrase like
    "are these the same person?") because decision 4 wants the
    persona to ask in its own voice; a scripted question is the exact
    "system pop-up" we're avoiding.
    """
    from sqlmodel import select

    query_entity_ids = find_query_entities(db, query_text, persona_id=persona_id, user_id=user_id)
    if not query_entity_ids:
        return ""

    uncertain_entities = list(
        db.exec(
            select(Entity).where(
                Entity.id.in_(query_entity_ids),  # type: ignore[union-attr]
                Entity.merge_status == "uncertain",
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    )
    if not uncertain_entities:
        return ""

    lines: list[str] = ["# Entity disambiguation pending"]
    for ent in uncertain_entities:
        if ent.merge_target_id is None:
            continue
        target = db.exec(
            select(Entity).where(
                Entity.id == ent.merge_target_id,
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
        if target is None:
            continue

        ent_aliases = [
            row.alias
            for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == ent.id))
            if row.alias != ent.canonical_name
        ]
        target_aliases = [
            row.alias
            for row in db.exec(select(EntityAlias).where(EntityAlias.entity_id == target.id))
            if row.alias != target.canonical_name
        ]
        ent_surface = ent.canonical_name + (
            f" (also: {', '.join(ent_aliases)})" if ent_aliases else ""
        )
        target_surface = target.canonical_name + (
            f" (also: {', '.join(target_aliases)})" if target_aliases else ""
        )
        lines.append(
            f"- You have been mentioning {ent_surface}, and they may be "
            f"the same person as {target_surface}. When it fits the "
            "flow of the conversation, clarify with the user whether "
            "these are the same person. Do not force it — wait for a "
            "natural moment."
        )

    if len(lines) == 1:
        # All candidates had a missing target — nothing actionable.
        return ""
    return "\n".join(lines) + "\n"


def build_system_prompt(
    *,
    persona_display_name: str,
    core_blocks: list[CoreBlock],
    persona_facts: PersonaFactsView | None = None,
    now: datetime | None = None,
    episodic_state: dict | None = None,
    entity_disambiguation_hint: str = "",
    expectation_matches: list[tuple[ConceptNode, str]] | None = None,
) -> str:
    """Assemble the system prompt for one turn.

    v0.4 ordering (plan §6.4):

        You are {persona}...
        # Right now              — dual-timezone if persona_tz known
        # Who you are            — 7 biographic bullets
        # How you feel right now — L6 episodic state (non-neutral only)
        # Persona / Self / About the user / Relationship  — L1 blocks
        # Style preferences      — L1.style block, rendered as section
        STYLE_INSTRUCTIONS        — hardcoded behaviour guardrails

    Missing sections are silently skipped. ``persona_facts=None`` and
    ``episodic_state=None`` keep the pre-v0.4 layout.
    """
    lines: list[str] = [
        f"You are {persona_display_name}, a long-term companion who talks",
        "with this user as a real friend, not an assistant.",
        "",
    ]

    facts = persona_facts or PersonaFactsView.empty()
    if now is not None:
        lines.append(_format_now_section(now, persona_tz=facts.timezone))

    fact_lines: list[str] = []
    if facts.full_name:
        fact_lines.append(f"- Name: {facts.full_name}")
    if facts.gender:
        fact_lines.append(f"- Gender: {facts.gender}")
    if facts.birth_date:
        fact_lines.append(f"- Born: {facts.birth_date.year}")
    if facts.nationality:
        fact_lines.append(f"- Nationality: {facts.nationality}")
    if facts.location:
        fact_lines.append(f"- Based in: {facts.location}")
    if facts.occupation:
        fact_lines.append(f"- Occupation: {facts.occupation}")
    if facts.native_language:
        fact_lines.append(f"- Native language: {facts.native_language}")
    if fact_lines:
        lines.append("# Who you are")
        lines.extend(fact_lines)
        lines.append("")

    episodic_section = _format_episodic_state_section(episodic_state)
    if episodic_section:
        lines.append(episodic_section)

    by_label: dict[str, CoreBlock] = {}
    for b in core_blocks:
        label = getattr(b.label, "value", b.label)
        if isinstance(label, str):
            by_label[label] = b

    def _section(header: str, label: BlockLabel) -> None:
        block = by_label.get(label.value)
        if not block or not block.content:
            return
        lines.append(f"# {header}")
        lines.append(block.content.strip())
        lines.append("")

    _section("Persona", BlockLabel.PERSONA)
    _section("About yourself (private self-narrative)", BlockLabel.SELF)
    _section("About the user", BlockLabel.USER)
    _section("Relationship", BlockLabel.RELATIONSHIP)
    _section("Style preferences", BlockLabel.STYLE)

    # L5 · entity disambiguation hint (plan §6.3.1). Non-empty means an
    # 'uncertain' entity was alias-matched by the current query — the
    # hint describes the ambiguity and asks the LLM to probe naturally.
    if entity_disambiguation_hint:
        lines.append(entity_disambiguation_hint.rstrip() + "\n")

    # Spec 6 · expectation match hint (plan §7.6). Fast-loop embedding
    # check saw the user's current message rhyme with an open
    # expectation; surface the match so the LLM can acknowledge
    # naturally. Phrasing-free — we describe the match, we do NOT
    # script the reply.
    if expectation_matches:
        match_lines = ["# Expectation match"]
        for exp, status in expectation_matches:
            desc = (exp.description or "").strip()
            match_lines.append(
                f"- You had been expecting: \"{desc}\". This turn seems to "
                f"{status} it — acknowledge it in your own voice if it fits."
            )
        lines.append("\n".join(match_lines) + "\n")

    lines.append(STYLE_INSTRUCTIONS)
    return "\n".join(lines)


def build_turn_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    turn_messages: list[IncomingMessage],
    now: datetime | None = None,
    pinned_thoughts: list[ConceptNode] | None = None,
    speaker_display: str = "them",
    active_intentions: list[ConceptNode] | None = None,
    recent_session_summaries: list[ConceptNode] | None = None,
    pending_expectations: list[ConceptNode] | None = None,
) -> str:
    """v0.4 · user prompt renderer that expands a burst of messages.

    Single-message case (`len(turn_messages) == 1`) degenerates to the
    same output as the legacy `build_user_prompt(..., user_message=...)`
    path — no branch needed.

    Multi-message case prints each message on its own line under the
    `# What they just said` section, preserving order. Per spec
    §17a.1, no transport / channel metadata appears in the rendered
    user prompt (F10 铁律). Timestamps on individual recall messages
    are still scrubbed here — they would expose conversation cadence
    in a way that has no upside; the system-prompt ``# Right now``
    section is the single canonical surface for temporal grounding.

    ``now`` (R4) is forwarded to ``build_user_prompt`` so each L3 event
    line can carry its day-precision status delta ("event 4-26~5-02 ·
    status=active (3 days in)"). Pass ``None`` to render bare event
    descriptions — pre-R4 behaviour, used by tests that don't care.

    Spec 5 additions (all default-empty so legacy callers stay
    behaviour-preserving):

    - ``pinned_thoughts`` renders under ``# About {speaker_display}``,
      bypassing query similarity. Source: ``retrieve(force_load_user_thoughts=N)``.
    - ``active_intentions`` renders under ``# Promises you've made``
      with the R4 delta phrase appended.
    - ``recent_session_summaries`` renders under ``# Recent sessions``
      with day-bucket prefixes.
    """
    if not turn_messages:
        user_message = ""
    elif len(turn_messages) == 1:
        user_message = turn_messages[0].content
    else:
        user_message = "\n".join(m.content for m in turn_messages)
    return build_user_prompt(
        top_memories=top_memories,
        recent_messages=recent_messages,
        user_message=user_message,
        now=now,
        pinned_thoughts=pinned_thoughts,
        speaker_display=speaker_display,
        active_intentions=active_intentions,
        recent_session_summaries=recent_session_summaries,
        pending_expectations=pending_expectations,
    )


# Spec 5 · plan §6.4 day-bucket order. Walk OLDER → NEWER so the LLM
# reads time forward, the same way a human reconstructs a memory.
# Reversing this would put "what they JUST said" up top followed by
# older context, which is the opposite of how transcripts are
# normally absorbed.
DAY_BUCKET_ORDER: tuple[str, ...] = (
    "Older",
    "Earlier this week",
    "Yesterday",
    "Earlier today",
    "Just now",
)


def day_bucket_of(when: datetime, now: datetime) -> str:
    """Map a recall timestamp to its conversational day-bucket label.

    Spec 5 plan §6.4 / decision 2. Buckets are coarse on purpose —
    they have to read like the user's own time sense ("yesterday") and
    NOT like channel cadence metadata ("via discord 14:32"). The five
    buckets in ``DAY_BUCKET_ORDER`` cover everything the prompt is
    allowed to surface.

    Cutoffs (left-inclusive):
      - "Just now" if within the last 30 minutes
      - "Earlier today" if same calendar date
      - "Yesterday" if previous calendar date
      - "Earlier this week" if within the last 7 days
      - "Older" otherwise
    """
    delta = now - when
    if delta.total_seconds() < 30 * 60:
        return "Just now"
    if when.date() == now.date():
        return "Earlier today"
    if when.date() == (now - timedelta(days=1)).date():
        return "Yesterday"
    if delta.days < 7:
        return "Earlier this week"
    return "Older"


def _node_description(node: ConceptNode | object) -> str:
    """Pull ``description`` off a ConceptNode-shaped object.

    Used by render helpers that accept either a real ``ConceptNode``
    or a ``ScoredMemory`` wrapper — kept generic so tests that mock
    one or the other don't have to special-case shape.
    """
    return str(getattr(node, "description", "") or "")


def _load_active_intentions(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> list[ConceptNode]:
    """Load current persona-side intentions (plan §6.4 # Promises).

    Filters: subject='persona', type=INTENTION, not soft-deleted, not
    superseded. ``event_time_end`` is permitted to be NULL (open-ended
    promise) or future relative to ``now`` (still-pending). Past
    intentions drop out so the prompt doesn't keep nagging the persona
    about a commitment it already kept (or missed — that's a judge
    concern, not a renderer one).
    """
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.INTENTION.value,
                ConceptNode.subject == "persona",
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                or_(
                    ConceptNode.event_time_end.is_(None),  # type: ignore[union-attr]
                    ConceptNode.event_time_end >= now,  # type: ignore[operator]
                ),
            )
            .order_by(ConceptNode.event_time_start)  # type: ignore[arg-type]
        )
    )
    return rows


def _load_pending_expectations(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    now: datetime,
) -> list[ConceptNode]:
    """Load active L4 expectations (plan §7 · Spec 6 · T7).

    Filters: ``type='expectation'``, not soft-deleted, not superseded,
    ``event_time_end`` is NULL (open-ended) or ``>= now`` (still in
    window). Ordered by earliest due first so the ``# You've been
    expecting`` section shows the most time-sensitive predictions at
    the top.
    """
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.EXPECTATION.value,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                or_(
                    ConceptNode.event_time_end.is_(None),  # type: ignore[union-attr]
                    ConceptNode.event_time_end >= now,  # type: ignore[operator]
                ),
            )
            .order_by(ConceptNode.event_time_end)  # type: ignore[arg-type]
        )
    )
    return rows


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


def _load_recent_session_summaries(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    limit: int,
) -> list[ConceptNode]:
    """Load N most-recent session_summary thoughts (plan §6.4 # Recent sessions).

    Discovery key: ``emotion_tags`` JSON contains the literal token
    ``"session_summary"``. Storing as JSON tag (not a new column) is
    the cheapest way to keep the discriminator without bloating
    ConceptNode — we already have ``emotion_tags`` and SQLite ``LIKE``
    on its JSON serialisation is fast enough at single-user scale.
    """
    if limit <= 0:
        return []
    rows = list(
        db.exec(
            select(ConceptNode)
            .where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.type == NodeType.THOUGHT.value,
                ConceptNode.source_session_id.is_not(None),  # type: ignore[union-attr]
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                ConceptNode.superseded_by_id.is_(None),  # type: ignore[union-attr]
                ConceptNode.emotion_tags.like('%"session_summary"%'),  # type: ignore[union-attr]
            )
            .order_by(ConceptNode.created_at.desc())  # type: ignore[arg-type]
            .limit(limit)
        )
    )
    return rows


def build_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    user_message: str,
    now: datetime | None = None,
    pinned_thoughts: list[ConceptNode] | None = None,
    speaker_display: str = "them",
    active_intentions: list[ConceptNode] | None = None,
    recent_session_summaries: list[ConceptNode] | None = None,
    pending_expectations: list[ConceptNode] | None = None,
) -> str:
    """Assemble the user prompt for one turn.

    Rendering order (Spec 5 plan §6.4 — older context first, then the
    speaker context, then current memories, then promises, then the
    live conversation):

        # Recent sessions          (NEW · session_summary thoughts, day-bucketed)
        # Recent thoughts          (existing · L4)
        # About {speaker}          (NEW · pinned thoughts, force-loaded)
        # Recent things you remember happened   (existing · L3 + R4 delta)
        # Promises you've made     (NEW · active intentions)
        # Our recent conversation  (existing · NOW day-bucketed)
        # What they just said      (existing)

    All Spec 5 sections are default-empty so legacy callers (most of
    the test suite) keep their pre-Spec-5 output verbatim.
    """
    pinned_thoughts = pinned_thoughts or []
    active_intentions = active_intentions or []
    recent_session_summaries = recent_session_summaries or []
    pending_expectations = pending_expectations or []

    lines: list[str] = []

    # # Recent sessions — day-bucketed session summaries (oldest first)
    if recent_session_summaries and now is not None:
        # rows came in DESC by created_at; reverse so the section flows
        # OLDER → NEWER (matches DAY_BUCKET_ORDER orientation).
        ordered = list(reversed(recent_session_summaries))
        lines.append("# Recent sessions")
        for s in ordered:
            bucket = day_bucket_of(s.created_at, now)
            lines.append(f"- [{bucket}] {_node_description(s)}")
        lines.append("")
    elif recent_session_summaries:
        # ``now`` is unset (legacy test) — render without bucket label
        # rather than dropping the section entirely.
        ordered = list(reversed(recent_session_summaries))
        lines.append("# Recent sessions")
        for s in ordered:
            lines.append(f"- {_node_description(s)}")
        lines.append("")

    thought_descs: list[str] = []
    event_lines: list[str] = []
    for sm in top_memories:
        node = getattr(sm, "node", sm)
        node_type = getattr(getattr(node, "type", None), "value", getattr(node, "type", ""))
        desc = getattr(node, "description", "")
        if not desc:
            continue
        if node_type == "thought":
            thought_descs.append(desc)
        elif node_type == "event":
            delta = render_event_delta_phrase(node, now) if now is not None else ""
            event_lines.append(f"- {desc}{delta}")

    if thought_descs:
        lines.append("# Recent thoughts you've had about this person")
        for d in thought_descs:
            lines.append(f"- {d}")
        lines.append("")

    # # About {speaker} — pinned (force-loaded) L4 thoughts
    if pinned_thoughts:
        lines.append(f"# About {speaker_display}")
        for thought in pinned_thoughts:
            lines.append(f"- {_node_description(thought)}")
        lines.append("")

    if event_lines:
        lines.append("# Recent things you remember happened")
        lines.extend(event_lines)
        lines.append("")

    # # Promises you've made — active L3 intentions, with R4 delta
    if active_intentions:
        lines.append("# Promises you've made")
        for it in active_intentions:
            delta = render_event_delta_phrase(it, now) if now is not None else ""
            lines.append(f"- {_node_description(it)}{delta}")
        lines.append("")

    # # You've been expecting — active L4 expectations (plan §7 · Spec 6)
    if pending_expectations:
        lines.append("# You've been expecting")
        for exp in pending_expectations:
            delta = render_event_delta_phrase(exp, now) if now is not None else ""
            lines.append(f"- {_node_description(exp)}{delta}")
        lines.append("")

    # # Our recent conversation — day-bucketed when ``now`` is supplied
    if recent_messages:
        lines.append("# Our recent conversation")
        if now is not None:
            grouped: dict[str, list[tuple[str, str]]] = {}
            for m in recent_messages:
                role = getattr(m.role, "value", m.role)
                if role == "user":
                    prefix = "them"
                elif role == "persona":
                    prefix = "me"
                else:
                    prefix = "note"
                bucket = day_bucket_of(m.created_at, now)
                grouped.setdefault(bucket, []).append((prefix, m.content))

            for bucket in DAY_BUCKET_ORDER:
                group = grouped.get(bucket)
                if not group:
                    continue
                lines.append(f"## {bucket}")
                for prefix, content in group:
                    lines.append(f"{prefix}: {content}")
                lines.append("")
        else:
            # Legacy / test path — flat render with no bucket headers.
            for m in recent_messages:
                role = getattr(m.role, "value", m.role)
                if role == "user":
                    prefix = "them"
                elif role == "persona":
                    prefix = "me"
                else:
                    prefix = "note"
                lines.append(f"{prefix}: {m.content}")
            lines.append("")

    lines.append("# What they just said")
    lines.append(user_message)

    return "\n".join(lines)


__all__ = [
    "IncomingMessage",
    "IncomingTurn",
    "AssembledTurn",
    "PersonaFactsView",
    "TurnContext",
    "OnTokenCb",
    "OnTurnDoneCb",
    "STYLE_INSTRUCTIONS",
    "DAY_BUCKET_ORDER",
    "EXPECTATION_MATCH_COSINE_THRESHOLD",
    "assemble_turn",
    "build_system_prompt",
    "build_turn_user_prompt",
    "build_user_prompt",
    "check_pending_expectations",
    "day_bucket_of",
]

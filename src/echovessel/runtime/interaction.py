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
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session as DbSession

from echovessel.channels.base import IncomingMessage, IncomingTurn
from echovessel.core.types import BlockLabel, MessageRole
from echovessel.memory.identity import resolve_internal_user_id
from echovessel.memory.ingest import ingest_message
from echovessel.memory.models import CoreBlock, Persona, RecallMessage
from echovessel.memory.retrieve import (
    list_recall_messages,
    load_core_blocks,
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
    "  unless they ask."
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
    """Read-only snapshot of the five biographic facts the system prompt
    renders in its "# Who you are" section (C option from the
    ``2026-04-persona-facts`` initiative plan).

    The persona row carries fifteen biographic columns, but only these
    five influence the LLM's sense of "who am I playing" enough to be
    worth the prompt budget — timezone, relationship_status, etc are used
    by system code (birthday reminders, locale detection) and stay out
    of the prompt. Unset fields are ``None`` and are skipped by the
    renderer; an all-``None`` view is equivalent to today's pre-facts
    behaviour.
    """

    full_name: str | None = None
    gender: str | None = None
    birth_date: date | None = None
    occupation: str | None = None
    native_language: str | None = None

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
                "resolve_internal_user_id failed; falling back to raw "
                "transport id: %s",
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

        # ---- Step 2b: persona biographic facts (C-option renderer) ----
        # Five columns on the persona row (name / gender / birth_date /
        # occupation / native_language) get injected into the system
        # prompt's "# Who you are" section. A missing Persona row or
        # all-null columns is equivalent to pre-facts behaviour.
        persona_facts = PersonaFactsView.empty()
        try:
            persona_row = ctx.db.get(Persona, ctx.persona_id)
            persona_facts = PersonaFactsView.from_persona_row(persona_row)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "load persona facts failed; continuing with empty view: %s", e
            )

        # ---- Step 3: L3/L4 retrieval (query = last user message) ---
        top_memories: list = []
        try:
            retrieval = retrieve(
                ctx.db,
                backend=ctx.backend,
                persona_id=ctx.persona_id,
                user_id=resolved_user_id,
                query_text=last_message.content,
                embed_fn=ctx.embed_fn,
                top_k=ctx.retrieve_k,
                now=_now(),
                relational_bonus_weight=ctx.relational_bonus_weight,
            )
            top_memories = retrieval.memories
        except Exception as e:  # noqa: BLE001
            log.warning("retrieve failed; continuing with empty memories: %s", e)

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
            log.warning(
                "list_recall_messages failed; continuing with empty L2: %s", e
            )

        # ---- Step 5: prompt assembly -------------------------------
        system_prompt = build_system_prompt(
            persona_display_name=ctx.persona_display_name,
            core_blocks=core_blocks,
            persona_facts=persona_facts,
        )
        user_prompt = build_turn_user_prompt(
            top_memories=top_memories,
            recent_messages=recent,
            turn_messages=turn.messages,
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
                "LLM stream transient error mid-turn (no retry, partial "
                "tokens kept): %s",
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


async def _invoke_on_turn_done(
    on_turn_done: OnTurnDoneCb, turn_id: str
) -> None:
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


def build_system_prompt(
    *,
    persona_display_name: str,
    core_blocks: list[CoreBlock],
    persona_facts: PersonaFactsView | None = None,
) -> str:
    """Assemble the system prompt for one turn.

    Structure follows spec §7.2 plus the ``2026-04-persona-facts``
    C-option addition: five structured facts (name / gender / birth
    year / occupation / native language) render as a ``# Who you are``
    section between the greeting and the existing core blocks. Any of
    those facts being ``None`` skips that bullet; an all-``None`` view
    (or ``persona_facts=None``) produces the same prompt as before the
    initiative landed.

    Core blocks are rendered in a fixed order; missing blocks are
    silently skipped. The STYLE_INSTRUCTIONS block is ALWAYS appended
    last.
    """
    lines: list[str] = [
        f"You are {persona_display_name}, a long-term companion who talks",
        "with this user as a real friend, not an assistant.",
        "",
    ]

    facts = persona_facts or PersonaFactsView.empty()
    fact_lines: list[str] = []
    if facts.full_name:
        fact_lines.append(f"- Name: {facts.full_name}")
    if facts.gender:
        fact_lines.append(f"- Gender: {facts.gender}")
    if facts.birth_date:
        fact_lines.append(f"- Born: {facts.birth_date.year}")
    if facts.occupation:
        fact_lines.append(f"- Occupation: {facts.occupation}")
    if facts.native_language:
        fact_lines.append(f"- Native language: {facts.native_language}")
    if fact_lines:
        lines.append("# Who you are")
        lines.extend(fact_lines)
        lines.append("")

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
    _section("Current mood", BlockLabel.MOOD)

    lines.append(STYLE_INSTRUCTIONS)
    return "\n".join(lines)


def build_turn_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    turn_messages: list[IncomingMessage],
) -> str:
    """v0.4 · user prompt renderer that expands a burst of messages.

    Single-message case (`len(turn_messages) == 1`) degenerates to the
    same output as the legacy `build_user_prompt(..., user_message=...)`
    path — no branch needed.

    Multi-message case prints each message on its own line under the
    `# What they just said` section, preserving order. Per spec
    §17a.1, no transport / channel metadata or timestamps appear in
    the rendered prompt (F10 铁律).
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
    )


def build_user_prompt(
    *,
    top_memories: list,
    recent_messages: list[RecallMessage],
    user_message: str,
) -> str:
    """Assemble the user prompt for one turn.

    Rendering rules (spec §7.3):

      - L4 thoughts: only description.
      - L3 events: only description.
      - Recent messages: role + content. NO channel_id / session_id /
        absolute timestamps. Role is rendered as 'you' / 'me' so the
        literal string 'persona' is never shown either.
    """
    lines: list[str] = []

    thought_descs: list[str] = []
    event_descs: list[str] = []
    for sm in top_memories:
        node = getattr(sm, "node", sm)
        node_type = getattr(getattr(node, "type", None), "value", getattr(node, "type", ""))
        desc = getattr(node, "description", "")
        if not desc:
            continue
        if node_type == "thought":
            thought_descs.append(desc)
        elif node_type == "event":
            event_descs.append(desc)

    if thought_descs:
        lines.append("# Recent thoughts you've had about this person")
        for d in thought_descs:
            lines.append(f"- {d}")
        lines.append("")

    if event_descs:
        lines.append("# Recent things you remember happened")
        for d in event_descs:
            lines.append(f"- {d}")
        lines.append("")

    if recent_messages:
        lines.append("# Our recent conversation")
        for m in recent_messages:
            role = getattr(m.role, "value", m.role)
            # Normalize roles to conversational pronouns so the persona
            # does NOT see the literal 'persona' / 'user' labels.
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
    "assemble_turn",
    "build_system_prompt",
    "build_turn_user_prompt",
    "build_user_prompt",
]

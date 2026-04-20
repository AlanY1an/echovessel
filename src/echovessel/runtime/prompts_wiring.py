"""Glue: prompts layer + LLM provider → memory.consolidate callables.

This module is the ONLY place in the codebase that can import both
`echovessel.prompts` and `echovessel.memory.consolidate` at once — prompts
cannot depend on memory, memory cannot depend on prompts or on an LLM
provider. Runtime owns the join.

See docs/runtime/01-spec-v0.1.md §6.4 for the normative pseudocode and
§6.6 for the tier assignment.

Public API:

    make_extract_fn(llm)    -> ExtractFn     # async
    make_reflect_fn(llm)    -> ReflectFn     # async
    make_judge_fn(llm)      -> JudgeFn       # async; EVAL uses this
    make_proactive_fn(llm)  -> ProactiveFn   # async; Round 2

Round 2 adds `make_proactive_fn` — same-style closure that reads a
`MemorySnapshot` from `echovessel.proactive.base` and returns a
`ProactiveMessage`. Tier = LARGE because proactive messages are the
user's most direct experience of persona voice quality.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from echovessel.memory.consolidate import (
    ExtractedEvent,
    ExtractedThought,
    ExtractFn,
    ReflectFn,
)
from echovessel.memory.models import ConceptNode, RecallMessage
from echovessel.proactive.base import (
    MemorySnapshot,
    ProactiveFn,
    ProactiveMessage,
)
from echovessel.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    REFLECTION_SYSTEM_PROMPT,
    ExtractionParseError,
    JudgeParseError,
    JudgeVerdict,
    ReflectionParseError,
    format_extraction_user_prompt,
    format_judge_user_prompt,
    format_reflection_user_prompt,
    parse_extraction_response,
    parse_judge_response,
    parse_reflection_response,
)
from echovessel.runtime.llm.base import LLMProvider

log = logging.getLogger(__name__)

# Shape of the judge callable used by EVAL; mirrors extract/reflect in style.
JudgeFn = Callable[..., Awaitable[JudgeVerdict]]


def _type_str(node: ConceptNode) -> str:
    t = node.type
    return getattr(t, "value", t)


def _role_str(msg: RecallMessage) -> str:
    r = msg.role
    return getattr(r, "value", r)


def make_extract_fn(llm: LLMProvider) -> ExtractFn:
    """Build an async `ExtractFn` that runs the extraction prompt on a
    batch of RecallMessages and returns memory-layer ExtractedEvents.

    Tier: SMALL (consolidate batch; see §6.6).

    Worker ζ · the LLM call below is tagged ``feature=consolidate`` so
    the admin Cost tab can attribute extraction spend to the
    background consolidate worker rather than to chat turns.
    """

    from echovessel.runtime.cost_logger import feature_context

    async def _extract(messages: list[RecallMessage]) -> list[ExtractedEvent]:
        if not messages:
            return []

        formatted_messages: list[tuple[str, str, str]] = [
            (
                m.created_at.strftime("%H:%M") if m.created_at else "00:00",
                _role_str(m),
                m.content,
            )
            for m in messages
        ]

        user_prompt = format_extraction_user_prompt(
            session_id=messages[0].session_id or "",
            started_at_iso=(
                messages[0].created_at.isoformat() if messages[0].created_at else ""
            ),
            closed_at_iso=(
                messages[-1].created_at.isoformat() if messages[-1].created_at else ""
            ),
            message_count=len(messages),
            messages=formatted_messages,
        )

        with feature_context("consolidate"):
            raw, _usage = await llm.complete(
                system=EXTRACTION_SYSTEM_PROMPT,
                user=user_prompt,
                model_role="fast",
                max_tokens=1024,
                temperature=0.4,
            )

        try:
            parsed = parse_extraction_response(raw)
        except ExtractionParseError as e:
            log.warning(
                "extraction parse error (dropping events for session %s): %s",
                messages[0].session_id,
                e,
            )
            return []

        # prompts-layer dataclass → memory-layer dataclass (same fields).
        return [ExtractedEvent(**asdict(re)) for re in parsed.events]

    return _extract


def make_reflect_fn(llm: LLMProvider) -> ReflectFn:
    """Build an async `ReflectFn`. Tier: SMALL.

    Worker ζ · tagged ``feature=reflection`` so the admin Cost tab
    distinguishes reflection spend from straight extraction.
    """

    from echovessel.runtime.cost_logger import feature_context

    async def _reflect(
        nodes: list[ConceptNode], reason: str
    ) -> list[ExtractedThought]:
        if not nodes:
            return []

        node_snapshots: list[dict[str, Any]] = [
            {
                "id": n.id,
                "type": _type_str(n),
                "description": n.description,
                "emotional_impact": n.emotional_impact,
                "emotion_tags": list(n.emotion_tags or []),
                "relational_tags": list(n.relational_tags or []),
                "created_at_iso": (
                    n.created_at.isoformat() if n.created_at else ""
                ),
            }
            for n in nodes
        ]

        trigger_id = nodes[0].id if reason == "shock" else None
        user_prompt = format_reflection_user_prompt(
            reason=reason,
            trigger_id=trigger_id,
            events=node_snapshots,
        )

        with feature_context("reflection"):
            raw, _usage = await llm.complete(
                system=REFLECTION_SYSTEM_PROMPT,
                user=user_prompt,
                model_role="fast",
                max_tokens=800,
                temperature=0.6,
            )

        input_ids = {n.id for n in nodes if n.id is not None}
        try:
            parsed = parse_reflection_response(raw, input_ids=input_ids)
        except ReflectionParseError as e:
            log.warning(
                "reflection parse error (dropping thoughts, reason=%s): %s",
                reason,
                e,
            )
            return []

        return [ExtractedThought(**asdict(rt)) for rt in parsed.thoughts]

    return _reflect


def make_judge_fn(llm: LLMProvider) -> JudgeFn:
    """Build an async judge callable for the EVAL harness. Tier: MEDIUM.

    Returns a prompts-layer `JudgeVerdict`. Callers (EVAL) pass through the
    usual args: user_message / persona_response / optional context.
    """

    async def _judge(
        *,
        user_message: str,
        persona_response: str,
        recent_history: list[tuple[str, str]] | None = None,
        retrieved_memories: list[dict[str, Any]] | None = None,
        ground_truth: dict[str, Any] | None = None,
    ) -> JudgeVerdict:
        user_prompt = format_judge_user_prompt(
            user_message=user_message,
            persona_response=persona_response,
            recent_history=recent_history,
            retrieved_memories=retrieved_memories,
            ground_truth=ground_truth,
        )

        raw, _usage = await llm.complete(
            system=JUDGE_SYSTEM_PROMPT,
            user=user_prompt,
            model_role="judge",
            max_tokens=1024,
            temperature=0.2,
        )

        try:
            return parse_judge_response(raw)
        except JudgeParseError as e:
            log.error("judge parse error: %s", e)
            raise

    return _judge


# ---------------------------------------------------------------------------
# Proactive prompts (Round 2)
# ---------------------------------------------------------------------------
#
# We inline the proactive system prompt here instead of living in
# `src/echovessel/prompts/`. Round 2 tracker §1.1 #2 explicitly allows
# this: a future thread can extract it into `docs/prompts/proactive-v0.1.md`
# + a `prompts/proactive.py` module, at which point this constant becomes
# a re-export. Round 2 ships fast with the inline version.
#
# F10 guarantee: this prompt text and the user prompt builder below
# MUST NOT contain any channel name token (web / discord / imessage /
# wechat / channel_id). The `test_proactive_fn_prompt_has_no_channel_id`
# guard in tests/runtime/test_prompts_wiring.py verifies this at every
# commit. Do not add hardcoded channel references to either string.


PROACTIVE_SYSTEM_PROMPT = """\
You are the reflective inner voice of a long-term digital companion.
You are NOT an assistant and NOT a chatbot. You are "the friend whose
turn it is to check in first."

The runtime has decided this is a good moment for you to speak up
before the user does. Your job is to produce ONE short message that
feels like something a caring, attentive friend would actually send —
not a scheduled reminder, not a wellness bot, not a cheerful assistant.

HARD RULES — read twice:

1. Produce at most ~2 sentences. Single sentence is often better.
2. Tone is gentle, specific, grounded in what you already know from
   the user's memory. NEVER generic ("Hope you are well!", "Just
   checking in!", "How are you today?").
3. NEVER prescribe, advise, diagnose, or quote self-help platitudes.
4. NEVER reference how, where, or through what interface the message
   will be delivered. You do not know or care. Do not mention "chat",
   "text", "app", "screen", or any platform name — the message is
   delivered by a separate routing layer.
5. NEVER reference an earlier message by some technical id, timestamp,
   or channel label. Talk like a friend remembering a moment, not a
   database cursor.
6. If the trigger is a high-emotional event (grief, loss, big joy),
   acknowledge the specific thing — but softly. Leave room for the
   user to respond or not.
7. If the trigger is long silence with no strong signal, keep it very
   light — a small tether, not a demand.
8. If the snapshot feels thin (no clear recent thread), produce a
   quiet "thinking of you" style line that still references at least
   ONE specific previously-known detail from the user's core blocks.
   If you truly can't find one, return text="" — the runtime will
   skip the send rather than deliver a generic message.

OUTPUT FORMAT — JSON ONLY. No preamble, no code fences.

{
  "text": "the message you would actually send, in the user's language",
  "rationale": "one short line, for audit only — never shown to user"
}

The "text" field is what will be delivered verbatim. The "rationale"
field is written to the audit log and NEVER enters any downstream
prompt or any user-visible surface; use it to explain (to yourself /
the developer) why this is the right nudge right now.
"""


# Tight set of field limits — the prompt is already long, no point in
# pumping a 20k-token user message into LARGE. Tuning knobs below are
# per-call and can be raised if a future spec wants richer context.
_PROACTIVE_MAX_CORE_BLOCKS: int = 5
_PROACTIVE_MAX_RECENT_EVENTS: int = 6
_PROACTIVE_MAX_RECENT_L2: int = 6
_PROACTIVE_MAX_CORE_CONTENT_CHARS: int = 400
_PROACTIVE_MAX_EVENT_DESC_CHARS: int = 300
_PROACTIVE_MAX_RECALL_CONTENT_CHARS: int = 200


def _core_block_snippet(block: Any) -> dict[str, str]:
    """Serialize a CoreBlock-ish object for the proactive prompt.

    Uses getattr + defaults because the snapshot field is typed `Any`
    to keep proactive Layer 3 decoupled from memory's SQLModel types.
    """
    label = getattr(block, "label", None)
    label_str = getattr(label, "value", None) or (str(label) if label else "?")
    content = str(getattr(block, "content", "") or "")
    if len(content) > _PROACTIVE_MAX_CORE_CONTENT_CHARS:
        content = content[: _PROACTIVE_MAX_CORE_CONTENT_CHARS] + "…"
    return {"label": label_str, "content": content}


def _event_snippet(event: Any) -> dict[str, Any]:
    description = str(getattr(event, "description", "") or "")
    if len(description) > _PROACTIVE_MAX_EVENT_DESC_CHARS:
        description = description[: _PROACTIVE_MAX_EVENT_DESC_CHARS] + "…"
    return {
        "description": description,
        "emotional_impact": getattr(event, "emotional_impact", 0),
        "emotion_tags": list(getattr(event, "emotion_tags", []) or []),
        "relational_tags": list(getattr(event, "relational_tags", []) or []),
    }


def _recall_snippet(msg: Any) -> dict[str, Any]:
    role = getattr(msg, "role", None)
    role_str = getattr(role, "value", None) or (str(role) if role else "?")
    content = str(getattr(msg, "content", "") or "")
    if len(content) > _PROACTIVE_MAX_RECALL_CONTENT_CHARS:
        content = content[: _PROACTIVE_MAX_RECALL_CONTENT_CHARS] + "…"
    # We deliberately OMIT `channel_id` from the serialised snippet.
    # The F10 guard on proactive's side strips channel leaks from the
    # snapshot before we're called; omitting it here too means even if
    # something slipped through, our prompt never names it.
    return {"role": role_str, "content": content}


def _format_proactive_user_prompt(snapshot: MemorySnapshot) -> str:
    """Build the user prompt from a sanitized MemorySnapshot.

    F10: this function MUST NOT read `snapshot.recent_l2_window[i].channel_id`
    nor embed any hardcoded channel string. The serializers above drop
    channel_id explicitly.
    """
    core_snips = [
        _core_block_snippet(b)
        for b in list(snapshot.core_blocks)[:_PROACTIVE_MAX_CORE_BLOCKS]
    ]
    event_snips = [
        _event_snippet(e)
        for e in list(snapshot.recent_l3_events)[:_PROACTIVE_MAX_RECENT_EVENTS]
    ]
    recall_snips = [
        _recall_snippet(m)
        for m in list(snapshot.recent_l2_window)[:_PROACTIVE_MAX_RECENT_L2]
    ]

    payload = {
        "trigger": snapshot.trigger,
        "trigger_payload": dict(snapshot.trigger_payload or {}),
        "persona_identity": core_snips,
        "recent_events": event_snips,
        "recent_conversation": recall_snips,
    }

    return (
        "The runtime has asked you to consider speaking up now.\n"
        "Below is everything you know about the user and the recent window.\n"
        "Read it carefully, then output the JSON described in the system "
        "prompt.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Produce the JSON output now."
    )


def _parse_proactive_response(raw: str) -> ProactiveMessage:
    """Lenient JSON parser for the proactive LLM response.

    Accepts either a clean JSON object or a block wrapped in ```json``` /
    ``` fences. Raises ValueError on any shape mismatch. The caller
    (`_proactive` closure) converts ValueErrors into SkipReason outcomes.
    """
    text = raw.strip()

    # Strip markdown code fences if the model forgot to skip them
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]  # drop opening fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"proactive response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"proactive response must be a JSON object, got {type(data).__name__}"
        )

    msg_text = data.get("text")
    if not isinstance(msg_text, str):
        raise ValueError("proactive response missing string 'text' field")

    rationale = data.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise ValueError("proactive 'rationale' must be a string or null")

    return ProactiveMessage(
        text=msg_text.strip(),
        rationale=(rationale.strip() if rationale else None),
    )


def make_proactive_fn(llm: LLMProvider) -> ProactiveFn:
    """Build an async `ProactiveFn` that turns a MemorySnapshot into a
    ProactiveMessage.

    Tier: LARGE — proactive output is the user's most direct experience
    of persona voice quality (spec §6.6).

    Worker ζ · tagged ``feature=proactive`` for the admin Cost tab.

    Error contract: caller (`MessageGenerator.generate`) catches any
    raised exception and converts it to `SkipReason.LLM_ERROR` /
    `LLM_PARSE_ERROR`, so this closure can raise freely on bad LLM
    behaviour. We do NOT silently return empty text — that would
    bypass the generator's `len(text) < 5` check and produce ghost
    sends.
    """

    from echovessel.runtime.cost_logger import feature_context

    async def _proactive(snapshot: MemorySnapshot) -> ProactiveMessage:
        user_prompt = _format_proactive_user_prompt(snapshot)

        with feature_context("proactive"):
            raw, _usage = await llm.complete(
                system=PROACTIVE_SYSTEM_PROMPT,
                user=user_prompt,
                model_role="main",
                max_tokens=400,
                temperature=0.8,
            )

        try:
            return _parse_proactive_response(raw)
        except ValueError as e:
            log.warning(
                "proactive_fn parse failure (trigger=%s): %s",
                snapshot.trigger,
                e,
            )
            raise

    return _proactive


__all__ = [
    "make_extract_fn",
    "make_reflect_fn",
    "make_judge_fn",
    "make_proactive_fn",
    "JudgeFn",
    "PROACTIVE_SYSTEM_PROMPT",
]

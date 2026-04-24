"""LLM-driven extraction: chunk → list[ContentItem].

Implements the "universal" half of the tracker: one prompt per chunk,
no format-specific parsers. The prompt body mirrors the authoritative
spec in ``docs/prompts/import-extraction-v0.1.md`` — only the essential
rule text is inlined here so tests and the runtime agent can ship
without reading the disk at import time. The full authoritative prompt
still lives in the docs tree; this constant is allowed to drift
slightly on whitespace but MUST track the schema rules in §5.3 / §5.4
of the import spec.

The caller (``pipeline.run_pipeline``) provides an ``llm`` object that
implements the ``LLMProvider`` Protocol from ``runtime/llm/base.py``.
We intentionally do NOT import that module (tracker §4 #11 —
``import_`` may not depend on runtime), so we duck-type the call site:
``await llm.complete(system=..., user=..., model_role="fast", ...)``.

Tracker hard constraint #4: extraction uses the "fast" model role.
The literal string is passed through to the provider's ``model_for()``
lookup.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from echovessel.import_.errors import ExtractionError
from echovessel.import_.models import Chunk, ContentItem, DroppedItem

log = logging.getLogger(__name__)


#: Model role accepted by the LLM provider. Tracker hard constraint #4.
#: One of the canonical roles in ``echovessel.runtime.llm.base.MODEL_ROLES``.
EXTRACTION_ROLE: str = "fast"


#: Closed vocabulary for the ``relational_tags`` field on L3.event writes.
RELATIONAL_TAG_VOCAB: frozenset[str] = frozenset(
    {
        "identity-bearing",
        "unresolved",
        "vulnerability",
        "turning-point",
        "correction",
        "commitment",
    }
)


#: The four legal LLM-side targets (v0.5 · plan §1). ``L1.self_block``
#: and ``L1.relationship_block`` were removed alongside the deletion of
#: their core_block labels — persona-side reflection now writes
#: L4.thought with ``subject='persona'``; third-party people / places
#: route to the L5 entities table via the consolidate extraction path.
LEGAL_LLM_TARGETS: frozenset[str] = frozenset(
    {
        "L1.persona_block",
        "L1.user_block",
        "L3.event",
        "L4.thought",
    }
)


IMPORT_EXTRACTION_SYSTEM_PROMPT: str = """\
You are an extraction engine for a long-term digital companion's memory
system. Your input is a CHUNK of external text that the user is importing
into the persona's memory.

Your job: read the chunk and output a list of atomic MEMORY WRITES that
this chunk justifies. Each write targets ONE of four legal memory
locations, and each write carries a verbatim EVIDENCE QUOTE from the
chunk text.

# Four legal targets (no others)
- L1.persona_block        identity-level fact about the persona
- L1.user_block           fact about the user, requires `category`
- L3.event                discrete episode, requires emotional_impact/emotion_tags
- L4.thought              persona first-person reflection, NOT user-authored

# Output (strict JSON, no markdown, no commentary)
{
  "writes": [ { "target": "...", ... } ],
  "chunk_summary": "..."
}

Every write MUST carry a verbatim ``evidence_quote`` substring from the
chunk text. Relational tags must come from the closed vocabulary
{identity-bearing, unresolved, vulnerability, turning-point, correction,
commitment}. ``emotional_impact`` is an integer in [-10, 10]. Empty
writes lists are valid for filler chunks.
"""


def format_user_prompt(
    *,
    chunk: Chunk,
    persona_context: str = "",
    source_label: str = "",
) -> str:
    """Build the user-message body for one chunk.

    D4 / F10: no ``channel_id`` may appear in this string. See
    ``docs/DISCUSSION.md`` 2026-04-14 D4 for the rule.
    """
    label = source_label or chunk.source_label or "pasted text"
    return (
        f"You are processing chunk {chunk.chunk_index + 1} of "
        f"{chunk.total_chunks} from a user import. "
        f'The source is labelled "{label}". The persona being built '
        f"is described below; use it as context (not as target).\n\n"
        f"Persona context:\n{persona_context or '(none)'}\n\n"
        f"Chunk text (verbatim, UTF-8):\n---\n{chunk.content}\n---\n\n"
        "Produce the JSON output now."
    )


async def extract_chunk(
    chunk: Chunk,
    *,
    llm: Any,
    persona_id: str,
    user_id: str,
    persona_context: str = "",
    source_label: str = "",
    imported_from: str = "",
) -> tuple[list[ContentItem], list[DroppedItem], str]:
    """Run one LLM extraction call and return validated items.

    Returns a 3-tuple ``(items, dropped, chunk_summary)``.

    Raises:
        ExtractionError(fatal=False): LLM returned non-JSON content.
            The pipeline may retry or pause for resume.
        ExtractionError(fatal=True): LLM returned JSON whose top-level
            shape is not a dict with the expected keys. Pipeline
            aborts this chunk.
    """
    user_prompt = format_user_prompt(
        chunk=chunk,
        persona_context=persona_context,
        source_label=source_label,
    )
    raw, _usage = await llm.complete(
        system=IMPORT_EXTRACTION_SYSTEM_PROMPT,
        user=user_prompt,
        model_role=EXTRACTION_ROLE,
        max_tokens=2048,
        temperature=0.3,
    )
    return parse_llm_response(
        raw,
        chunk=chunk,
        persona_id=persona_id,
        user_id=user_id,
        imported_from=imported_from,
    )


def parse_llm_response(
    raw: str,
    *,
    chunk: Chunk,
    persona_id: str,
    user_id: str,
    imported_from: str = "",
) -> tuple[list[ContentItem], list[DroppedItem], str]:
    """Parse and validate a raw LLM response string into ContentItems.

    This is the pure-function half of ``extract_chunk`` — unit tests
    hit it directly without needing an async LLM stub.
    """
    from echovessel.import_.routing import translate_llm_write  # lazy import

    raw_stripped = raw.strip()
    # Tolerate LLMs that wrap JSON in a ```json fence.
    if raw_stripped.startswith("```"):
        fence_end = raw_stripped.rfind("```")
        if fence_end > 3:
            inner = raw_stripped[3:fence_end]
            if inner.startswith("json"):
                inner = inner[4:]
            raw_stripped = inner.strip()

    try:
        data = json.loads(raw_stripped)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"extract_chunk: LLM output is not valid JSON: {exc}",
            fatal=False,
        ) from exc

    if not isinstance(data, dict):
        raise ExtractionError(
            f"extract_chunk: expected JSON object at top level, "
            f"got {type(data).__name__}",
            fatal=True,
        )

    writes_raw = data.get("writes", [])
    if not isinstance(writes_raw, list):
        raise ExtractionError(
            "extract_chunk: 'writes' must be a list",
            fatal=True,
        )

    chunk_summary = str(data.get("chunk_summary", ""))

    items: list[ContentItem] = []
    dropped: list[DroppedItem] = []

    for raw_write in writes_raw:
        if not isinstance(raw_write, dict):
            dropped.append(
                DroppedItem(
                    chunk_index=chunk.chunk_index,
                    reason="write is not a dict",
                    payload_excerpt=str(raw_write)[:80],
                )
            )
            continue
        target = raw_write.get("target", "")
        if target not in LEGAL_LLM_TARGETS:
            dropped.append(
                DroppedItem(
                    chunk_index=chunk.chunk_index,
                    reason="unknown target",
                    raw_target=str(target),
                    payload_excerpt=json.dumps(raw_write, ensure_ascii=False)[:120],
                )
            )
            continue
        try:
            item = translate_llm_write(
                raw_write,
                chunk=chunk,
                persona_id=persona_id,
                user_id=user_id,
                imported_from=imported_from,
            )
        except (ValueError, KeyError, TypeError) as exc:
            dropped.append(
                DroppedItem(
                    chunk_index=chunk.chunk_index,
                    reason=f"validation failed: {exc}",
                    raw_target=str(target),
                    payload_excerpt=json.dumps(raw_write, ensure_ascii=False)[:120],
                )
            )
            continue
        if item is None:  # routing chose to silently drop (e.g. low confidence)
            dropped.append(
                DroppedItem(
                    chunk_index=chunk.chunk_index,
                    reason="routing dropped (confidence / unsupported)",
                    raw_target=str(target),
                    payload_excerpt=json.dumps(raw_write, ensure_ascii=False)[:120],
                )
            )
            continue
        items.append(item)

    return items, dropped, chunk_summary


__all__ = [
    "EXTRACTION_ROLE",
    "IMPORT_EXTRACTION_SYSTEM_PROMPT",
    "LEGAL_LLM_TARGETS",
    "RELATIONAL_TAG_VOCAB",
    "extract_chunk",
    "format_user_prompt",
    "parse_llm_response",
]

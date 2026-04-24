"""Dispatch LLM-extracted writes onto the memory import API.

Two responsibilities:

1. :func:`translate_llm_write` — collapse the 4-target LLM union
   (``L1.persona_block`` / ``L1.user_block`` / ``L3.event`` /
   ``L4.thought``) into the four-content-type memory whitelist.
   Validates fields and returns a :class:`ContentItem` ready for
   dispatch — or ``None`` when the write must be dropped (e.g.
   confidence below 0.5, missing required fields, etc.).

2. :func:`dispatch_item` — actually call ``memory.import_content`` in
   the caller's DB session. Returns the fresh row IDs.

v0.5 cleanup: ``L1.self_block`` and ``L1.relationship_block`` were
removed (plan §1). Persona-side reflection routes to L4.thought with
``subject='persona'`` via slow_cycle; third-party people / places /
orgs route to the L5 entities table via the extraction pipeline.
Importer LLMs that still emit those legacy targets see a
``ValueError`` here — no silent compatibility shim (CLAUDE.md
``no backcompat shims`` rule).

Hard-coded behavior decisions (tracker §2.1 mapping divergence):

* Confidence gate: L1.* writes with ``confidence < 0.5`` are silently
  dropped (mirrors import spec §5.4).

* Evidence-quote substring check is done case-insensitively so we
  tolerate trivial normalization (tracker §5.4 rule).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session as DbSession

from echovessel.import_.errors import RoutingError
from echovessel.import_.models import (
    ALLOWED_CONTENT_TYPES,
    Chunk,
    ContentItem,
)
from echovessel.memory.imports import (
    ImportResult,
    import_content,
)

log = logging.getLogger(__name__)


#: Target → memory content_type mapping table. v0.5 · 4 targets only.
_TARGET_TO_CONTENT_TYPE: dict[str, str] = {
    "L1.persona_block": "persona_traits",
    "L1.user_block": "user_identity_facts",
    "L3.event": "user_events",
    "L4.thought": "user_reflections",
}


_MIN_CONFIDENCE: float = 0.5


def translate_llm_write(
    raw_write: dict[str, Any],
    *,
    chunk: Chunk,
    persona_id: str,
    user_id: str,
    imported_from: str = "",
) -> ContentItem | None:
    """Validate one LLM write and produce a :class:`ContentItem`.

    Returns ``None`` to signal "drop silently" — the caller records
    a :class:`DroppedItem` for audit. Raises ``ValueError`` for
    hard schema violations that should also produce a drop (but
    with a reason string).
    """
    target = raw_write.get("target", "")
    evidence = str(raw_write.get("evidence_quote", "")).strip()
    if not evidence:
        raise ValueError("missing evidence_quote")

    # Case-insensitive substring check against the chunk content.
    if evidence.lower() not in chunk.content.lower():
        raise ValueError("evidence_quote not a substring of chunk")

    # ----- L1.persona_block → persona_traits ---------------------
    if target == "L1.persona_block":
        content = _require_short_content(raw_write)
        if _confidence(raw_write) < _MIN_CONFIDENCE:
            return None
        return ContentItem(
            content_type="persona_traits",
            payload={
                "persona_id": persona_id,
                "user_id": user_id,
                "content": content,
                "source_label": chunk.source_label,
                "chunk_index": chunk.chunk_index,
            },
            chunk_index=chunk.chunk_index,
            evidence_quote=evidence,
            raw_target=target,
        )

    # ----- L1.user_block → user_identity_facts -------------------
    if target == "L1.user_block":
        content = _require_short_content(raw_write)
        category = str(raw_write.get("category", "other"))
        if _confidence(raw_write) < _MIN_CONFIDENCE:
            return None
        return ContentItem(
            content_type="user_identity_facts",
            payload={
                "persona_id": persona_id,
                "user_id": user_id,
                "content": content,
                "category": category,
                "source_label": chunk.source_label,
                "chunk_index": chunk.chunk_index,
            },
            chunk_index=chunk.chunk_index,
            evidence_quote=evidence,
            raw_target=target,
        )

    # ----- L3.event → user_events --------------------------------
    if target == "L3.event":
        description = str(raw_write.get("description", "")).strip()
        if not description:
            raise ValueError("L3.event missing description")
        impact = raw_write.get("emotional_impact", 0)
        if not isinstance(impact, int):
            raise ValueError(
                f"L3.event.emotional_impact must be int, got {type(impact).__name__}"
            )
        if not -10 <= impact <= 10:
            raise ValueError(
                f"L3.event.emotional_impact out of range: {impact}"
            )
        emotion_tags = _sanitize_str_list(
            raw_write.get("emotion_tags", []), max_items=5
        )
        relational_tags = _filter_relational_tags(
            raw_write.get("relational_tags", [])
        )
        return ContentItem(
            content_type="user_events",
            payload={
                "persona_id": persona_id,
                "user_id": user_id,
                "events": [
                    {
                        "description": description,
                        "emotional_impact": impact,
                        "emotion_tags": emotion_tags,
                        "relational_tags": relational_tags,
                    }
                ],
            },
            chunk_index=chunk.chunk_index,
            evidence_quote=evidence,
            raw_target=target,
        )

    # ----- L4.thought → user_reflections -------------------------
    if target == "L4.thought":
        description = str(raw_write.get("description", "")).strip()
        if not description:
            raise ValueError("L4.thought missing description")
        return ContentItem(
            content_type="user_reflections",
            payload={
                "persona_id": persona_id,
                "user_id": user_id,
                "thoughts": [
                    {
                        "description": description,
                        "emotional_impact": 0,
                        "emotion_tags": [],
                        "relational_tags": [],
                    }
                ],
            },
            chunk_index=chunk.chunk_index,
            evidence_quote=evidence,
            raw_target=target,
        )

    # Unknown target — caller already filters via LEGAL_LLM_TARGETS
    # but this branch is a second line of defence.
    raise ValueError(f"unknown target: {target!r}")


def dispatch_item(
    item: ContentItem,
    *,
    db: DbSession,
    source: str,
) -> tuple[ImportResult, list[int]]:
    """Write one :class:`ContentItem` to memory.

    Returns a tuple ``(import_result, new_concept_node_ids)``.
    ``new_concept_node_ids`` is the subset of concept_nodes that
    landed on disk — used by the embed pass to know which rows need
    vectorization. For L1.* writes the list is empty.

    Raises:
        ValueError: ``item.content_type`` is not in the memory
            whitelist. Tracker §4 #2 hard constraint.
    """
    # Strict whitelist guard — this must raise for non-whitelist
    # content_types. The ContentItem dataclass already enforces this
    # at construction time, but we re-check for defence-in-depth
    # since dispatch could theoretically be called with a raw dict.
    if item.content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(
            f"dispatch_item: unknown content_type {item.content_type!r}. "
            f"Allowed: {sorted(ALLOWED_CONTENT_TYPES)}"
        )

    payload = dict(item.payload)

    if item.content_type == "user_events":
        result = import_content(
            db,
            source=source,
            content_type="user_events",
            payload=payload,
        )
        return result, list(result.event_ids)

    if item.content_type == "user_reflections":
        result = import_content(
            db,
            source=source,
            content_type="user_reflections",
            payload=payload,
        )
        return result, list(result.thought_ids)

    # persona_traits / user_identity_facts
    result = import_content(
        db,
        source=source,
        content_type=item.content_type,
        payload=payload,
    )
    return result, []


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _require_short_content(raw_write: dict[str, Any]) -> str:
    content = raw_write.get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("L1.* write missing 'content'")
    if len(content) > 200:
        raise ValueError(f"L1.* content over 200 chars ({len(content)})")
    return content


def _confidence(raw_write: dict[str, Any]) -> float:
    c = raw_write.get("confidence", 1.0)
    try:
        return float(c)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_str_list(value: Any, *, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value[:max_items]:
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip().lower())
    return out


def _filter_relational_tags(value: Any) -> list[str]:
    from echovessel.import_.extraction import RELATIONAL_TAG_VOCAB

    if not isinstance(value, list):
        return []
    return [
        v.strip().lower()
        for v in value
        if isinstance(v, str) and v.strip().lower() in RELATIONAL_TAG_VOCAB
    ][:3]


__all__ = [
    "translate_llm_write",
    "dispatch_item",
    "RoutingError",
]

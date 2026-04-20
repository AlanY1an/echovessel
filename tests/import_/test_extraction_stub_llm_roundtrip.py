"""StubLLM returning pre-canned JSON is parsed into ContentItems."""

from __future__ import annotations

import json

from echovessel.import_.extraction import extract_chunk, parse_llm_response
from echovessel.import_.models import Chunk


class _StubLLM:
    def __init__(self, canned: str) -> None:
        self.canned = canned
        self.calls: list[dict] = []

    async def complete(self, system: str, user: str, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        return self.canned, None


CANNED_WRITES = {
    "writes": [
        {
            "target": "L1.persona_block",
            "content": "她很怕鬼但对她爱的人坚定",
            "confidence": 0.92,
            "evidence_quote": "very durable fact here",
        },
        {
            "target": "L3.event",
            "description": "Anna argued with Alan about his mother",
            "approximate_date": "2023-03-18",
            "emotional_impact": -5,
            "emotion_tags": ["conflict", "shame"],
            "relational_tags": ["unresolved"],
            "filling_description": [],
            "evidence_quote": "Anna argued with Alan about his mother",
        },
    ],
    "chunk_summary": "Anna 的一段冲突回忆",
}


def _chunk_with(content: str, *, index: int = 0, total: int = 1) -> Chunk:
    return Chunk(
        chunk_index=index,
        total_chunks=total,
        content=content,
        source_label="unit",
    )


async def test_extract_chunk_roundtrip():
    # Evidence quotes must be substrings of the chunk text — build one
    # that contains both verbatim.
    chunk_body = (
        "The chunk contains: very durable fact here, and then a note that\n"
        "Anna argued with Alan about his mother, leading to a cold dinner."
    )
    stub = _StubLLM(json.dumps(CANNED_WRITES, ensure_ascii=False))

    items, dropped, summary = await extract_chunk(
        _chunk_with(chunk_body),
        llm=stub,
        persona_id="p_test",
        user_id="self",
        imported_from="hash-aaa",
    )
    assert not dropped
    assert summary == "Anna 的一段冲突回忆"
    kinds = {it.content_type for it in items}
    assert "persona_traits" in kinds
    assert "user_events" in kinds
    # Model role must be "fast" per tracker hard constraint #4
    assert stub.calls[0]["model_role"] == "fast"


def test_parse_ignores_unknown_targets_via_dropped():
    payload = {
        "writes": [
            {"target": "L5.nonsense", "evidence_quote": "present"},
            {
                "target": "L3.event",
                "description": "event description",
                "approximate_date": None,
                "emotional_impact": 1,
                "emotion_tags": [],
                "relational_tags": [],
                "filling_description": [],
                "evidence_quote": "present",
            },
        ],
        "chunk_summary": "",
    }
    chunk = _chunk_with("this text contains: present")
    items, dropped, _ = parse_llm_response(
        json.dumps(payload),
        chunk=chunk,
        persona_id="p_test",
        user_id="self",
    )
    assert len(items) == 1
    assert items[0].content_type == "user_events"
    assert len(dropped) == 1
    assert dropped[0].raw_target == "L5.nonsense"

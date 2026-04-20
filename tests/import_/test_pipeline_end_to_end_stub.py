"""End-to-end run_pipeline walk-through with a StubLLM."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.import_ import PipelineEventLike, run_pipeline


class _StubLLM:
    def __init__(self, per_chunk_responses: list[str]) -> None:
        self.responses = list(per_chunk_responses)

    async def complete(self, system: str, user: str, **kwargs):
        if not self.responses:
            return '{"writes": [], "chunk_summary": ""}', None
        return self.responses.pop(0), None


DIARY_TEXT = """\
First paragraph: today Anna thought about her dog.

Second paragraph: Anna remembered the window where Mochi used to sit.
"""

CHUNK_1 = {
    "writes": [
        {
            "target": "L1.user_block",
            "content": "用户有一只叫 Mochi 的猫",
            "category": "pet",
            "confidence": 0.92,
            "evidence_quote": "her dog",
        }
    ],
    "chunk_summary": "关于 Mochi 的背景",
}

CHUNK_2 = {
    "writes": [
        {
            "target": "L3.event",
            "description": "Anna remembered Mochi's favorite window",
            "approximate_date": "2024-06-14",
            "emotional_impact": -5,
            "emotion_tags": ["grief", "longing"],
            "relational_tags": ["unresolved"],
            "filling_description": [],
            "evidence_quote": "the window where Mochi used to sit",
        }
    ],
    "chunk_summary": "用户回忆 Mochi 常去的窗口",
}


async def test_pipeline_end_to_end(db_session_factory, backend, engine):
    captured: list[PipelineEventLike] = []

    async def sink(event: PipelineEventLike) -> None:
        captured.append(event)

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[1.0 / 384.0] * 384 for _ in texts]

    report = await run_pipeline(
        pipeline_id="pl-test-1",
        raw_bytes=DIARY_TEXT.encode(),
        suffix=".txt",
        source_label="Anna diary",
        file_hash="hash-diary",
        persona_id="p_test",
        user_id="self",
        llm=_StubLLM(
            [
                json.dumps(CHUNK_1, ensure_ascii=False),
                json.dumps(CHUNK_2, ensure_ascii=False),
            ]
        ),
        db_session_factory=db_session_factory,
        embed_fn=fake_embed,
        vector_writer=backend.insert_vector,
        event_sink=sink,
    )

    assert report.status == "success"
    assert report.total_chunks == 2
    assert report.processed_chunks == 2
    # A user_block append + an event were both recorded.
    assert report.writes_by_target.get("user_identity_facts", 0) == 1
    assert report.writes_by_target.get("user_events", 0) == 1
    assert report.embedded_vector_count == 1  # only the event is a concept node

    event_kinds = {e.type for e in captured}
    assert "pipeline.start" in event_kinds
    assert "chunk.start" in event_kinds
    assert "chunk.done" in event_kinds
    assert "pipeline.done" in event_kinds

    # And the vector table received the row.
    with DbSession(engine) as db:
        count = db.exec(text("SELECT COUNT(*) FROM concept_nodes_vec")).one()
        assert count[0] == 1


async def test_pipeline_raises_embed_error_when_fn_missing(
    db_session_factory,
):
    async def sink(event):
        pass

    report = await run_pipeline(
        pipeline_id="pl-test-2",
        raw_bytes=DIARY_TEXT.encode(),
        suffix=".txt",
        source_label="Anna diary",
        file_hash="hash-diary2",
        persona_id="p_test",
        user_id="self",
        llm=_StubLLM(
            [
                json.dumps(CHUNK_1, ensure_ascii=False),
                json.dumps(CHUNK_2, ensure_ascii=False),
            ]
        ),
        db_session_factory=db_session_factory,
        embed_fn=None,  # deliberately missing
        vector_writer=None,
        event_sink=sink,
    )
    # Dispatch succeeds for both chunks but embed pass fails, so the
    # pipeline finishes in "failed" with writes intact and an embed
    # error recorded.
    assert report.status == "failed"
    assert any("embed" in d.reason for d in report.dropped_items)

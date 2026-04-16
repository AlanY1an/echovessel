"""ImporterFacade.start_pipeline integration with the real pipeline."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.runtime.importer_facade import ImporterFacade


class _StubLLM:
    provider_name = "stub"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def complete(self, system: str, user: str, **kwargs):
        if not self.responses:
            return '{"writes": [], "chunk_summary": ""}'
        return self.responses.pop(0)


CANNED = {
    "writes": [
        {
            "target": "L1.user_block",
            "content": "用户有一只叫 Mochi 的猫",
            "category": "pet",
            "confidence": 0.9,
            "evidence_quote": "Mochi",
        },
        {
            "target": "L3.event",
            "description": "Anna 回到 Mochi 常去的窗户下面",
            "approximate_date": "2024-06-14",
            "emotional_impact": -5,
            "emotion_tags": ["grief"],
            "relational_tags": ["unresolved"],
            "filling_description": [],
            "evidence_quote": "Mochi 常去的窗户",
        },
    ],
    "chunk_summary": "用户回忆 Mochi 的窗户",
}


class _MemStub:
    """Tiny memory_api stub the facade can introspect."""

    def __init__(self, db_factory) -> None:
        self._db_factory = db_factory


async def test_facade_start_pipeline_runs_real_pipeline(
    db_session_factory, backend, engine
):
    mem = _MemStub(db_session_factory)
    facade = ImporterFacade(
        llm_provider=_StubLLM([json.dumps(CANNED, ensure_ascii=False)]),
        voice_service=None,
        memory_api=mem,
    )

    text_payload = "Anna is thinking about Mochi 常去的窗户 today."
    pipeline_id = await facade.start_pipeline(
        "upload-1",
        raw_bytes=text_payload.encode(),
        suffix=".txt",
        source_label="diary",
        file_hash="h-1",
        persona_id="p_test",
        user_id="self",
        embed_fn=lambda texts: [[1.0 / 384.0] * 384 for _ in texts],
        vector_writer=backend.insert_vector,
    )

    collected = []

    async def consume():
        async for ev in facade.subscribe_events(pipeline_id):
            collected.append(ev)
            if ev.type == "pipeline.done":
                break

    # 30s not 5s — GitHub Actions runners can be meaningfully slower than
    # local dev machines, and the real import pipeline spawns a chunker +
    # LLM stub + embed pass before pipeline.done fires.
    await asyncio.wait_for(consume(), timeout=30.0)

    # Note: `pipeline.registered` is emitted synchronously inside
    # start_pipeline() before the subscriber attaches, so it doesn't
    # show up in the collected stream — that's expected and matches
    # the RT-round3 smoke test behavior.
    kinds = [e.type for e in collected]
    assert "pipeline.start" in kinds
    assert "pipeline.done" in kinds
    done = next(e for e in collected if e.type == "pipeline.done")
    assert done.payload.get("status") == "success"

    # Confirm at least one vector row made it to disk.
    with DbSession(engine) as db:
        count = db.exec(text("SELECT COUNT(*) FROM concept_nodes_vec")).one()
        assert count[0] >= 1

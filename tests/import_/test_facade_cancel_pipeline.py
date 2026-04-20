"""ImporterFacade.cancel_pipeline propagates to the asyncio task."""

from __future__ import annotations

import asyncio

from echovessel.runtime.importer_facade import ImporterFacade, PipelineEvent


class _SlowLLM:
    provider_name = "stub"

    async def complete(self, system: str, user: str, **kwargs):
        # Block long enough that the cancel can arrive mid-pipeline.
        await asyncio.sleep(5.0)
        return ('{"writes": [], "chunk_summary": ""}', None)


class _MemStub:
    def __init__(self, factory):
        self._db_factory = factory


async def test_cancel_pipeline_cancels_task(db_session_factory):
    facade = ImporterFacade(
        llm_provider=_SlowLLM(),
        voice_service=None,
        memory_api=_MemStub(db_session_factory),
    )
    pipeline_id = await facade.start_pipeline(
        "upload-1",
        raw_bytes=b"Some small body text.\n\nAnother paragraph.",
        suffix=".txt",
        source_label="diary",
        file_hash="h-slow",
        persona_id="p_test",
        user_id="self",
        embed_fn=lambda texts: [[1.0 / 384.0] * 384 for _ in texts],
        vector_writer=lambda cid, vec: None,
    )

    # Give the task one scheduler tick so it actually enters the slow
    # LLM call.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    collected: list[PipelineEvent] = []

    async def consume():
        async for ev in facade.subscribe_events(pipeline_id):
            collected.append(ev)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)

    await facade.cancel_pipeline(pipeline_id)
    await asyncio.wait_for(consumer, timeout=2.0)

    kinds = [e.type for e in collected]
    assert "pipeline.cancelled" in kinds


async def test_cancel_unknown_pipeline_is_noop(db_session_factory):
    facade = ImporterFacade(
        llm_provider=_SlowLLM(),
        voice_service=None,
        memory_api=_MemStub(db_session_factory),
    )
    # Should not raise.
    await facade.cancel_pipeline("never-registered")

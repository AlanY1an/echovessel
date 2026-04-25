"""ImporterFacade smoke tests (spec §17a.6).

Round 3 scope: facade exists, can be constructed, start_pipeline /
subscribe_events / emit_event round-trip events. Real pipeline logic
lands in Thread IMPORT-code.
"""

from __future__ import annotations

import asyncio

import pytest

from echovessel.runtime.wiring.importer import ImporterFacade, PipelineEvent


def _make_facade() -> ImporterFacade:
    class _LlmStub:
        provider_name = "stub"

    class _MemStub:
        pass

    return ImporterFacade(
        llm_provider=_LlmStub(),
        voice_service=None,
        memory_api=_MemStub(),
    )


async def test_importer_facade_construct():
    facade = _make_facade()
    pipeline_id = await facade.start_pipeline("upload-1")
    assert isinstance(pipeline_id, str) and pipeline_id

    # subscribe_events returns an async iterator (no StopIteration yet).
    it = facade.subscribe_events(pipeline_id)
    assert hasattr(it, "__anext__")

    # Cancel so the iterator's sentinel wakes up any active awaiter.
    await facade.cancel_pipeline(pipeline_id)


async def test_importer_facade_event_broadcast():
    facade = _make_facade()
    pipeline_id = await facade.start_pipeline("upload-2")

    it = facade.subscribe_events(pipeline_id)

    async def _consume():
        seen: list[PipelineEvent] = []
        async for ev in it:
            seen.append(ev)
        return seen

    consumer_task = asyncio.create_task(_consume())

    # Yield so the consumer registers on the queue.
    await asyncio.sleep(0)

    await facade.emit_event(
        PipelineEvent(
            pipeline_id=pipeline_id,
            type="chunk.done",
            payload={"chunk_id": "c-1"},
        )
    )
    await facade.cancel_pipeline(pipeline_id)  # sentinel → consumer exits

    events = await asyncio.wait_for(consumer_task, timeout=1.0)
    kinds = [e.type for e in events]
    # Expect at least the chunk.done event and the cancellation notice.
    assert "chunk.done" in kinds
    assert "pipeline.cancelled" in kinds


async def test_importer_facade_subscribe_unknown_pipeline_raises():
    facade = _make_facade()
    with pytest.raises(KeyError):
        facade.subscribe_events("nope")

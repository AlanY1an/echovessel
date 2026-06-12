"""ImporterFacade smoke tests (spec §17a.6).

Round 3 scope: facade exists, can be constructed, start_pipeline /
subscribe_events / emit_event round-trip events. Real pipeline logic
lands in Thread IMPORT-code.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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


async def test_subscribe_after_cancel_replays_history_then_terminates():
    """A subscriber attaching after the pipeline reached a terminal
    state must get the replayed history and then a clean end of
    iteration — not block forever on a queue nothing will feed."""

    facade = _make_facade()
    pipeline_id = await facade.start_pipeline("upload-late")
    await facade.cancel_pipeline(pipeline_id)

    async def _consume() -> list[PipelineEvent]:
        return [ev async for ev in facade.subscribe_events(pipeline_id)]

    events = await asyncio.wait_for(_consume(), timeout=1.0)

    kinds = [e.type for e in events]
    assert kinds == ["pipeline.registered", "pipeline.cancelled"]
    # The finished iterator detached its queue from the pipeline state.
    assert facade._pipelines[pipeline_id].subscribers == []


# ---------------------------------------------------------------------------
# Terminal-state payload release
# ---------------------------------------------------------------------------
#
# A pipeline's run kwargs pin the full upload bytes (raw_bytes, up to
# MAX_UPLOAD_BYTES) plus dependency refs. Once a pipeline reaches a
# non-resumable terminal state those must be released so a long-running
# daemon does not accumulate one upload per import. ``history`` /
# ``progress`` stay so late SSE subscribers can still replay the event
# stream. Resumable terminal states (failed, cancelled) are the
# exception: they keep kwargs so resume_pipeline can re-spawn from the
# stored ProgressSnapshot — for cancelled runs that is the only path
# that gets pre-cancel committed nodes embedded.


class _MemStubWithDb:
    """Memory stub exposing _db_factory so the facade leaves smoke mode
    and actually spawns the pipeline task."""

    def _db_factory(self):  # pragma: no cover — never invoked by fakes
        raise AssertionError("fake pipelines never open a db session")


def _make_real_facade() -> ImporterFacade:
    class _LlmStub:
        provider_name = "stub"

    return ImporterFacade(
        llm_provider=_LlmStub(),
        voice_service=None,
        memory_api=_MemStubWithDb(),
    )


async def _start_real_pipeline(facade: ImporterFacade) -> str:
    return await facade.start_pipeline(
        "upload-real",
        raw_bytes=b"x" * 4096,
        suffix=".txt",
        persona_id="p1",
        user_id="self",
    )


async def test_completed_pipeline_releases_upload_bytes(monkeypatch):
    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        await event_sink(SimpleNamespace(type="pipeline.done", payload={"status": "success"}))

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]
    assert state.kwargs.get("raw_bytes")  # pinned while running

    await asyncio.wait_for(state.task, timeout=2.0)

    assert state.status == "success"
    assert state.kwargs == {}
    # Summary survives: history still replays for late subscribers.
    assert any(ev.type == "pipeline.done" for ev in state.history)


async def test_subscribe_after_completion_yields_history_then_stops(monkeypatch):
    """SSE reconnects (and fast pipelines that finish before their
    consumer attaches) subscribe after ``pipeline.done``. The iterator
    must yield the full history and then stop, so the admin events
    route's response actually closes."""

    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        await event_sink(SimpleNamespace(type="pipeline.done", payload={"status": "success"}))

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]
    await asyncio.wait_for(state.task, timeout=2.0)

    it = facade.subscribe_events(pid)

    async def _consume() -> list[PipelineEvent]:
        return [ev async for ev in it]

    events = await asyncio.wait_for(_consume(), timeout=2.0)

    kinds = [e.type for e in events]
    assert kinds == ["pipeline.registered", "pipeline.done"]
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(it.__anext__(), timeout=1.0)
    # Each finished iterator detaches its queue — repeated reconnects
    # must not accumulate dead queues on the pipeline state.
    assert state.subscribers == []


async def test_cancelled_pipeline_keeps_kwargs_for_resume(monkeypatch):
    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]
    await asyncio.sleep(0)  # let the task enter run_pipeline

    await facade.cancel_pipeline(pid)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=2.0)

    assert state.status == "cancelled"
    assert state.kwargs.get("raw_bytes") == b"x" * 4096


async def test_cancelled_pipeline_resume_respawns_task(monkeypatch):
    calls: list[str] = []

    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        calls.append(pipeline_id)
        if len(calls) == 1:
            await asyncio.Event().wait()
        await event_sink(SimpleNamespace(type="pipeline.done", payload={"status": "success"}))

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]
    await asyncio.sleep(0)  # let the task enter run_pipeline

    await facade.cancel_pipeline(pid)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=2.0)
    first_task = state.task

    await facade.resume_pipeline(pid)
    assert state.task is not first_task
    assert any(ev.type == "pipeline.resumed" for ev in state.history)

    await asyncio.wait_for(state.task, timeout=2.0)
    assert calls == [pid, pid]
    assert state.status == "success"
    # The resumed run finished successfully — now the payload goes.
    assert state.kwargs == {}


async def test_resume_released_pipeline_is_honest_noop(monkeypatch):
    """A completed pipeline has released its run payload; resume must
    not pretend otherwise — status stays terminal, no ``pipeline.resumed``
    is emitted, and no task is spawned."""

    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        await event_sink(SimpleNamespace(type="pipeline.done", payload={"status": "success"}))

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]
    await asyncio.wait_for(state.task, timeout=2.0)
    assert state.kwargs == {}
    finished_task = state.task

    await facade.resume_pipeline(pid)

    assert state.status == "success"
    assert state.task is finished_task
    assert not any(ev.type == "pipeline.resumed" for ev in state.history)


async def test_failed_pipeline_keeps_kwargs_for_resume(monkeypatch):
    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        raise RuntimeError("simulated pipeline crash")

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]

    await asyncio.wait_for(state.task, timeout=2.0)

    assert state.status == "failed"
    assert state.kwargs.get("raw_bytes") == b"x" * 4096


async def test_pipeline_reporting_failed_status_keeps_kwargs(monkeypatch):
    """run_pipeline swallows its own errors and reports them through
    the ``pipeline.done`` event — the facade must treat that the same
    as a raised failure (kwargs kept for resume)."""

    async def fake_run_pipeline(*, pipeline_id, event_sink, **kwargs):
        await event_sink(
            SimpleNamespace(type="pipeline.done", payload={"status": "failed", "error": "boom"})
        )

    monkeypatch.setattr("echovessel.runtime.wiring.importer.run_pipeline", fake_run_pipeline)
    facade = _make_real_facade()
    pid = await _start_real_pipeline(facade)
    state = facade._pipelines[pid]

    await asyncio.wait_for(state.task, timeout=2.0)

    assert state.status == "failed"
    assert state.kwargs.get("raw_bytes") == b"x" * 4096

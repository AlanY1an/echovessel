"""Audit P1-8: import pipeline must advance ``progress.current_chunk``
even on chunk failure.

If progress doesn't advance on failure, a subsequent ``resume_pipeline``
call restarts from the failed chunk — and because memory helpers
``import_content`` / ``bulk_create_events`` commit per call, any items
that happened to land on disk before the failure (or any items from
later successful chunks, if the last chunk is the one that failed)
get re-extracted and re-written as *duplicate* rows with different
primary keys. The fix is to advance progress after every attempt so
resume picks up strictly beyond what was tried.

Trade-off that comes with the fix: a failed chunk is NOT retried on
resume. The operator has to re-import the whole file to replay it.
This matches the already-documented "partial semantics" of the
dispatch helper (see ``_dispatch_chunk_items`` docstring).
"""

from __future__ import annotations

import json

from echovessel.import_ import run_pipeline
from echovessel.import_.models import ProgressSnapshot


class _StubLLM:
    """LLM stub that returns a queued response per ``complete`` call.

    When called more often than responses provided, returns an empty
    writes list (valid but no-op chunk). Raising is handled by returning
    a garbage string on a specific call index so the extractor parses
    it as invalid JSON.
    """

    def __init__(self, per_chunk_responses: list[str]) -> None:
        self.responses = list(per_chunk_responses)

    async def complete(self, system: str, user: str, **kwargs):
        if not self.responses:
            return '{"writes": [], "chunk_summary": ""}', None
        return self.responses.pop(0), None


# Text deliberately long / structurally split enough that chunk_text
# produces at least two chunks.
TWO_CHUNK_TEXT = """\
First section — a long paragraph describing a walk in the morning
with the dog Mochi. They went to the park on the corner of 4th street.
The air was cool and the leaves were turning.

Second section — later that evening Anna wrote in her diary about how
the walk had made her feel calmer. She remembered the window where
Mochi used to sit when she was a kitten.
"""

_VALID_CHUNK_RESPONSE = json.dumps(
    {
        "writes": [
            {
                "target": "L3.event",
                "description": "Anna walked Mochi on 4th street",
                "approximate_date": "2024-06-14",
                "emotional_impact": 3,
                "emotion_tags": ["calm"],
                "relational_tags": [],
                "filling_description": [],
                "evidence_quote": "walk in the morning",
            }
        ],
        "chunk_summary": "morning walk",
    },
    ensure_ascii=False,
)


async def test_progress_advances_past_failed_chunk(
    db_session_factory, backend
):
    """Chunk 0 succeeds; chunk 1 fails extraction (the LLM returns
    garbage JSON). After the pipeline finishes, ``progress.current_chunk``
    must point *past* chunk 1, not stay stuck on it. Otherwise a
    ``resume_pipeline`` would re-extract chunk 1 and the committed
    writes from the successful chunk 0 would stay on disk while
    chunk 1 tries again → potential duplicates if it succeeds the
    second time.
    """
    progress = ProgressSnapshot(pipeline_id="pl-p18")

    async def sink(event):  # noqa: ARG001
        pass

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]

    await run_pipeline(
        pipeline_id="pl-p18",
        raw_bytes=TWO_CHUNK_TEXT.encode(),
        suffix=".txt",
        source_label="diary",
        file_hash="hash-p18",
        persona_id="p_test",
        user_id="self",
        llm=_StubLLM(
            [
                _VALID_CHUNK_RESPONSE,  # chunk 0 succeeds
                "this is not valid json {{",  # chunk 1 extraction fails
            ]
        ),
        db_session_factory=db_session_factory,
        embed_fn=fake_embed,
        vector_writer=backend.insert_vector,
        event_sink=sink,
        progress=progress,
    )

    # Pipeline saw 2 chunks total. progress.current_chunk should reflect
    # that both were attempted, not that we're still waiting on chunk 1.
    assert progress.total_chunks == 2
    assert progress.current_chunk == 2, (
        f"progress.current_chunk must advance past every attempted "
        f"chunk (success or failure); got {progress.current_chunk}. "
        f"A resume starting here would restart from chunk "
        f"{progress.current_chunk}, re-running extraction for the "
        f"failed chunk and potentially creating duplicate rows if "
        f"the retry commits any items."
    )


async def test_progress_advances_when_dispatch_raises(
    db_session_factory, backend
):
    """When the dispatch layer raises for a chunk (e.g. DB contention,
    FK violation, downstream bug), progress must still advance. Without
    the fix, the exception is caught at the pipeline level and the
    loop continues to the next chunk — but ``progress.current_chunk``
    stays on the failing chunk. On resume, the failing chunk re-runs
    its LLM extraction and any items that happened to commit during
    the first attempt before the raise pile up alongside the retry's
    fresh inserts.
    """
    progress = ProgressSnapshot(pipeline_id="pl-p18b")

    async def sink(event):  # noqa: ARG001
        pass

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]

    # Make the dispatch layer raise on the LAST chunk — this is the
    # case where the bug is visible. When a later chunk succeeds, its
    # success path updates progress and inadvertently masks the earlier
    # chunk's stuck progress. A failure on the final chunk leaves
    # progress stuck for real.
    call_count = {"n": 0}

    def exploding_factory():
        call_count["n"] += 1
        # chunks dispatch on factory calls 1, 2 (one per chunk)
        if call_count["n"] == 2:
            raise RuntimeError("simulated DB pool exhaustion on last chunk")
        return db_session_factory()

    await run_pipeline(
        pipeline_id="pl-p18b",
        raw_bytes=TWO_CHUNK_TEXT.encode(),
        suffix=".txt",
        source_label="diary",
        file_hash="hash-p18b",
        persona_id="p_test",
        user_id="self",
        llm=_StubLLM(
            [
                _VALID_CHUNK_RESPONSE,
                _VALID_CHUNK_RESPONSE,
            ]
        ),
        db_session_factory=exploding_factory,
        embed_fn=fake_embed,
        vector_writer=backend.insert_vector,
        event_sink=sink,
        progress=progress,
    )

    assert progress.total_chunks == 2
    assert progress.current_chunk == 2, (
        f"last-chunk dispatch failure must still advance progress; "
        f"got {progress.current_chunk}. A resume from chunk "
        f"{progress.current_chunk} would re-run the failed chunk's "
        f"extraction and risk creating duplicate rows if the retry "
        f"commits any items."
    )

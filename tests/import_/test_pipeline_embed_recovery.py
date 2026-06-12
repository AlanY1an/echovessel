"""Committed concept nodes must reach the embed pass even when a run
is interrupted.

Two interruption paths can leave L3 / L4 rows on disk without vectors,
making them permanently invisible to ``retrieve.vector_search``:

1. Cancellation mid-loop: ``CancelledError`` re-raises before the
   embed pass, so nodes committed earlier in the run have no vectors
   yet. The resume run must pick them up from the
   ``ProgressSnapshot.written_concept_node_ids`` ledger.

2. Mid-chunk dispatch failure: ``import_content`` commits per call,
   so when item N of a chunk raises, items 0..N-1 are already durable.
   Their IDs must survive the failure path and be embedded by the
   same run's embed pass.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

import echovessel.import_.pipeline as pipeline_module
from echovessel.import_ import run_pipeline
from echovessel.import_.models import ProgressSnapshot
from echovessel.memory.models import ConceptNode


class _StubLLM:
    def __init__(self, per_chunk_responses: list[str]) -> None:
        self.responses = list(per_chunk_responses)

    async def complete(self, system: str, user: str, **kwargs):
        if not self.responses:
            return '{"writes": [], "chunk_summary": ""}', None
        return self.responses.pop(0), None


class _CancelOnSecondCallLLM:
    """Returns one valid response, then raises CancelledError —
    simulating a cancel arriving while chunk 1's LLM call is in flight."""

    def __init__(self, first_response: str) -> None:
        self.first_response = first_response
        self.calls = 0

    async def complete(self, system: str, user: str, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.first_response, None
        raise asyncio.CancelledError


def _event_response(description: str, evidence: str) -> str:
    return json.dumps(
        {
            "writes": [
                {
                    "target": "L3.event",
                    "description": description,
                    "approximate_date": "2024-06-14",
                    "emotional_impact": 2,
                    "emotion_tags": ["calm"],
                    "relational_tags": [],
                    "filling_description": [],
                    "evidence_quote": evidence,
                }
            ],
            "chunk_summary": "stub chunk",
        },
        ensure_ascii=False,
    )


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[0.0] * 8 for _ in texts]


# Long enough / structurally split enough that chunk_text produces
# two chunks (same shape as test_pipeline_resume_safety.py).
TWO_CHUNK_TEXT = """\
First section — a long paragraph describing a walk in the morning
with the dog Mochi. They went to the park on the corner of 4th street.
The air was cool and the leaves were turning.

Second section — later that evening Anna wrote in her diary about how
the walk had made her feel calmer. She remembered the window where
Mochi used to sit when she was a kitten.
"""

ONE_CHUNK_TEXT = """\
Anna walked Mochi in the park this morning, and later that evening
she wrote in her diary about how calm the walk had made her feel.
"""


async def test_resume_embeds_nodes_committed_before_cancel(db_session_factory, engine):
    """Run 1 commits chunk 0's concept node, then is cancelled before
    the embed pass. The resume run (same ProgressSnapshot) must embed
    that node alongside its own — not just the IDs it produced itself.
    """
    progress = ProgressSnapshot(pipeline_id="pl-embed-resume")
    embedded_ids: list[int] = []

    def vector_writer(cid: int, vec: list[float]) -> None:
        embedded_ids.append(cid)

    common = {
        "pipeline_id": "pl-embed-resume",
        "raw_bytes": TWO_CHUNK_TEXT.encode(),
        "suffix": ".txt",
        "source_label": "diary",
        "file_hash": "hash-embed-resume",
        "persona_id": "p_test",
        "user_id": "self",
        "db_session_factory": db_session_factory,
        "embed_fn": _fake_embed,
        "vector_writer": vector_writer,
        "progress": progress,
    }

    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(
            llm=_CancelOnSecondCallLLM(
                _event_response(
                    "Anna walked Mochi on 4th street",
                    "walk in the morning",
                )
            ),
            **common,
        )

    # Chunk 0's node is on disk but the embed pass never ran.
    with DbSession(engine) as db:
        committed_ids = {n.id for n in db.exec(select(ConceptNode)).all()}
    assert len(committed_ids) == 1
    assert embedded_ids == []
    assert progress.state == "cancelled"
    assert list(committed_ids) == progress.written_concept_node_ids

    # Resume: chunk 1 succeeds; the embed pass must cover BOTH nodes.
    report = await run_pipeline(
        llm=_StubLLM(
            [
                _event_response(
                    "Anna wrote in her diary about the walk",
                    "wrote in her diary",
                )
            ]
        ),
        **common,
    )

    with DbSession(engine) as db:
        all_ids = {n.id for n in db.exec(select(ConceptNode)).all()}
    assert len(all_ids) == 2
    assert set(embedded_ids) == all_ids, (
        f"resume must embed nodes committed before the cancel; "
        f"embedded {sorted(embedded_ids)} of {sorted(all_ids)}"
    )
    assert len(embedded_ids) == 2  # no duplicate embed calls either
    assert report.embedded_vector_count == 2


async def test_mid_chunk_dispatch_failure_keeps_committed_ids_for_embed(
    db_session_factory, engine, monkeypatch
):
    """Item 0 of the chunk commits its concept node, item 1's dispatch
    raises. The committed node's ID must survive the failure path so
    the embed pass still vectorizes it (and the snapshot records it).
    """
    progress = ProgressSnapshot(pipeline_id="pl-embed-partial")
    embedded_ids: list[int] = []

    def vector_writer(cid: int, vec: list[float]) -> None:
        embedded_ids.append(cid)

    real_dispatch = pipeline_module.dispatch_item
    calls = {"n": 0}

    def flaky_dispatch(item, *, db, source):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated write failure on second item")
        return real_dispatch(item, db=db, source=source)

    monkeypatch.setattr(pipeline_module, "dispatch_item", flaky_dispatch)

    two_writes = json.dumps(
        {
            "writes": [
                {
                    "target": "L3.event",
                    "description": "Anna walked Mochi in the park",
                    "emotional_impact": 2,
                    "emotion_tags": ["calm"],
                    "relational_tags": [],
                    "evidence_quote": "walked Mochi in the park",
                },
                {
                    "target": "L3.event",
                    "description": "Anna wrote in her diary that evening",
                    "emotional_impact": 1,
                    "emotion_tags": ["calm"],
                    "relational_tags": [],
                    "evidence_quote": "wrote in her diary",
                },
            ],
            "chunk_summary": "walk and diary",
        },
        ensure_ascii=False,
    )

    report = await run_pipeline(
        pipeline_id="pl-embed-partial",
        raw_bytes=ONE_CHUNK_TEXT.encode(),
        suffix=".txt",
        source_label="diary",
        file_hash="hash-embed-partial",
        persona_id="p_test",
        user_id="self",
        llm=_StubLLM([two_writes]),
        db_session_factory=db_session_factory,
        embed_fn=_fake_embed,
        vector_writer=vector_writer,
        progress=progress,
    )

    # Item 0's node is durable on disk despite the chunk failing.
    with DbSession(engine) as db:
        committed_ids = {n.id for n in db.exec(select(ConceptNode)).all()}
    assert len(committed_ids) == 1

    assert any("dispatch" in d.reason for d in report.dropped_items)
    assert set(embedded_ids) == committed_ids, (
        f"embed pass must cover nodes committed before the mid-chunk "
        f"failure; embedded {sorted(embedded_ids)} of "
        f"{sorted(committed_ids)}"
    )
    assert report.embedded_vector_count == 1
    assert progress.written_concept_node_ids == list(committed_ids)

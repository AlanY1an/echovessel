"""Worker κ · POST /api/admin/persona/bootstrap-from-material tests.

Exercises the endpoint that turns a just-imported batch of events +
thoughts into five suggested core blocks for first-run onboarding.

Test strategy:

- Build a real :class:`Runtime` with stub LLM + in-memory file DB so
  FastAPI's threaded TestClient can see tables created on another
  thread.
- Build a real :class:`ImporterFacade` so the route's
  ``subscribe_events`` + ``start_pipeline`` paths exercise the
  production code — we control the pipeline's `pipeline.done` event
  via ``facade.emit_event`` rather than spawning a real pipeline.
- Pre-seed :class:`ConceptNode` rows directly into the DB to stand in
  for what the pipeline would have written; this isolates the
  bootstrap logic from the pipeline's own exhaustively-tested code.
- The happy path uses ``httpx.AsyncClient`` + ``asyncio.create_task``
  so the POST can block inside ``subscribe_events`` while the test
  body emits the terminal event from a different task.
"""

from __future__ import annotations

import asyncio
import json
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory import append_to_core_block
from echovessel.memory.models import ConceptNode
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.importer_facade import ImporterFacade, PipelineEvent
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "bootstrap-test"
display_name = "Initial"

[memory]
db_path = "memory.db"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


class _StubMemoryApi:
    """Importer facade duck-types this; we do NOT expose ``_db_factory``
    so ``start_pipeline`` stays in smoke mode (no spawned task)."""


def _build_rig(
    *,
    llm_reply: str,
) -> tuple[Runtime, ImporterFacade, TestClient]:
    """Assemble a Runtime + ImporterFacade + mounted FastAPI app.

    ``llm_reply`` is returned by the stub for every ``complete()`` call
    — the bootstrap route's only LLM request produces this exact text.
    """

    tmp = tempfile.mkdtemp(prefix="echovessel-bootstrap-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback=llm_reply),
        embed_fn=build_zero_embedder(),
    )
    facade = ImporterFacade(
        llm_provider=rt.ctx.llm,
        voice_service=None,
        memory_api=_StubMemoryApi(),
    )
    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    app = build_web_app(
        channel=channel,
        broadcaster=broadcaster,
        runtime=rt,
        importer_facade=facade,
        heartbeat_seconds=0.5,
    )
    return rt, facade, TestClient(app)


def _seed_concept_nodes(
    rt: Runtime,
    *,
    events: list[tuple[str, int]],
    thoughts: list[str],
) -> None:
    """Insert events + thoughts the test's 'completed import' produced."""

    persona_id = rt.ctx.persona.id
    with DbSession(rt.ctx.engine) as db:
        for desc, impact in events:
            db.add(
                ConceptNode(
                    persona_id=persona_id,
                    user_id="self",
                    type=NodeType.EVENT,
                    description=desc,
                    emotional_impact=impact,
                    emotion_tags=[],
                    relational_tags=[],
                    imported_from="test-upload",
                )
            )
        for desc in thoughts:
            db.add(
                ConceptNode(
                    persona_id=persona_id,
                    user_id="self",
                    type=NodeType.THOUGHT,
                    description=desc,
                    emotional_impact=0,
                    emotion_tags=[],
                    relational_tags=[],
                    imported_from="test-upload",
                )
            )
        db.commit()


_VALID_LLM_REPLY = json.dumps(
    {
        "persona_block": "你是一个愿意认真听的朋友。",
        "user_block": "用户是一位 28 岁的软件工程师,住在北京,有一只叫 Mochi 的猫。",
    }
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_happy_path_with_pipeline_id() -> None:
    rt, facade, client = _build_rig(llm_reply=_VALID_LLM_REPLY)

    # Kick a pipeline so we have a pipeline_id to pass in. Smoke mode:
    # no pipeline task spawns, so we control the lifecycle via
    # emit_event below.
    pipeline_id = await facade.start_pipeline("upload-123")

    # Stand in for the pipeline's side effects.
    _seed_concept_nodes(
        rt,
        events=[
            ("用户 28 岁,在北京做软件工程师。", +2),
            ("用户 2020 年领养了一只黑猫 Mochi。", +4),
        ],
        thoughts=["用户是一个会把平静日常仔细说出来的人。"],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://testserver",
        timeout=5.0,
    ) as ac:
        # Fire the POST first — it will block waiting for pipeline.done.
        post_task = asyncio.create_task(
            ac.post(
                "/api/admin/persona/bootstrap-from-material",
                json={"pipeline_id": pipeline_id},
            )
        )
        # Give the handler a moment to reach its subscribe loop.
        await asyncio.sleep(0.05)

        # Now emit the terminal event. The handler's subscriber queue
        # sees it, exits the loop, reads events + thoughts, calls LLM,
        # returns.
        await facade.emit_event(
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.done",
                payload={"status": "success", "processed_chunks": 1},
            )
        )

        resp = await asyncio.wait_for(post_task, timeout=5.0)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pipeline_status"] == "success"
    assert body["source_event_count"] == 2
    assert body["source_thought_count"] == 1
    assert body["suggested_blocks"]["persona_block"].startswith("你是一个愿意")
    assert "Mochi" in body["suggested_blocks"]["user_block"]
    # v0.5 · response is strictly 2 keys
    assert set(body["suggested_blocks"]) == {"persona_block", "user_block"}


# ---------------------------------------------------------------------------
# 400: no identifier
# ---------------------------------------------------------------------------


def test_bootstrap_rejects_missing_identifier() -> None:
    _rt, _facade, client = _build_rig(llm_reply=_VALID_LLM_REPLY)
    with client:
        resp = client.post(
            "/api/admin/persona/bootstrap-from-material",
            json={},
        )
    assert resp.status_code == 400
    assert "upload_id" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 404: pipeline_id unknown
# ---------------------------------------------------------------------------


def test_bootstrap_404_on_unknown_pipeline_id() -> None:
    _rt, _facade, client = _build_rig(llm_reply=_VALID_LLM_REPLY)
    with client:
        resp = client.post(
            "/api/admin/persona/bootstrap-from-material",
            json={"pipeline_id": "never-existed-abc"},
        )
    assert resp.status_code == 404
    assert "never-existed-abc" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 409: already onboarded
# ---------------------------------------------------------------------------


def test_bootstrap_conflict_when_persona_already_onboarded() -> None:
    rt, _facade, client = _build_rig(llm_reply=_VALID_LLM_REPLY)

    # Simulate a prior onboarding by writing one core block directly.
    with DbSession(rt.ctx.engine) as db:
        append_to_core_block(
            db,
            persona_id=rt.ctx.persona.id,
            user_id=None,
            label=BlockLabel.PERSONA.value,
            content="已经 onboarded",
            provenance={"source": "test_setup"},
        )

    with client:
        resp = client.post(
            "/api/admin/persona/bootstrap-from-material",
            json={"upload_id": "upload-123"},
        )
    assert resp.status_code == 409
    assert "already completed" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 400: pipeline ended with status=failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rejects_failed_pipeline_status() -> None:
    _rt, facade, client = _build_rig(llm_reply=_VALID_LLM_REPLY)

    pipeline_id = await facade.start_pipeline("upload-failed")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://testserver",
        timeout=5.0,
    ) as ac:
        post_task = asyncio.create_task(
            ac.post(
                "/api/admin/persona/bootstrap-from-material",
                json={"pipeline_id": pipeline_id},
            )
        )
        await asyncio.sleep(0.05)
        await facade.emit_event(
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.done",
                payload={"status": "failed", "error": "extraction crashed"},
            )
        )
        resp = await asyncio.wait_for(post_task, timeout=5.0)

    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 502: LLM returned malformed JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_502_on_malformed_llm_json() -> None:
    rt, facade, client = _build_rig(llm_reply="not json at all {")

    pipeline_id = await facade.start_pipeline("upload-malformed")
    _seed_concept_nodes(
        rt,
        events=[("trivial event", +1)],
        thoughts=[],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app),
        base_url="http://testserver",
        timeout=5.0,
    ) as ac:
        post_task = asyncio.create_task(
            ac.post(
                "/api/admin/persona/bootstrap-from-material",
                json={"pipeline_id": pipeline_id},
            )
        )
        await asyncio.sleep(0.05)
        await facade.emit_event(
            PipelineEvent(
                pipeline_id=pipeline_id,
                type="pipeline.done",
                payload={"status": "success"},
            )
        )
        resp = await asyncio.wait_for(post_task, timeout=5.0)

    assert resp.status_code == 502
    assert "malformed" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 503: facade not wired
# ---------------------------------------------------------------------------


def test_bootstrap_503_when_importer_facade_missing() -> None:
    tmp = tempfile.mkdtemp(prefix="echovessel-bootstrap-no-facade-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback=_VALID_LLM_REPLY),
        embed_fn=build_zero_embedder(),
    )
    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    # Deliberately NOT passing importer_facade.
    app = build_web_app(
        channel=channel,
        broadcaster=broadcaster,
        runtime=rt,
        importer_facade=None,
        heartbeat_seconds=0.5,
    )
    client = TestClient(app)

    with client:
        resp = client.post(
            "/api/admin/persona/bootstrap-from-material",
            json={"upload_id": "something"},
        )
    assert resp.status_code == 503
    assert "import" in resp.json()["detail"].lower()

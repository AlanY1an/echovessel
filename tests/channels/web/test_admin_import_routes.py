"""Tests for the Worker-E admin-import routes (v0.0.2).

Exercises the five handlers:

- ``POST /api/admin/import/upload`` (multipart) and
  ``POST /api/admin/import/upload_text`` (JSON paste)
- ``POST /api/admin/import/estimate``
- ``POST /api/admin/import/start``
- ``POST /api/admin/import/cancel``
- ``GET  /api/admin/import/events?pipeline_id=...`` (SSE)

The tests build a real :class:`Runtime` via ``config_override`` + a
real :class:`ImporterFacade` wired to a stub LLM / memory. No daemon
is started — the Web app is driven directly through
``httpx.AsyncClient`` with FastAPI's ASGI transport so SSE streams
actually flow.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
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
id = "import-test"
display_name = "ImportTest"

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


def _build_rig() -> tuple[Runtime, ImporterFacade, TestClient]:
    """Construct a Runtime + ImporterFacade + mounted FastAPI app.

    Uses a file-backed SQLite DB (not ``:memory:``) because FastAPI
    TestClient runs async handlers on a different thread; every
    connection to a shared SQLite file sees the same tables, whereas
    ``:memory:`` is per-connection and loses the schema across the
    thread boundary.
    """

    tmp = tempfile.mkdtemp(prefix="echovessel-import-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
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


class _StubMemoryApi:
    """Minimal stand-in for the MemoryFacade used in these tests.

    Deliberately omits ``_db_factory`` so ``ImporterFacade.start_pipeline``
    runs in its "smoke mode" — register the pipeline + emit
    ``pipeline.registered`` + return the id without spawning a real
    pipeline task. That keeps the test deterministic and independent
    of the full import pipeline wiring, which is covered by the
    pipeline-specific test suite.
    """


# ---------------------------------------------------------------------------
# POST /api/admin/import/upload (multipart) + /upload_text (JSON)
# ---------------------------------------------------------------------------


def test_upload_text_returns_upload_id() -> None:
    rt, _facade, client = _build_rig()
    with client:
        resp = client.post(
            "/api/admin/import/upload_text",
            json={"text": "hello world", "source_label": "paste"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "upload_id",
        "file_hash",
        "suffix",
        "source_label",
        "size_bytes",
    }
    assert body["size_bytes"] == len(b"hello world")
    assert body["suffix"] == ".txt"
    assert body["source_label"] == "paste"

    upload_dir = (
        Path(rt.ctx.config.runtime.data_dir).expanduser()
        / "imports"
        / body["upload_id"]
    )
    assert upload_dir.exists()
    assert (upload_dir / "raw.txt").read_text() == "hello world"
    meta = json.loads((upload_dir / "meta.json").read_text())
    assert meta["file_hash"] == body["file_hash"]


def test_upload_file_returns_upload_id_and_hash() -> None:
    _rt, _facade, client = _build_rig()
    payload = b"a file body"
    with client:
        resp = client.post(
            "/api/admin/import/upload",
            files={"file": ("diary.md", payload, "text/markdown")},
            data={"source_label": "from disk"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["size_bytes"] == len(payload)
    assert body["suffix"] == ".md"
    assert body["source_label"] == "from disk"
    # Hash matches sha256 of the uploaded bytes.
    import hashlib

    assert body["file_hash"] == hashlib.sha256(payload).hexdigest()


def test_upload_missing_file_returns_400() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        # No `file` form field AND no JSON — upload_text is the right
        # path for JSON. The multipart handler should 400 with the
        # redirect hint.
        resp = client.post("/api/admin/import/upload", data={"source_label": "x"})
    assert resp.status_code == 400
    assert "upload_text" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/admin/import/estimate
# ---------------------------------------------------------------------------


def test_estimate_returns_cost_shape() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        upload = client.post(
            "/api/admin/import/upload_text",
            json={"text": "the quick brown fox jumps over the lazy dog"},
        ).json()
        resp = client.post(
            "/api/admin/import/estimate",
            json={"upload_id": upload["upload_id"], "stage": "llm"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"tokens_in", "tokens_out_est", "cost_usd_est"}
    assert body["tokens_in"] > 0
    assert body["tokens_out_est"] >= 0
    assert isinstance(body["cost_usd_est"], (int, float))
    assert body["cost_usd_est"] >= 0.0


def test_estimate_unknown_stage_returns_400() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        upload = client.post(
            "/api/admin/import/upload_text", json={"text": "x"}
        ).json()
        resp = client.post(
            "/api/admin/import/estimate",
            json={"upload_id": upload["upload_id"], "stage": "embed"},
        )
    assert resp.status_code == 400


def test_estimate_unknown_upload_id_returns_404() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        resp = client.post(
            "/api/admin/import/estimate",
            json={"upload_id": "does-not-exist", "stage": "llm"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/admin/import/start
# ---------------------------------------------------------------------------


def test_start_returns_pipeline_id_and_emits_registered_event() -> None:
    _rt, facade, client = _build_rig()
    with client:
        upload = client.post(
            "/api/admin/import/upload_text",
            json={"text": "import me"},
        ).json()
        resp = client.post(
            "/api/admin/import/start",
            json={"upload_id": upload["upload_id"]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "pipeline_id" in body
    assert body["pipeline_id"]
    # Facade registered the pipeline (smoke mode because _StubMemoryApi
    # has no _db_factory — real run_pipeline task is not spawned).
    assert body["pipeline_id"] in facade._pipelines


# ---------------------------------------------------------------------------
# POST /api/admin/import/cancel
# ---------------------------------------------------------------------------


def test_cancel_unknown_pipeline_is_noop() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        resp = client.post(
            "/api/admin/import/cancel",
            json={"pipeline_id": "ghost"},
        )
    # ImporterFacade.cancel_pipeline silently no-ops on unknown ids;
    # the route mirrors that — 200 with {"status": "cancelled"} is the
    # idempotent contract.
    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled"}


def test_cancel_real_pipeline_marks_it_cancelled() -> None:
    _rt, facade, client = _build_rig()
    with client:
        upload = client.post(
            "/api/admin/import/upload_text", json={"text": "x"}
        ).json()
        started = client.post(
            "/api/admin/import/start",
            json={"upload_id": upload["upload_id"]},
        ).json()
        resp = client.post(
            "/api/admin/import/cancel",
            json={"pipeline_id": started["pipeline_id"]},
        )
    assert resp.status_code == 200
    assert (
        facade._pipelines[started["pipeline_id"]].status == "cancelled"
    )


# ---------------------------------------------------------------------------
# GET /api/admin/import/events — SSE streaming
# ---------------------------------------------------------------------------


def test_events_route_is_registered() -> None:
    """Smoke: the six admin-import routes are mounted on the app.

    TestClient's sync-to-async bridge cannot drain
    ``EventSourceResponse``'s async generator cleanly (same limitation
    that forced Stage 2's chat-SSE test into a live-server pattern),
    so the body-level SSE assertion lives in the ``_over_live_server``
    test below.
    """

    _rt, _facade, client = _build_rig()
    routes = {r.path for r in client.app.routes}  # type: ignore[attr-defined]
    assert "/api/admin/import/events" in routes
    assert "/api/admin/import/upload" in routes
    assert "/api/admin/import/upload_text" in routes
    assert "/api/admin/import/estimate" in routes
    assert "/api/admin/import/start" in routes
    assert "/api/admin/import/cancel" in routes


@pytest.mark.asyncio
async def test_events_sse_streams_pipeline_events_over_live_server() -> None:
    """Spin up a real uvicorn on a free port, emit an event through
    the facade, confirm the SSE frame reaches the HTTP client.

    Uses the same live-socket pattern Stage 2 adopted for chat SSE —
    TestClient's ``client.stream`` wedges on sse-starlette generators,
    so a real HTTP transport is the robust path.
    """

    import socket

    import uvicorn

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    _rt, facade, client = _build_rig()
    app = client.app  # type: ignore[attr-defined]

    cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    server_task = asyncio.create_task(server.serve())

    try:
        # Wait for uvicorn to bind by polling any trivially-valid route.
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0
        ) as probe:
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await probe.post(
                        "/api/admin/import/upload_text",
                        json={"text": "ping"},
                    )
                    if r.status_code == 200:
                        break
                except (httpx.ConnectError, httpx.ReadError):
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("uvicorn never came up")

        pipeline_id = await facade.start_pipeline("upload-sse")

        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=5.0
            ) as stream_client,
            stream_client.stream(
                "GET",
                "/api/admin/import/events",
                params={"pipeline_id": pipeline_id},
            ) as resp,
        ):
            assert resp.status_code == 200

            async def _collect() -> str:
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    if "pipeline.update" in buf:
                        return buf
                    if len(buf) > 8192:
                        return buf
                return buf

            collector = asyncio.create_task(_collect())
            await asyncio.sleep(0.1)
            await facade.emit_event(
                PipelineEvent(
                    pipeline_id=pipeline_id,
                    type="pipeline.update",
                    payload={"stage": "chunking", "progress": 42},
                )
            )
            await asyncio.sleep(0.1)
            # Cancel pushes the sentinel that unblocks the handler.
            await facade.cancel_pipeline(pipeline_id)
            buf = await asyncio.wait_for(collector, timeout=3.0)

        assert "event: import.progress" in buf
        assert "pipeline.update" in buf
        assert "chunking" in buf
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except TimeoutError:
            server_task.cancel()


def test_events_unknown_pipeline_returns_404() -> None:
    _rt, _facade, client = _build_rig()
    with client:
        resp = client.get(
            "/api/admin/import/events",
            params={"pipeline_id": "no-such-pipeline"},
        )
    assert resp.status_code == 404

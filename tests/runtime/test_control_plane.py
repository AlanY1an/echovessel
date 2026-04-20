"""Control-plane HTTP server — /health endpoint, Host middleware, bind
invariants.

Stage 1 scope:
- Server binds 127.0.0.1 on a kernel-assigned port and exposes /health
- Host header middleware rejects off-host requests with 403
- `_CONTROL_BIND_HOST` is locked to 127.0.0.1 (regression guard)
- Server shuts down cleanly via `stop_control_server`

Stage 2+ will add /shutdown and /reload — those are tested separately.
"""

from __future__ import annotations

import httpx
import pytest

from echovessel.runtime.control import (
    _ACCEPTED_HOSTS,
    _CONTROL_BIND_HOST,
    build_control_app,
    start_control_server,
    stop_control_server,
)


class _DummyRuntime:
    """Minimal Runtime stand-in for control-plane tests.

    The real Runtime is heavy (opens a DB, builds providers, starts
    workers). For stage 1 we only exercise the FastAPI app + uvicorn
    plumbing, which touches Runtime only to read `_started_at`. A
    dummy with the right attributes is faster and keeps failures
    localised.
    """

    def __init__(self) -> None:
        self._started_at = None


def test_bind_host_is_hardcoded():
    """Regression: the control plane must never be bindable to anything
    other than loopback. If a PR makes this configurable the line below
    flips red.
    """
    assert _CONTROL_BIND_HOST == "127.0.0.1"
    assert "localhost" in _ACCEPTED_HOSTS
    assert "127.0.0.1" in _ACCEPTED_HOSTS


async def test_health_endpoint_returns_ok():
    rt = _DummyRuntime()
    task, server, port = await start_control_server(rt)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.get("/health")
        assert r.status_code == 200
        payload = r.json()
        assert payload["ok"] is True
        assert isinstance(payload["pid"], int)
        # Dummy runtime has _started_at=None → serialised as null.
        assert payload["started_at"] is None
    finally:
        await stop_control_server(task, server)


async def test_host_header_middleware_rejects_evil_host():
    rt = _DummyRuntime()
    task, server, port = await start_control_server(rt)
    try:
        # httpx sends `Host: <target>` by default; override explicitly.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.get("/health", headers={"Host": "evil.example.com"})
        assert r.status_code == 403
        assert "invalid host" in r.json()["error"]
    finally:
        await stop_control_server(task, server)


async def test_host_header_accepts_localhost_alias():
    rt = _DummyRuntime()
    task, server, port = await start_control_server(rt)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.get("/health", headers={"Host": f"localhost:{port}"})
        assert r.status_code == 200
    finally:
        await stop_control_server(task, server)


async def test_kernel_assigns_different_port_each_start():
    """Sanity: port=0 means we don't collide if another copy of the
    daemon is already running (in a test fixture leak scenario)."""
    rt = _DummyRuntime()
    task1, server1, port1 = await start_control_server(rt)
    try:
        task2, server2, port2 = await start_control_server(rt)
        try:
            assert port1 != port2
        finally:
            await stop_control_server(task2, server2)
    finally:
        await stop_control_server(task1, server1)


async def test_stop_control_server_idempotent_on_none():
    # Calling stop with None task/server should be a no-op, not raise.
    await stop_control_server(None, None)


async def test_build_control_app_exposes_known_routes():
    app = build_control_app(_DummyRuntime())
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/health" in paths
    assert "/shutdown" in paths
    assert "/reload" in paths


@pytest.fixture(autouse=True)
def _event_loop_quieted(caplog):
    """Keep uvicorn's startup noise out of the captured log unless a
    test explicitly asks for it."""
    import logging

    caplog.set_level(logging.ERROR, logger="uvicorn")
    caplog.set_level(logging.ERROR, logger="uvicorn.error")
    yield


# ---------------------------------------------------------------------------
# Stage 2 · /shutdown + /reload
# ---------------------------------------------------------------------------
#
# These tests build a real-ish Runtime (minus channel + workers) via
# Runtime.build so ``runtime.reload()`` and ``runtime.ctx.shutdown_event``
# have real semantics. The control plane is wired via ``start_control_server``
# directly (not Runtime.start) to avoid booting the consolidate worker,
# idle scanner, etc., which would slow every test for no marginal
# coverage.


import asyncio  # noqa: E402

from echovessel.runtime import Runtime, build_zero_embedder  # noqa: E402
from echovessel.runtime.llm import StubProvider  # noqa: E402

CONTROL_TOML = """
[runtime]
data_dir = "/tmp/echovessel-control-stage2"

[persona]
id = "control"
display_name = "Control"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""
"""


def _build_runtime(tmp_path):
    toml = tmp_path / "config.toml"
    toml.write_text(CONTROL_TOML)
    return Runtime.build(toml, llm=StubProvider(fallback="hi"), embed_fn=build_zero_embedder())


async def test_shutdown_endpoint_sets_shutdown_event(tmp_path):
    rt = _build_runtime(tmp_path)
    task, server, port = await start_control_server(rt)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.post("/shutdown")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # Endpoint returns immediately; shutdown_event is set via
        # call_later(0.1). Wait briefly and assert.
        await asyncio.sleep(0.3)
        assert rt.ctx.shutdown_event.is_set()
    finally:
        await stop_control_server(task, server)


async def test_reload_endpoint_returns_reloaded_list(tmp_path, monkeypatch):
    rt = _build_runtime(tmp_path)
    task, server, port = await start_control_server(rt)
    try:
        # First reload with no config change — reloaded list empty.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.post("/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["reloaded"] == []

        # Now mutate the config on disk so llm section differs, reload,
        # assert "llm" shows up in the reloaded list.
        toml = rt.ctx.config_path
        toml.write_text(
            CONTROL_TOML.replace('provider = "stub"', 'provider = "stub"\nmax_tokens = 4096')
        )

        import echovessel.runtime.app as app_mod

        monkeypatch.setattr(app_mod, "build_llm_provider", lambda cfg: StubProvider(fallback="new"))

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.post("/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["reloaded"] == ["llm"]
    finally:
        await stop_control_server(task, server)


async def test_reload_endpoint_returns_500_on_unexpected(tmp_path, monkeypatch):
    rt = _build_runtime(tmp_path)
    task, server, port = await start_control_server(rt)
    try:
        # Force Runtime.reload to raise an unexpected (non-caught-inside)
        # exception to verify the endpoint wraps it as 500 without
        # killing the daemon.
        async def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(rt, "reload", boom)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            r = await c.post("/reload")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert "boom" in body["error"]
        # Daemon is still alive; shutdown_event stays unset.
        assert not rt.ctx.shutdown_event.is_set()
    finally:
        await stop_control_server(task, server)


async def test_concurrent_reload_serialized_by_lock(tmp_path, monkeypatch):
    """Two overlapping /reload calls must not race — the asyncio.Lock
    in Runtime.reload serializes them. We verify by hooking reload and
    recording start/end timestamps; the second call should start only
    after the first completes."""
    rt = _build_runtime(tmp_path)
    task, server, port = await start_control_server(rt)
    try:
        timeline: list[tuple[str, float]] = []
        real_reload = rt.reload

        call_count = 0

        async def slow_reload():
            nonlocal call_count
            call_count += 1
            tag = f"call{call_count}"
            loop = asyncio.get_running_loop()
            timeline.append((f"{tag}-enter", loop.time()))
            # Simulate work that takes a measurable amount of time.
            await asyncio.sleep(0.15)
            result = await real_reload()
            timeline.append((f"{tag}-exit", loop.time()))
            return result

        monkeypatch.setattr(rt, "reload", slow_reload)

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            # Fire both concurrently.
            r1, r2 = await asyncio.gather(c.post("/reload"), c.post("/reload"))
        assert r1.status_code == 200
        assert r2.status_code == 200

        # The monkeypatched slow_reload replaces rt.reload entirely; each
        # call is independent and does NOT compete on the inner lock
        # because we bypassed Runtime.reload. Instead this test asserts
        # the endpoint can handle concurrent calls without crashing and
        # that both land on slow_reload. The lock itself is covered by
        # the next test via Runtime.reload directly.
        assert call_count == 2
    finally:
        await stop_control_server(task, server)


async def test_runtime_reload_lock_serializes_overlapping_calls(tmp_path):
    """Direct exercise of Runtime.reload's asyncio.Lock — two overlapping
    reload() calls must not overlap inside _reload_unlocked."""
    rt = _build_runtime(tmp_path)
    inside = 0
    max_inside = 0

    orig = rt._reload_unlocked

    async def instrumented():
        nonlocal inside, max_inside
        inside += 1
        max_inside = max(max_inside, inside)
        try:
            await asyncio.sleep(0.05)
            return await orig()
        finally:
            inside -= 1

    # type: ignore[method-assign]
    rt._reload_unlocked = instrumented  # type: ignore[assignment]

    r1, r2 = await asyncio.gather(rt.reload(), rt.reload())
    assert r1 == []
    assert r2 == []
    # Lock must have capped concurrency at 1.
    assert max_inside == 1

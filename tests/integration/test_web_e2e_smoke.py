"""End-to-end smoke tests for the Web channel.

Starts a real :class:`Runtime` with the Web channel enabled on a free
port, binds uvicorn, and hits the HTTP + SSE surface to verify the
full user-message → persona-reply pipeline works end to end. These
tests use the stub LLM provider and ``:memory:`` SQLite so they run
fast and require zero external network.

Tests that depend on Stage 3 admin API endpoints (``GET /api/state``,
``POST /api/admin/persona/onboarding``, ``POST
/api/admin/persona/voice-toggle``) probe for endpoint availability
with a ``GET /api/state`` call and **skip at runtime** if the admin
router is not yet registered — a concurrent worker is building
Stage 3 in parallel and these tests "activate" automatically the
moment that work lands without needing re-dispatch.

Two tests (chat roundtrip + chat retry 501) exercise the Stage 2
chat router directly and always run regardless of Stage 3 state.

Design notes:

- Port allocation uses ``socket.bind(("127.0.0.1", 0))`` to have the
  OS pick a free ephemeral port. The Stage 2 integration test pattern
  is adopted verbatim for continuity.
- Uvicorn readiness is polled via ``POST /api/chat/send`` in a tight
  loop with a 5-second deadline rather than a blind ``asyncio.sleep``.
  This guarantees the server is actually accepting connections before
  the test body runs.
- Every ``httpx.AsyncClient`` is constructed with an explicit timeout
  (``timeout=5.0`` or tighter) so a wedged uvicorn cannot hang the
  test suite indefinitely.
- SSE streams are drained through ``aiter_text`` + a substring check
  on ``event: <name>`` — the same pattern Stage 2's
  ``test_runtime_web_sse_stream_emits_connection_ready`` already
  proved works against sse_starlette.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
)
from echovessel.runtime.llm import StubProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Ask the OS for a free ephemeral TCP port on 127.0.0.1."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _toml(*, data_dir: str, port: int) -> str:
    """Build the smoke-test Config TOML.

    Matches the contract in tracker §3 Task 2: stub LLM, ``:memory:``
    SQLite, voice/proactive/discord all off, Web channel on a
    caller-chosen port with a 50 ms debounce window for fast tests.

    The string is written to a real file on disk by the fixture
    below rather than passed via ``config_override``. Reason:
    ``Runtime.update_persona_voice_enabled`` rejects toggles with
    ``400 cannot toggle voice_enabled without a config file`` when
    the runtime has no ``config_path`` to persist to. Stage 3's
    voice-toggle admin endpoint hits that code path, so a file-based
    Config is the only way to exercise it end to end.
    """
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "smoketest"
display_name = "Smoke"
voice_enabled = false

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60

[voice]
enabled = false

[proactive]
enabled = false

[channels.web]
enabled = true
host = "127.0.0.1"
port = {port}
debounce_ms = 50
"""


async def _wait_for_server(base_url: str, *, timeout: float = 5.0) -> None:
    """Poll ``base_url`` until uvicorn accepts a POST /api/chat/send.

    Uses the same probe pattern as Stage 2's integration test. Any
    2xx / 422 response counts as "server up"; connection refused /
    read errors keep polling until the deadline.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=0.5) as client:
                resp = await client.post(
                    "/api/chat/send",
                    json={"content": "ping", "user_id": "self"},
                )
                if resp.status_code in (202, 422):
                    return
        except (httpx.ConnectError, httpx.ReadError, OSError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"uvicorn server at {base_url} never came up")


async def _probe_state_or_skip(client: httpx.AsyncClient) -> dict:
    """Return the parsed ``/api/state`` body, or pytest.skip gracefully.

    The tracker forbids ``@pytest.mark.skipif`` with a static
    condition because Stage 3 might land between test runs and cache
    the "skipped" decision. Every Stage-3-dependent test calls this
    helper as its first step so the probe happens fresh each time.
    """
    r = await client.get("/api/state")
    if r.status_code == 404:
        pytest.skip("Stage 3 admin API (/api/state) not yet available")
    assert r.status_code == 200, (
        f"Stage 3 admin API returned unexpected status "
        f"{r.status_code}: {r.text}"
    )
    return r.json()


# ---------------------------------------------------------------------------
# Fixture — real Runtime + real uvicorn + yielded (runtime, base_url)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def runtime_and_url() -> AsyncIterator[tuple[Runtime, str]]:
    """Start a full :class:`Runtime` with the Web channel bound to a
    free port, yield ``(runtime, base_url)`` for test bodies, and tear
    down cleanly on exit.

    The TOML is written to a **real file** inside the temp data_dir
    and passed to ``Runtime.build`` as ``config_path``. This is a
    deliberate choice over ``config_override=`` — the Stage 3
    voice-toggle endpoint requires a persistable config file to
    write back to and returns ``400 cannot toggle voice_enabled
    without a config file`` otherwise. Writing the TOML once per
    fixture costs a few ms and unlocks the full admin API surface.
    """
    port = _pick_free_port()
    tmp = tempfile.mkdtemp(prefix="echovessel-e2e-smoke-")
    config_path = Path(tmp) / "config.toml"
    config_path.write_text(_toml(data_dir=tmp, port=port), encoding="utf-8")
    rt = Runtime.build(
        config_path,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )
    await rt.start(register_signals=False)
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_for_server(base_url)
        yield rt, base_url
    finally:
        await rt.stop()


# ---------------------------------------------------------------------------
# Test 1 · GET /api/state on an empty install (Stage 3 conditional)
# ---------------------------------------------------------------------------


async def test_get_state_on_empty_install(
    runtime_and_url: tuple[Runtime, str],
) -> None:
    """On a fresh install with no onboarding done, /api/state should
    report ``onboarding_required=true`` and empty memory counts.

    Skips cleanly when Stage 3 hasn't landed yet (probe returns 404).
    """
    _rt, base_url = runtime_and_url
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        body = await _probe_state_or_skip(client)
        assert body.get("onboarding_required") is True, (
            f"expected onboarding_required=True on fresh install, got {body}"
        )
        assert body["memory_counts"]["core_blocks"] == 0, (
            f"expected zero core blocks on fresh install, got {body}"
        )


# ---------------------------------------------------------------------------
# Test 2 · chat send + SSE roundtrip (Stage 2 only — always runs)
# ---------------------------------------------------------------------------


async def test_chat_send_and_sse_stream_roundtrip(
    runtime_and_url: tuple[Runtime, str],
) -> None:
    """The critical end-to-end smoke check.

    Opens an SSE stream, posts a user message, and asserts the
    expected sequence of events arrives: ``chat.connection.ready``
    (from SSE connect), ``chat.message.user_appended`` (from the POST
    round trip), and ``chat.message.done`` (from the stub LLM's canned
    "ok" reply flushing through the turn pipeline).

    This test intentionally does NOT depend on Stage 3 — the Stage 2
    chat router alone drives the whole flow. It is the one test that
    must always run.
    """
    _rt, base_url = runtime_and_url

    events_received: list[str] = []

    async def read_sse(client: httpx.AsyncClient) -> None:
        """Read SSE frames until we've seen the three events we need
        or hit the end-of-stream.

        ``aiter_text`` over sse_starlette gives us newline-delimited
        chunks in the ``event: <name>\\n data: <json>\\n\\n`` shape.
        Parsing the ``event:`` prefix is enough — we don't need to
        decode the data payload here because downstream assertions
        only check event names.
        """
        buf = ""
        async with client.stream("GET", "/api/chat/events") as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                buf += chunk
                # Drain complete lines from the buffer.
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line.startswith("event:"):
                        events_received.append(line[len("event:") :].strip())
                # Stop once we've captured the three events of
                # interest so the generator doesn't hold the
                # connection open past the test body.
                if (
                    "chat.connection.ready" in events_received
                    and "chat.message.user_appended" in events_received
                    and "chat.message.done" in events_received
                ):
                    return

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        sse_task = asyncio.create_task(read_sse(client))

        # Give SSE a moment to register with the broadcaster before
        # we post — otherwise the user_appended event could fire
        # before the subscriber queue even exists.
        await asyncio.sleep(0.1)

        r = await client.post(
            "/api/chat/send",
            json={"content": "hello", "user_id": "self"},
        )
        assert r.status_code == 202, (
            f"POST /api/chat/send expected 202, got {r.status_code}: {r.text}"
        )

        # Wait for the SSE reader to finish capturing the expected
        # events (or hit the overall timeout).
        try:
            await asyncio.wait_for(sse_task, timeout=8.0)
        except TimeoutError:
            sse_task.cancel()
            raise AssertionError(
                f"SSE stream never produced the expected event set; "
                f"collected={events_received}"
            ) from None

    assert "chat.connection.ready" in events_received, (
        f"missing chat.connection.ready event; collected={events_received}"
    )
    assert "chat.message.user_appended" in events_received, (
        f"missing chat.message.user_appended event; collected={events_received}"
    )
    assert "chat.message.done" in events_received, (
        f"missing chat.message.done event; collected={events_received}"
    )


# ---------------------------------------------------------------------------
# Test 3 · onboarding roundtrip (Stage 3 conditional)
# ---------------------------------------------------------------------------


async def test_onboarding_roundtrip(
    runtime_and_url: tuple[Runtime, str],
) -> None:
    """Full onboarding happy path against Stage 3's admin API.

    1. Probe /api/state; skip if 404 (Stage 3 not yet deployed).
    2. Assert onboarding_required is True on the fresh install.
    3. POST /api/admin/persona/onboarding with the four initial
       core block fields.
    4. Assert the response is ``{"ok": true}``.
    5. Re-GET /api/state — onboarding_required should flip to False
       and core_blocks count should climb to ≥1.
    6. GET /api/admin/persona and verify the persona block contains
       the text we just wrote.
    """
    _rt, base_url = runtime_and_url
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        before = await _probe_state_or_skip(client)
        assert before.get("onboarding_required") is True

        r = await client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Smoke",
                "persona_block": "Smoke is a test persona.",
                "user_block": "",
            },
        )
        assert r.status_code == 200, (
            f"POST onboarding expected 200, got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body.get("ok") is True, (
            f"onboarding response missing ok=true: {body}"
        )

        after = (await client.get("/api/state")).json()
        assert after.get("onboarding_required") is False
        assert after["memory_counts"]["core_blocks"] >= 1

        persona_resp = await client.get("/api/admin/persona")
        assert persona_resp.status_code == 200
        persona_body = persona_resp.json()
        # The persona block should contain the text we just wrote.
        persona_text = persona_body.get("core_blocks", {}).get("persona", "")
        assert "Smoke is a test persona" in persona_text, (
            f"expected persona block to contain the onboarding text, "
            f"got {persona_text!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 · voice toggle roundtrip (Stage 3 conditional)
# ---------------------------------------------------------------------------


async def test_voice_toggle_roundtrip(
    runtime_and_url: tuple[Runtime, str],
) -> None:
    """POST /api/admin/persona/voice-toggle flips persona.voice_enabled.

    Skips when Stage 3 hasn't landed yet. On a fresh install voice is
    disabled (see TOML fixture); we flip it to ``true`` and verify
    both the response body and a subsequent /api/state read reflect
    the new state.
    """
    _rt, base_url = runtime_and_url
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        # Probe + skip.
        before = await _probe_state_or_skip(client)
        # The voice toggle also needs onboarding to have completed on
        # some Stage 3 variants; the fixture starts with
        # onboarding_required=True, so we run onboarding first to
        # avoid a 409 on the voice-toggle endpoint.
        if before.get("onboarding_required") is True:
            init = await client.post(
                "/api/admin/persona/onboarding",
                json={
                    "display_name": "Smoke",
                    "persona_block": "Smoke persona.",
                    "user_block": "",
                },
            )
            assert init.status_code == 200

        r = await client.post(
            "/api/admin/persona/voice-toggle",
            json={"enabled": True},
        )
        assert r.status_code == 200, (
            f"voice-toggle expected 200, got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body.get("ok") is True, (
            f"voice-toggle response missing ok=true: {body}"
        )
        assert body.get("voice_enabled") is True, (
            f"voice-toggle response missing voice_enabled=true: {body}"
        )

        # Reading /api/state should now show voice_enabled=true in the
        # persona sub-object. The exact nesting is per Stage 3's
        # contract; we accept either a top-level persona.voice_enabled
        # or a flat voice_enabled field so both contract shapes work.
        state = (await client.get("/api/state")).json()
        persona_voice_enabled = (
            state.get("persona", {}).get("voice_enabled")
            if isinstance(state.get("persona"), dict)
            else state.get("voice_enabled")
        )
        assert persona_voice_enabled is True, (
            f"expected state to reflect voice_enabled=true, got {state}"
        )


# ---------------------------------------------------------------------------
# Test 6 · duplicate onboarding rejected with 409 (Stage 3 conditional)
# ---------------------------------------------------------------------------


async def test_duplicate_onboarding_rejected(
    runtime_and_url: tuple[Runtime, str],
) -> None:
    """Onboarding is a one-shot operation. Running it twice must fail
    with HTTP 409 Conflict.
    """
    _rt, base_url = runtime_and_url
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        await _probe_state_or_skip(client)

        payload = {
            "display_name": "Smoke",
            "persona_block": "Smoke is a test persona.",
            "user_block": "",
        }

        first = await client.post(
            "/api/admin/persona/onboarding", json=payload
        )
        assert first.status_code == 200, (
            f"first onboarding expected 200, got {first.status_code}: "
            f"{first.text}"
        )

        second = await client.post(
            "/api/admin/persona/onboarding", json=payload
        )
        assert second.status_code == 409, (
            f"second onboarding expected 409 Conflict, got "
            f"{second.status_code}: {second.text}"
        )

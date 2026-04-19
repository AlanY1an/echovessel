"""Unit tests for ImsgRpcClient.

Uses the real `asyncio.create_subprocess_exec` wired to a tiny Python
script that mimics `imsg rpc`'s stdio JSON-RPC behaviour. This keeps
the tests hermetic (no dependency on `imsg` being installed) while
still exercising the real subprocess/pipe machinery.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from echovessel.channels.imessage.client import (
    ImsgRpcClient,
    ImsgRpcError,
    ImsgRpcNotStartedError,
    ImsgRpcTimeoutError,
)


def _fake_imsg_script(tmp_path: Path, body: str) -> str:
    """Write a small Python script that impersonates `imsg rpc`.

    ``body`` is inlined as the script's main body; it can read lines
    from stdin and write frames to stdout. Returns the path as a
    string so the caller can pass it to `cli_path=`.
    """
    prelude = (
        "import json\n"
        "import sys\n"
        "\n"
        "def emit(frame):\n"
        '    sys.stdout.write(json.dumps(frame) + "\\n")\n'
        "    sys.stdout.flush()\n"
        "\n"
    )
    source = prelude + textwrap.dedent(body).lstrip("\n")
    (tmp_path / "fake_imsg.py").write_text(source)
    return sys.executable  # we pass the script as the first extra arg


@pytest.fixture
def fake_client_factory(tmp_path: Path):
    """Build an :class:`ImsgRpcClient` bound to an inline Python fake."""

    def _build(body: str, **kwargs) -> ImsgRpcClient:
        python = _fake_imsg_script(tmp_path, body)
        script_path = str(tmp_path / "fake_imsg.py")
        return ImsgRpcClient(
            cli_path=python,
            extra_args=(script_path,),
            **kwargs,
        )

    return _build


# ---------------------------------------------------------------------------
# request / response roundtrip
# ---------------------------------------------------------------------------


async def test_request_receives_result(fake_client_factory):
    body = """
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except Exception:
            continue
        if req.get("method") == "chats.list":
            emit({"jsonrpc": "2.0", "id": req["id"], "result": {"chats": [{"id": 1}]}})
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        result = await client.request("chats.list", {"limit": 1}, timeout_s=5.0)
        assert result == {"chats": [{"id": 1}]}
    finally:
        await client.stop()


async def test_request_error_envelope_raises(fake_client_factory):
    body = """
    for line in sys.stdin:
        req = json.loads(line)
        emit({
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32000, "message": "chat.db locked", "data": {"retry": True}},
        })
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        with pytest.raises(ImsgRpcError) as exc_info:
            await client.request("chats.list", {}, timeout_s=5.0)
        assert exc_info.value.code == -32000
        assert "chat.db locked" in exc_info.value.message
        assert exc_info.value.data == {"retry": True}
    finally:
        await client.stop()


async def test_request_timeout_when_no_response(fake_client_factory):
    # Fake reads stdin but never responds.
    body = """
    for line in sys.stdin:
        pass
    """
    client = fake_client_factory(body, request_timeout_s=0.2)
    await client.start()
    try:
        with pytest.raises(ImsgRpcTimeoutError):
            await client.request("chats.list", {})
    finally:
        await client.stop()


async def test_request_before_start_raises():
    client = ImsgRpcClient(cli_path="/bin/true")
    with pytest.raises(ImsgRpcNotStartedError):
        await client.request("chats.list", {})


async def test_concurrent_requests_each_get_correct_response(fake_client_factory):
    """Two outstanding requests should be matched by id, not order."""
    body = """
    import threading
    seen = []
    lock = threading.Lock()

    for line in sys.stdin:
        req = json.loads(line)
        with lock:
            seen.append(req)
            if len(seen) == 2:
                # reply to #2 first · client must still route by id
                for r in reversed(seen):
                    emit({"jsonrpc": "2.0", "id": r["id"], "result": {"method": r["method"]}})
                seen.clear()
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        r1, r2 = await asyncio.gather(
            client.request("chats.list", {}, timeout_s=5.0),
            client.request("send", {}, timeout_s=5.0),
        )
        assert r1 == {"method": "chats.list"}
        assert r2 == {"method": "send"}
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


async def test_subscribe_receives_notifications(fake_client_factory):
    body = """
    # Emit 3 notifications as soon as we start, then echo requests back.
    for i in range(3):
        emit({"jsonrpc": "2.0", "method": "message", "params": {"seq": i}})

    for line in sys.stdin:
        req = json.loads(line)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": {"ack": True}})
    """
    client = fake_client_factory(body)
    await client.start()

    received: list[dict] = []
    event = asyncio.Event()

    async def handler(params):
        received.append(params)
        if len(received) == 3:
            event.set()

    client.subscribe("message", handler)

    try:
        # Round-trip a request to synchronise — ensures stdout drained.
        await client.request("ping", {}, timeout_s=5.0)
        await asyncio.wait_for(event.wait(), timeout=5.0)
        assert [p["seq"] for p in received] == [0, 1, 2]
    finally:
        await client.stop()


async def test_notification_without_subscriber_is_logged_not_crashed(fake_client_factory):
    body = """
    emit({"jsonrpc": "2.0", "method": "orphan_method", "params": {}})
    for line in sys.stdin:
        req = json.loads(line)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": "ok"})
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        # If the unsubscribed notification crashed the reader loop, this
        # round-trip would time out. We want it to succeed.
        result = await client.request("ping", {}, timeout_s=5.0)
        assert result == "ok"
    finally:
        await client.stop()


async def test_handler_exception_does_not_break_reader(fake_client_factory):
    body = """
    emit({"jsonrpc": "2.0", "method": "message", "params": {}})
    emit({"jsonrpc": "2.0", "method": "message", "params": {}})
    for line in sys.stdin:
        req = json.loads(line)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": "ok"})
    """
    client = fake_client_factory(body)
    await client.start()

    call_count = 0

    async def bad_handler(params):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    client.subscribe("message", bad_handler)

    try:
        result = await client.request("ping", {}, timeout_s=5.0)
        assert result == "ok"
        # Give handler tasks time to complete / raise.
        await asyncio.sleep(0.1)
        assert call_count == 2
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# lifecycle edge cases
# ---------------------------------------------------------------------------


async def test_malformed_stdout_line_is_dropped(fake_client_factory):
    body = """
    sys.stdout.write("this is not JSON\\n")
    sys.stdout.flush()
    for line in sys.stdin:
        req = json.loads(line)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": "ok"})
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        # A round-trip must still succeed even though the first line on
        # stdout was garbage.
        assert await client.request("ping", {}, timeout_s=5.0) == "ok"
    finally:
        await client.stop()


async def test_stop_is_idempotent(fake_client_factory):
    body = """
    for line in sys.stdin:
        req = json.loads(line)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": "ok"})
    """
    client = fake_client_factory(body)
    await client.start()
    await client.stop()
    # Second stop must not raise.
    await client.stop()


async def test_subprocess_exit_unblocks_pending(fake_client_factory, tmp_path):
    """If the subprocess dies mid-request, pending futures must fail."""
    body = """
    # Read one line then exit without responding.
    sys.stdin.readline()
    sys.exit(0)
    """
    client = fake_client_factory(body, request_timeout_s=5.0)
    await client.start()

    async def fire_request():
        try:
            await client.request("ping", {})
        except ImsgRpcError as exc:
            return exc
        return None

    task = asyncio.create_task(fire_request())
    # Give the subprocess time to read the request and exit.
    await asyncio.sleep(0.2)
    await client.stop()
    exc = await task
    assert isinstance(exc, ImsgRpcError)
    assert "terminated" in exc.message.lower() or exc.code == -32603


# ---------------------------------------------------------------------------
# JSON-RPC wire-format sanity
# ---------------------------------------------------------------------------


async def test_request_envelope_is_well_formed(fake_client_factory, tmp_path):
    """Capture the raw stdin line to verify envelope shape."""
    captured_path = tmp_path / "captured.jsonl"
    body = f"""
    import json as _json
    path = {str(captured_path)!r}
    with open(path, "w") as f:
        for line in sys.stdin:
            f.write(line)
            f.flush()
            req = _json.loads(line)
            emit({{"jsonrpc": "2.0", "id": req["id"], "result": None}})
    """
    client = fake_client_factory(body)
    await client.start()
    try:
        await client.request("chats.list", {"limit": 7}, timeout_s=5.0)
    finally:
        await client.stop()

    raw = captured_path.read_text().strip().splitlines()
    assert len(raw) == 1
    frame = json.loads(raw[0])
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "chats.list"
    assert frame["params"] == {"limit": 7}
    assert isinstance(frame["id"], int)

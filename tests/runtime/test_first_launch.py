"""Stage 3 first-launch detection tests.

Verifies that :meth:`Runtime.start` schedules a browser open when
``core_blocks`` is empty and the Web channel is enabled, and that the
path skips the browser open on subsequent boots (after onboarding)
and when Web is disabled.

``webbrowser.open`` is monkeypatched so the tests work in headless
CI environments without spawning a real browser.
"""

from __future__ import annotations

import asyncio
import socket
import tempfile

import pytest
from sqlmodel import Session as DbSession

from echovessel.memory import append_to_core_block
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _toml(*, web_enabled: bool, port: int, data_dir: str) -> str:
    web = ""
    if web_enabled:
        web = f"""
[channels.web]
enabled = true
host = "127.0.0.1"
port = {port}
debounce_ms = 50
"""
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "first-launch"
display_name = "FirstLaunch"

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
{web}
"""


def _make_runtime(*, web_enabled: bool, port: int) -> Runtime:
    tmp = tempfile.mkdtemp(prefix="echovessel-firstlaunch-")
    cfg = load_config_from_str(_toml(web_enabled=web_enabled, port=port, data_dir=tmp))
    return Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
        embed_fn=build_zero_embedder(),
    )


async def _wait_for_task_named(name: str, timeout: float = 3.0) -> bool:
    """Poll asyncio tasks for one with the given name."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for t in asyncio.all_tasks():
            if t.get_name() == name:
                return True
        await asyncio.sleep(0.05)
    return False


async def test_first_launch_empty_memory_schedules_browser_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def _fake_open(url: str, new: int = 0) -> bool:
        calls.append((url, new))
        return True

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", _fake_open)

    port = _pick_free_port()
    rt = _make_runtime(web_enabled=True, port=port)
    await rt.start(register_signals=False)
    try:
        scheduled = await _wait_for_task_named("first_launch_browser_open")
        assert scheduled, "first_launch_browser_open task was never created"
        # Give the task its 0.5s sleep + headroom to call webbrowser.open.
        await asyncio.sleep(0.8)
    finally:
        await rt.stop()

    assert calls, "webbrowser.open was not called"
    url, _ = calls[0]
    assert url == f"http://127.0.0.1:{port}/"


async def test_second_launch_with_onboarding_done_does_not_open_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With at least one core_blocks row, first-launch detection must
    return False and the browser-open task must not be scheduled."""

    calls: list[str] = []

    def _fake_open(url: str, new: int = 0) -> bool:
        calls.append(url)
        return True

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", _fake_open)

    port = _pick_free_port()
    rt = _make_runtime(web_enabled=True, port=port)

    # Seed a core_block BEFORE start() so first-launch detection sees it.
    with DbSession(rt.ctx.engine) as db:
        append_to_core_block(
            db,
            persona_id="first-launch",
            user_id=None,
            label="persona",
            content="already onboarded",
            provenance={"source": "test-seed"},
        )

    await rt.start(register_signals=False)
    try:
        await asyncio.sleep(1.0)
        names = {t.get_name() for t in asyncio.all_tasks()}
    finally:
        await rt.stop()

    assert "first_launch_browser_open" not in names
    assert calls == []


async def test_web_disabled_skips_first_launch_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_open(url: str, new: int = 0) -> bool:
        calls.append(url)
        return True

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", _fake_open)

    rt = _make_runtime(web_enabled=False, port=0)
    await rt.start(register_signals=False)
    try:
        await asyncio.sleep(0.3)
    finally:
        await rt.stop()

    assert calls == []


def test_check_first_launch_direct_true_on_empty_memory() -> None:
    rt = _make_runtime(web_enabled=False, port=0)
    assert rt._check_first_launch() is True


def test_check_first_launch_direct_false_after_block_exists() -> None:
    rt = _make_runtime(web_enabled=False, port=0)
    with DbSession(rt.ctx.engine) as db:
        append_to_core_block(
            db,
            persona_id="first-launch",
            user_id=None,
            label="persona",
            content="seeded",
            provenance={"source": "test-seed"},
        )
    assert rt._check_first_launch() is False

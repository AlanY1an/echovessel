"""End-to-end daemon lifecycle via the control plane.

Spawns `uv run echovessel run --no-embedder` as a real subprocess and
exercises the CLI round-trip:

    echovessel run          → pidfile written, control plane up
    GET /health             → 200
    POST /reload            → 200, reloaded list
    echovessel stop         → control-plane POST /shutdown
    daemon exits            → pidfile removed

This is the single integration check that proves the whole
2026-04-daemon-control refactor is wired correctly in a real process
(not just the in-fixture Runtime the unit tests use).

Skipped on platforms / environments where spawning a subprocess is
prohibitively slow or sandboxed — `--no-embedder` keeps the cold
boot under 5 seconds locally.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

SMOKE_TOML = """
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "control-smoke"
display_name = "ControlSmoke"

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""
"""


def _wait_for_file(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _wait_for_port_ready(port: int, timeout: float = 5.0) -> bool:
    """Poll /health until 200 or timeout — gives the control plane a
    moment after pidfile write before we start hitting endpoints.

    The port is written to the pidfile AFTER `start_control_server`
    returns (which waits for uvicorn.Server.started), so in practice
    this loop should succeed on the first probe. Retained as a
    safety net for slow CI.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=1.0) as c:
                r = c.get(f"http://127.0.0.1:{port}/health")
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return False


def test_daemon_lifecycle_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(SMOKE_TOML.format(data_dir=str(data_dir)))
    pidfile = data_dir / "runtime.pid"

    env = os.environ.copy()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "echovessel",
            "run",
            "--config",
            str(cfg),
            "--log-level",
            "warn",
            "--no-embedder",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # 1. Pidfile appears within 30s (cold boot with zero-embedder).
        assert _wait_for_file(pidfile, timeout=30.0), (
            "daemon never wrote pidfile. "
            f"stderr tail: {(proc.stderr.read() if proc.stderr else b'').decode(errors='replace')[-500:]}"
        )

        # 2. Pidfile is v2 JSON with pid + control_port.
        payload = json.loads(pidfile.read_text(encoding="utf-8"))
        assert payload["pid"] == proc.pid
        assert payload["version"] == 1
        port = payload["control_port"]
        assert isinstance(port, int) and port > 0

        # 3. /health answers 200 within a short grace period.
        assert _wait_for_port_ready(port), (
            f"control plane /health did not come ready on port {port}"
        )

        # 4. /reload returns 200 with a reloaded list (empty because we
        #    didn't mutate the config).
        with httpx.Client(timeout=5.0) as c:
            r = c.post(f"http://127.0.0.1:{port}/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["reloaded"] == []

        # 5. Off-host Host header is rejected with 403.
        with httpx.Client(timeout=5.0) as c:
            r = c.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Host": "evil.example.com"},
            )
        assert r.status_code == 403

        # 6. POST /shutdown triggers clean exit.
        with httpx.Client(timeout=5.0) as c:
            r = c.post(f"http://127.0.0.1:{port}/shutdown")
        assert r.status_code == 200
        try:
            rc = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
            raise AssertionError(
                "daemon did not exit within 10s of POST /shutdown"
            ) from None

        assert rc in (0, -signal.SIGTERM), (
            f"unexpected exit code {rc}. "
            f"stderr tail: {(proc.stderr.read() if proc.stderr else b'').decode(errors='replace')[-500:]}"
        )
        assert not pidfile.exists(), "pidfile not cleaned up on clean exit"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def test_echovessel_stop_goes_through_control_plane(tmp_path: Path) -> None:
    """Spawn the daemon and stop it via `echovessel stop` (not POST).
    The stop CLI should prefer the control plane over SIGTERM and print
    the "via control plane" tag."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(SMOKE_TOML.format(data_dir=str(data_dir)))
    pidfile = data_dir / "runtime.pid"

    env = os.environ.copy()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "echovessel",
            "run",
            "--config",
            str(cfg),
            "--log-level",
            "warn",
            "--no-embedder",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_file(pidfile, timeout=30.0)

        stop_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "echovessel",
                "stop",
                "--config",
                str(cfg),
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert stop_result.returncode == 0, (
            f"stop exited non-zero: {stop_result.stderr}"
        )
        assert "control plane" in stop_result.stdout

        rc = proc.wait(timeout=10.0)
        assert rc in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

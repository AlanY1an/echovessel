"""End-to-end CLI smoke tests for the ``echovessel`` launcher.

Two layers of coverage:

1. **Unit-level (CliRunner)** — exercises ``init`` / ``run --help`` /
   ``status`` / ``stop`` / ``reload`` through click's in-process runner.
   ``os.kill`` is patched where we want to assert the signal without
   actually raising one.

2. **Integration (subprocess)** — spawns ``python -m echovessel run`` as
   a real child process with a stub-provider config + ``--no-embedder``
   so no API keys or model downloads are required. Validates the
   pidfile lifecycle and clean SIGTERM shutdown.

``tests/cli/test_init.py`` already covers deeper init semantics; we keep
a small smoke subset here so this file is a complete CLI contract
check on its own.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from echovessel.runtime.launcher import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SMOKE_TOML = """
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "cli-smoke"
display_name = "CLI Smoke"

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
"""


def _runner() -> CliRunner:
    return CliRunner()


def _stream(result) -> str:
    """CliRunner sometimes routes click.echo(err=True) to stdout and
    sometimes to a separate stderr buffer depending on the click
    version; tests should look at both."""
    return result.output + (getattr(result, "stderr", None) or "")


def _write_config(tmp_path: Path) -> Path:
    """Write a stub-provider config + ensure its data_dir exists so the
    pidfile resolver can write to it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(SMOKE_TOML.format(data_dir=str(data_dir)))
    return cfg


def _wait_for_file(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# init smoke
# ---------------------------------------------------------------------------


def test_init_writes_sample_to_target_path(tmp_path: Path) -> None:
    target = tmp_path / "fresh.toml"
    result = _runner().invoke(cli, ["init", "--config-path", str(target)])
    assert result.exit_code == 0, _stream(result)
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "[llm]" in body
    assert "[persona]" in body


def test_init_refuses_to_overwrite_existing(tmp_path: Path) -> None:
    target = tmp_path / "existing.toml"
    target.write_text("# user config\n")

    result = _runner().invoke(cli, ["init", "--config-path", str(target)])

    assert result.exit_code == 1
    assert target.read_text(encoding="utf-8") == "# user config\n"
    assert "already exists" in _stream(result)


def test_init_force_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "existing.toml"
    target.write_text("# old\n")

    result = _runner().invoke(
        cli, ["init", "--force", "--config-path", str(target)]
    )

    assert result.exit_code == 0, _stream(result)
    assert "[llm]" in target.read_text(encoding="utf-8")


def test_init_also_writes_env_template_when_missing(tmp_path: Path) -> None:
    """init drops a commented-out ``.env`` next to the config so the
    new-user flow does not have to guess which keys exist."""
    target = tmp_path / "fresh.toml"
    env_target = target.parent / ".env"
    result = _runner().invoke(cli, ["init", "--config-path", str(target)])
    assert result.exit_code == 0, _stream(result)
    assert env_target.exists()
    body = env_target.read_text(encoding="utf-8")
    # Key names appear but are all commented out so the daemon never
    # accidentally loads empty strings into os.environ.
    assert "OPENAI_API_KEY" in body
    assert "FISH_AUDIO_KEY" in body
    assert "ECHOVESSEL_DISCORD_TOKEN" in body
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(
            ("OPENAI_API_KEY", "FISH_AUDIO_KEY", "ECHOVESSEL_DISCORD_TOKEN",
             "ANTHROPIC_API_KEY"),
        ):
            raise AssertionError(
                f"env.sample should have every key commented out; found "
                f"active line: {line!r}"
            )


def test_init_never_clobbers_existing_env_even_with_force(tmp_path: Path) -> None:
    """--force overwrites config.toml but must NEVER overwrite .env —
    that file typically holds real API keys the user already pasted in."""
    target = tmp_path / "existing.toml"
    env_target = target.parent / ".env"
    target.write_text("# old config\n")
    env_target.write_text("OPENAI_API_KEY=sk-real-key-keep-me\n")

    result = _runner().invoke(
        cli, ["init", "--force", "--config-path", str(target)]
    )

    assert result.exit_code == 0, _stream(result)
    # config was rewritten…
    assert "[llm]" in target.read_text(encoding="utf-8")
    # …but .env was preserved byte-for-byte.
    assert env_target.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=sk-real-key-keep-me\n"
    )


# ---------------------------------------------------------------------------
# --help / argument parsing
# ---------------------------------------------------------------------------


def test_cli_help_lists_all_subcommands() -> None:
    result = _runner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for sub in ("init", "run", "stop", "reload", "status"):
        assert sub in result.output, f"subcommand {sub!r} missing from --help"


def test_run_help_lists_flags() -> None:
    result = _runner().invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--log-level" in result.output
    assert "--no-embedder" in result.output


def test_run_missing_config_exits_two(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    result = _runner().invoke(cli, ["run", "--config", str(missing)])
    assert result.exit_code == 2
    assert "echovessel init" in _stream(result)


def test_run_invalid_config_exits_two(tmp_path: Path) -> None:
    """Config parses as TOML but fails Pydantic validation."""
    bad = tmp_path / "bad.toml"
    bad.write_text('[llm]\nprovider = "not_a_real_provider"\n')

    result = _runner().invoke(cli, ["run", "--config", str(bad)])
    assert result.exit_code == 2
    assert "Config invalid" in _stream(result) or "invalid" in _stream(result).lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_no_pidfile_reports_stopped(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)

    result = _runner().invoke(cli, ["status", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "stopped" in result.output


def test_status_live_pidfile_reports_running(tmp_path: Path) -> None:
    """Our own PID is guaranteed alive for the duration of this test."""
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text(str(os.getpid()))

    result = _runner().invoke(cli, ["status", "--config", str(cfg)])

    assert result.exit_code == 0
    assert f"running pid={os.getpid()}" in result.output


def test_status_stale_pidfile_reports_stale(tmp_path: Path) -> None:
    """PID 999999 is extremely unlikely to exist on any test host — if
    it happens to, the ProcessLookupError branch won't fire. We accept
    that risk; swap for a patched ``os.kill`` if this ever flakes."""
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text("999999")

    result = _runner().invoke(cli, ["status", "--config", str(cfg)])

    assert result.exit_code == 0
    assert "stale" in result.output


# ---------------------------------------------------------------------------
# stop / reload (signal dispatch)
# ---------------------------------------------------------------------------


def test_stop_without_pidfile_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)

    result = _runner().invoke(cli, ["stop", "--config", str(cfg)])

    assert result.exit_code == 1
    assert "no pidfile" in _stream(result)


def test_reload_without_pidfile_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)

    result = _runner().invoke(cli, ["reload", "--config", str(cfg)])

    assert result.exit_code == 1
    assert "no pidfile" in _stream(result)


def test_stop_sends_sigterm_to_pidfile(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text("12345")

    captured: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        captured.append((pid, sig))

    with patch("echovessel.runtime.launcher.os.kill", fake_kill):
        result = _runner().invoke(cli, ["stop", "--config", str(cfg)])

    assert result.exit_code == 0, _stream(result)
    assert captured == [(12345, signal.SIGTERM)]


def test_reload_sends_sighup_to_pidfile(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text("12345")

    captured: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        captured.append((pid, sig))

    with patch("echovessel.runtime.launcher.os.kill", fake_kill):
        result = _runner().invoke(cli, ["reload", "--config", str(cfg)])

    assert result.exit_code == 0, _stream(result)
    assert captured == [(12345, signal.SIGHUP)]


def test_stop_invalid_pidfile_content_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text("not-a-number")

    result = _runner().invoke(cli, ["stop", "--config", str(cfg)])

    assert result.exit_code == 1
    assert "valid integer" in _stream(result)


def test_stop_stale_pid_removes_pidfile(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"
    pidfile.write_text("12345")

    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(f"no such process {pid}")

    with patch("echovessel.runtime.launcher.os.kill", fake_kill):
        result = _runner().invoke(cli, ["stop", "--config", str(cfg)])

    assert result.exit_code == 1
    assert not pidfile.exists(), "stale pidfile should have been removed"


# ---------------------------------------------------------------------------
# Pidfile lifecycle + SIGTERM integration
# ---------------------------------------------------------------------------


def test_run_sigterm_exits_cleanly_and_removes_pidfile(tmp_path: Path) -> None:
    """Spawn ``python -m echovessel run`` as a subprocess and verify:

    - daemon writes its pidfile within 30s (embedder skipped)
    - pidfile contents match the real child PID
    - SIGTERM triggers clean exit inside 10s
    - pidfile is removed on exit (finally: block in ``_async_run``)

    Uses ``--no-embedder`` to avoid downloading the ~90MB
    sentence-transformers model on a cold CI runner, and a stub LLM
    provider so no API keys are needed.
    """
    cfg = _write_config(tmp_path)
    pidfile = tmp_path / "data" / "runtime.pid"

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
        assert _wait_for_file(pidfile, timeout=30.0), (
            "daemon never wrote pidfile. "
            f"stderr tail: {(proc.stderr.read() if proc.stderr else b'').decode(errors='replace')[-500:]}"
        )

        assert pidfile.read_text(encoding="utf-8").strip() == str(proc.pid)

        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
            raise AssertionError("daemon did not exit within 10s of SIGTERM") from None

        # Python's signal handler in asyncio.run() normally causes a clean
        # exit with code 0. A -SIGTERM exit is also acceptable if the
        # handler chain re-raises instead of returning cleanly.
        assert rc in (0, -signal.SIGTERM), (
            f"unexpected exit code {rc}. "
            f"stderr: {(proc.stderr.read() if proc.stderr else b'').decode(errors='replace')[-500:]}"
        )
        assert not pidfile.exists(), "pidfile not cleaned up on clean exit"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

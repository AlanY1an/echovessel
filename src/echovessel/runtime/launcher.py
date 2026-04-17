"""Click-based CLI launcher.

Subcommands:
    echovessel init [--force] [--config-path PATH]
    echovessel run [--config PATH] [--log-level LEVEL] [--no-embedder]
    echovessel stop
    echovessel reload
    echovessel status

`init` copies the bundled config sample into the user's config path so
fresh installs (especially wheel installs where the repo root is not on
disk) have a starting point. `run` is the daemon entry point; it blocks
until SIGINT / SIGTERM. `stop` and `reload` read the pidfile
(`<data_dir>/runtime.pid`) and send the appropriate signal. `status`
reports whether a daemon is live.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import click

from echovessel.runtime.app import (
    Runtime,
    build_sentence_transformers_embedder,
    build_zero_embedder,
)
from echovessel.runtime.config import load_config

DEFAULT_CONFIG_PATH = Path("~/.echovessel/config.toml").expanduser()

log = logging.getLogger("echovessel.launcher")


def _load_dotenv() -> None:
    """Load ``<cwd>/.env`` if it exists.

    Simple KEY=VALUE parser (no shell expansion, no interpolation). Lines
    starting with ``#`` are comments. Blank lines are ignored. Double or
    single quotes around values are stripped.

    This runs BEFORE config validation so ``api_key_env`` references can
    resolve to env vars defined in the .env file. Users put their API
    keys in a ``.env`` file in the directory they run ``echovessel run``
    from — typically the project root during development.
    """
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return

    loaded = 0
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    if loaded > 0:
        log.info("loaded %d env vars from %s", loaded, env_path)


@click.group()
def cli() -> None:
    """EchoVessel — local-first digital persona daemon."""
    pass


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _resolve_config_path(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def _pidfile_for(config_path: Path) -> Path:
    # Load config purely to resolve data_dir. Fall back to ~/.echovessel/
    try:
        cfg = load_config(config_path)
        return Path(cfg.runtime.data_dir).expanduser() / "runtime.pid"
    except Exception:  # noqa: BLE001
        return Path("~/.echovessel/runtime.pid").expanduser()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing config file instead of refusing.",
)
@click.option(
    "--config-path",
    "config_path",
    type=str,
    default=None,
    help=(
        "Target path for the new config.toml "
        "(default: ~/.echovessel/config.toml)."
    ),
)
def init(force: bool, config_path: str | None) -> None:
    """Create a starter config.toml from the bundled sample.

    Reads the sample from ``echovessel.resources.config.toml.sample``
    via :mod:`importlib.resources` so it works identically in a source
    checkout and in a wheel install. Writes to
    ``~/.echovessel/config.toml`` by default; pass ``--config-path`` to
    write somewhere else. Without ``--force`` the command refuses to
    clobber an existing file and exits with status 1.
    """
    target = (
        Path(config_path).expanduser()
        if config_path is not None
        else DEFAULT_CONFIG_PATH
    )

    if target.exists() and not force:
        click.echo(
            (
                f"error: config file already exists at {target}\n"
                f"       use --force to overwrite, or edit the existing "
                f"file directly"
            ),
            err=True,
        )
        sys.exit(1)

    try:
        sample_text = (
            resources.files("echovessel.resources")
            .joinpath("config.toml.sample")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as e:
        click.echo(
            (
                f"error: bundled config sample is missing from the install "
                f"({type(e).__name__}: {e})\n"
                f"       this is a packaging bug; please file an issue"
            ),
            err=True,
        )
        sys.exit(2)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sample_text, encoding="utf-8")
    except OSError as e:
        click.echo(f"error: could not write to {target}: {e}", err=True)
        sys.exit(1)

    click.echo(f"wrote config to {target}")

    # Also drop a commented-out ``.env`` template in the current directory
    # (where the user will run ``echovessel run``). We NEVER overwrite an
    # existing ``.env`` — real keys live there and ``--force`` should not
    # wipe them.
    env_target = Path.cwd() / ".env"
    env_written = False
    if not env_target.exists():
        try:
            env_sample_text = (
                resources.files("echovessel.resources")
                .joinpath("env.sample")
                .read_text(encoding="utf-8")
            )
            env_target.write_text(env_sample_text, encoding="utf-8")
            # 0600 — this file holds secrets once the user fills it in.
            with contextlib.suppress(OSError):
                env_target.chmod(0o600)
            env_written = True
            click.echo(f"wrote env template to {env_target}")
        except (FileNotFoundError, ModuleNotFoundError, OSError) as e:
            click.echo(
                f"warning: could not write env template: {e}",
                err=True,
            )

    click.echo("")
    click.echo("Next steps:")
    click.echo(f"  1. Edit {target} to pick an LLM provider")
    if env_written:
        click.echo(
            f"  2. Uncomment and fill keys in {env_target} "
            "(OPENAI_API_KEY / FISH_AUDIO_KEY / ECHOVESSEL_DISCORD_TOKEN)"
        )
    else:
        click.echo(
            f"  2. Put your API keys in {env_target} "
            "(it already exists — daemon will auto-load it)"
        )
    click.echo("  3. Run `echovessel run` to start the daemon")
    click.echo("")
    click.echo(
        'For a smoke test without any API key, set [llm].provider = "stub"'
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--config", "config_path", type=str, default=None, help="Path to config.toml")
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warn", "error"], case_sensitive=False),
    default="info",
)
@click.option(
    "--no-embedder",
    is_flag=True,
    default=False,
    help="Skip loading sentence-transformers; use a deterministic zero embedder (dev/test).",
)
def run(config_path: str | None, log_level: str, no_embedder: bool) -> None:
    """Start the persona daemon (blocks until SIGINT/SIGTERM)."""
    _setup_logging(log_level)

    # Auto-load ~/.echovessel/.env if it exists — sets env vars for API keys
    # so users don't have to `export` them every time they open a terminal.
    _load_dotenv()

    resolved = _resolve_config_path(config_path)
    if not resolved.exists():
        click.echo(
            f"Config file not found: {resolved}\n"
            f"Run `echovessel init` to create a starter config at {resolved}, then edit it.",
            err=True,
        )
        sys.exit(2)

    try:
        cfg = load_config(resolved)
    except Exception as e:  # noqa: BLE001
        click.echo(f"Config invalid: {e}", err=True)
        sys.exit(2)

    data_dir = Path(cfg.runtime.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    pidfile = data_dir / "runtime.pid"

    asyncio.run(_async_run(resolved, pidfile, no_embedder=no_embedder))


async def _async_run(config_path: Path, pidfile: Path, *, no_embedder: bool) -> None:
    if no_embedder:
        embed_fn = build_zero_embedder()
    else:
        cfg = load_config(config_path)
        data_dir = Path(cfg.runtime.data_dir).expanduser()
        try:
            embed_fn = build_sentence_transformers_embedder(
                cfg.memory.embedder, data_dir / "embedder.cache"
            )
        except ImportError as e:
            log.error("%s", e)
            sys.exit(4)
        except Exception as e:  # noqa: BLE001
            log.error("embedder load failed: %s", e)
            sys.exit(4)

    rt = Runtime.build(config_path, embed_fn=embed_fn)

    try:
        await rt.start()
        # 2026-04-daemon-control · Pidfile is written AFTER start so
        # the control-plane port is known. Format is JSON v2; see
        # :func:`_write_pidfile`. Old readers (integer-only) will fail
        # fast with a clear error, which is fine because stop/reload
        # CLIs have been upgraded in lockstep.
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            _write_pidfile(pidfile, os.getpid(), rt.ctx.control_port)
        except Exception as e:  # noqa: BLE001
            log.warning("could not write pidfile %s: %s", pidfile, e)

        await rt.wait_until_shutdown()
    finally:
        await rt.stop()
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("could not remove pidfile %s: %s", pidfile, e)


# ---------------------------------------------------------------------------
# Pidfile · v2 format (JSON)
# ---------------------------------------------------------------------------
#
# Pidfile shape:
#
#     {"pid": 12345, "control_port": 54321, "version": 1}
#
# v1 was just the PID as plain integer text. `_read_pidfile` still
# accepts v1 by falling back — it returns `control_port=None` so stop /
# reload fall through to the signal-based path.


_PIDFILE_VERSION = 1


@dataclass
class PidfileInfo:
    pid: int
    control_port: int | None
    version: int


def _write_pidfile(path: Path, pid: int, control_port: int | None) -> None:
    payload = {
        "pid": pid,
        "control_port": control_port,
        "version": _PIDFILE_VERSION,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_pidfile(path: Path) -> PidfileInfo:
    """Read a v1 or v2 pidfile. Raises ValueError on malformed content."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"pidfile {path} is empty")
    # v1 fallback: integer-only.
    if raw.isdigit():
        return PidfileInfo(pid=int(raw), control_port=None, version=0)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"pidfile {path} is neither integer nor valid JSON: {e}"
        ) from e
    if not isinstance(payload, dict) or "pid" not in payload:
        raise ValueError(f"pidfile {path} missing 'pid' field")
    try:
        pid = int(payload["pid"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"pidfile {path} has non-integer 'pid'") from e
    port = payload.get("control_port")
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = None
    version = int(payload.get("version", 0) or 0)
    return PidfileInfo(pid=pid, control_port=port, version=version)


# ---------------------------------------------------------------------------
# stop / reload / status
# ---------------------------------------------------------------------------


def _control_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _try_control_post(port: int, path: str, *, timeout: float = 5.0) -> dict | None:
    """POST to the control plane and return the JSON body on 2xx.

    Returns None on any connection error / non-2xx — the caller then
    falls back to the signal-based path. We deliberately catch broadly
    so weird httpx edge cases (SSL, proxy, etc.) don't explode the
    CLI; the signal fallback is the safety net.
    """
    try:
        import httpx

        with httpx.Client(timeout=timeout) as c:
            r = c.post(_control_url(port, path))
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                return {"ok": True}
        return None
    except Exception:  # noqa: BLE001
        return None


def _try_control_get(port: int, path: str, *, timeout: float = 2.0) -> dict | None:
    try:
        import httpx

        with httpx.Client(timeout=timeout) as c:
            r = c.get(_control_url(port, path))
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                return None
        return None
    except Exception:  # noqa: BLE001
        return None


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def stop(config_path: str | None) -> None:
    """Stop a running daemon.

    Prefers the control-plane HTTP endpoint (POST /shutdown) because
    it's testable, works across process supervisors, and returns a
    success acknowledgement. Falls back to SIGTERM when the pidfile is
    v1 (no control_port) or the control plane is unreachable.
    """
    pid_path = _pidfile_for(_resolve_config_path(config_path))
    if not pid_path.exists():
        click.echo(f"no pidfile at {pid_path}; is the daemon running?", err=True)
        sys.exit(1)
    try:
        info = _read_pidfile(pid_path)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if info.control_port is not None:
        resp = _try_control_post(info.control_port, "/shutdown")
        if resp is not None:
            click.echo(f"stopped (via control plane, pid={info.pid})")
            return

    # Fallback: signal-based.
    try:
        os.kill(info.pid, signal.SIGTERM)
        click.echo(f"sent SIGTERM to pid {info.pid} (control plane unreachable)")
    except ProcessLookupError:
        click.echo(f"pid {info.pid} not running; removing stale pidfile", err=True)
        pid_path.unlink(missing_ok=True)
        sys.exit(1)
    except PermissionError as e:
        click.echo(f"permission denied sending SIGTERM to {info.pid}: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def reload(config_path: str | None) -> None:
    """Reload daemon config without restarting.

    Prefers the control-plane HTTP endpoint (POST /reload) because it
    returns the list of components that were actually reloaded. Falls
    back to SIGHUP when the pidfile is v1 or the control plane is
    unreachable.
    """
    pid_path = _pidfile_for(_resolve_config_path(config_path))
    if not pid_path.exists():
        click.echo(f"no pidfile at {pid_path}; is the daemon running?", err=True)
        sys.exit(1)
    try:
        info = _read_pidfile(pid_path)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if info.control_port is not None:
        resp = _try_control_post(info.control_port, "/reload")
        if resp is not None:
            reloaded = resp.get("reloaded", []) if isinstance(resp, dict) else []
            if reloaded:
                click.echo(
                    "reloaded (via control plane): " + ", ".join(reloaded)
                )
            else:
                click.echo(
                    "reloaded (via control plane): no changes detected"
                )
            return

    # Fallback: signal-based.
    try:
        os.kill(info.pid, signal.SIGHUP)
        click.echo(f"sent SIGHUP to pid {info.pid} (control plane unreachable)")
    except ProcessLookupError:
        click.echo(f"pid {info.pid} not running; removing stale pidfile", err=True)
        pid_path.unlink(missing_ok=True)
        sys.exit(1)
    except PermissionError as e:
        click.echo(f"permission denied sending SIGHUP to {info.pid}: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def status(config_path: str | None) -> None:
    """Report whether the daemon is running.

    Reports PID + control-plane reachability. A live PID with an
    unreachable control plane indicates a degraded daemon (main loop
    still running but its HTTP admin surface has died) — worth
    flagging so the operator knows to investigate.
    """
    pid_path = _pidfile_for(_resolve_config_path(config_path))
    if not pid_path.exists():
        click.echo("stopped")
        return
    try:
        info = _read_pidfile(pid_path)
    except ValueError as e:
        click.echo(f"stale pidfile ({e}); daemon not running")
        return

    try:
        os.kill(info.pid, 0)
    except (ProcessLookupError, PermissionError):
        click.echo("stale pidfile; daemon not running")
        return

    if info.control_port is None:
        click.echo(f"running · pid={info.pid} · control=n/a (v1 pidfile)")
        return

    health = _try_control_get(info.control_port, "/health")
    if health is not None:
        click.echo(
            f"running · pid={info.pid} · "
            f"control=http://127.0.0.1:{info.control_port} ok"
        )
    else:
        click.echo(
            f"running · pid={info.pid} · "
            f"control=http://127.0.0.1:{info.control_port} unreachable"
        )


def main() -> None:
    cli()


__all__ = ["cli", "main", "init", "run", "stop", "reload", "status"]

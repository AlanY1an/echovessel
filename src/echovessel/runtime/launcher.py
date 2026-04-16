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
import logging
import os
import signal
import sys
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
    """Load ``~/.echovessel/.env`` if it exists.

    Simple KEY=VALUE parser (no shell expansion, no interpolation). Lines
    starting with ``#`` are comments. Blank lines are ignored. Double or
    single quotes around values are stripped.

    This runs BEFORE config validation so ``api_key_env`` references can
    resolve to env vars defined in the .env file. Users write their API
    keys once in ``~/.echovessel/.env`` and never have to ``export`` them
    in every new terminal session.
    """
    env_path = Path("~/.echovessel/.env").expanduser()
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

    # Also drop a commented-out ``.env`` template next to the config so new
    # users know where to put their API keys. We NEVER overwrite an existing
    # ``.env`` — real keys live there and ``--force`` should not wipe them.
    env_target = target.parent / ".env"
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
    # Write pidfile just before blocking.
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except Exception as e:  # noqa: BLE001
        log.warning("could not write pidfile %s: %s", pidfile, e)

    try:
        await rt.start()
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
# stop / reload / status
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def stop(config_path: str | None) -> None:
    """Stop a running daemon by sending SIGTERM to the pidfile."""
    _send_signal(config_path, signal.SIGTERM, "stop")


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def reload(config_path: str | None) -> None:
    """Reload LLM provider config without restarting the daemon."""
    _send_signal(config_path, signal.SIGHUP, "reload")


def _send_signal(config_path: str | None, sig: signal.Signals, verb: str) -> None:
    pid_path = _pidfile_for(_resolve_config_path(config_path))
    if not pid_path.exists():
        click.echo(f"no pidfile at {pid_path}; is the daemon running?", err=True)
        sys.exit(1)
    try:
        pid = int(pid_path.read_text().strip())
    except ValueError:
        click.echo(f"pidfile {pid_path} is not a valid integer", err=True)
        sys.exit(1)
    try:
        os.kill(pid, sig)
        click.echo(f"sent {sig.name} to pid {pid}")
    except ProcessLookupError:
        click.echo(f"pid {pid} not running; removing stale pidfile", err=True)
        pid_path.unlink(missing_ok=True)
        sys.exit(1)
    except PermissionError as e:
        click.echo(f"permission denied sending {sig.name} to {pid}: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--config", "config_path", type=str, default=None)
def status(config_path: str | None) -> None:
    """Report whether the daemon is running."""
    pid_path = _pidfile_for(_resolve_config_path(config_path))
    if not pid_path.exists():
        click.echo("stopped")
        return
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        click.echo(f"running pid={pid}")
    except (ValueError, ProcessLookupError, PermissionError):
        click.echo("stale pidfile; daemon not running")


def main() -> None:
    cli()


__all__ = ["cli", "main", "init", "run", "stop", "reload", "status"]

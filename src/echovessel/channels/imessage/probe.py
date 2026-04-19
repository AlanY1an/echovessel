"""Probe — end-to-end health check for the imsg subprocess link.

Tests the entire chain (binary present → subprocess spawns → RPC round-
trip works) rather than probing individual macOS permission APIs. This
mirrors openclaw's `src/probe.ts` rationale: the OS permission query
surface is unreliable, but a real RPC request will only succeed if
every layer — binary, FDA, chat.db readability — is actually OK.

Can be used two ways:

1. Programmatically from :class:`IMessageChannel.is_ready` / startup
2. As a CLI: ``uv run python -m echovessel.channels.imessage.probe``
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from typing import Any

from echovessel.channels.imessage.client import (
    ImsgRpcClient,
    ImsgRpcError,
    ImsgRpcNotStartedError,
    ImsgRpcTimeoutError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a probe run.

    ``ok`` True means every stage passed. ``error`` is a short human-
    readable hint when ``ok`` is False. ``sample_chats`` carries the
    first few chats returned by ``chats.list`` so callers (or the CLI)
    can print them as confirmation.
    """

    ok: bool
    error: str | None = None
    sample_chats: list[dict[str, Any]] | None = None


async def probe(
    *,
    cli_path: str = "imsg",
    timeout_s: float = 10.0,
    limit: int = 5,
) -> ProbeResult:
    """Run a full end-to-end probe of the imsg RPC path.

    Steps:

    1. Verify the binary exists on PATH (or at the supplied absolute path)
    2. Spawn ``imsg rpc`` and call ``chats.list`` once
    3. Return the first ``limit`` chats as a confidence signal

    Any failure produces a ``ProbeResult(ok=False, error=…)`` with a
    user-actionable hint. No exceptions propagate.
    """
    # Step 1 · binary resolvable
    if "/" not in cli_path:
        resolved = shutil.which(cli_path)
        if resolved is None:
            return ProbeResult(
                ok=False,
                error=(
                    f"imsg binary not found on PATH (cli_path={cli_path!r}). "
                    "Install via `brew install steipete/tap/imsg` or set an "
                    "absolute path in config."
                ),
            )

    # Step 2 & 3 · spawn and round-trip
    client = ImsgRpcClient(cli_path=cli_path)
    try:
        await client.start()
    except FileNotFoundError:
        return ProbeResult(
            ok=False,
            error=f"imsg binary not found: {cli_path!r}",
        )
    except PermissionError as exc:
        return ProbeResult(
            ok=False,
            error=f"imsg not executable ({exc}). Check file permissions.",
        )

    try:
        try:
            result = await client.request(
                "chats.list",
                {"limit": limit},
                timeout_s=timeout_s,
            )
        except ImsgRpcTimeoutError as exc:
            return ProbeResult(
                ok=False,
                error=(
                    f"imsg rpc timed out after {timeout_s}s · {exc}. "
                    "Common causes: Full Disk Access not granted to the "
                    "terminal running this process, or chat.db locked by "
                    "a heavy Messages sync."
                ),
            )
        except ImsgRpcError as exc:
            # Recognise the two most common failure signatures that
            # arrive as error envelopes (or as subprocess-exit
            # diagnostics captured by the client):
            text = (exc.message or "").lower()
            hint = None
            if "permissiondenied" in text or "authorization denied" in text:
                hint = (
                    "Full Disk Access is missing. Open System Settings → "
                    "Privacy & Security → Full Disk Access, add the "
                    "terminal app running this process, then restart the "
                    "terminal."
                )
            elif "messages" in text and ("not running" in text or "no such" in text):
                hint = (
                    "Messages.app is not running or not signed in. Open "
                    "Messages, sign in with the persona's Apple ID, and "
                    "leave it running in the background."
                )
            error = f"imsg rpc error [{exc.code}]: {exc.message}"
            if hint is not None:
                error = f"{error}\n  hint · {hint}"
            return ProbeResult(ok=False, error=error)
        except ImsgRpcNotStartedError as exc:
            return ProbeResult(ok=False, error=str(exc))

        chats: list[dict[str, Any]] = []
        if isinstance(result, dict):
            maybe = result.get("chats")
            if isinstance(maybe, list):
                chats = [c for c in maybe if isinstance(c, dict)]
        return ProbeResult(ok=True, sample_chats=chats)
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "End-to-end health probe for the imsg subprocess bridge. "
            "Exits 0 if everything works, 1 otherwise."
        ),
    )
    parser.add_argument(
        "--cli-path",
        default="imsg",
        help="Path to the imsg binary (default: %(default)s, resolved from PATH).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-request RPC timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="How many sample chats to display on success (default: %(default)s).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s · %(message)s",
    )

    result = asyncio.run(
        probe(
            cli_path=args.cli_path,
            timeout_s=args.timeout,
            limit=args.limit,
        )
    )

    if not result.ok:
        print(f"✗ probe failed · {result.error}", file=sys.stderr)
        return 1

    chats = result.sample_chats or []
    print(f"✓ imsg RPC OK · {len(chats)} chat(s) visible")
    for chat in chats:
        identifier = chat.get("identifier", "?")
        service = chat.get("service", "?")
        is_group = chat.get("is_group", False)
        last = chat.get("last_message_at", "—")
        tag = "group" if is_group else "dm"
        print(f"  · [{service:8s}] {tag} {identifier}  ({last})")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

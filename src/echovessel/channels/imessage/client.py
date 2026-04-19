"""ImsgRpcClient — async JSON-RPC 2.0 client over `imsg rpc` stdio.

Wraps a long-lived `imsg rpc` subprocess and exposes:

- ``request(method, params)`` — one-shot RPC call that awaits a response
- ``subscribe(method, handler)`` — register a handler for server-initiated
  notifications (e.g. the ``message`` stream from ``watch.subscribe``)

Why this is a standalone class (not a generic JSON-RPC library):

- `imsg` is the only client, so the scope is tight
- macOS-specific error handling (FDA denied, Messages.app not running)
  belongs here, not in a generic abstraction
- the lifecycle semantics are opinionated: start-once, crash-loud,
  restart is the caller's job via the channel

Concurrency model:

- one reader task consumes ``stdout`` line-by-line and routes each frame
  either to a pending response future (matched by ``id``) or to
  subscriber handlers (matched by ``method``)
- ``request`` awaits a future keyed on a monotonically increasing id
- ``subscribe`` is fire-and-forget; handlers are scheduled on the event
  loop so a slow handler does not block the reader

All of this mirrors openclaw's `extensions/imessage/src/client.ts`, ported
to asyncio. The ideas are theirs; the code is ours.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ImsgRpcError(Exception):
    """Raised on any RPC-level error.

    ``code`` and ``data`` mirror the JSON-RPC 2.0 error envelope
    (https://www.jsonrpc.org/specification#error_object). ``data`` is
    provider-specific — `imsg` uses it to surface macOS-level causes
    (permission denied, binary missing, etc.).
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class ImsgRpcNotStartedError(RuntimeError):
    """Raised when ``request`` / ``subscribe`` runs before ``start``."""


class ImsgRpcTimeoutError(TimeoutError):
    """Raised when a ``request`` does not receive a response in time."""


# ---------------------------------------------------------------------------
# Notification handler contract
# ---------------------------------------------------------------------------


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """One in-flight request awaiting its matching response frame."""

    future: asyncio.Future[Any]
    method: str


@dataclass
class ImsgRpcClient:
    """Long-lived JSON-RPC stdio client for `imsg rpc`.

    Usage::

        client = ImsgRpcClient(cli_path="imsg")
        await client.start()
        try:
            chats = await client.request("chats.list", {"limit": 10})
            client.subscribe("message", on_message)
            await client.request("watch.subscribe", {})
            # ... handlers fire as notifications arrive ...
        finally:
            await client.stop()

    Thread safety: not thread-safe. All calls must happen on a single
    asyncio event loop. `ImsgRpcClient` instances are single-use — a
    stopped client should be discarded.
    """

    cli_path: str = "imsg"
    extra_args: tuple[str, ...] = ("rpc",)
    request_timeout_s: float = 30.0

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False)
    _pending: dict[int, _Pending] = field(default_factory=dict, init=False)
    _subscribers: dict[str, list[NotificationHandler]] = field(default_factory=dict, init=False)
    _next_id: int = field(default=1, init=False)
    _stopping: bool = field(default=False, init=False)
    _closed_event: asyncio.Event | None = field(default=None, init=False)
    # Ring buffers keep the last few non-JSON stdout lines and stderr
    # lines so `stop()` / subprocess-exit paths can include them in
    # the error surfaced to pending requests — makes diagnosing "imsg
    # died before responding" failures (usually FDA missing, Messages
    # not running, etc.) much easier.
    _recent_stdout_junk: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=5), init=False
    )
    _recent_stderr: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=10), init=False
    )

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn `imsg rpc` and begin consuming its stdout.

        Returns once the subprocess is spawned; does NOT wait for any
        handshake — the first ``request`` will fail with the relevant
        error if the daemon is not actually healthy.

        Safe to call once. Re-starting after ``stop`` is not supported;
        create a new client.
        """
        if self._proc is not None:
            raise RuntimeError("ImsgRpcClient already started")

        logger.info("imsg: spawning subprocess · %s %s", self.cli_path, " ".join(self.extra_args))
        self._proc = await asyncio.create_subprocess_exec(
            self.cli_path,
            *self.extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._closed_event = asyncio.Event()
        self._reader_task = asyncio.create_task(self._reader_loop(), name="imsg-rpc-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="imsg-rpc-stderr")

    async def stop(self) -> None:
        """Close stdin, wait briefly for graceful exit, then terminate.

        Idempotent. Cancels the reader task, rejects every pending
        request, and drops the subprocess handle.
        """
        if self._stopping:
            return
        self._stopping = True

        proc = self._proc
        if proc is None:
            return

        # Close stdin so the daemon's stdin loop exits. `imsg rpc`
        # shuts down cleanly when stdin closes.
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # Give the daemon a short window to exit on its own.
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            logger.warning("imsg: subprocess did not exit after stdin close; terminating")
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning("imsg: subprocess still alive after terminate; killing")
                proc.kill()
                await proc.wait()

        # Unblock pending requests — they can't succeed once the
        # daemon's gone. Include any captured diagnostic so callers
        # see WHY (permission denied, Messages.app crash, etc.).
        self._fail_pending(reason="imsg subprocess terminated")

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        if self._closed_event is not None:
            self._closed_event.set()
        self._proc = None

    async def wait_closed(self) -> None:
        """Block until the subprocess has exited (or ``stop`` completed).

        Useful for channels whose "run until stopped" loop wants a
        single await point on the RPC connection's lifetime.
        """
        if self._closed_event is None:
            return
        await self._closed_event.wait()

    # -- public RPC surface -----------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        """Send one JSON-RPC request and await its response.

        Returns the ``result`` field from the response envelope.
        Raises :class:`ImsgRpcError` when the server returns an error
        envelope. Raises :class:`ImsgRpcTimeoutError` if no response
        arrives in ``timeout_s`` (defaults to ``request_timeout_s``).
        """
        if self._proc is None or self._proc.stdin is None:
            raise ImsgRpcNotStartedError("ImsgRpcClient.start() has not been called")

        req_id = self._next_id
        self._next_id += 1
        envelope = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        line = json.dumps(envelope, ensure_ascii=False) + "\n"

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = _Pending(future=future, method=method)

        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(req_id, None)
            raise ImsgRpcError(code=-32603, message=f"imsg stdin broken: {exc}") from exc

        timeout = timeout_s if timeout_s is not None else self.request_timeout_s
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise ImsgRpcTimeoutError(f"imsg {method!r} timed out after {timeout}s") from exc

    def subscribe(self, method: str, handler: NotificationHandler) -> None:
        """Register a coroutine handler for server-initiated notifications.

        Multiple handlers for the same method are allowed; they are
        awaited concurrently when a matching frame arrives. Exceptions
        inside handlers are logged but do not disturb the reader loop.
        """
        self._subscribers.setdefault(method, []).append(handler)

    # -- internals --------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Consume stdout line-by-line and dispatch each frame.

        JSON-RPC frames are one-per-line, JSON objects. Malformed lines
        are captured into a ring buffer (so EOF paths can surface them
        as diagnostics) and otherwise dropped. The loop exits when
        stdout closes (EOF) or the reader task is cancelled.
        """
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        try:
            while True:
                line = await stdout.readline()
                if not line:
                    # EOF — daemon exited. Give stderr a moment to
                    # flush, then fail any pending requests with the
                    # collected diagnostic so the user sees WHY.
                    logger.info("imsg: stdout EOF · subprocess exited")
                    await asyncio.sleep(0.05)
                    self._fail_pending(reason="imsg subprocess exited before responding")
                    return

                text = line.decode("utf-8", errors="replace").rstrip("\n")
                try:
                    frame = json.loads(text)
                except json.JSONDecodeError:
                    self._recent_stdout_junk.append(text[:500])
                    logger.warning("imsg: non-JSON line on stdout: %r", text[:200])
                    continue

                if not isinstance(frame, dict):
                    logger.warning("imsg: unexpected frame (not object): %r", frame)
                    continue

                await self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("imsg: reader loop crashed")
        finally:
            if self._closed_event is not None:
                self._closed_event.set()

    async def _stderr_loop(self) -> None:
        """Drain stderr line-by-line into the ring buffer.

        imsg writes most diagnostics to stdout (as non-JSON lines when
        something goes wrong before RPC is up), but anything on stderr
        also needs to be available to _fail_pending's diagnostic string.
        """
        if self._proc is None or self._proc.stderr is None:
            return
        stderr = self._proc.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                if text:
                    self._recent_stderr.append(text[:500])
                    logger.warning("imsg stderr · %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("imsg: stderr loop crashed")

    def _collected_diagnostic(self) -> str:
        """Build a one-line summary of recent non-JSON output for errors."""
        fragments: list[str] = []
        if self._recent_stdout_junk:
            fragments.append("stdout: " + " / ".join(self._recent_stdout_junk))
        if self._recent_stderr:
            fragments.append("stderr: " + " / ".join(self._recent_stderr))
        return " | ".join(fragments)

    def _fail_pending(self, *, reason: str) -> None:
        """Fail every pending request with the collected diagnostic."""
        if not self._pending:
            return
        diag = self._collected_diagnostic()
        msg = f"{reason} · {diag}" if diag else reason
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(ImsgRpcError(code=-32603, message=msg))
        self._pending.clear()

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        """Route one parsed JSON-RPC frame to pending / subscriber."""
        frame_id = frame.get("id")

        # Response frame (has `id` matching a pending request).
        if frame_id is not None and frame_id in self._pending:
            pending = self._pending.pop(frame_id)
            if "error" in frame and frame["error"] is not None:
                err = frame["error"]
                pending.future.set_exception(
                    ImsgRpcError(
                        code=int(err.get("code", -32000)),
                        message=str(err.get("message", "unknown error")),
                        data=err.get("data"),
                    )
                )
            else:
                pending.future.set_result(frame.get("result"))
            return

        # Notification frame (has `method`, no matching id).
        method = frame.get("method")
        if isinstance(method, str):
            handlers = self._subscribers.get(method, [])
            if not handlers:
                logger.debug("imsg: notification %r has no subscriber", method)
                return
            params = frame.get("params") or {}
            if not isinstance(params, dict):
                logger.warning("imsg: notification %r params not an object", method)
                return
            for handler in handlers:
                asyncio.create_task(
                    self._run_handler(handler, params), name=f"imsg-notify-{method}"
                )
            return

        # Unrecognized frame shape.
        logger.warning("imsg: unroutable frame %r", frame)

    async def _run_handler(self, handler: NotificationHandler, params: dict[str, Any]) -> None:
        """Run one subscriber handler, swallowing + logging exceptions."""
        try:
            await handler(params)
        except Exception:
            logger.exception("imsg: notification handler failed")


__all__ = [
    "ImsgRpcClient",
    "ImsgRpcError",
    "ImsgRpcNotStartedError",
    "ImsgRpcTimeoutError",
    "NotificationHandler",
]

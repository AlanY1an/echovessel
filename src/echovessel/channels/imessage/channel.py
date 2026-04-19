"""IMessageChannel — Channel Protocol v0.2 implementation over imsg.

Satisfies :class:`echovessel.channels.base.Channel` by driving a
long-lived ``imsg rpc`` subprocess (see :class:`ImsgRpcClient`). The
debounce state machine is a verbatim copy of the
:class:`DiscordChannel` implementation — main thread will eventually
extract a shared helper, but for now the duplication is deliberate
(see the module-top comment in ``discord/channel.py``).

Scope (MVP)
-----------

Included:

- 1:1 DM ingestion via ``watch.subscribe`` on imsg
- 8-step inbound pipeline (destination filter, is_from_me,
  is_group, handle allowlist, echo cache, rate limiter) guarding
  the debounce state machine
- Outbound send via ``imsg`` RPC, targeting the originating peer
  of the current turn
- Dual-TTL echo cache so our own sends don't re-enter as inbound
- Per-conversation loop rate limiter

Deferred:

- Group chats (dropped at pipeline step 4)
- Attachments / reactions / tapbacks
- Proactive pushes initiated by persona (OutgoingKind="proactive")
- SMS-vs-iMessage service auto-detect beyond what imsg chooses
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

from echovessel.channels.base import (
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)
from echovessel.channels.imessage.client import ImsgRpcClient, ImsgRpcError
from echovessel.channels.imessage.echo_cache import EchoCache
from echovessel.channels.imessage.handles import normalize_handle
from echovessel.channels.imessage.rate_limiter import LoopRateLimiter

log = logging.getLogger(__name__)


# Hard upper bounds per turn — identical semantics to Discord/Web channel.
MAX_MESSAGES_PER_TURN = 20
MAX_CHARS_PER_TURN = 10_000


DropReason = str  # "wrong_destination" | "is_from_me" | "group"
# | "unauthorized" | "echo" | "rate_limited" | "empty"


class IMessageChannel:
    """Channel Protocol implementation for iMessage via ``imsg``.

    Required attributes from the Protocol::

        channel_id = "imessage"
        name = "iMessage"
        in_flight_turn_id: str | None = None

    Lifecycle:

    1. ``start()`` spawns the ``imsg rpc`` subprocess and subscribes to
       the ``message`` notification stream.
    2. Every incoming notification runs through the inbound pipeline
       (``_process_inbound``). Survivors are wrapped in an
       :class:`IncomingMessage` and handed to ``push_user_message``.
    3. The debounce state machine batches bursts into ``IncomingTurn``
       objects and drops them onto an internal queue that ``incoming()``
       iterates.
    4. ``send()`` routes a runtime-provided reply to the peer handle of
       the turn being answered, via ``imsg send`` over RPC.
    5. ``stop()`` tears down the subprocess and ends ``incoming()``
       cleanly via a ``None`` sentinel.
    """

    channel_id: ClassVar[str] = "imessage"
    name: ClassVar[str] = "iMessage"

    def __init__(
        self,
        *,
        persona_apple_id: str = "",
        cli_path: str = "imsg",
        db_path: str = "",
        allowed_handles: Iterable[str] | None = None,
        default_service: str = "auto",
        region: str = "US",
        debounce_ms: int = 2000,
        client: ImsgRpcClient | None = None,
    ) -> None:
        """Construct an iMessage channel.

        Arguments
        ---------

        persona_apple_id:
            Optional. The Apple ID the persona answers on. When set,
            the destination filter drops every inbound message whose
            ``destination_caller_id`` differs from this address — used
            for the "dual Apple ID on one macOS user" setup. Leave
            empty (``""``) for the single-account fast path, where the
            Mac's Messages.app only has one iMessage account anyway.

        cli_path:
            Path to the ``imsg`` binary. Defaults to ``"imsg"`` (resolved
            from ``PATH``). Can point at an SSH wrapper for remote-Mac
            deployments.

        db_path:
            Optional path to a non-default ``chat.db``. When empty,
            imsg uses its own default (``~/Library/Messages/chat.db``).

        allowed_handles:
            Optional allowlist of peer handles. Empty / ``None`` means
            "accept every non-self 1:1 DM to ``persona_apple_id``". Entries
            are normalized before comparison.

        default_service:
            One of ``"imessage" | "sms" | "auto"``. Forwarded to the
            ``send`` RPC call. Defaults to ``"auto"`` which lets imsg
            pick based on the peer.

        region:
            Region code used when a peer handle arrives without a ``+``
            country-code prefix. Passed to :func:`normalize_handle`.

        debounce_ms:
            Debounce window in milliseconds.

        client:
            Optional pre-built :class:`ImsgRpcClient` (for tests). When
            ``None``, one is constructed from ``cli_path`` on ``start``.
        """
        normalized_pid = persona_apple_id.strip() if persona_apple_id else ""
        self._persona_apple_id = (
            normalize_handle(normalized_pid, region=region) if normalized_pid else ""
        )
        self._cli_path = cli_path
        self._db_path = db_path.strip() if db_path else ""
        self._allowed_handles = {
            normalize_handle(h, region=region) for h in (allowed_handles or ()) if h and h.strip()
        }
        self._default_service = default_service
        self._region = region
        self._debounce_ms = debounce_ms
        self._debounce_seconds: float = debounce_ms / 1000.0

        # Debounce state machine.
        self._current_turn: list[IncomingMessage] = []
        self._next_turn: list[IncomingMessage] = []
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._out_queue: asyncio.Queue[IncomingTurn | None] = asyncio.Queue()
        self.in_flight_turn_id: str | None = None

        # Peer tracking — maps our internal user_id (the normalized peer
        # handle) back to any metadata we need when ``send`` fires. For
        # iMessage this is just the handle itself, but keeping the same
        # shape as Discord's ``_dm_channels`` map makes the duplication
        # easier to share later.
        self._current_user_id: str | None = None

        # Shared utilities.
        self._client = client
        self._owns_client = client is None
        self._echo = EchoCache()
        self._rate_limiter = LoopRateLimiter()

    # ---- Lifecycle -------------------------------------------------------

    def is_ready(self) -> bool:
        """Return True once the RPC client is started.

        imsg itself has no explicit "ready" signal — a successful
        ``watch.subscribe`` response is the closest analogue, and
        ``start()`` waits for that to resolve, so by the time ``start()``
        returns the channel is usable.
        """
        return self._client is not None and self._client._proc is not None

    async def start(self) -> None:
        """Spawn imsg, subscribe to message notifications.

        Idempotent-ish: calling twice with a pre-injected client is a
        no-op; calling twice without is a programming error.
        """
        if self._client is not None and self._client._proc is not None:
            return

        if self._client is None:
            # imsg's `rpc` subcommand accepts `--db <path>` to read from
            # a non-default chat.db. The flag lives after the subcommand
            # in the argv, matching how openclaw invokes it.
            extra_args: tuple[str, ...] = ("rpc",)
            if self._db_path:
                extra_args = ("rpc", "--db", self._db_path)
            self._client = ImsgRpcClient(cli_path=self._cli_path, extra_args=extra_args)

        await self._client.start()
        self._client.subscribe("message", self._handle_notification)
        # imsg also emits `error` notifications when the watch pipeline
        # itself has trouble (FSEvents hiccup, transient db lock, …).
        # Log them at warning level — they do not correspond to a user
        # message but are useful for triage.
        self._client.subscribe("error", self._handle_watch_error)

        # Ask imsg to start pushing new-message notifications.
        try:
            await self._client.request("watch.subscribe", {})
        except ImsgRpcError:
            # Surface the failure so runtime's startup logs show it
            # with the diagnostic string the client assembled; the
            # channel is unusable at this point.
            await self._client.stop()
            raise

        log.info(
            "imessage: channel started · persona=%s allowlist=%d",
            self._persona_apple_id,
            len(self._allowed_handles),
        )

    async def stop(self) -> None:
        """Tear down the subprocess and the state machine.

        Idempotent. Always drops a ``None`` sentinel onto the queue so
        any live ``incoming()`` iterator terminates cleanly.
        """
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

        if self._client is not None and self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.stop()
        self._client = None

        self._out_queue.put_nowait(None)

    # ---- Inbound pipeline (notification → IncomingMessage) --------------

    async def _handle_notification(self, params: dict[str, Any]) -> None:
        """Top of the inbound pipeline.

        Runs a single notification through the 8 gates (see plan file
        `develop-docs/initiatives/_active/2026-04-imessage-channel/
        00-plan.md`). Each gate either drops (logs a reason) or hands
        off to the next; a survivor is forwarded to the debounce state
        machine via :meth:`push_user_message`.

        ``imsg rpc`` wraps its notification payloads in an envelope
        shaped like ``{"subscription": N, "message": {...fields...}}``.
        We unwrap here so the rest of the pipeline sees a flat fields
        dict. If a future imsg version flattens the envelope, the
        unwrap is a no-op (fallback returns ``params`` as-is).
        """
        msg = _unwrap_notification(params)
        processed = self._process_inbound(msg)
        if processed is None:
            return
        await self.push_user_message(processed)

    async def _handle_watch_error(self, params: dict[str, Any]) -> None:
        """Log watch-layer errors without disturbing the main pipeline.

        imsg emits ``error`` notifications for transient issues (chat.db
        lock, FSEvents glitch, etc.). We surface them at warning level
        so operators can see a pattern of flakiness, but we do not
        treat a single error as fatal — the watch subscription keeps
        running and the next good notification will flow normally.
        """
        log.warning("imessage: imsg watch error · %s", _unwrap_notification(params))

    def _process_inbound(self, params: dict[str, Any]) -> IncomingMessage | None:
        """Apply the 8 inbound-pipeline gates.

        Returns the :class:`IncomingMessage` to enqueue, or ``None`` if
        the notification was dropped. Every drop is logged at debug
        level with a concrete reason string — easy to grep when
        triaging "why didn't persona answer".
        """
        # Step 1 · the RPC client already parsed JSON for us. If this
        # handler runs at all, the frame was well-formed.

        # Step 2 · destination filter · only messages addressed to the
        # persona's Apple ID survive. This separates persona traffic
        # from the user's own Apple ID on a shared Messages.app.
        # imsg publishes the field as ``destination_caller_id`` (see
        # openclaw's IMessagePayload type in extensions/imessage/src/
        # monitor/types.ts); older names are tolerated as fallbacks.
        # Skipped entirely when ``persona_apple_id`` is empty — that is
        # the single-account fast path (openclaw's default pattern).
        if self._persona_apple_id:
            destination = normalize_handle(
                self._first_str(
                    params,
                    "destination_caller_id",
                    "destination",
                    "account",
                    "account_id",
                ),
                region=self._region,
            )
            if destination and destination != self._persona_apple_id:
                self._drop("wrong_destination", params, extra={"destination": destination})
                return None

        # Step 3 · our own sends (is_from_me=true) surface on the same
        # watch stream and would loop forever.
        if bool(params.get("is_from_me")):
            self._drop("is_from_me", params)
            return None

        # Step 4 · groups dropped at MVP. imsg marks groups with
        # ``is_group=true``.
        if bool(params.get("is_group")):
            self._drop("group", params)
            return None

        # Step 5 · peer-handle allowlist.
        raw_handle = self._first_str(params, "sender", "handle", "from")
        handle = normalize_handle(raw_handle, region=self._region)
        if not handle:
            self._drop("empty", params, extra={"reason": "no sender"})
            return None
        if self._allowed_handles and handle not in self._allowed_handles:
            self._drop("unauthorized", params, extra={"handle": handle})
            return None

        text = str(params.get("text") or "").strip()
        if not text:
            self._drop("empty", params, extra={"handle": handle})
            return None

        # imsg emits guid as a UUID string and id as a numeric chat.db
        # ROWID. Prefer guid (stable across sessions, matches what the
        # `send` response returns) but tolerate either.
        message_id = self._first_str(params, "guid", "id")

        # Step 6 · echo cache. Our own send records these so the
        # watch-subscribe feedback loop doesn't spam the LLM.
        if self._echo.contains(text=text, message_id=message_id):
            self._rate_limiter.record_drop(handle)
            self._drop("echo", params, extra={"handle": handle})
            return None

        # Step 7 · per-conversation loop rate limit. Takes effect
        # *after* echo counts toward the limit, so a genuine echo
        # storm (two personas talking) trips suppression quickly.
        if self._rate_limiter.is_suppressed(handle):
            self._drop("rate_limited", params, extra={"handle": handle})
            return None

        # Step 8 · construct the envelope · user_id is the peer
        # handle (matches Discord's convention: user_id = external
        # peer id). external_ref carries the imsg message guid for
        # future threading.
        # imsg's timestamp field is ``created_at`` (ISO-8601); the
        # older ``date`` name is tolerated in case an older imsg
        # version is in use.
        received_at = (
            self._parse_iso(params.get("created_at"))
            or self._parse_iso(params.get("date"))
            or datetime.now()
        )
        return IncomingMessage(
            channel_id=self.channel_id,
            user_id=handle,
            content=text,
            received_at=received_at,
            external_ref=message_id,
        )

    async def push_user_message(self, msg: IncomingMessage) -> None:
        """Feed one processed message into the debounce state machine.

        Verbatim copy of Discord's implementation — see
        ``discord/channel.py::push_user_message`` for the state rules.
        """
        if self.in_flight_turn_id is None:
            self._current_turn.append(msg)
            if self._current_turn_over_limits():
                self._flush_current_turn()
                return
            self._schedule_flush()
        else:
            self._next_turn.append(msg)
            if self._next_turn_over_limits():
                log.warning(
                    "imessage channel next_turn hit hard limit while "
                    "runtime is mid-turn; holding until on_turn_done "
                    "(queued=%d)",
                    len(self._next_turn),
                )

    def _schedule_flush(self) -> None:
        """(Re-)schedule the debounce flush. Cancels any pending timer."""
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        loop = asyncio.get_running_loop()
        self._debounce_handle = loop.call_later(
            self._debounce_seconds,
            self._flush_current_turn,
        )

    def _flush_current_turn(self) -> None:
        """Emit ``_current_turn`` as an :class:`IncomingTurn`."""
        if not self._current_turn:
            self._debounce_handle = None
            return

        turn_id = _generate_turn_id()
        stamped_msgs = [replace(m, turn_id=turn_id) for m in self._current_turn]
        turn = IncomingTurn(
            turn_id=turn_id,
            channel_id=self.channel_id,
            user_id=stamped_msgs[0].user_id,
            messages=stamped_msgs,
            received_at=datetime.now(),
        )
        self._current_turn = []
        self._debounce_handle = None
        self.in_flight_turn_id = turn_id
        self._current_user_id = stamped_msgs[0].user_id
        self._out_queue.put_nowait(turn)

    # ---- Inbound iterator -----------------------------------------------

    async def incoming(self) -> AsyncIterator[IncomingTurn]:
        """Yield :class:`IncomingTurn` objects pulled from the out queue.

        Ends cleanly when :meth:`stop` drops a ``None`` sentinel.
        """
        while True:
            item = await self._out_queue.get()
            if item is None:
                return
            yield item

    # ---- Outbound (runtime → channel) -----------------------------------

    async def send(self, msg: OutgoingMessage) -> None:
        """Deliver a persona reply to the current turn's peer.

        Routes by ``_current_user_id`` (the peer handle recorded when
        the turn flushed). On success, records the sent text/id in the
        echo cache so the same message doesn't bounce back in through
        the watch stream.
        """
        if self._current_user_id is None:
            log.warning(
                "imessage send called with no current_user_id; dropping reply content_len=%d",
                len(msg.content),
            )
            return

        if self._client is None or not self.is_ready():
            log.warning(
                "imessage send: client not ready; dropping reply content_len=%d",
                len(msg.content),
            )
            return

        params: dict[str, Any] = {
            "to": self._current_user_id,
            "text": msg.content,
            "service": self._default_service,
            "region": self._region,
        }
        # Voice delivery · when the runtime's TTS pipeline produced an
        # audio artifact, attach it to the iMessage send. imsg forwards
        # the file as a generic audio attachment — recipients see a
        # playable "audio.mp3" bubble, not a native voice memo bubble
        # (that needs private APIs · out of scope per the plan file).
        # Both text and audio are sent; empty text stays acceptable.
        if msg.voice_result is not None and msg.voice_result.cache_path.exists():
            params["file"] = str(msg.voice_result.cache_path)
            log.info(
                "imessage send: attaching voice file %s (%.1fs)",
                msg.voice_result.cache_path.name,
                msg.voice_result.duration_seconds,
            )

        try:
            result = await self._client.request("send", params)
        except ImsgRpcError as exc:
            log.warning(
                "imessage send failed for handle=%s: [%s] %s",
                self._current_user_id,
                exc.code,
                exc.message,
            )
            return

        # Seed the echo cache with what we just sent so the inevitable
        # watch-subscribe echo of our own message doesn't reach the
        # LLM. Key priority matches openclaw's resolveMessageId in
        # extensions/imessage/src/send.ts so we honour whichever field
        # the current imsg version emits.
        sent_id: str | None = None
        if isinstance(result, dict):
            for key in ("messageId", "message_id", "id", "guid", "ok"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    sent_id = val.strip()
                    break
                if isinstance(val, int):
                    sent_id = str(val)
                    break
        self._echo.add(text=msg.content, message_id=sent_id)

    # ---- Runtime callback ------------------------------------------------

    async def on_turn_done(self, turn_id: str) -> None:
        """Clear ``in_flight_turn_id`` and promote ``_next_turn``.

        Identical semantics to Discord/Web — see their implementations
        for the review M1 iron rule details.
        """
        if turn_id != self.in_flight_turn_id:
            log.warning(
                "imessage channel on_turn_done called with turn_id=%r "
                "but in_flight_turn_id=%r; clearing state defensively",
                turn_id,
                self.in_flight_turn_id,
            )

        self.in_flight_turn_id = None
        self._current_user_id = None

        if not self._next_turn:
            return

        self._current_turn = self._next_turn
        self._next_turn = []
        self._schedule_flush()

    # ---- Hard-limit helpers ---------------------------------------------

    def _current_turn_over_limits(self) -> bool:
        if len(self._current_turn) >= MAX_MESSAGES_PER_TURN:
            return True
        total_chars = sum(len(m.content) for m in self._current_turn)
        return total_chars >= MAX_CHARS_PER_TURN

    def _next_turn_over_limits(self) -> bool:
        if len(self._next_turn) >= MAX_MESSAGES_PER_TURN:
            return True
        total_chars = sum(len(m.content) for m in self._next_turn)
        return total_chars >= MAX_CHARS_PER_TURN

    # ---- Diagnostics -----------------------------------------------------

    def _drop(
        self,
        reason: DropReason,
        params: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Centralised drop logger so triage greps produce consistent output."""
        log.debug(
            "imessage: drop reason=%s guid=%s sender=%s%s",
            reason,
            params.get("guid"),
            params.get("sender"),
            "" if not extra else f" extra={extra}",
        )

    # ---- Small helpers ---------------------------------------------------

    @staticmethod
    def _first_str(params: dict[str, Any], *keys: str) -> str:
        """Return the first ``params[key]`` that is a non-empty string.

        imsg may vary field names across versions (``sender`` vs
        ``handle`` vs ``from`` — the docs are not pinned). Tolerate a
        few shapes instead of guessing exactly one.
        """
        for key in keys:
            val = params.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return ""

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        """Parse an ISO-8601 timestamp into a **naive** local-time datetime.

        imsg emits UTC strings (``2026-04-19T06:22:10.280Z``); the rest
        of the runtime (memory.sessions, interaction, ingest) compares
        timestamps using the naive-local convention produced by
        ``datetime.now()``. Returning an aware datetime here would cross
        conventions and raise
        ``TypeError: can't compare offset-naive and offset-aware
        datetimes`` inside ``sessions._is_stale``. So after parsing
        the tz-aware form we convert to local and drop tzinfo.
        """
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone().replace(tzinfo=None)


def _generate_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex[:12]}"


def _unwrap_notification(params: dict[str, Any]) -> dict[str, Any]:
    """Return the inner message dict from an imsg notification envelope.

    Real imsg (v0.5.0, tested 2026-04) shapes ``message`` notifications
    as ``{"subscription": <int>, "message": {...}}``. The fields we want
    (sender, destination_caller_id, text, guid, …) are inside
    ``message``. Older / hypothetical future imsg versions may emit a
    flat dict — in that case the fallback returns ``params`` unchanged
    so the filter still sees the fields at top level.
    """
    inner = params.get("message")
    if isinstance(inner, dict):
        return inner
    return params


__all__ = [
    "IMessageChannel",
    "MAX_MESSAGES_PER_TURN",
    "MAX_CHARS_PER_TURN",
]

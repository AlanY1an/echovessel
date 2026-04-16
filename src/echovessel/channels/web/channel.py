"""Web channel core — debounce state machine and lifecycle stubs.

Stage 1 of the web v1 release (``develop-docs/web-v1/01-stage-1-tracker.md``)
delivers the non-transport half of the web channel: an in-memory
debounce state machine that implements the Channel Protocol v0.2 shape
described in ``develop-docs/channels/01-spec-v0.1.md`` §2.3a.

What is here:

- The :class:`WebChannel` class itself, satisfying
  :class:`echovessel.channels.base.Channel`
- A fully-working debounce state machine with ``current_turn`` /
  ``next_turn`` buffers and a ``call_later``-driven flush timer
- ``push_user_message`` as the single entry point for Stage 2's
  FastAPI ``POST /api/chat/send`` route (and for the Stage 1 tests)
- A stub ``send`` that appends to a ``sent`` list — Stage 2 replaces
  this with SSE fan-out

What is NOT here (deferred to later stages per tracker §5):

- FastAPI app factory, HTTP routes, SSE broadcast (Stage 2)
- Admin API for first-launch detection and persona voice toggle
  (Stage 3)
- Any frontend changes (Stage 4)
- Discord / iMessage / WeChat transport (Stage 6)
- Voice delivery wiring beyond the ``OutgoingMessage.delivery`` field
  (Stage 7)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from echovessel.channels.base import (
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)

if TYPE_CHECKING:
    from echovessel.channels.web.sse import SSEBroadcaster

log = logging.getLogger(__name__)


# Hard upper bounds from spec §2.3a — once hit, the channel flushes
# immediately instead of waiting for the debounce timer. These keep a
# single turn's LLM prompt bounded and prevent runaway burst input from
# wedging the state machine.
MAX_MESSAGES_PER_TURN = 50
MAX_CHARS_PER_TURN = 20_000


class WebChannel:
    """Channel Protocol v0.2 implementation for the local Web transport.

    The channel owns a debounce state machine that groups user messages
    into :class:`IncomingTurn` bursts. Usage:

    1. Something (Stage 2: a FastAPI route handler) calls
       :meth:`push_user_message` for each arriving message.
    2. The state machine accumulates messages into ``_current_turn``
       and schedules a flush after ``debounce_ms``.
    3. When the timer fires (or a hard limit is hit), the buffer is
       emitted as an :class:`IncomingTurn` onto the internal queue.
    4. The runtime's turn dispatcher consumes the turn via
       :meth:`incoming` and drives ``assemble_turn``.
    5. When the LLM finishes, runtime calls :meth:`on_turn_done`.
       If messages arrived during generation (``_next_turn`` non-empty),
       they are promoted back into ``_current_turn`` and scheduled via
       the **normal** debounce timer — not flushed immediately.

    The state machine is single-threaded (one event loop) and therefore
    needs no locking. Runtime's ``on_turn_done`` call is routed through
    the same event loop as ``push_user_message`` so the internal buffers
    are only mutated from one coroutine context.

    ``send`` is a Stage 1 stub: it appends the :class:`OutgoingMessage`
    to ``self.sent``. Tests assert on ``self.sent`` and Stage 2 replaces
    this method with real SSE fan-out.
    """

    channel_id: ClassVar[str] = "web"
    name: ClassVar[str] = "Web"

    def __init__(self, *, debounce_ms: int = 2000, user_id: str = "self") -> None:
        self._debounce_ms = debounce_ms
        self._debounce_seconds: float = debounce_ms / 1000.0
        self._user_id = user_id
        self._current_turn: list[IncomingMessage] = []
        self._next_turn: list[IncomingMessage] = []
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._out_queue: asyncio.Queue[IncomingTurn | None] = asyncio.Queue()
        self.in_flight_turn_id: str | None = None
        # Stage 1 stub: tests assert on `self.sent` as a flat list of
        # OutgoingMessage. Stage 2 keeps the list as a fall-back when no
        # SSEBroadcaster is attached so the Stage 1 debounce tests stay
        # green, but the real fan-out path is `_broadcaster.broadcast`.
        self.sent: list[OutgoingMessage] = []
        self._broadcaster: SSEBroadcaster | None = None

    # ---- Lifecycle --------------------------------------------------------

    def is_ready(self) -> bool:
        """Web channel has no external dependency — always ready.

        The browser-facing SSE transport lives inside the same asyncio
        event loop as runtime, so there is no remote handshake to wait
        on. Returns ``True`` unconditionally so ``/api/state`` can
        render "Web · 就绪" from the moment the daemon serves its
        first request.
        """
        return True

    async def start(self) -> None:
        """No-op for Stage 1.

        Stage 2 will bind the FastAPI server + SSE broadcaster here.
        """

    async def stop(self) -> None:
        """Terminate the ``incoming()`` iterator and cancel pending timers.

        Idempotent: multiple calls drop later sentinels onto the queue
        but the iterator only ever returns once because the first
        ``None`` ends the loop.
        """
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        # Put sentinel so ``incoming()`` terminates.
        self._out_queue.put_nowait(None)

    # ---- Inbound (state machine entry points) ----------------------------

    async def push_user_message(self, msg: IncomingMessage) -> None:
        """Feed one raw user message into the debounce state machine.

        This is the single entry point for user messages arriving on
        the transport. Stage 2's ``POST /api/chat/send`` route will
        call this; Stage 1 tests invoke it directly.

        State rules (spec §2.3a):

        1. If the channel is **idle** (``in_flight_turn_id is None``),
           the message joins ``_current_turn`` and (re-)starts the
           debounce timer.
        2. If the channel is **in-flight** (runtime is currently
           processing a turn), the message joins ``_next_turn`` and
           **no** timer is scheduled. ``on_turn_done`` will promote
           these messages through a normal debounce cycle.
        3. Either buffer can trigger the hard limits (message count or
           char count). Hitting a limit on ``_current_turn`` flushes
           immediately. Hitting a limit on ``_next_turn`` is a soft
           signal logged at warning level — messages stay queued until
           ``on_turn_done`` arrives.
        """
        if self.in_flight_turn_id is None:
            self._current_turn.append(msg)
            if self._current_turn_over_limits():
                self._flush_current_turn()
            else:
                self._schedule_flush()
        else:
            self._next_turn.append(msg)
            if self._next_turn_over_limits():
                log.warning(
                    "web channel next_turn hit hard limit while runtime "
                    "is mid-turn; holding until on_turn_done (queued=%d)",
                    len(self._next_turn),
                )

        # Stage 2 · echo the user message to every connected browser tab
        # so multi-tab UIs stay in sync. This fires BEFORE the LLM runs
        # so the UI can render the user bubble immediately.
        #
        # Worker X · ``source_channel_id`` is pinned to "web" so the
        # frontend doesn't need to branch on a null vs string contract.
        # Runtime publishes the same event with the originating channel
        # for non-Web turns (see Runtime._publish_cross_channel_event).
        await self.push_sse(
            "chat.message.user_appended",
            {
                "user_id": msg.user_id,
                "content": msg.content,
                "received_at": msg.received_at.isoformat(),
                "external_ref": msg.external_ref,
                "source_channel_id": self.channel_id,
            },
        )

    # ---- Debounce timer plumbing -----------------------------------------

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
        """Emit ``_current_turn`` as an :class:`IncomingTurn`.

        Synchronous — ``asyncio.call_later`` callbacks are not awaited.
        Manipulates the event-loop-owned buffers directly and uses
        ``put_nowait`` on the queue since the channel owns it.
        """
        # If something cancels us between schedule and fire, or if the
        # buffer was drained via another path, bail gracefully.
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
        self._out_queue.put_nowait(turn)

    # ---- Inbound iterator (channel → runtime) ----------------------------

    async def incoming(self) -> AsyncIterator[IncomingTurn]:
        """Yield :class:`IncomingTurn` objects pulled from the out queue.

        Ends cleanly when ``stop()`` drops a ``None`` sentinel onto the
        queue.
        """
        while True:
            item = await self._out_queue.get()
            if item is None:
                return
            yield item

    # ---- Outbound (runtime → channel) ------------------------------------

    async def send(self, msg: OutgoingMessage) -> None:
        """Deliver a persona reply / proactive push to connected clients.

        Stage 2 replaces the Stage 1 stub: when a broadcaster is
        attached, the message is fanned out as a
        ``chat.message.done`` SSE event. When no broadcaster is
        attached (tests that don't build a FastAPI app), the Stage 1
        stub behavior is preserved and the message is appended to
        ``self.sent`` so the original debounce tests keep passing.

        Stage 7 addition: when ``msg.voice_result`` is not None,
        a second SSE event ``chat.message.voice_ready`` is broadcast
        immediately after ``chat.message.done``. The frontend keys
        on this event to render the ``<audio>`` playback element.
        """

        if self._broadcaster is None:
            self.sent.append(msg)
            return

        # Compute a stable message_id that matches the on_token stream.
        # Channels can't import from runtime.interaction, so we
        # duplicate the hash computation inline.
        msg_id: int
        if msg.in_reply_to_turn_id is not None:
            msg_id = abs(hash(msg.in_reply_to_turn_id)) & 0x7FFFFFFF
        else:
            msg_id = id(msg)

        # Worker X · ``source_channel_id`` mirrors the outgoing envelope's
        # ``source_channel_id`` (which runtime populates from
        # ``turn.channel_id``). For Web-sourced turns that value is
        # "web"; for cross-channel runtime-mirrored turns the runtime
        # helper publishes the same event shape directly on the
        # broadcaster instead of going through this path.
        source_channel_id = msg.source_channel_id or self.channel_id
        await self.push_sse(
            "chat.message.done",
            {
                "message_id": msg_id,
                "content": msg.content,
                "in_reply_to": msg.in_reply_to,
                "in_reply_to_turn_id": msg.in_reply_to_turn_id,
                "delivery": msg.delivery,
                "source_channel_id": source_channel_id,
            },
        )

        # Stage 7: broadcast voice URL alongside the done event.
        if msg.voice_result is not None:
            await self.push_sse(
                "chat.message.voice_ready",
                {
                    "message_id": msg_id,
                    "url": msg.voice_result.url,
                    "duration_seconds": msg.voice_result.duration_seconds,
                    "cached": msg.voice_result.cached,
                    "source_channel_id": source_channel_id,
                },
            )

    # ---- Stage 2 · SSE capability ----------------------------------------

    def attach_broadcaster(self, broadcaster: SSEBroadcaster) -> None:
        """Bind an :class:`SSEBroadcaster` to this channel.

        Called by :func:`echovessel.channels.web.app.build_web_app` at
        startup. Before this is called the channel still works — it
        just falls back to the Stage 1 ``self.sent`` buffer.
        """

        self._broadcaster = broadcaster

    async def push_sse(self, event: str, payload: dict) -> None:
        """Fan-out an SSE event through the attached broadcaster.

        This is the capability method the runtime's memory observer
        and the voice_enabled toggle path detect via
        ``getattr(channel, "push_sse", None)``. When no broadcaster is
        attached (e.g. Stage 1 unit tests) the call is silently
        discarded with a debug log line so callers don't need to
        branch on channel type.
        """

        if self._broadcaster is None:
            log.debug(
                "push_sse dropped (no broadcaster attached): event=%s", event
            )
            return
        await self._broadcaster.broadcast(event, payload)

    def on_token_callback(self) -> Callable[[int, str], Awaitable[None]]:
        """Return an ``on_token`` callable for :func:`assemble_turn`.

        Runtime ``_handle_turn`` detects this method via ``getattr`` and
        passes the returned callable as the ``on_token`` kwarg into
        ``assemble_turn``. Each token delta arrives here and is
        broadcast as a ``chat.message.token`` SSE event so every open
        browser tab streams the persona reply in near-real-time.

        The callback uses ``message_id`` (an int — ``id()`` of the
        assembled turn placeholder per spec §17a.2) as the client-side
        correlation key. Channels never round-trip this id back to
        memory — it is purely a grouping handle for the client.
        """

        async def _push(message_id: int, delta: str) -> None:
            await self.push_sse(
                "chat.message.token",
                {
                    "message_id": message_id,
                    "delta": delta,
                    "source_channel_id": self.channel_id,
                },
            )

        return _push

    # ---- Runtime callback ------------------------------------------------

    async def on_turn_done(self, turn_id: str) -> None:
        """Clear ``in_flight_turn_id`` and promote ``_next_turn``.

        Review M1 iron rule: when ``_next_turn`` is non-empty, it is
        moved into ``_current_turn`` and scheduled through the
        **normal** debounce timer. The messages are NOT flushed
        immediately, so the user can keep typing and have their
        follow-up merged into the same burst.

        Idempotent and never raises — runtime may call this twice for
        the same turn on recovery paths, and a failing callback must
        not roll back the LLM reply that triggered it.
        """

        if turn_id != self.in_flight_turn_id:
            log.warning(
                "web channel on_turn_done called with turn_id=%r but "
                "in_flight_turn_id=%r; clearing state defensively",
                turn_id,
                self.in_flight_turn_id,
            )

        self.in_flight_turn_id = None

        if not self._next_turn:
            return

        # Promote next_turn → current_turn and start a normal debounce
        # cycle. We deliberately do NOT flush here — see spec §2.3a
        # review M1.
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


def _generate_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex[:12]}"


__all__ = [
    "WebChannel",
    "MAX_MESSAGES_PER_TURN",
    "MAX_CHARS_PER_TURN",
]

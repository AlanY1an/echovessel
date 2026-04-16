"""Discord DM channel — Channel Protocol v0.2 implementation.

Satisfies :class:`echovessel.channels.base.Channel` for direct messages
received via the official ``discord.py`` library. The state machine is
the same debounce pattern used by :class:`WebChannel` — see the big
block comment in ``push_user_message`` below for the spec reference.

Scope (tracker §5)
------------------

Included:

- Direct-message ingestion via a bundled ``discord.py`` client
- Per-turn debounce with the M1 iron rule (``next_turn`` promotes via
  a normal debounce cycle, never instant flush)
- DM reply routing keyed by persona-side ``user_id``
- Optional allowlist of Discord snowflakes to gate incoming DMs
- Hard upper bounds on per-turn message count / char count

Deferred to later rounds:

- Guild channels, slash commands, voice attachments, reactions
- SSE-style ``push_sse`` capability (Discord has no SSE)
- Auto-reconnect strategy beyond whatever ``discord.py`` does by
  default

Concurrent editing notes (why this is a verbatim copy of WebChannel's
state machine)
---------------------------------------------------------------------

Stage 2 (Web FastAPI + SSE) is editing
``src/echovessel/channels/web/channel.py`` in parallel with this
change. To avoid merge conflicts in ``channels/base.py`` and in the
WebChannel file itself, the debounce state machine is **duplicated**
into this file. Main thread will extract a shared ``DebounceState``
helper in a follow-up cleanup round after both Stage 2 and Stage 6
land. The duplication is intentional and documented.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from echovessel.channels.base import (
    IncomingMessage,
    IncomingTurn,
    OutgoingMessage,
)
from echovessel.channels.discord.bot import DiscordBot

if TYPE_CHECKING:
    import discord

log = logging.getLogger(__name__)


# Hard upper bounds from spec §2.3a — when hit, the channel flushes
# immediately instead of waiting for the debounce timer. These keep a
# single turn's LLM prompt bounded and prevent runaway burst input
# from wedging the state machine. Kept in sync with WebChannel.
MAX_MESSAGES_PER_TURN = 50
MAX_CHARS_PER_TURN = 20_000

# Discord native voice message flag (1 << 13). Set on the message so
# the client renders it as a playable voice bubble instead of a file.
_VOICE_MESSAGE_FLAG = 1 << 13


async def _convert_to_ogg_opus(mp3_path: Path) -> Path | None:
    """Re-encode an MP3 cache file to OGG Opus next to the original.

    Discord's native voice-message format requires OGG Opus — sending an
    MP3 with the voice flag set just renders as a generic audio file.
    Returns ``None`` if ffmpeg is not on PATH or the encode fails so the
    caller can fall back to a regular attachment.
    """
    if shutil.which("ffmpeg") is None:
        return None
    ogg_path = mp3_path.with_suffix(".ogg")
    if (
        ogg_path.exists()
        and ogg_path.stat().st_mtime >= mp3_path.stat().st_mtime
        and ogg_path.stat().st_size > 0
    ):
        return ogg_path
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-c:a", "libopus", "-b:a", "64k",
        "-application", "voip",
        str(ogg_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    if rc != 0 or not ogg_path.exists() or ogg_path.stat().st_size == 0:
        return None
    return ogg_path


async def _compute_waveform_b64(
    audio_path: Path,
    duration_seconds: float,
) -> str:
    """Build the base64-encoded amplitude bytes Discord renders as bars.

    Falls back to a flat waveform (all 0x80) if ffmpeg is unavailable —
    Discord still accepts the message, the client just shows a flat bar.
    """
    num_samples = max(20, min(256, int(max(duration_seconds, 1.0) * 5)))
    flat = base64.b64encode(bytes([0x80] * num_samples)).decode("ascii")
    if shutil.which("ffmpeg") is None:
        return flat
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(audio_path),
        "-ac", "1", "-f", "u8", "-ar", "8000", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    raw, _ = await proc.communicate()
    if not raw:
        return flat
    bucket = max(1, len(raw) // num_samples)
    out = bytearray(num_samples)
    for i in range(num_samples):
        chunk = raw[i * bucket : (i + 1) * bucket]
        if not chunk:
            out[i] = 0x80
            continue
        peak = max(abs(b - 128) for b in chunk)
        out[i] = min(255, peak * 2)
    return base64.b64encode(bytes(out)).decode("ascii")


async def _send_native_voice_message(
    target_dm: discord.DMChannel,
    *,
    ogg_path: Path,
    duration_seconds: float,
    waveform_b64: str,
) -> bool:
    """POST a native voice message via the lower-level HTTP route.

    discord.py 2.x's public ``send`` does not let you attach
    ``duration_secs`` / ``waveform`` to a file, so we build the
    multipart form ourselves and dispatch through the bot's HTTPClient.
    """
    import json

    from discord.http import Route

    state = target_dm._state
    http = state.http

    payload = {
        "flags": _VOICE_MESSAGE_FLAG,
        "attachments": [
            {
                "id": "0",
                "filename": "voice-message.ogg",
                "duration_secs": float(duration_seconds),
                "waveform": waveform_b64,
            }
        ],
    }

    file_bytes = ogg_path.read_bytes()
    form = [
        {
            "name": "payload_json",
            "value": json.dumps(payload),
            "content_type": "application/json",
        },
        {
            "name": "files[0]",
            "value": file_bytes,
            "filename": "voice-message.ogg",
            "content_type": "audio/ogg",
        },
    ]
    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=target_dm.id,
    )
    await http.request(route, form=form, files=[])
    return True


class DiscordChannel:
    """Channel Protocol v0.2 implementation for Discord direct messages.

    Owns a debounce state machine (same shape as
    :class:`echovessel.channels.web.channel.WebChannel`) that groups
    DM messages into :class:`IncomingTurn` bursts. Usage pattern when
    wired into runtime:

    1. ``Runtime.start()`` calls :meth:`start` which spins up a
       ``discord.py`` client on the runtime event loop.
    2. Every incoming DM is translated to an
       :class:`IncomingMessage` by :class:`DiscordBot.on_message` and
       dispatched into the state machine via :meth:`push_user_message`.
    3. The state machine debounces for ``debounce_ms`` milliseconds
       and emits an :class:`IncomingTurn` onto the internal queue.
    4. Runtime's turn dispatcher consumes turns via :meth:`incoming`.
    5. After ``assemble_turn`` completes, runtime calls
       :meth:`on_turn_done` which clears ``in_flight_turn_id`` and
       promotes ``_next_turn`` via the **normal** debounce timer.
    6. Runtime hands the reply back via :meth:`send` which resolves
       the target DM channel from the per-user mapping and forwards
       the content to Discord.

    DM routing
    ----------

    Runtime only knows a string ``user_id``; Discord needs a live
    ``discord.DMChannel`` object to send replies. The channel keeps a
    ``user_id -> discord.DMChannel`` map that is populated every time
    a DM arrives. ``_current_user_id`` tracks the author of the turn
    runtime is currently processing so :meth:`send` knows which DM
    channel to target.

    The mapping is in-process only and is lost on restart — that is
    fine because ``discord.py`` re-materialises a DM channel on
    demand whenever a new DM arrives from the same user.
    """

    channel_id: ClassVar[str] = "discord"
    name: ClassVar[str] = "Discord"

    def __init__(
        self,
        *,
        token: str,
        debounce_ms: int = 2000,
        allowed_user_ids: set[int] | None = None,
    ) -> None:
        """Construct a Discord channel.

        Arguments
        ---------

        token:
            Discord bot token. Runtime is responsible for reading the
            actual secret from an environment variable — the channel
            never reads ``os.environ`` itself so tests can inject a
            placeholder.

        debounce_ms:
            Debounce window in milliseconds. Defaults to 2000 ms
            (matches WebChannel and review M1 guidance). Lower values
            make tests faster; a real deployment should leave it at
            the default.

        allowed_user_ids:
            Optional set of Discord user snowflakes allowed to DM the
            bot. ``None`` disables the allowlist (every DM is
            accepted). Strongly recommended for any public-facing
            bot.
        """
        self._token = token
        self._debounce_ms = debounce_ms
        self._debounce_seconds: float = debounce_ms / 1000.0
        self._allowed_user_ids = allowed_user_ids

        # Debounce state machine (copy of WebChannel's contract).
        self._current_turn: list[IncomingMessage] = []
        self._next_turn: list[IncomingMessage] = []
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._out_queue: asyncio.Queue[IncomingTurn | None] = asyncio.Queue()
        self.in_flight_turn_id: str | None = None

        # Discord-specific state.
        self._bot: DiscordBot | None = None
        self._bot_task: asyncio.Task[None] | None = None
        # user_id -> live DMChannel. Populated in ``_handle_dm``.
        self._dm_channels: dict[str, discord.DMChannel] = {}
        # Tracks the user_id whose turn runtime is currently
        # processing so ``send`` knows which DM to target. Set when a
        # turn flushes, cleared in ``on_turn_done``.
        self._current_user_id: str | None = None

    # ---- Lifecycle -------------------------------------------------------

    def is_ready(self) -> bool:
        """Return True once the discord.py client has completed the gateway handshake.

        The bot is ``_bot is not None`` from the moment :meth:`start`
        kicks off the background task, but the gateway connection
        takes several seconds to complete (login + identify + ready
        event). ``discord.Client.is_ready()`` flips once the ``READY``
        dispatch arrives, which is the signal admin UIs need to show
        "Discord · 已连接" instead of "连接中".
        """
        return self._bot is not None and self._bot.is_ready()

    async def start(self) -> None:
        """Spin up the ``discord.py`` client on the running loop.

        Uses ``asyncio.create_task`` with ``self._bot.start(token)`` so
        the client runs concurrently with the rest of the runtime. If
        the token is rejected, ``discord.py`` raises
        ``discord.LoginFailure`` which propagates out of the background
        task — runtime's task exception handler surfaces the error.
        """
        if self._bot is not None:
            return  # already started; idempotent

        self._bot = DiscordBot(
            on_dm_received=self._handle_dm,
            allowed_user_ids=self._allowed_user_ids,
        )
        loop = asyncio.get_running_loop()
        self._bot_task = loop.create_task(self._bot.start(self._token))

    async def stop(self) -> None:
        """Gracefully tear down the discord client and the state machine.

        Idempotent. Always drops a ``None`` sentinel onto the queue so
        any live ``incoming()`` iterator terminates cleanly.
        """
        # Cancel the debounce timer first so a firing flush does not
        # race the queue sentinel below.
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

        if self._bot is not None:
            try:
                await self._bot.close()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "discord bot close raised %s: %s",
                    type(exc).__name__,
                    exc,
                )
            self._bot = None

        if self._bot_task is not None:
            self._bot_task.cancel()
            # Either the cancellation landed (expected) or the task
            # itself raised because the token was rejected — we do not
            # want ``stop()`` to re-raise either case.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._bot_task
            self._bot_task = None

        # Sentinel so ``incoming()`` ends.
        self._out_queue.put_nowait(None)

    # ---- Inbound (state machine entry points) ----------------------------
    #
    # The methods below are a verbatim copy of WebChannel's debounce
    # state machine. See the concurrent-editing note at the top of
    # this module for why the duplication is deliberate.

    async def _handle_dm(
        self,
        msg: IncomingMessage,
        dm_channel: discord.DMChannel,
    ) -> None:
        """Bot-side DM event handler.

        Called by :class:`DiscordBot.on_message` for every DM that
        survives the allowlist filter. Records the live
        ``discord.DMChannel`` object keyed by ``user_id`` so
        :meth:`send` can route replies, then dispatches the envelope
        into the debounce state machine.
        """
        self._dm_channels[msg.user_id] = dm_channel
        await self.push_user_message(msg)

    async def push_user_message(self, msg: IncomingMessage) -> None:
        """Feed one raw user message into the debounce state machine.

        State rules (spec §2.3a):

        1. If the channel is **idle** (``in_flight_turn_id is None``),
           the message joins ``_current_turn`` and (re-)starts the
           debounce timer.
        2. If the channel is **in-flight** (runtime is currently
           processing a turn), the message joins ``_next_turn`` and
           **no** timer is scheduled. ``on_turn_done`` will promote
           these messages through a normal debounce cycle.
        3. Either buffer can trigger the hard limits (message count
           or char count). Hitting a limit on ``_current_turn``
           flushes immediately. Hitting a limit on ``_next_turn`` is
           a soft signal logged at warning level — messages stay
           queued until ``on_turn_done`` arrives.
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
                    "discord channel next_turn hit hard limit while "
                    "runtime is mid-turn; holding until on_turn_done "
                    "(queued=%d)",
                    len(self._next_turn),
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

        Synchronous — ``loop.call_later`` callbacks are not awaited.
        Mutates the event-loop-owned buffers directly and uses
        ``put_nowait`` on the queue since the channel owns it.

        Also records ``_current_user_id`` so :meth:`send` knows which
        Discord DM channel to deliver the reply to.
        """
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
        # Remember who sent this turn so ``send`` can pick the right
        # DM channel. Every message in a single IncomingTurn shares a
        # user_id by invariant, so taking the first message's id is
        # sufficient.
        self._current_user_id = stamped_msgs[0].user_id
        self._out_queue.put_nowait(turn)

    # ---- Inbound iterator (channel → runtime) ----------------------------

    async def incoming(self) -> AsyncIterator[IncomingTurn]:
        """Yield :class:`IncomingTurn` objects pulled from the out queue.

        Ends cleanly when :meth:`stop` drops a ``None`` sentinel onto
        the queue.
        """
        while True:
            item = await self._out_queue.get()
            if item is None:
                return
            yield item

    # ---- Outbound (runtime → channel) ------------------------------------

    async def send(self, msg: OutgoingMessage) -> None:
        """Send ``msg.content`` to the Discord DM for the current turn.

        Looks up the DM channel by ``_current_user_id`` which was set
        when the current turn flushed. If no DM channel is mapped (the
        turn belongs to a user who never actually DMed the bot — a
        synthetic test path, or an edge case during startup), logs a
        warning and drops the message rather than raising.

        Discord's own ``Messageable.send`` handles markdown, embeds,
        and the 2000-character per-message limit. Messages longer
        than that are truncated by ``discord.py`` itself and the
        caller sees a ``HTTPException`` — we do not attempt to split
        here because chunking is a downstream concern handled by the
        runtime's streaming path (which already keeps token deltas
        bounded).
        """
        if self._current_user_id is None:
            log.warning(
                "discord send called with no current user_id; dropping "
                "reply content_len=%d",
                len(msg.content),
            )
            return

        target_dm = self._dm_channels.get(self._current_user_id)
        if target_dm is None:
            log.warning(
                "discord send: no DM channel mapped for user_id=%s; "
                "dropping reply content_len=%d",
                self._current_user_id,
                len(msg.content),
            )
            return

        try:
            # Stage 7: when a voice artifact exists AND the cached file
            # is on disk, try sending as a native Discord voice message:
            # convert MP3→OGG Opus, derive a waveform, and POST the
            # multipart form directly so we can include the
            # ``duration_secs`` / ``waveform`` attachment metadata
            # discord.py's public ``send`` does not expose. Fall back to
            # a regular MP3 attachment if any step fails.
            if (
                msg.voice_result is not None
                and msg.voice_result.cache_path.exists()
            ):
                import discord as _discord

                voice_sent = False
                ogg_path = await _convert_to_ogg_opus(
                    msg.voice_result.cache_path
                )
                if ogg_path is not None:
                    try:
                        waveform = await _compute_waveform_b64(
                            ogg_path,
                            msg.voice_result.duration_seconds,
                        )
                        await _send_native_voice_message(
                            target_dm,
                            ogg_path=ogg_path,
                            duration_seconds=msg.voice_result.duration_seconds,
                            waveform_b64=waveform,
                        )
                        voice_sent = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "discord native voice send failed (%s: %s); "
                            "falling back to mp3 attachment",
                            type(exc).__name__,
                            exc,
                        )

                if voice_sent:
                    pass
                else:
                    fallback_file = _discord.File(
                        fp=str(msg.voice_result.cache_path),
                        filename=(
                            f"reply-{msg.voice_result.duration_seconds:.1f}s.mp3"
                        ),
                    )
                    await target_dm.send(content=msg.content, file=fallback_file)
            else:
                await target_dm.send(msg.content)
        except Exception as exc:  # noqa: BLE001
            # Discord transient errors (5xx, rate limits) would come
            # through here. We do not retry at this layer — runtime's
            # higher-level error handling decides whether to replay
            # the turn.
            log.warning(
                "discord send failed for user_id=%s: %s: %s",
                self._current_user_id,
                type(exc).__name__,
                exc,
            )

    # ---- Runtime callback ------------------------------------------------

    async def on_turn_done(self, turn_id: str) -> None:
        """Clear ``in_flight_turn_id`` and promote ``_next_turn``.

        Review M1 iron rule: when ``_next_turn`` is non-empty, it is
        moved into ``_current_turn`` and scheduled through the
        **normal** debounce timer. Messages are NOT flushed
        immediately, so the user can keep typing and have their
        follow-up merged into the same burst.

        Idempotent and must not raise — runtime may call this twice
        for the same turn on recovery paths.
        """
        if turn_id != self.in_flight_turn_id:
            log.warning(
                "discord channel on_turn_done called with turn_id=%r "
                "but in_flight_turn_id=%r; clearing state defensively",
                turn_id,
                self.in_flight_turn_id,
            )

        self.in_flight_turn_id = None
        self._current_user_id = None

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
    "DiscordChannel",
    "MAX_MESSAGES_PER_TURN",
    "MAX_CHARS_PER_TURN",
]

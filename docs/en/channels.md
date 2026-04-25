# Channels

## Overview

> 🧭 **Naming note.** EchoVessel's **channel** (the term used throughout the code and this doc) is what you might call a **stateful message gateway** in API-infrastructure terms. Each channel translates between one external transport's protocol (Discord WebSocket, HTTP POST + SSE for the Web UI, future iMessage webhook, etc.) and the daemon's internal `IncomingTurn` / `OutgoingMessage` format, while carrying the connection-level state that translation needs (debounce timers, in-flight turn tracking, per-user DM routing). "Channel" is the chat-UX-flavoured name for the same concept — we kept it because that's what end users see in Discord / Slack / iMessage. If "gateway" is a clearer mental model for you, substitute freely; the code will keep saying "channel."

**Channels are dumb I/O adapters.** A channel owns a single external transport — the Web UI, Discord, iMessage, WeChat — and its only job is to shuttle text between that transport and the rest of the daemon. Channels do not call the LLM, do not read memory, do not decide what the persona says, do not hold persona state. Every "thought" happens one layer up, in `runtime` + `memory` + the prompt assembler. If a channel implementation is tempted to cache a core block or run a retrieval query, the design is wrong.

**One persona, many mouths.** The architectural commitment at the root of the channel system is that a persona is a single continuous identity across every transport it speaks on. When the same user talks to the persona through the Web UI in the morning and through Discord in the evening, the persona remembers the morning conversation word-for-word — not because each channel ships its own memory, but because there is exactly one memory store and it is *never sharded by transport*. Memory retrieval takes no `channel_id` filter and will never accept one. This is the most load-bearing rule in the whole module: if you break it, the illusion of a single persona collapses, and every channel becomes a separate bot.

**The debounce problem belongs here.** Real humans type in bursts: three lines in four seconds, pause, one more line. If the channel forwarded each line to the runtime as a separate turn, the persona would interrupt the user mid-thought. The fix is to group bursts into a single `IncomingTurn` *at the channel layer*, not at the runtime layer — the channel is the only component that knows the transport's native timing (Discord's typing indicator, iMessage's read-receipt cadence, the Web UI's input events) and is the only component with a stable per-user timer. Runtime stays simple: it consumes one turn at a time. All the "wait and see if the user is still talking" logic lives inside each channel, behind a small state machine documented in §Architecture.

---

## Core Concepts

**`Channel` Protocol** — the Python `Protocol` every transport implements. Defines the small set of methods runtime calls: `start()`, `stop()`, `incoming()`, `send()`, `on_turn_done()`, plus the `channel_id` identity property. A class is a channel if and only if it satisfies this shape; there is no base class, no plugin registry, no decorator. The authoritative definition lives in `src/echovessel/channels/base.py`.

**`IncomingTurn`** — a debounced burst of one or more `IncomingMessage` objects that share a single `turn_id` and represent what the user said in one breath. The channel emits `IncomingTurn`s from its `incoming()` async iterator; runtime treats each yielded turn as exactly one LLM invocation unit. Even a single-line message becomes an `IncomingTurn` of length one — there is no second code path for the degenerate case.

**`IncomingMessage`** — a single raw user message inside a turn. Carries `channel_id`, `user_id`, `content`, `received_at`, an optional channel-native `external_ref`, and a back-pointer `turn_id` to the enclosing `IncomingTurn`. Memory persists these one-by-one as the leaf units of the L2 recall log.

**`OutgoingMessage`** — what runtime hands to `channel.send()`. Contains only what a dumb I/O adapter needs: `content`, an optional `in_reply_to_turn_id`, a `kind` discriminating normal `"reply"` from autonomous `"proactive"` pushes, and a `delivery` field (`"text"` or `"voice_neutral"` in the current codebase) that tells the channel how to physically deliver the message. Persona state, episodic state, retrieval results are not in here — they have already been consumed to produce `content`.

**`in_flight_turn_id`** — channel-side state. Holds the `turn_id` of the turn that runtime is currently processing, or `None` if runtime is idle. This one field is the entire coordination protocol between the channel's debounce machine and the runtime's turn loop. When it is `None`, new user input goes to the current turn and runs the debounce timer. When it is set, new user input goes to the next-turn buffer and waits. Runtime clears it by calling `on_turn_done(turn_id)` from the other end.

**`current_turn` / `next_turn`** — the two buffers that make up the debounce state machine. `current_turn` is the burst the channel is still accumulating for the *next* flush; `next_turn` is the buffer for messages that arrived while runtime was still busy on the previous turn. The two-buffer scheme is what prevents user messages from being dropped during an LLM call while still keeping the "one turn at a time" guarantee that runtime relies on.

**`on_turn_done(turn_id)`** — the runtime-to-channel callback fired after the LLM finishes processing a turn. Signals to the channel that its `in_flight_turn_id` is clear and it may promote the `next_turn` buffer. The single most important rule about this callback: if `next_turn` is non-empty, the channel does **not** flush it immediately — it moves it into `current_turn` and *starts a normal debounce timer*. See §Architecture for why.

---

## Architecture

### Where channels sit in the stack

EchoVessel is five layered modules with strict import direction. Channels live in the third layer, above memory/voice/core and below runtime:

```
   ┌─────────────────────────────────────────────┐
   │                   runtime                   │   Layer 4
   │   daemon, turn dispatch, LLM, observers     │
   └─────────────────┬───────────────────────────┘
                     │ imports
          ┌──────────┴──────────┐
          ▼                     ▼
   ┌────────────┐        ┌──────────────┐        Layer 3
   │  channels  │        │  proactive   │
   │  I/O, debounce      │  policy, trigger
   └─────┬──────┘        └──────┬───────┘
         │ imports              │ imports
         └──────────┬───────────┘
                    ▼
          ┌──────────────────┐
          │      memory      │                   Layer 2
          │   L1 L2 L3 L4    │
          └────────┬─────────┘
                   │
          ┌────────┴─────────┐
          │       voice      │                   Layer 2
          │   TTS / STT      │
          └────────┬─────────┘
                   ▼
          ┌──────────────────┐
          │       core       │                   Layer 1
          │  types, enums    │
          └──────────────────┘
```

A channel may import from `echovessel.core` (types), `echovessel.memory` (read-only query API for history display, plus `ingest_message` via runtime), and `echovessel.voice` (to materialise voice replies). A channel may **not** import from `echovessel.runtime` — the dependency goes the other way, runtime imports channels. A channel may also not import another channel: the web channel does not import the Discord channel, every channel stands alone behind the shared Protocol. The layering is enforced in CI by `import-linter`.

### Two ironrules the channel layer exists to enforce

Two rules sit at the root of the whole design. Both are phrased in terms of what channels are forbidden from doing, because the channel layer is the boundary where the temptation is strongest.

**Memory retrieval never accepts a channel filter.** The memory module's `retrieve()` function, its core-block loader, and its recall-message query all take persona and user, and never take a transport identifier. There is no `retrieve(..., channel_id="web")` overload and there never will be. If you are writing a channel and you find yourself wanting to "just show this channel's history to the persona", stop: the persona has one history, and the whole point of the architecture is that it does not know which transport a memory came from. The only place a `channel_id` ever reaches is the L2 recall-message row, where it lives as a `via-` tag for UI rendering — not for retrieval.

**LLM prompts never contain transport-identity tokens.** The system prompt, the user turn, the retrieved context blocks — none of them contain the strings `"web"`, `"discord"`, `"imessage"`, `"wechat"`, or any other transport name. The LLM has no concept of where a message came from. The runtime's prompt assembler has a hard-coded Style section that tells the model to reference topics and feelings, never the medium, even if the user jokes about it. Channels are forbidden from stashing any "channel context" field in the envelope passed upward, because such a field would eventually leak into a prompt.

Together these two rules are the reason the channel layer exists as a separate module at all. Without them, channels would just be thin wrappers that each call the LLM directly, and the persona would fragment into per-transport clones.

### The debounce state machine

A channel has four states, driven by three events: a new user message, a debounce timer expiring, and `on_turn_done` firing from runtime.

```
          ┌──────────────────────────────────────┐
          │                                      │
          │            ┌──────────┐              │
          │            │   idle   │◀─────────────┘
          │            └────┬─────┘   on_turn_done
          │                 │          (next_turn empty)
          │  new msg,       │
          │  in_flight=None │
          │                 ▼
          │         ┌──────────────┐
          │         │  collecting  │◀──┐
          │         │ (timer ticks)│   │ new msg (reset timer)
          │         └──────┬───────┘   │
          │                │           │
          │     timer      │           │
          │     fires      │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │   in_flight  │   │
          │         │ turn dispatched  │
          │         └──────┬───────┘   │
          │                │           │
          │  new msg       │           │
          │  (during LLM)  │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │ queued_next  │   │
          │         │   buffered   │   │
          │         └──────┬───────┘   │
          │                │           │
          │  on_turn_done  │           │
          │  (promote and  │           │
          │   start timer) │           │
          │                ▼           │
          │         ┌──────────────┐   │
          │         │  collecting  │───┘
          │         └──────────────┘
          │
          └──── stop() ─────────────────▶  stopped
```

Written out as rules:

1. **On new user message.**
   - If `in_flight_turn_id is None` — runtime is idle. Append the message to `current_turn`, cancel any running debounce timer, and start a fresh one (default 2000 ms, per channel, read from config).
   - If `in_flight_turn_id is not None` — runtime is busy. Append the message to `next_turn`. **Do not start a timer for `next_turn`.** The timer will be started later, when `on_turn_done` fires.

2. **On debounce timer expiry.** Mint a fresh `turn_id`, wrap the accumulated `current_turn` as an `IncomingTurn`, push it onto the outbound queue that `incoming()` yields from, set `in_flight_turn_id = <new turn_id>`, and clear the `current_turn` buffer. The channel does not call memory or the LLM here; runtime does that downstream when it pulls the turn from `incoming()`.

3. **On `on_turn_done(turn_id)` from runtime.** Clear `in_flight_turn_id = None`. Then:
   - If `next_turn` is empty, do nothing. The channel is now idle; the next user message re-enters rule 1.
   - If `next_turn` is non-empty, promote it: move its contents into `current_turn`, clear `next_turn`, and **start a normal debounce timer**. The rest of rule 1 resumes: if the user keeps typing, the timer resets; if they stop, the timer fires and rule 2 runs.

The single most easy-to-get-wrong detail is this: when `on_turn_done` promotes `next_turn` to `current_turn`, the channel must **not** flush immediately. It must start a normal debounce window. The reason is that a user who fired three messages in rapid succession during the previous LLM call is very likely still typing. Flushing the promoted buffer on the spot would feel like "she interrupted me again". The worst-case tail latency from running a full debounce window is one debounce interval — user-paced delay — and that is acceptable. Implementations must preserve this behaviour.

Two hard caps break the debounce early: `MAX_MESSAGES_PER_TURN = 50` and `MAX_CHARS_PER_TURN = 20000`. If either is reached the channel flushes immediately, regardless of the timer. These exist to protect the LLM context window and to prevent a runaway producer from ballooning a single turn.

Debounce state is mutated from exactly one coroutine (the channel's internal ingest loop). The `on_turn_done` callback coming from runtime runs in a different coroutine, so the channel implementation is responsible for marshalling it onto the ingest loop — typically by putting an event on an internal `asyncio.Queue` and letting the ingest loop consume it. The Protocol does not mandate the mechanism, only the serial-mutation invariant.

### Data flow end-to-end

The end-to-end flow for one turn, from a user typing in the Web UI to the persona replying:

```
1. User types in the Web UI
        │
        ▼
2. channel.incoming_raw_message(...)  (channel-internal)
        │   append to current_turn
        │   reset debounce timer
        ▼
3. debounce timer expires
        │   mint turn_id
        │   push IncomingTurn onto internal queue
        │   set in_flight_turn_id
        ▼
4. channel.incoming() yields IncomingTurn
        │
        ▼
5. TurnDispatcher puts turn on its serial queue
        │
        ▼
6. runtime._handle_turn(turn)
        │   calls assemble_turn(ctx, turn, llm_provider)
        │     - ingest_message(...) for each leaf (writes L2)
        │     - retrieve(...)    (NO channel_id argument)
        │     - assemble prompt  (NO transport name in text)
        │     - llm.complete(...)  (streaming tokens)
        │     - ingest_message(role=ASSISTANT, ...) for reply
        ▼
7. runtime calls channel.send(OutgoingMessage(
        content=reply_text,
        in_reply_to_turn_id=turn.turn_id,
        kind="reply",
        delivery="text" | "voice_neutral",
     ))
        │
        ▼
8. runtime calls channel.on_turn_done(turn.turn_id)
        │   channel clears in_flight_turn_id
        │   if next_turn is non-empty:
        │       promote to current_turn and start debounce timer
        ▼
9. loop back to step 1
```

Runtime's `ChannelRegistry` owns the lifecycle (`start_all` / `stop_all`) and merges every registered channel's `incoming()` into one async stream via `registry.all_incoming()`. The `TurnDispatcher` reads from that merged stream and feeds a single serial handler — so even with four channels live, there is still only one turn being processed at a time. This is intentional: it is the guarantee that lets memory writes and LLM calls assume no concurrent mutation.

### The Web channel

The Web channel is fully wired end-to-end. A FastAPI `WebChannel` under `src/echovessel/channels/web/` exposes `POST /api/chat/send`, streams `chat.message.*` events over SSE via `GET /api/chat/events`, implements the debounce state machine described above, and talks to runtime through the Channel Protocol. The React + Vite + TypeScript frontend under `src/echovessel/channels/web/frontend/` is built via `npm run build` into `channels/web/static/` and served by the same daemon at `http://127.0.0.1:7777/` — end users never touch Node.js. Contributors who want to hack on the UI can run `npm run dev` against a live daemon (the dev server proxies to the FastAPI process).

#### Memory Timeline panel

The chat page exposes a right-hand **Memory Timeline** sidebar — open by default, collapses to a 40 px vertical tab, and turns into an overlay drawer below 1200 px. It is the SSE-driven real-time view of the persona's growing memory: every new L3 event / L4 thought / L5 entity / L6 mood / session-close prepends a row, reverse-chronologically, with `icon + relative time ("just now" / "30 min ago") + one-line summary`, and an expand affordance for details. The `…` menu offers per-kind filters (events only, thoughts only, etc.) and a "clear local cache" action that re-runs backfill.

Two backend paths feed it. Cold start: `GET /api/admin/memory/timeline?since=<iso>&limit=50` returns a paginated, reverse-chronological merge of L3 / L4 / L5 / L6 / session-close items. Uncertain entities are filtered server-side; entity-description edits whose `updated_at > created_at` show up as their own "description" rows on backfill so historical owner overrides surface. Warm path: subscribe to six `memory.*` / `chat.*` SSE topics (catalog below) and prepend on hit, with the client-side store deduping, sorting, and capping at 200 rows.

```
useMemoryTimeline()          src/echovessel/channels/web/frontend/src/screens/Chat/timeline/
  ├─ initial backfill       GET /api/admin/memory/timeline
  └─ live subscription      6 SSE topics (see catalog below)
```

The active filter set and the panel-collapsed flag persist in `localStorage` (`echovessel.timeline.filters` / `echovessel.timeline.collapsed`) so a new tab continues with the same view.

#### SSE topic catalog (everything the Web channel subscribes to)

The Web channel is currently the only channel that exposes `push_sse`, so it consumes both its own `chat.message.*` turn stream and the memory / cross-channel topics broadcast by the runtime memory observer. Full topic table:

| Topic | Producer | Key payload fields | Consumer |
|---|---|---|---|
| `chat.message.user_appended` | web channel's own turn pipeline | `turn_id` · `messages` | main bubble column |
| `chat.message.typing_started` | web channel at LLM-stream start | `turn_id` | main bubble column ("Typing…" placeholder) |
| `chat.message.done` | runtime broadcaster mirroring any channel's turn completion | `turn_id` · `source_channel_id` · `content` | main bubble column (cross-channel mirror) |
| `chat.message.voice_ready` | voice service after async synthesis | `turn_id` · `audio_url` | bubble's playback control |
| `chat.session.boundary` | runtime memory observer | `closed_session_id` · `new_session_id` · `events_count` · `thoughts_count` | timeline session-close summary row |
| `chat.mood.update` | runtime memory observer | `persona_id` · `user_id` · `mood_summary` | timeline mood-shift row |
| `chat.settings.updated` | runtime voice / admin runtime settings change | `voice_enabled`, etc. | settings UI |
| `memory.event.created` | runtime memory observer | `event_id` · `description` · `emotional_impact` · `session_id` | timeline event row |
| `memory.thought.created` | runtime memory observer | `thought_id` · `type` · `subject` · `description` · `source` · `filling_event_ids` | timeline thought / intention / expectation row |
| `memory.entity.confirmed` | runtime memory observer | `entity_id` · `canonical_name` · `kind` · `merge_status` | timeline entity row (uncertain entities pre-filtered by the observer) |
| `memory.entity.description_updated` | runtime memory observer | `entity_id` · `canonical_name` · `description` · `source` | timeline entity-description row |

The four `memory.*` topics are driven by `RuntimeMemoryObserver`; see `runtime.md § Cross-Channel SSE topic catalog` for the producer side. `chat.session.boundary` is shared between the close and open hooks — the unused side is null; only the close edge carries `events_count` and `thoughts_count`. Discord and iMessage do not expose `push_sse`, so the observer simply skips them — they still feed their own turn stream into runtime, and cross-channel real-time mirroring is what the broadcaster handles for them on the Web side.

### Cross-channel unified timeline (runtime-owned SSE broadcaster)

EchoVessel's architectural promise is "one persona across every channel." The memory layer guarantees this at the storage level (iron rule D4: retrieval never filters by `channel_id`), and the live view is unified the same way:

- `SSEBroadcaster` is owned by the runtime, not by any single channel.
- Every channel's turn events (`chat.message.user_appended` / `chat.message.typing_started` / `chat.message.done` / `chat.message.voice_ready`) are mirrored through the broadcaster with a `source_channel_id` field identifying where the turn came from. `chat.message.typing_started` fires once per turn right before the LLM stream begins — the browser renders it as a "Typing…" bubble until `chat.message.done` arrives with the full reply.
- The same SSE stream also carries the memory write events from `RuntimeMemoryObserver` (`memory.event.created` / `memory.thought.created` / `memory.entity.confirmed` / `memory.entity.description_updated`) and the cross-channel-shared `chat.session.boundary` / `chat.mood.update`. The Web chat's Memory Timeline panel is the consumer; this is why memory produced by a Discord DM appears in the Web tab's timeline in real time, on the same SSE that mirrors the message into the main bubble column. Full topic table in [SSE topic catalog](#sse-topic-catalog-everything-the-web-channel-subscribes-to) above.
- The Web chat timeline subscribes to the shared SSE stream, so Discord DMs appear inline in real time with a `📱 Discord` pill in the timestamp. iMessage and future channels inherit the behaviour — **no frontend change needed** when a new channel is added, as long as the channel implements the Channel Protocol and turns flow through `runtime.assemble_turn()`.
- A paired `GET /api/chat/history?limit=50&before=<turn_id>` endpoint returns cross-channel history (still D4-unfiltered) so a fresh browser session backfills past Discord conversations alongside past Web conversations. Memory Timeline uses a different backfill: `GET /api/admin/memory/timeline?since=<iso>&limit=50` returns a cross-kind merge of L3 / L4 / L5 / L6 memory events — also D4-unfiltered.
- Turn serialisation still holds: the `TurnDispatcher` processes one turn at a time across the entire process, so the "one persona one brain" invariant is unchanged. Parallel Web + Discord requests simply queue.

The publish path is failure-isolated: if the broadcaster raises, a `log.warning` fires and the originating channel's `send()` still completes. Likewise, if `send()` raises, `chat.message.done` is **not** mirrored — a failed Discord DM will not tell the Web tab the turn succeeded.

---

## How to Extend

### 1. Write a minimal channel adapter

A channel is any class that satisfies the `Channel` Protocol. There is no base class to inherit and no plugin registry to call. The skeleton looks like this:

```python
# src/echovessel/channels/myxform/channel.py
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from echovessel.runtime.turn.coordinator import IncomingMessage, IncomingTurn


class MyTransformChannel:
    # --- Identity -------------------------------------------------------
    channel_id: str = "myxform"
    display_name: str = "My Transform"

    def __init__(self, *, config: dict) -> None:
        self._config = config
        self._outbox: asyncio.Queue[IncomingTurn] = asyncio.Queue()
        self._stopped = asyncio.Event()
        # Debounce state — see example 2.
        self._current_turn: list[IncomingMessage] = []
        self._next_turn: list[IncomingMessage] = []
        self._timer: asyncio.Task | None = None
        self._in_flight_turn_id: str | None = None
        self._debounce_ms: int = config.get("turn_debounce_ms", 2000)

    # --- Lifecycle ------------------------------------------------------
    async def start(self) -> None:
        # Open sockets, bind ports, connect to the external service.
        # Idempotent: calling start() twice is a no-op.
        pass

    async def stop(self) -> None:
        # Graceful shutdown: flush outgoing buffers, close sockets.
        # After stop() returns, incoming() must exhaust on its next pull.
        self._stopped.set()
        if self._timer is not None:
            self._timer.cancel()
        await self._outbox.put(None)  # sentinel

    # --- Inbound --------------------------------------------------------
    async def incoming(self) -> AsyncIterator[IncomingTurn]:
        while not self._stopped.is_set():
            item = await self._outbox.get()
            if item is None:
                return
            yield item

    async def on_turn_done(self, turn_id: str) -> None:
        # Runtime finished the turn. Idempotent; never raises.
        self._in_flight_turn_id = None
        if self._next_turn:
            self._current_turn = self._next_turn
            self._next_turn = []
            self._start_debounce_timer()

    # --- Outbound -------------------------------------------------------
    async def send(self, message) -> None:
        # Deliver the persona reply. message.delivery tells you whether
        # to push text or to call VoiceService first. See example 3 for
        # the voice path.
        if message.delivery == "text":
            await self._push_text(message.content)
        elif message.delivery == "voice_neutral":
            await self._push_voice(message.content)

    # --- User id mapping -----------------------------------------------
    def map_external_user(self, external_id: str) -> str:
        # MVP: single-user contract. Every channel returns "self" until
        # multi-user support is wired through runtime/config.
        return "self"
```

For more fleshed-out examples, look at the channel stubs under `tests/` — they exercise the Protocol end-to-end with a fake transport and are the easiest thing to copy-paste from when starting a new adapter. The merge point that runtime uses is in `src/echovessel/runtime/channel_registry.py`; as long as your class exposes `channel_id`, `start`, `stop`, `incoming`, `send`, and (optionally) `on_turn_done`, the registry will accept it.

### 2. Implement the debounce state machine

The two buffers, the single timer, the `in_flight_turn_id` flag, and the promote-and-debounce rule on `on_turn_done`. The core lives in two methods of the channel class:

```python
import uuid


class MyTransformChannel:
    # ... lifecycle methods omitted ...

    async def _on_raw_user_message(self, content: str, *, user_id: str) -> None:
        """Called by the channel's transport ingest loop for every raw
        user message the external service hands it. This is the ONLY
        place debounce state is mutated."""

        msg = IncomingMessage(
            channel_id=self.channel_id,
            user_id=user_id,
            content=content,
            received_at=datetime.now(timezone.utc),
        )

        if self._in_flight_turn_id is None:
            # Runtime is idle: append to current_turn and (re)start timer.
            self._current_turn.append(msg)
            self._start_debounce_timer()
        else:
            # Runtime is busy: buffer in next_turn, no timer yet.
            self._next_turn.append(msg)

        # Hard caps: flush immediately if the current burst exceeds
        # either limit. Runs only when the buffer we just grew belongs
        # to current_turn.
        if self._in_flight_turn_id is None:
            if (
                len(self._current_turn) >= 50
                or sum(len(m.content) for m in self._current_turn) >= 20000
            ):
                await self._flush_current_turn()

    def _start_debounce_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.create_task(self._debounce_and_flush())

    async def _debounce_and_flush(self) -> None:
        try:
            await asyncio.sleep(self._debounce_ms / 1000.0)
        except asyncio.CancelledError:
            return
        await self._flush_current_turn()

    async def _flush_current_turn(self) -> None:
        if not self._current_turn:
            return
        turn_id = str(uuid.uuid4())
        # Back-fill turn_id on each leaf message.
        stamped = [
            IncomingMessage(
                channel_id=m.channel_id,
                user_id=m.user_id,
                content=m.content,
                received_at=m.received_at,
                external_ref=m.external_ref,
                turn_id=turn_id,
            )
            for m in self._current_turn
        ]
        turn = IncomingTurn(
            turn_id=turn_id,
            channel_id=self.channel_id,
            user_id=stamped[0].user_id,
            messages=stamped,
            received_at=datetime.now(timezone.utc),
        )
        self._current_turn = []
        self._in_flight_turn_id = turn_id
        await self._outbox.put(turn)
```

Two things to watch for when porting this to a real transport:

- **`on_turn_done` promotes through the normal debounce window, never an instant flush.** The promote path in the lifecycle section above must call `_start_debounce_timer()`. Skipping the timer because "the next buffer is already full" re-introduces the interruption bug that this whole module exists to fix.
- **Serial mutation.** Only one coroutine should touch `_current_turn`, `_next_turn`, `_timer`, and `_in_flight_turn_id`. If your transport callback runs on a different loop or thread, marshal it onto the ingest loop via an `asyncio.Queue` or `loop.call_soon_threadsafe`, and do the actual state update inside the ingest loop.

### 3. Expose a `push_sse` capability

Some transports — the Web channel is the obvious example — naturally speak server-sent events. Runtime's observer layer wants to push lifecycle events (memory writes, consolidation ticks, voice-generation progress) to any channel that can forward them to a live UI, but it does not want to require this capability of every channel. Discord has no SSE; iMessage has no SSE.

The pattern is an optional method, detected by `getattr`:

```python
from typing import Any


class MyTransformChannel:
    # ... Protocol methods omitted ...

    async def push_sse(self, event: str, payload: dict[str, Any]) -> None:
        """Optional capability. Channels that can stream events to a
        live UI implement this; channels that can't simply don't define
        it. Runtime's observer code detects support with:

            push = getattr(channel, "push_sse", None)
            if push is not None:
                await push(event_name, payload)
        """
        # Fan out to every connected SSE subscriber.
        for subscriber in list(self._sse_subscribers):
            try:
                await subscriber.send(event, payload)
            except Exception:
                self._sse_subscribers.discard(subscriber)
```

Runtime's observer wiring (in `src/echovessel/runtime/`) checks for `push_sse` with `getattr` and silently skips channels that don't define it. This keeps the Channel Protocol minimal while still letting a Web channel light up a rich real-time UI. Do not add `push_sse` to the core Protocol — its optionality is the whole point.

---

For the authoritative source of the Channel Protocol, see `src/echovessel/channels/base.py`. For the registry and dispatch plumbing, see `src/echovessel/runtime/channel_registry.py` and `src/echovessel/runtime/turn_dispatcher.py`. The turn pipeline itself — where memory retrieval and LLM assembly enforce the no-transport-leak rules — lives in `src/echovessel/runtime/interaction.py`.

---

## Discord DM channel setup

EchoVessel ships an optional Discord channel so a persona can receive direct messages from allow-listed Discord users. Same persona, same memory, same episodic state — the only difference from Web is the transport. A persona DMed on Discord still remembers conversations that started on the Web UI, and vice versa, because memory retrieval never filters by channel.

Current scope is **DM only**. Guild channels, slash commands, and voice message attachments are not in v1.

### 1. Create a Discord bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Give it whatever name you like — the display name is what users see on DMs.
2. In the sidebar open **Bot**, click **Add Bot**, and accept the warning.
3. Scroll to **Privileged Gateway Intents** and enable **Message Content Intent**. Without this, your bot receives empty DM bodies.
4. Still on the Bot page, click **Reset Token** (or **Copy** if a token is already issued) and save the string — Discord only shows it once.

### 2. Install the optional dependency

The Discord channel is an optional extra so users who only want the Web UI do not need to install `discord.py`:

```bash
uv sync --extra discord
```

### 3. Expose the token via environment variable

EchoVessel never stores the token inside `config.toml`. Set it as an environment variable and reference the variable name from config:

```bash
export ECHOVESSEL_DISCORD_TOKEN='your-bot-token-here'
```

Add the same line to your shell profile (`~/.zshrc`, `~/.bashrc`, or equivalent) so it survives reboots.

### 4. Enable the channel in `config.toml`

Open your `config.toml` (typically at `~/.echovessel/config.toml`) and add:

```toml
[channels.discord]
enabled = true
channel_id = "discord"
token_env = "ECHOVESSEL_DISCORD_TOKEN"
# Optional but recommended: only let specific Discord user IDs DM the bot.
# Leave this out to accept DMs from anyone the bot can reach.
allowed_user_ids = [123456789012345678]
# Debounce window in milliseconds. Matches the Web channel default.
debounce_ms = 2000
```

`allowed_user_ids` takes Discord **user snowflakes** — the 17–19 digit numeric IDs visible when you right-click a user with Developer Mode enabled. Without this allowlist, any Discord user who happens to find your bot can DM it.

### 5. Invite the bot to a shared location

Discord only lets a user DM a bot if they share at least one server with it. Create an OAuth2 invite link in the Developer Portal (Scopes: `bot`, Permissions: just **Read Messages / Send Messages** is enough) and open it to add the bot to a server you already belong to. No channel permissions inside the server are needed — the bot just needs to "be there" so Discord allows the DM.

### 6. Start EchoVessel and send a DM

Run the daemon as usual. On a successful startup you should see a log line like `Discord bot connected as YourBot#1234`. Click the bot in your server's member list and send it a direct message — the persona replies on the same DM thread with the same memory and episodic state it has on the Web UI.

### Troubleshooting

- **"Discord bot rejected DM from non-allowlisted user"** — your user ID is not in `allowed_user_ids`. Copy your Discord user ID (right-click yourself with Developer Mode on → Copy User ID) and add it.
- **DM body arrives empty** — Message Content Intent is off on the Bot page. Re-enable it and restart the daemon.
- **Bot comes online but never responds** — make sure you and the bot share a server. A bot you never invited anywhere cannot receive DMs.
- **"Improper token" at startup** — `ECHOVESSEL_DISCORD_TOKEN` is unset or copied with surrounding whitespace. A healthy token is roughly 70 characters.

## iMessage channel setup

The iMessage channel lets a persona receive direct messages from allow-listed contacts over Apple's Messages.app. Same persona, same memory, same episodic state as Web and Discord — the only difference is the transport.

The channel does **not** implement the iMessage protocol itself. Apple's IDS / APNs servers only accept genuine Apple devices, so every non-Apple project converges on the same pattern: spawn a small macOS-native helper that speaks iMessage on your behalf. EchoVessel uses [`steipete/imsg`](https://github.com/steipete/imsg) — a Swift CLI that reads `~/Library/Messages/chat.db` for inbound and drives Messages.app via AppleScript for outbound, surfaced over JSON-RPC on stdio. Our channel code spawns one long-lived `imsg rpc` subprocess, subscribes to its `message` notifications, and sends replies by calling its `send` method.

### Prerequisites

- **macOS**. The daemon must run on the Mac that is signed into the persona's Apple ID. Linux / Windows hosts cannot participate.
- **Dedicated Apple ID for the persona, strongly recommended.** Use a separate iCloud email (free) so persona traffic is isolated from your personal Messages. A single-account setup works for testing but feels wrong in practice — the persona ends up looking like "you talking to yourself" on every device signed into your main Apple ID.
- **Messages.app signed in** with the persona's Apple ID and running in the background.

### 1. Install `imsg`

```bash
brew install steipete/tap/imsg
imsg rpc --help   # sanity check
```

### 2. Grant macOS permissions

Both are granted to the process context that runs EchoVessel (typically your terminal app). Changes take effect only for newly-launched processes.

- **Full Disk Access** — Messages.app's `chat.db` lives under `~/Library/Messages/` which is protected. Open **System Settings → Privacy & Security → Full Disk Access**, click `+`, and add your terminal app (Terminal.app, iTerm, Warp, ghostty, VS Code — whichever you use to launch the daemon). Fully quit and reopen that terminal afterwards.
- **Automation** — sending requires AppleScript driving Messages.app. Open **System Settings → Privacy & Security → Automation → *your terminal* → Messages**, and toggle it on. (Entry appears only after a first send attempt.)

### 3. Verify the probe

With the venv activated:

```bash
uv run python -m echovessel.channels.imessage.probe
```

Healthy output looks like:

```
✓ imsg RPC OK · 5 chat(s) visible
  · [iMessage] dm +14155551234  (2026-04-19T03:51:12.934Z)
  · [RCS     ] dm +19803698620  (2026-04-16T23:11:24.466Z)
  ...
```

If the probe fails the error message tells you which step broke (binary missing, FDA denied, imsg crashed, etc.). Every failure includes an actionable hint.

### 4. Configure EchoVessel

In your `config.toml`, set:

```toml
[channels.imessage]
enabled = true
persona_apple_id = "anya-persona@icloud.com"     # required
# allowed_handles = ["+14155551234", "friend@example.com"]
# default_service = "auto"
# region = "US"
# debounce_ms = 2000
```

`persona_apple_id` is the destination filter — inbound messages addressed to any other Apple ID registered on the same Mac are dropped before they reach the LLM. This is the core isolation mechanism when a single Messages.app serves both the persona and the user's main account.

`allowed_handles` is an optional peer allowlist. Empty means "accept every 1:1 DM addressed to `persona_apple_id`". Entries accept phone numbers (E.164 like `+14155551234`, or bare digits combined with `region`) and email addresses.

You can also edit these fields from the Web UI without touching `config.toml`: open **Admin → Channels → iMessage**, toggle the channel on, and edit `persona_apple_id` / `allowed_handles` inline. Saves call `PATCH /api/admin/channels`, write the merged TOML to disk, and surface a "restart required" banner — the iMessage subprocess is bound at startup, so live edits do not take effect until you restart the daemon.

### 5. Start the daemon

Launch `echovessel run` as usual. A successful startup logs:

```
imessage channel: registered (persona=anya-persona@icloud.com, allowlist=open, debounce_ms=2000)
imessage: channel started · persona=anya-persona@icloud.com allowlist=0
```

Send an iMessage from a phone or another Mac to `persona_apple_id` — after one debounce window the persona replies in the same thread.

### Voice replies

When the persona has voice enabled (Admin → Voice → toggle on, with a configured `voice_id`), iMessage replies arrive as an MP3 attachment alongside the text. The runtime synthesises the reply with `VoiceService.generate_voice()`, hands the resulting `VoiceResult` to `channel.send()`, and the iMessage channel forwards `voice_result.cache_path` to `imsg send --file`. If voice synthesis fails or the cache file is missing, the channel still delivers the text — voice is best-effort, never blocking. There is no separate iMessage voice toggle; the `persona.voice_enabled` switch governs every channel uniformly.

### Known limitations (MVP)

- **1:1 DMs only.** Group chats are dropped at the inbound pipeline. Group support would need mention detection and chat-id routing; deferred.
- **Text in, text + optional voice MP3 out.** Inbound attachments, tapbacks, read receipts, edits, and unsends are all dropped (AppleScript does not expose most of them cleanly). Outbound is text by default; voice replies attach an MP3 file but are not transcribed iMessage audio messages — they show up as a regular file attachment.
- **No backfill.** `imsg watch.subscribe` streams from "now" — messages that arrived while the daemon was off are **not** replayed into memory. The iMessage server-side still delivers them to Messages.app normally, they just don't propagate into the persona's memory unless the daemon was running when they arrived.
- **Send can occasionally double-deliver** under heavy load (AppleScript race). The echo cache absorbs the inbound duplicate; the outbound duplicate is a known wrinkle of the platform.

### Troubleshooting

- **"Full Disk Access is missing" on probe** — you added the wrong terminal app, or didn't quit the terminal after adding it. FDA is per-process-context and inherited at fork time; existing terminal processes keep the old (denied) permission forever.
- **`imsg` not found** — the `brew install` didn't land on this user's `PATH`. Verify with `which imsg`, and if the binary is elsewhere, set `cli_path = "/absolute/path/to/imsg"` in the config.
- **Inbound messages are ignored silently** — check the log for `imessage: drop reason=…`. The reason string tells you whether it's the destination filter, allowlist, echo cache, or rate limiter firing.
- **Persona sends look like they're from you** — you forgot to log the persona Apple ID into Messages.app, so AppleScript is sending from your main account. Verify the `from` in the sent thread on the recipient's side.
- **Two bots talking in circles** — the rate limiter will suppress after 5 drops in 60s. If the storm persists, check whether both sides have an AI answering; this is unsolvable from one end.

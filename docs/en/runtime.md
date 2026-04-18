# Runtime

> The daemon. One Python process that boots memory, voice, proactive, and every channel, drives the turn loop, streams LLM tokens, and stays up until you stop it.

Runtime is the only layer in EchoVessel that is allowed to import from every other module. Memory, voice, proactive, and channels each live in their own box and cannot see each other. Runtime is the glue above them: it builds the objects, wires the callables, owns the event loop, and serves as the single process where everything executes.

This page is written for a developer who wants to understand the daemon from zero — what `echovessel run` actually does, how a single turn flows end to end, and where to plug in a new LLM provider, startup step, or signal handler.

---

## Overview

`echovessel run` starts one long-lived asyncio process and blocks your shell. That process is the entire daemon — there are no workers, no forks, no companion services. Inside the process lives one event loop; on top of the loop lives a `Runtime` instance that holds references to the memory engine, the LLM provider, the channel registry, the voice service, the proactive scheduler, and a handful of background tasks. Ctrl+C (or `echovessel stop`) flips a shutdown event and the loop unwinds.

The launcher exposes four subcommands: `echovessel run` boots the daemon in the foreground, `echovessel stop` asks the daemon to exit cleanly, `echovessel reload` applies config changes without a restart, and `echovessel status` reports whether a daemon is alive and whether its control plane is reachable. `stop` and `reload` speak HTTP to the daemon's local control plane (see **Control plane** below) and fall back to signals (`SIGTERM` / `SIGHUP`) only when the control plane is unreachable or the pidfile is from an older install. All four commands read the same config file (`~/.echovessel/config.toml` by default); the pidfile lives under the configured `data_dir` so multiple daemons with different configs never fight over it.

Why one process for everything? Because EchoVessel stores everything in a single SQLite database with sqlite-vec, and SQLite has exactly one writer at a time. A single asyncio loop gives us trivial serialization — consolidate, the turn handler, the idle scanner, the proactive scheduler all share the same loop and cannot race with each other. A second process would force us to manage write locks, crash-recovery coordination between workers, and cross-process LLM client pools. None of that is worth it for a local-first persona daemon that mostly sits idle waiting for one human to type. The layering rule that comes out of this is equally simple: runtime imports channels, proactive, memory, voice, and core, and none of those modules ever imports anything from runtime. An import-linter contract enforces it in CI.

---

## Core Concepts

**Runtime** — the top-level daemon object (`src/echovessel/runtime/app.py::Runtime`). It has a classmethod `Runtime.build(config_path)` that loads config, opens the database, migrates schema, seeds persona rows, builds the LLM provider, constructs the voice service and a `RuntimeContext`, and returns an unstarted instance. `await rt.start()` then launches the background tasks and returns; `await rt.wait_until_shutdown()` blocks on the shutdown event; `await rt.stop()` tears everything down in reverse order.

**RuntimeContext** — the dataclass that holds the shared state all tasks read from (`Runtime.ctx`). It carries the parsed config, the `config_path` (so hot-reload knows where to re-read from), the resolved `data_dir` and `db_path`, the open SQLModel engine, the `SQLiteBackend`, the `embed_fn` callable, the `LLMProvider`, the `ChannelRegistry`, the `shutdown_event`, and a `RuntimePersonaContext` for fields that can change at runtime (`voice_enabled`). Every task in the daemon reads from one `RuntimeContext`; there is no global state.

**Turn loop** — the serial pipeline that transforms a channel's incoming burst into a persona reply. A channel's `incoming()` yields an `IncomingTurn`; the turn dispatcher pushes the turn into a single-consumer queue; the handler pulls one turn at a time and calls `assemble_turn(turn, llm, on_token, on_turn_done)`; that function writes each user message to memory, runs retrieval, assembles the prompt, streams the LLM response, writes the reply to memory, and hands the reply back so the handler can call `channel.send(...)`. One handler task, one queue, one turn at a time.

**Streaming token callback** — `on_token(message_id, delta)`. The channel passes this callable into `assemble_turn`; for every text delta the LLM emits, the function is invoked so the channel can push the delta onto whatever transport it owns (for the web channel that is an SSE frame). The callback receives **text deltas only** — never structured JSON, never tone hints, never delivery metadata. If a delta fails to push (client disconnected, socket closed), the failure is logged and the stream continues; the reply will still land in memory and be visible the next time the client reconnects.

**on_turn_done callback** — `on_turn_done(turn_id)`. The channel's way of knowing "runtime is done with this turn, you can clear your in-flight state and consider whether to flush the next debounced turn". Runtime always calls this exactly once per turn, from a `finally` block inside `assemble_turn`, regardless of whether the turn succeeded, failed on LLM error, or failed on memory ingest. Exceptions raised by the callback are swallowed — channels are expected to be no-throw here and a misbehaving channel must not corrupt runtime's turn pipeline.

**LLM tier** — a semantic label declared at the call site. Runtime holds exactly one `LLMProvider` instance, and every call passes a `tier=LLMTier.SMALL | MEDIUM | LARGE` argument. The provider maps the tier to a concrete model name internally. The mapping resolves in this priority order: if config pins a single `llm.model`, every tier returns that one model ("one model for everything"); otherwise if config sets `[llm.tier_models]`, the per-tier values are used; otherwise the provider falls back to its built-in defaults (Anthropic: Haiku / Sonnet / Opus; OpenAI official: `gpt-4o-mini` / `gpt-4o`). EchoVessel's call-site tiering is fixed: extraction uses SMALL, reflection uses SMALL, a future judge uses MEDIUM, and interaction and proactive always use LARGE because the user is staring at the screen.

**Local-first disclosure** — the single line printed at the end of startup that enumerates exactly which outbound endpoint the daemon will talk to. It includes the data directory, the resolved database path, the persona id, the LLM provider name, the model resolved for the LARGE tier, the base URL the provider will hit (e.g. `https://api.anthropic.com` or your local Ollama URL), the list of enabled channels, and the embedder name. A second line repeats the outbound URL in plain language. Any auditor running `tail -f logs/runtime-*.log | head -2` immediately sees where traffic goes.

**SIGHUP reload** — sending `SIGHUP` to the daemon (or running `echovessel reload`) causes runtime to re-read `config.toml`, validate it, rebuild the LLM provider if the `[llm]` section changed, and atomically swap `ctx.llm` for the new instance. In-flight turns keep running against the old provider because the turn handler captures a local `llm = self.ctx.llm` reference at the start of each turn — Python reference semantics give us zero-cost liveness without any lock or versioning. Structural sections (`[memory]`, `[channels.*]`, `[persona].id`) cannot be reloaded; changing them requires a full `echovessel stop && echovessel run`.

---

## Architecture

### Startup sequence

`Runtime.build(config_path)` followed by `await rt.start()` executes these steps in this order. Every step has a defined failure mode; unless noted as fatal, a failure logs a warning and the daemon continues with the affected subsystem disabled.

Load and validate config. `load_config(path)` parses the TOML file with `tomllib` and runs the Pydantic v2 schema in `runtime/config.py`. A missing file, a malformed section, or a missing environment variable for `api_key_env` exits the daemon before any I/O. Secrets never appear in the TOML — only the name of the environment variable that holds them.

Create the data directory and subdirectories. `data_dir` (default `~/.echovessel`) is created if missing, along with `logs/` and `embedder.cache/`. The data directory is never the site-packages install location; a pip upgrade must not wipe the user's persona.

Open the SQLite engine. `create_engine(db_path)` opens the database with WAL mode and loads the `sqlite-vec` extension. Failure here is fatal.

Run idempotent schema migration. `ensure_schema_up_to_date(engine)` inspects the current schema and runs `ADD COLUMN` / `CREATE TABLE IF NOT EXISTS` for any missing pieces. On a fresh database this is a no-op; on a legacy database it brings it forward to the current shape. Migration failure is fatal — a half-migrated database would explode at insert time, and failing at boot is strictly better.

Create any remaining tables. `create_all_tables(engine)` runs the SQLModel metadata create. Safe to call on an up-to-date database.

Seed persona and user rows. The daemon ensures a `Persona` row with `config.persona.id` and a `User` row with id `self` exist. MVP is single-persona, single-user — both rows are write-once at first boot.

Catch up stale sessions. `catch_up_stale_sessions(db, now=...)` scans `sessions` for rows whose `status='open'` but whose `last_message_at` is older than the idle threshold, marks them `closing`, and commits. This happens before the consolidate worker starts so the initial queue sees every orphan from the last crash.

Build the LLM provider. `build_llm_provider(config.llm)` dispatches on `config.llm.provider` and instantiates one of `AnthropicProvider`, `OpenAICompatibleProvider`, or `StubProvider`. Construction never calls the network — it only caches the API key and builds the tier-to-model map. The provider is attached to `ctx.llm` as the single shared instance.

Build the voice service if `[voice].enabled`. When voice is enabled, `build_voice_service(VoiceServiceConfig(...))` constructs a `VoiceService` and attaches it to `ctx.voice_service`. Voice failures are non-fatal — if the TTS provider is unreachable, the daemon logs and boots with `voice_service = None`, and channels / proactive gracefully downgrade to text.

Build the proactive scheduler if `[proactive].enabled`. `_build_proactive_scheduler` assembles a `MemoryFacade`, a `ProactiveChannelRegistry` adapter, a proactive prompt callable, and a `PersonaView` that reads `voice_enabled` live on every access. The scheduler is not yet started; a reference is kept on `Runtime._proactive_scheduler` for its own later start call.

Build the importer facade. `ImporterFacade` holds references to the LLM provider, the voice service, and a read-only `MemoryFacade`. It mediates between a future web admin route and the import pipeline so channels and import never import each other directly.

Register channels with the channel registry. Any channel instance passed into `Runtime.start(channels=[...])` is added to the registry keyed by its `channel_id`.

Start all channels. `await registry.start_all()` runs each channel's `start()` concurrently. A channel that fails to start logs an error and is left unregistered; the daemon keeps booting so the other subsystems stay available.

Construct and register the runtime memory observer. `RuntimeMemoryObserver(registry, loop)` is created and passed to `register_observer(...)` from the memory module. From this point on, whenever memory commits a session close, a new session start, or a mood update, the observer fans the event out to every channel in the registry that exposes a `push_sse()` capability.

Populate `ctx.persona.voice_enabled` from config. The bool from `[persona].voice_enabled` is copied into the mutable `RuntimePersonaContext` so that interaction and proactive both read the same in-memory value at turn time.

Start the turn dispatcher, the consolidate worker, and the idle scanner as background tasks via `asyncio.create_task`. These three tasks live for the rest of the daemon's life; each checks `shutdown_event.is_set()` on every tick. The proactive scheduler is also started here via `await scheduler.start()`; it spawns its own internal task so runtime does not need to own the handle directly.

Register signal handlers. `loop.add_signal_handler(SIGINT / SIGTERM)` flips the shutdown event; `SIGHUP` schedules `Runtime.reload()` as a task. On Windows this is a no-op with a warning.

Print the local-first disclosure line. One summary log line with every outbound endpoint this process will contact. The line is always the last thing startup emits so an auditor running `echovessel run | head -2` sees it first.

### The turn loop

```
channel.incoming()   ┐
channel.incoming()   ├── ChannelRegistry.all_incoming()
channel.incoming()   ┘        │
                              ▼
                    ┌──────────────────┐
                    │ TurnDispatcher   │
                    │  asyncio.Queue   │  (one queue, one consumer)
                    └───────┬──────────┘
                            ▼
                    Runtime._handle_turn(envelope)
                            │
                            │ normalize IncomingMessage → IncomingTurn
                            │ llm = self.ctx.llm     (local snapshot)
                            │ on_token      = getattr(channel, "on_token", None)
                            │ on_turn_done  = getattr(channel, "on_turn_done", None)
                            │ channel.in_flight_turn_id = turn.turn_id
                            ▼
                    assemble_turn(turn_ctx, turn, llm,
                                  on_token=..., on_turn_done=...)
                            │
                            │  1. ingest each user message  → memory  (turn_id)
                            │  2. load L1 core blocks
                            │  3. retrieve L3+L4 memories
                            │  4. load L2 recent window
                            │  5. build system + user prompts
                            │  6. async for token in llm.stream(...):
                            │        accumulated.append(token)
                            │        await on_token(msg_id, token)
                            │  7. ingest persona reply → memory  (same turn_id)
                            │  finally:
                            │        await on_turn_done(turn.turn_id)
                            ▼
                    AssembledTurn(reply=..., system_prompt=..., ...)
                            │
                            ▼
                    await channel.send(external_ref, reply)
```

A few details about the flow worth internalising. The LLM reference is captured as a **local snapshot** at the top of `_handle_turn`. A hot-reload that replaces `self.ctx.llm` mid-turn cannot corrupt the in-flight turn — the old provider object stays alive until the turn's local variable goes out of scope. There is no lock and no epoch counter; Python's reference semantics do the work for free.

Each handler invocation is wrapped in `asyncio.wait_for(..., timeout=runtime.turn_timeout_seconds)`. A hung `llm.stream` call — most commonly a mis-routed provider proxy that TCP-connects but never answers — is cancelled and the dispatcher continues to the next envelope. Default is 120 seconds; set to 0 in `config.toml` to disable (not recommended — a hung handler silently blocks every subsequent turn on every channel). On timeout the event is logged at `error` level with the channel id, any partial LLM tokens already written to L2 are preserved, and `on_turn_done` still fires via its `finally` block so the channel's debounce state unwinds cleanly.

The persona reply is written to memory **before** the channel is told to send. If the write fails the send is refused — the daemon would rather not emit a line the persona has no record of having said. If the send fails but the write succeeded, the reply is still in L2 and the client will see it on its next reconnect. This ordering rule is the single most important invariant in the turn loop.

`on_turn_done` is always called exactly once, from a `finally` block at the bottom of `assemble_turn`. On a successful turn, on a transient LLM error that kept partial tokens, on a permanent LLM error that produced nothing, on a memory ingest failure — the channel is always notified. Without this invariant a channel's debounce state machine could hang permanently waiting for a turn that already ended.

### Memory observer wiring

Memory's lifecycle hooks (`on_session_closed`, `on_new_session_started`, `on_mood_updated`) are defined as **sync** methods on the `MemoryEventObserver` Protocol — memory cannot import asyncio because its write path is synchronous and runs inside SQLite's single-writer lock. Runtime's observer implementation is also sync, which means its methods return immediately; the real work of broadcasting the event to channels is scheduled onto the runtime event loop via `asyncio.run_coroutine_threadsafe(self._broadcast(...), self._loop)`.

The effect is a clean separation: memory fires a sync hook after a successful commit, the hook returns in microseconds, and the async broadcast runs concurrently on the loop, iterating the channel registry and calling `await channel.push_sse(event, payload)` on any channel that exposes that capability. Per-channel push failures are caught and logged; one bad channel cannot poison another channel's broadcast, and the memory write is already committed no matter what the observer does. If the loop is unavailable (observer fired during shutdown), the coroutine is closed cleanly and a warning is logged — there is nothing to do about a dropped broadcast because the memory state is already on disk.

### The `voice_enabled` toggle

`voice_enabled` is a persona-level main switch. When true, reactive replies and proactive nudges are delivered as neutral voice clips; when false, everything stays text-only. It needs a runtime API because it is mutable at run time — the admin UI can flip it without restarting the daemon — and because the change must be persisted back to `config.toml` so the next boot remembers it.

`Runtime.update_persona_voice_enabled(enabled)` implements the flip in four strict steps. First, the input is validated as a real `bool` so an accidental integer does not corrupt the TOML file. Second, `_atomic_write_config_field` reads the current file with `tomllib`, mutates the parsed dict, serializes it to a tempfile in the same directory with `tomli_w`, fsyncs the tempfile, and runs `os.replace` so the rename is atomic on POSIX. Third — only after the disk write succeeds — `ctx.persona.voice_enabled` is mutated in place; if the write had raised, the in-memory state is untouched so config and ctx never diverge. Fourth, a `chat.settings.updated` SSE event is broadcast to every channel that exposes `push_sse`, with per-channel failures logged but swallowed.

Interaction reads `ctx.persona.voice_enabled` at the moment it constructs the outgoing reply; proactive reads the same field via a `RuntimeContextPersonaView` adapter whose properties read live from `ctx.persona` on every access. No locks, no caching — bool reads in Python are atomic at the bytecode level and a brief race across a tick boundary is acceptable.

### The LLM tier system

Every call site in runtime declares the tier it wants, and the provider maps the tier to a model at call time. The design of the LLMProvider contract (`runtime/llm/base.py`) is a tiny `Protocol` with three methods: `model_for(tier)` for logging and audit, `complete(system, user, *, tier, ...)` for single-shot completions, and `stream(system, user, *, tier, ...)` for token-by-token streaming. Every call signature carries the tier as a keyword argument with `MEDIUM` as the default.

The tier assignments are fixed in code, not in config, because they reflect architectural intent rather than user preference. Extraction and reflection are SMALL — they run on closed sessions, they batch cheap calls, and the user is not waiting. Reflection could in principle benefit from a stronger model but Haiku-class output is good enough for MVP; users who disagree can lift the SMALL tier in `[llm.tier_models]` without code changes. The judge (eval harness, future) is MEDIUM — strict evaluation wants consistency, not the most expensive model. Interaction and proactive are LARGE — the user is staring at the screen, and a better model gives meaningfully better replies.

The three concrete providers in `runtime/llm/` cover 15+ real endpoints between them. `AnthropicProvider` uses the native `anthropic` SDK and targets Claude. `OpenAICompatibleProvider` uses the native `openai` SDK with a configurable `base_url`, which means it covers OpenAI official, OpenRouter, Ollama, LM Studio, llama.cpp server, vLLM, DeepSeek, Together, Groq, xAI, and every other provider that implements OpenAI-compatible REST. `StubProvider` returns canned text for tests and dry runs.

### Control plane

Runtime serves an **independent HTTP control plane** for operator-level lifecycle operations — `/health`, `/shutdown`, `/reload`. The plane lives in `runtime/control.py`, binds `127.0.0.1:0` (kernel assigns a random free port), runs on its own uvicorn instance, and is orthogonal to the Web channel. The three pillars of its trust model: the bind host is a module-level constant (never a config field), a Host-header middleware rejects anything other than `localhost` / `127.0.0.1` with 403 (defence against DNS-rebinding attacks that trick a browser into POSTing to the port), and no CORS headers are emitted so browsers' same-origin policy handles the rest. The assigned port is written into the pidfile as JSON: `{"pid": 12345, "control_port": 54321, "version": 1}`.

The CLI consumes this plane directly. `echovessel stop` reads the pidfile, POSTs `/shutdown`, and prints `stopped (via control plane, pid=…)`; `echovessel reload` POSTs `/reload` and prints `reloaded (via control plane): llm` or `no changes detected`; `echovessel status` probes `/health` and reports the plane's reachability alongside the PID. Each CLI command falls back to Unix signals if the pidfile is v1 (integer-only, from an older install) or the plane is unreachable — in that case stop still sends `SIGTERM`, reload still sends `SIGHUP`, and the output tags `(control plane unreachable)` so the operator knows the slow path was used.

The control plane is orthogonal to Web channel availability. Even when `[channels.web].enabled = false`, the plane is up; operators can still shut down the daemon cleanly. Conversely, a catastrophic Web-channel failure does not bring down the plane — that is the principal separation property the architecture buys.

### Reload

`Runtime.reload()` re-reads `config.toml` from disk, validates it, and propagates the changes to running components. Entry points: HTTP `POST /reload` (the modern path), `SIGHUP` (legacy compat), and the admin `PATCH /api/admin/config` route which calls `reload()` internally after writing the merged TOML. All three paths take the same `asyncio.Lock` so two overlapping reload requests cannot race on `ctx.config` mid-swap.

What actually reloads live:

- **`[llm].*`** — new provider built via `build_llm_provider`, atomically swapped into `ctx.llm`. In-flight turns keep using the old provider because their local `llm` variable captures a snapshot at the start of the turn; the old provider stays alive until the last in-flight turn finishes and Python garbage-collects it.
- **`[memory].retrieve_k` / `.recent_window_size` / `.relational_bonus_weight`** — turn handler reads `ctx.config.memory.*` per-turn, so the new value is live on the next turn without any worker restart.
- **`[consolidate].trivial_message_count` / `.trivial_token_count` / `.reflection_hard_gate_24h`** — mirrored onto the live `ConsolidateWorker` instance (it was constructed once at boot with these values, so `ctx.config` alone would not take effect; the reload path mutates its attributes directly).

Everything else in the `HOT_RELOADABLE_CONFIG_PATHS` allowlist has the same update rule: `ctx.config` replaced wholesale, callers read live. Structural sections — `[runtime].data_dir`, `[memory].db_path`, `[memory].embedder`, `[voice]`, `[proactive]`, `[channels.*]` — require a full restart; see `configuration.md` for the complete per-field matrix. `[persona].voice_enabled` has its own dedicated API (`update_persona_voice_enabled`) and is not touched by reload. `[persona].display_name` updates `ctx.config` but not `ctx.persona` on pure reload; the admin PATCH path has a side-step that mirrors into `ctx.persona` for visible effect.

Reload returns the list of component names that actually changed (e.g. `["llm", "consolidate.trivial_message_count"]`), which the CLI renders and the admin API surfaces to the UI. An empty list means "nothing in the allowlist differed from what's already live."

### Why LLM prompts never contain transport identifiers

The system prompt and user prompt that `assemble_turn` feeds into the LLM contain **zero** information about which channel the turn arrived on. No channel id in any field. No "web" / "discord" / "imessage" literal anywhere in the render path. The hard-coded style instruction at the bottom of the system prompt explicitly forbids the persona from referring to any transport name, thread name, or interface name, even if the user jokes about them. Memory retrieval accepts no `channel_id` filter either — the L1 core blocks, the L3+L4 retrieved memories, and the L2 recent window are all loaded unfiltered.

The reason is that EchoVessel promises a single continuous persona across every channel it speaks on. The moment the persona says "I saw you on Discord yesterday" the illusion breaks, because the user was not "on Discord" from the persona's point of view — they were just talking. Channel id lives in two places only: the memory schema, where it is stored per-row as provenance so a frontend can render a "via Web" badge, and the ingest path, where runtime passes it verbatim into `memory.ingest_message(...)` as the last legitimate use. Neither of those places feeds a prompt.

---

## How to Extend

### 1. Add a new LLM provider

Implement the `LLMProvider` Protocol in a new file under `src/echovessel/runtime/llm/`, register it in `runtime/llm/factory.py::build_llm_provider`, and add the literal to the `Literal[...]` in `runtime/config.py::LLMSection.provider`. The minimum skeleton for a provider:

```python
# src/echovessel/runtime/llm/my_provider.py
from __future__ import annotations

from collections.abc import AsyncIterator

from echovessel.runtime.llm.base import LLMProvider, LLMTier
from echovessel.runtime.llm.errors import LLMPermanentError, LLMTransientError


class MyProvider(LLMProvider):
    """Minimal skeleton for a new LLM provider."""

    provider_name = "my_provider"

    _DEFAULT_TIERS = {
        LLMTier.SMALL: "my-small-model",
        LLMTier.MEDIUM: "my-medium-model",
        LLMTier.LARGE: "my-large-model",
    }

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        pinned_model: str | None = None,
        tier_models: dict[str, str] | None = None,
        default_max_tokens: int = 1024,
        default_temperature: float = 0.7,
        default_timeout: float = 60.0,
    ) -> None:
        # Construction NEVER hits the network. Only cache config.
        self._api_key = api_key
        self.base_url = base_url or "https://api.example.com/v1"
        self._pinned = pinned_model
        self._tier_models = tier_models or {}
        self._max_tokens = default_max_tokens
        self._temperature = default_temperature
        self._timeout = default_timeout

    def model_for(self, tier: LLMTier) -> str:
        if self._pinned:
            return self._pinned
        if tier.value in self._tier_models:
            return self._tier_models[tier.value]
        return self._DEFAULT_TIERS[tier]

    async def complete(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> str:
        try:
            # ... call self._client.complete(...) ...
            return "response text"
        except TimeoutError as e:
            raise LLMTransientError(str(e)) from e
        except ValueError as e:
            raise LLMPermanentError(str(e)) from e

    async def stream(
        self,
        system: str,
        user: str,
        *,
        tier: LLMTier = LLMTier.MEDIUM,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        timeout: float | None = None,
    ) -> AsyncIterator[str]:
        # For providers that cannot stream, fall back to complete()
        # and yield the full body once.
        text = await self.complete(
            system, user,
            tier=tier, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout,
        )
        yield text
```

Register it in `build_llm_provider`:

```python
if provider == "my_provider":
    from echovessel.runtime.llm.my_provider import MyProvider
    return MyProvider(
        api_key=api_key,
        base_url=cfg.base_url,
        pinned_model=cfg.model,
        tier_models=cfg.tier_models or None,
        default_max_tokens=cfg.max_tokens,
        default_temperature=cfg.temperature,
        default_timeout=float(cfg.timeout_seconds),
    )
```

Three rules the skeleton has to honour. Construction must not touch the network — if the user's API key is wrong we find out on the first `complete` call, not at boot. Transient and permanent failures are two different exception types because the consolidate worker retries on transient errors only. And the tier-to-model resolution must follow the priority order `pinned > tier_models > defaults`; users rely on `llm.model = "x"` to mean "use x for everything".

### 2. Add a new startup step

Startup is split between `Runtime.build` (synchronous construction) and `Runtime.start` (async launch). Pure object construction belongs in `build`; anything that spawns a background task or awaits a call belongs in `start`.

If the new step produces a long-lived object that other subsystems need (a new background service, a new adapter layer), add a field to `RuntimeContext`, construct it in `build`, and teach `start` to register it with the channel registry or spawn its task. If the new step needs to run before channels start (because channels depend on it), add it before `registry.start_all()`; if it needs to run after channels are alive (because it fans events into channels), add it after `registry.start_all()` — the memory observer registration is the template.

```python
# in Runtime.build(...)
my_service = MyService(cfg=config.my_section, engine=engine)
ctx = RuntimeContext(
    ...,
    my_service=my_service,     # new field on RuntimeContext
)

# in Runtime.start(...)
# Example A: needs channels alive first
await self.ctx.registry.start_all()
try:
    self.ctx.my_service.attach(self.ctx.registry)
except Exception as e:
    log.warning("my_service.attach failed: %s", e)

# Example B: needs its own background task
self._tasks.append(
    asyncio.create_task(self.ctx.my_service.run(), name="my_service")
)
```

Two rules. Failures in non-critical subsystems degrade gracefully — log a warning, null out the reference, and let the daemon boot. Only schema migration and database open are fatal, because a half-open daemon writes corrupted data. And if the new service needs to be stopped cleanly, add a matching block to `Runtime.stop` so `shutdown_event` propagation still works.

### 3. Handle a new signal

Signal handlers are registered in `_register_signal_handlers` via `loop.add_signal_handler`. Handlers must not do real work — they flip a flag or schedule a task, then return immediately. Blocking inside a signal handler deadlocks the loop.

```python
# in Runtime._register_signal_handlers
import signal

def _dump_state() -> None:
    """SIGUSR1: dump runtime state to the log for debugging."""
    log.info("runtime state dump: channels=%s, in_flight=%s",
             self.ctx.registry.channel_ids(),
             self.ctx.registry.any_channel_in_flight())

try:
    loop.add_signal_handler(signal.SIGUSR1, _dump_state)
except NotImplementedError:
    # Windows: signal handlers not supported; silently skip.
    pass
```

For a handler that needs graceful shutdown semantics, follow the SIGINT/SIGTERM template: the handler flips `self.ctx.shutdown_event`, and every background task checks that event on its next tick. The top of `Runtime.stop` already waits for the event, cancels the background tasks, stops the proactive scheduler, and tears down the channel registry — if the new signal should trigger a drain, just set the shutdown event. For a handler that should rebuild something without stopping the daemon (SIGHUP is the template), schedule an async method via `asyncio.create_task(self.my_reload())` and do the reload inside that coroutine so I/O and locks are async-safe.

---

## See also

- [`memory.md`](./memory.md) — the ground truth store runtime feeds through `ingest_message` / `retrieve` / `load_core_blocks`
- [`channels.md`](./channels.md) — the transport layer that yields `IncomingTurn` and consumes the `on_token` / `on_turn_done` callbacks
- [`voice.md`](./voice.md) — `VoiceService`, built in startup step 9 and read at reply time through `ctx.persona.voice_enabled`
- [`proactive.md`](./proactive.md) — the scheduler built in startup step 10 and started alongside the turn dispatcher
- [`configuration.md`](./configuration.md) — every field in `config.toml` and how they map onto `RuntimeContext`
- `echovessel init` — creates `~/.echovessel/config.toml` from the bundled sample

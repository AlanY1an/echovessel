"""Runtime orchestrator — wires memory + voice + proactive + channels + LLM.

Implements spec §3 (startup sequence) and §2.5 (graceful shutdown) as an
async `Runtime` class. The Click launcher in `runtime/launcher.py` thin-
wraps this class as an `echovessel run` subcommand.

Round 2 additions (`docs/runtime/03-round2-integration-tracker.md`):
- Step 10.5 · Instantiate VoiceService when `[voice].enabled=true`
- Step 10.6 · Build ProactiveScheduler via `echovessel.proactive.build_proactive_scheduler`
- Step 16 graceful stop · Await proactive scheduler.stop() before
  cancelling background tasks

Key responsibilities:
- Load config, ensure data dir, open DB, run migrations
- Catch up orphan sessions
- Build LLM provider + embedder + extract/reflect/proactive callables
- Spin up consolidate worker, idle scanner, voice service, proactive scheduler
- Register + start channels
- Start turn dispatcher
- Handle SIGINT / SIGTERM (graceful) and SIGHUP (reload LLM provider)
- Print local-first disclosure log line at the end of startup (§13.1)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tomli_w
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.base import OutgoingMessage
from echovessel.memory import (
    Persona,
    User,
    create_all_tables,
    create_engine,
    ensure_schema_up_to_date,
    register_observer,
    unregister_observer,
)
from echovessel.memory.backends.sqlite import SQLiteBackend
from echovessel.memory.models import Session as SessionRow
from echovessel.memory.sessions import catch_up_stale_sessions
from echovessel.proactive import (
    ProactiveScheduler as ProactiveSchedulerProtocol,
)
from echovessel.proactive import (
    build_proactive_scheduler,
)
from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.config import Config, load_config
from echovessel.runtime.consolidate_worker import ConsolidateWorker
from echovessel.runtime.idle_scanner import IdleScanner
from echovessel.runtime.importer_facade import ImporterFacade
from echovessel.runtime.interaction import (
    AssembledTurn,
    IncomingTurn,
    TurnContext,
    assemble_turn,
)
from echovessel.runtime.llm import LLMProvider, LLMTier, build_llm_provider
from echovessel.runtime.memory_facade import (
    MemoryFacade,
    ProactiveChannelRegistry,
)
from echovessel.runtime.memory_observers import RuntimeMemoryObserver
from echovessel.runtime.prompts_wiring import (
    make_extract_fn,
    make_proactive_fn,
    make_reflect_fn,
)
from echovessel.runtime.turn_dispatcher import TurnDispatcher
from echovessel.voice import (
    VoiceService,
    VoiceServiceConfig,
    build_voice_service,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Embedder factory
# ---------------------------------------------------------------------------


EmbedCallable = Callable[[str], list[float]]


def build_sentence_transformers_embedder(
    model_name: str, cache_dir: Path
) -> EmbedCallable:
    """Load a sentence-transformers model and return its `.encode` callable.

    Lazy import so daemons that inject a custom embedder (tests, eval) don't
    need the heavy dependency.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "sentence-transformers not installed. Install the [embeddings] "
            "extra: `uv sync --extra embeddings`."
        ) from e

    cache_dir.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(model_name, cache_folder=str(cache_dir))

    def _encode(text: str) -> list[float]:
        vec = model.encode([text], convert_to_numpy=True, normalize_embeddings=False)
        return list(map(float, vec[0]))

    return _encode


def build_zero_embedder(dim: int = 384) -> EmbedCallable:
    """Fallback embedder used by tests + `--no-embedder` dry runs. Produces
    a sparse, deterministic vector that won't match anything real but lets
    the pipeline run end-to-end."""

    def _encode(text: str) -> list[float]:
        v = [0.0] * dim
        v[hash(text) % dim] = 1.0
        return v

    return _encode


# ---------------------------------------------------------------------------
# RuntimeContext + Runtime
# ---------------------------------------------------------------------------


@dataclass
class RuntimePersonaContext:
    """Mutable runtime copy of persona state (spec §17a.7).

    Unlike `config.persona` (which is a frozen Pydantic snapshot of what
    the TOML file said at `Runtime.build()` time), this dataclass is
    MUTATED in-place by `Runtime.update_persona_voice_enabled` so that
    interaction / proactive can read the "live" persona state on every
    tick without re-reading the config file.

    Only `voice_enabled` is currently mutable. `id` / `display_name` /
    `voice_id` are write-once at startup — v1.0 may add admin mutations
    for them as well.

    Read-time atomicity: Python attribute reads on a `bool` are atomic at
    the bytecode level, so cross-coroutine races can at worst observe an
    old value for a few ticks (§17a.7 note: acceptable — no lock).
    """

    id: str
    display_name: str
    voice_id: str | None = None
    voice_provider: str | None = None
    voice_enabled: bool = False


@dataclass
class RuntimeContext:
    """Global runtime state shared across interaction, consolidate, idle,
    voice, proactive, and channel tasks. See spec §14 decision 5.

    `llm` is mutable — SIGHUP reload replaces the attribute pointer. Turn
    handlers capture a local snapshot of `runtime.llm` at the start of a
    turn, so in-flight work keeps its old provider alive until it finishes.

    `voice_service` is None when `[voice].enabled=false` in config —
    channels and proactive scheduler handle the None case as "voice is
    not available" (Voice spec §4.7 + Proactive spec §10.3).

    `persona` is the v0.4 mutable runtime persona context (spec §17a.7).
    Interaction reads `ctx.persona.voice_enabled` at the moment it
    constructs the OutgoingMessage; proactive reads it via the
    `RuntimeContextPersonaView` adapter passed to `build_proactive_scheduler`.
    """

    config: Config
    config_path: Path | None
    data_dir: Path
    db_path: Path
    engine: Any
    backend: SQLiteBackend
    embed_fn: EmbedCallable
    llm: LLMProvider
    registry: ChannelRegistry
    shutdown_event: asyncio.Event
    persona: RuntimePersonaContext = field(
        default_factory=lambda: RuntimePersonaContext(
            id="default", display_name="Your Companion"
        )
    )
    voice_service: VoiceService | None = None
    loop: asyncio.AbstractEventLoop | None = None


class RuntimeContextPersonaView:
    """PersonaView adapter the proactive scheduler consumes (proactive
    spec §6.2a + runtime spec §17a.7).

    Reads `voice_enabled` / `voice_id` **live** on every property
    access — no caching. This is the whole reason the adapter exists:
    `Runtime.update_persona_voice_enabled` mutates
    `ctx.persona.voice_enabled` in-place, and the next proactive tick
    picks up the new value without needing a reload hook.

    Why not a plain dataclass? The proactive `PersonaView` Protocol
    (`echovessel.proactive.base.PersonaView`) defines the members as
    `@property`; structural typing requires property semantics, not
    plain attributes. Implementing them as methods backed by live
    reads is the simplest way to satisfy the Protocol AND guarantee
    the admin-toggle-takes-effect-next-tick contract from review
    Check 3.
    """

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    @property
    def voice_enabled(self) -> bool:
        return bool(self._ctx.persona.voice_enabled)

    @property
    def voice_id(self) -> str | None:
        return self._ctx.persona.voice_id


class Runtime:
    """The daemon.

    Usage:

        rt = Runtime.build(config_path, embed_fn=build_zero_embedder())
        await rt.start()
        await rt.wait_until_shutdown()
        await rt.stop()
    """

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []
        self._worker: ConsolidateWorker | None = None
        self._scanner: IdleScanner | None = None
        self._proactive_scheduler: ProactiveSchedulerProtocol | None = None
        self._dispatcher: TurnDispatcher | None = None
        self._sighup_registered = False
        # v0.4 · Step 10.7 / 12.5 additions
        self._importer_facade: ImporterFacade | None = None
        self._memory_observer: RuntimeMemoryObserver | None = None
        # Stage 2 · Web channel uvicorn server (optional)
        self._web_channel: Any = None
        self._web_uvicorn_server: Any = None
        self._web_uvicorn_task: asyncio.Task | None = None
        self._discord_channel: Any = None

    # ---- Public accessors (for channels / CLI / tests) ---------------------

    @property
    def voice_service(self) -> VoiceService | None:
        """The VoiceService instance, or None if `[voice].enabled=false`.

        Web channel (future Thread WEB) reads this to decide whether to
        render the "🔊 play" / "🎙 record" buttons. Tests assert its
        presence / absence based on the config they passed in.
        """
        return self.ctx.voice_service

    @property
    def proactive_scheduler(self) -> ProactiveSchedulerProtocol | None:
        """The ProactiveScheduler instance, or None if
        `[proactive].enabled=false`. Primarily for tests and debugging.
        """
        return self._proactive_scheduler

    # ---- Builders ----------------------------------------------------------

    @classmethod
    def build(
        cls,
        config_path: Path | str | None,
        *,
        config_override: Config | None = None,
        embed_fn: EmbedCallable | None = None,
        llm: LLMProvider | None = None,
    ) -> Runtime:
        """Run spec §3 startup steps 1-8 (no background task launches yet).

        Callers typically use `embed_fn=build_zero_embedder()` for quick
        starts / tests, or pass `embed_fn=build_sentence_transformers_embedder(...)`
        for the real daemon.
        """
        if config_override is not None:
            config = config_override
            resolved_path: Path | None = None
        else:
            if config_path is None:
                raise ValueError("Runtime.build requires config_path or config_override")
            resolved_path = Path(config_path).expanduser()
            config = load_config(resolved_path)

        data_dir = Path(config.runtime.data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (data_dir / "embedder.cache").mkdir(parents=True, exist_ok=True)

        db_path_raw = config.memory.db_path
        if db_path_raw == ":memory:":
            db_path = Path(":memory:")
        else:
            db_path = Path(db_path_raw)
            if not db_path.is_absolute():
                db_path = data_dir / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(str(db_path) if db_path != Path(":memory:") else ":memory:")

        # --- Step 6.5 · v0.4 · idempotent schema migration --------------
        #
        # Spec §17a.4: run BEFORE `create_all_tables`. On a fresh DB this
        # is a no-op (every check short-circuits); on a legacy v0.2 DB it
        # ALTERs in new columns. Failure is FATAL — a half-migrated DB
        # will explode at insert time, we'd rather fail-fast at boot.
        try:
            ensure_schema_up_to_date(engine)
        except Exception as e:
            log.error(
                "schema migration failed, refusing to start: %s", e, exc_info=True
            )
            raise

        create_all_tables(engine)
        backend = SQLiteBackend(engine)

        # Ensure persona + user rows exist.
        with DbSession(engine) as db:
            persona = db.get(Persona, config.persona.id)
            if persona is None:
                db.add(
                    Persona(
                        id=config.persona.id,
                        display_name=config.persona.display_name,
                    )
                )
                db.commit()
            user = db.get(User, "self")
            if user is None:
                db.add(User(id="self", display_name="self"))
                db.commit()

        # Catch up stale sessions (spec §3 step 5)
        with DbSession(engine) as db:
            stale = catch_up_stale_sessions(db, now=datetime.now())
            if stale:
                db.commit()
                log.info("catch-up: marked %d stale sessions closing", len(stale))

        if llm is None:
            llm = build_llm_provider(config.llm)

        if embed_fn is None:
            embed_fn = build_zero_embedder()

        # --- Step 10.5 · Instantiate VoiceService (Round 2) -------------
        #
        # Voice spec §7.3.2: runtime builds the VoiceService after
        # memory/LLM are ready but BEFORE channels (channels may take a
        # VoiceService reference in their constructor to wire the
        # "🔊 play" button — Web channel does this).
        #
        # Failure is non-fatal: a bad tts_provider / missing key logs a
        # warning and the daemon boots with voice_service=None.
        voice_service: VoiceService | None = None
        if config.voice.enabled:
            try:
                voice_cfg = VoiceServiceConfig(
                    enabled=True,
                    tts_provider=config.voice.tts_provider,
                    stt_provider=config.voice.stt_provider,
                    tts_api_key_env=config.voice.tts_api_key_env,
                    stt_api_key_env=config.voice.stt_api_key_env,
                    default_audio_format=config.voice.default_audio_format,
                    default_voice_id=config.persona.voice_id,
                )
                voice_service = build_voice_service(voice_cfg)
                log.info(
                    "voice service initialized: tts=%s stt=%s voice_id=%s",
                    voice_service.tts_provider_name,
                    voice_service.stt_provider_name,
                    config.persona.voice_id or "<default>",
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "voice service construction failed, continuing without voice: %s",
                    e,
                )
                voice_service = None
        else:
            log.info("voice service: disabled (config.voice.enabled=false)")

        # v0.4 · Step 12.6 (materialized at build() because ctx.persona
        # must exist before `Runtime.start()` populates any subsystem
        # that reads from it). Failure to read the toggle degrades to
        # the safe default (voice_enabled=False) with a warning, as per
        # spec §17a.4 Step 12.6 failure handling.
        try:
            voice_enabled_initial = bool(config.persona.voice_enabled)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "could not read persona.voice_enabled from config, "
                "defaulting to False: %s",
                e,
            )
            voice_enabled_initial = False

        persona_ctx = RuntimePersonaContext(
            id=config.persona.id,
            display_name=config.persona.display_name,
            voice_id=config.persona.voice_id,
            voice_provider=config.persona.voice_provider,
            voice_enabled=voice_enabled_initial,
        )

        ctx = RuntimeContext(
            config=config,
            config_path=resolved_path,
            data_dir=data_dir,
            db_path=db_path,
            engine=engine,
            backend=backend,
            embed_fn=embed_fn,
            llm=llm,
            registry=ChannelRegistry(),
            shutdown_event=asyncio.Event(),
            persona=persona_ctx,
            voice_service=voice_service,
        )

        return cls(ctx)

    # ---- Public lifecycle --------------------------------------------------

    async def start(
        self, *, channels: Iterable[Any] | None = None, register_signals: bool = True
    ) -> None:
        """Run spec §3 steps 9-15. Does NOT block — returns as soon as all
        background tasks are scheduled."""
        # Capture running loop so observer dispatch from sync memory
        # callbacks (M-round4) can bridge into async broadcasts.
        self.ctx.loop = asyncio.get_running_loop()

        for ch in channels or []:
            self.ctx.registry.register(ch)

        # Pick up orphan closing sessions for the initial worker queue
        orphan_ids: list[str] = []
        with DbSession(self.ctx.engine) as db:
            stmt = select(SessionRow).where(
                SessionRow.status.in_(["closing"]),
                SessionRow.extracted == False,  # noqa: E712
                SessionRow.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            for s in db.exec(stmt):
                if s.id:
                    orphan_ids.append(s.id)

        def _db_factory() -> DbSession:
            return DbSession(self.ctx.engine)

        extract_fn = make_extract_fn(self.ctx.llm)
        reflect_fn = make_reflect_fn(self.ctx.llm)

        self._worker = ConsolidateWorker(
            db_factory=_db_factory,
            backend=self.ctx.backend,
            extract_fn=extract_fn,
            reflect_fn=reflect_fn,
            embed_fn=self.ctx.embed_fn,
            poll_seconds=self.ctx.config.consolidate.worker_poll_seconds,
            max_retries=self.ctx.config.consolidate.worker_max_retries,
            shutdown_event=self.ctx.shutdown_event,
            initial_session_ids=tuple(orphan_ids),
            trivial_message_count=self.ctx.config.consolidate.trivial_message_count,
            trivial_token_count=self.ctx.config.consolidate.trivial_token_count,
            reflection_hard_limit_24h=self.ctx.config.consolidate.reflection_hard_gate_24h,
        )

        self._scanner = IdleScanner(
            db_factory=_db_factory,
            interval_seconds=self.ctx.config.idle_scanner.interval_seconds,
            shutdown_event=self.ctx.shutdown_event,
        )

        # --- Step 10.6 · Instantiate ProactiveScheduler (Round 2) ------
        #
        # Proactive is optional: `[proactive].enabled=false` skips the
        # entire scheduler. When enabled, we wire up:
        #   - MemoryFacade        (reads + single write, D4-clean)
        #   - ProactiveChannelReg (translates ChannelLike → ChannelProtocol)
        #   - proactive_fn        (LARGE-tier LLM call with inline prompt)
        #   - voice_service       (may be None; proactive handles that)
        # Audit sink defaults to JSONL under <data_dir>/logs/.
        self._proactive_scheduler = self._build_proactive_scheduler(
            _db_factory
        )

        # --- Step 10.7 · v0.4 · Instantiate ImporterFacade -------------
        #
        # Per spec §17a.6 the facade is constructed before channel
        # registration so Web channel admin routes can capture the
        # reference in their constructor. Construction is best-effort:
        # if it fails the daemon logs + boots without import support.
        try:
            memory_facade_for_importer = MemoryFacade(_db_factory)
            self._importer_facade = ImporterFacade(
                llm_provider=self.ctx.llm,
                voice_service=self.ctx.voice_service,
                memory_api=memory_facade_for_importer,
            )
            log.info("importer facade: built")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "importer facade construction failed, import routes will "
                "be disabled: %s",
                e,
            )
            self._importer_facade = None

        # --- Stage 2 · Web channel + uvicorn background server --------
        #
        # When `[channels.web].enabled=true`, build a WebChannel +
        # SSEBroadcaster + FastAPI app and launch uvicorn inside this
        # event loop as a create_task. Failures are non-fatal — the
        # daemon still boots with other channels alive; only the web
        # surface is disabled.
        if self.ctx.config.channels.web.enabled:
            try:
                await self._start_web_channel()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "web channel startup failed; continuing without web: %s",
                    e,
                    exc_info=True,
                )
                self._web_channel = None
                self._web_uvicorn_server = None
                self._web_uvicorn_task = None

            # --- Stage 3 · First-launch detection + browser auto-open --
            #
            # If the web channel came up cleanly AND the configured
            # persona has no core_blocks rows, this is a fresh install
            # that needs onboarding. Schedule a best-effort browser
            # open to the onboarding URL. webbrowser.open runs in a
            # background task so it does not block the rest of
            # startup.
            if self._web_channel is not None:
                try:
                    onboarding_required = self._check_first_launch()
                except Exception as e:  # noqa: BLE001
                    log.warning("first-launch detection failed: %s", e)
                    onboarding_required = False
                if onboarding_required:
                    self._tasks.append(
                        asyncio.create_task(
                            self._open_browser_when_ready(),
                            name="first_launch_browser_open",
                        )
                    )

        # --- Stage 6 · Discord DM channel registration ----------------
        #
        # Discord.py is an optional extra (`pip install echovessel[discord]`).
        # If the import fails, log and skip — the daemon boots normally
        # with whatever other channels are configured. If the token env
        # var is unset, same treatment. The channel is started by the
        # unified `registry.start_all()` below once registered.
        if self.ctx.config.channels.discord.enabled:
            try:
                self._register_discord_channel()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "discord channel startup failed; continuing without discord: %s",
                    e,
                    exc_info=True,
                )
                self._discord_channel = None

        await self.ctx.registry.start_all()

        # --- Step 12.5 · v0.4 · Register RuntimeMemoryObserver ---------
        #
        # Spec §17a.5: observer is registered AFTER channels start so
        # broadcasts can reach an alive `push_sse` surface. Failure to
        # register is non-fatal — memory writes still commit, just
        # without SSE visibility (degradation matches spec §17a.4 Step
        # 12.5 failure handling).
        try:
            self._memory_observer = RuntimeMemoryObserver(
                registry=self.ctx.registry,
                loop=self.ctx.loop,
            )
            register_observer(self._memory_observer)
            log.info("memory observer: registered")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "runtime memory observer registration failed: %s", e
            )
            self._memory_observer = None

        self._dispatcher = TurnDispatcher(
            registry=self.ctx.registry,
            handler=self._handle_turn,
            shutdown_event=self.ctx.shutdown_event,
        )

        self._tasks = [
            asyncio.create_task(self._worker.run(), name="consolidate_worker"),
            asyncio.create_task(self._scanner.run(), name="idle_scanner"),
            asyncio.create_task(self._dispatcher.run(), name="turn_dispatcher"),
        ]

        # Proactive scheduler is launched separately because its
        # `.start()` method spawns its own internal background task. We
        # keep a reference for `.stop()` but don't enter it into
        # `self._tasks` (stop() handles its own task lifecycle).
        if self._proactive_scheduler is not None:
            try:
                await self._proactive_scheduler.start()
            except Exception as e:  # noqa: BLE001
                log.error(
                    "proactive scheduler failed to start; continuing "
                    "without proactive: %s",
                    e,
                )
                self._proactive_scheduler = None

        if register_signals:
            self._register_signal_handlers()

        self._print_local_first_disclosure()

    async def wait_until_shutdown(self) -> None:
        await self.ctx.shutdown_event.wait()

    async def stop(self, *, timeout: float = 15.0) -> None:
        log.info("runtime stopping")
        self.ctx.shutdown_event.set()

        # Unregister memory observer early so in-flight session-closed
        # events from consolidate/idle shutdown don't try to broadcast
        # after channels have already torn down.
        if self._memory_observer is not None:
            try:
                unregister_observer(self._memory_observer)
            except Exception as e:  # noqa: BLE001
                log.warning("memory observer unregister failed: %s", e)
            self._memory_observer = None

        # Stop proactive scheduler first — its tick loop may be mid-send,
        # which wants a graceful finish window (spec §2.5 graceful stop).
        if self._proactive_scheduler is not None:
            try:
                await asyncio.wait_for(
                    self._proactive_scheduler.stop(), timeout=timeout
                )
            except TimeoutError:
                log.warning("proactive scheduler stop timeout")
            except Exception as e:  # noqa: BLE001
                log.warning("proactive scheduler stop errored: %s", e)
            self._proactive_scheduler = None

        try:
            await asyncio.wait_for(self.ctx.registry.stop_all(), timeout=timeout)
        except TimeoutError:
            log.warning("channel stop timeout")

        # Stage 2 · tear down the uvicorn server AFTER channels have
        # been stopped so any in-flight SSE / chat requests see a
        # channel whose `incoming()` iterator has already terminated
        # (runtime.dispatcher already stopped).
        if self._web_uvicorn_server is not None or self._web_uvicorn_task is not None:
            await self._stop_web_channel(timeout=timeout)

        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        log.info("runtime stopped")

    # ---- Proactive scheduler factory ---------------------------------------

    def _make_persona_view(self) -> RuntimeContextPersonaView:
        """Spec §17a.7 + proactive spec §6.2a.

        Produces a PersonaView adapter that reads `voice_enabled` and
        `voice_id` *live* from `self.ctx.persona` on every property
        access. This lets `update_persona_voice_enabled` take effect on
        the next proactive tick without any reload plumbing.
        """
        return RuntimeContextPersonaView(self.ctx)

    def _build_proactive_scheduler(
        self,
        db_factory: Callable[[], DbSession],
    ) -> ProactiveSchedulerProtocol | None:
        """Assemble a `ProactiveScheduler` using runtime-side adapters.

        Returns None when `[proactive].enabled=false` — the runtime then
        skips the scheduler entirely. When enabled, construction failure
        logs and returns None (daemon continues without proactive rather
        than refusing to start).
        """
        if not self.ctx.config.proactive.enabled:
            log.info("proactive scheduler: disabled (config.proactive.enabled=false)")
            return None

        try:
            memory_api = MemoryFacade(db_factory)
            channel_registry = ProactiveChannelRegistry(self.ctx.registry)
            proactive_fn = make_proactive_fn(self.ctx.llm)
            proactive_config = self.ctx.config.proactive.to_proactive_config(
                persona_id=self.ctx.config.persona.id,
                user_id="self",
            )

            # v0.4 · persona= replaces legacy voice_id= kwarg (proactive
            # round2 shim). is_turn_in_flight closes over the registry
            # so every tick reads the current set of channels rather
            # than a stale snapshot.
            scheduler = build_proactive_scheduler(
                config=proactive_config,
                memory_api=memory_api,
                channel_registry=channel_registry,
                proactive_fn=proactive_fn,
                persona=self._make_persona_view(),
                voice_service=self.ctx.voice_service,
                is_turn_in_flight=self.ctx.registry.any_channel_in_flight,
                audit_sink=None,  # JSONL sink default under data_dir/logs
                log_dir=self.ctx.data_dir / "logs",
                shutdown_event=self.ctx.shutdown_event,
            )
            log.info(
                "proactive scheduler: built (tick=%ds, max_per_24h=%d, "
                "voice=%s)",
                self.ctx.config.proactive.tick_interval_seconds,
                self.ctx.config.proactive.max_per_24h,
                "enabled" if self.ctx.voice_service is not None else "disabled",
            )
            return scheduler
        except Exception as e:  # noqa: BLE001
            log.error(
                "proactive scheduler build failed; daemon will boot "
                "without proactive: %s",
                e,
                exc_info=True,
            )
            return None

    # ---- Stage 2 · Web channel lifecycle -----------------------------------

    async def _start_web_channel(self) -> None:
        """Instantiate the Web channel + uvicorn server.

        Called from :meth:`start` when ``[channels.web].enabled=true``.
        Steps:

        1. Build :class:`SSEBroadcaster` and :class:`WebChannel`, attach
           the broadcaster so ``push_sse`` becomes live.
        2. Register the WebChannel via the channel registry. The
           dispatcher will start iterating its ``incoming()`` queue once
           ``registry.start_all()`` runs in ``start()``.
        3. Build the FastAPI app via
           :func:`echovessel.channels.web.build_web_app`.
        4. Launch uvicorn programmatically (``uvicorn.Server(Config(...))``)
           as an :func:`asyncio.create_task` — staying in the same loop as
           the rest of the runtime so graceful shutdown works.

        On failure at any step, the partial state is rolled back by
        ``start()``'s exception handler. This method itself does not
        swallow exceptions.
        """

        import uvicorn

        from echovessel.channels.web import SSEBroadcaster, WebChannel, build_web_app

        web_cfg = self.ctx.config.channels.web
        broadcaster = SSEBroadcaster()
        channel = WebChannel(debounce_ms=web_cfg.debounce_ms)
        channel.attach_broadcaster(broadcaster)

        self.ctx.registry.register(channel)
        self._web_channel = channel

        app = build_web_app(
            channel=channel,
            broadcaster=broadcaster,
            runtime=self,
            voice_service=self.ctx.voice_service,
            importer_facade=self._importer_facade,
        )

        config = uvicorn.Config(
            app,
            host=web_cfg.host,
            port=web_cfg.port,
            log_level="warning",
            loop="asyncio",
            lifespan="on",
            access_log=False,
        )
        server = uvicorn.Server(config)
        # `serve()` blocks for the lifetime of the server, so wrap it in
        # a task. The runtime keeps a reference so `stop()` can request
        # graceful shutdown.
        self._web_uvicorn_server = server
        self._web_uvicorn_task = asyncio.create_task(
            server.serve(), name="web_uvicorn_server"
        )
        log.info(
            "web channel: serving on http://%s:%d (debounce_ms=%d)",
            web_cfg.host,
            web_cfg.port,
            web_cfg.debounce_ms,
        )

    def _register_discord_channel(self) -> None:
        """Instantiate and register a Discord DM channel.

        Called from :meth:`start` when ``[channels.discord].enabled=true``.
        Steps:

        1. Lazy-import ``echovessel.channels.discord.DiscordChannel`` —
           raises :class:`ImportError` if the optional ``discord`` extra
           isn't installed, which the caller catches and logs.
        2. Read the bot token from the configured env var. A missing
           or empty token is a soft error: log and skip (no channel
           registered), leaving the rest of the daemon alive.
        3. Construct the channel with the config-validated ``debounce_ms``
           and an optional allowlist set.
        4. Register with ``ctx.registry`` so the unified
           ``registry.start_all()`` call will bring the bot online.
        """

        import os

        try:
            from echovessel.channels.discord import DiscordChannel
        except ImportError as e:
            log.error(
                "discord channel enabled but discord.py is not installed "
                "(install with `uv sync --extra discord` or `pip install "
                "echovessel[discord]`): %s",
                e,
            )
            return

        discord_cfg = self.ctx.config.channels.discord
        token = os.environ.get(discord_cfg.token_env, "")
        if not token:
            log.error(
                "discord channel enabled but env var %s is not set",
                discord_cfg.token_env,
            )
            return

        allowed_ids_list = discord_cfg.allowed_user_ids
        allowed_ids = (
            set(allowed_ids_list) if allowed_ids_list else None
        )

        channel = DiscordChannel(
            token=token,
            debounce_ms=discord_cfg.debounce_ms,
            allowed_user_ids=allowed_ids,
        )
        self.ctx.registry.register(channel)
        self._discord_channel = channel
        log.info(
            "discord channel: registered (allowlist=%s, debounce_ms=%d)",
            "open" if allowed_ids is None else f"{len(allowed_ids)} users",
            discord_cfg.debounce_ms,
        )

    async def _stop_web_channel(self, *, timeout: float = 5.0) -> None:
        """Tear down the uvicorn server started by :meth:`_start_web_channel`.

        Uvicorn's :class:`uvicorn.Server` exposes a ``should_exit`` flag
        that its main loop checks between requests. Flipping it to True
        gives in-flight requests a graceful window; cancelling the task
        is the fallback if they don't finish in ``timeout`` seconds.
        """

        if self._web_uvicorn_server is not None:
            try:
                self._web_uvicorn_server.should_exit = True
            except Exception as e:  # noqa: BLE001
                log.warning("uvicorn should_exit set failed: %s", e)

        if self._web_uvicorn_task is not None:
            try:
                await asyncio.wait_for(self._web_uvicorn_task, timeout=timeout)
            except TimeoutError:
                log.warning("web uvicorn stop timeout; cancelling task")
                self._web_uvicorn_task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, Exception
                ):
                    await self._web_uvicorn_task
            except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                log.debug("web uvicorn task exited: %s", e)

        self._web_channel = None
        self._web_uvicorn_server = None
        self._web_uvicorn_task = None

    # ---- Stage 3 · First-launch detection + browser auto-open --------------

    def _check_first_launch(self) -> bool:
        """Return True iff the configured persona has zero core_blocks.

        Runs a single LIMIT 1 query against the ``core_blocks`` table
        — cheap enough to run on every boot without any caching layer.
        Used by the Stage 3 startup path to decide whether to open a
        browser tab at the onboarding URL.
        """

        from echovessel.memory import CoreBlock

        persona_id = self.ctx.config.persona.id
        with DbSession(self.ctx.engine) as db:
            stmt = (
                select(CoreBlock)
                .where(
                    CoreBlock.persona_id == persona_id,
                    CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .limit(1)
            )
            return db.exec(stmt).first() is None

    async def _open_browser_when_ready(self) -> None:
        """Open a browser tab at the Web channel's root URL.

        Scheduled as a background task so it cannot block daemon
        startup. Waits ~500ms for uvicorn to finish binding the port,
        then calls :func:`webbrowser.open`. Headless environments
        (CI, servers) cause the call to return False — in that case
        we log instructions for manual navigation.
        """

        import webbrowser

        web_cfg = self.ctx.config.channels.web
        url = f"http://{web_cfg.host}:{web_cfg.port}/"

        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

        try:
            opened = webbrowser.open(url, new=1)
        except Exception as e:  # noqa: BLE001
            log.warning("webbrowser.open failed: %s", e)
            opened = False

        if opened:
            log.info("first launch: opened browser at %s", url)
        else:
            log.info(
                "first launch: could not open browser automatically. "
                "Open %s manually to complete onboarding.",
                url,
            )

    # ---- Turn handling -----------------------------------------------------

    async def _handle_turn(self, envelope: Any) -> None:
        """Consume one `IncomingTurn` (or legacy `IncomingMessage`) from
        the dispatcher queue and drive `assemble_turn` through it.

        v0.4 adds:
          - Auto-wrap of legacy `IncomingMessage` into a 1-element
            `IncomingTurn` so the handler has exactly one shape.
          - `on_token` / `on_turn_done` callbacks pulled from the
            channel (via getattr — channels that don't expose them
            just get None, which assemble_turn handles).
          - `in_flight_turn_id` bookkeeping on the channel so the
            proactive `is_turn_in_flight` gate can see the turn in
            progress.
        """
        # Normalize into IncomingTurn shape.
        if isinstance(envelope, IncomingTurn):
            turn = envelope
        else:
            # Legacy IncomingMessage. Wrap so the rest of the flow
            # doesn't need branching.
            turn = IncomingTurn.from_single_message(envelope)

        # Capture snapshot of llm reference — SIGHUP reload may replace
        # self.ctx.llm, but this local binding keeps the old provider alive
        # for the duration of this turn (spec §6.5).
        llm = self.ctx.llm

        channel = self.ctx.registry.get(turn.channel_id)
        on_token = None
        channel_on_turn_done = None
        if channel is not None:
            # Stage 2 · Web channel exposes an `on_token_callback()`
            # factory that returns a fresh async callable for this
            # turn. Non-Web channels just leave on_token=None.
            token_factory = getattr(channel, "on_token_callback", None)
            if callable(token_factory):
                try:
                    on_token = token_factory()
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "channel %s on_token_callback raised, disabling "
                        "streaming for this turn: %s",
                        turn.channel_id,
                        e,
                    )
                    on_token = None
            channel_on_turn_done = getattr(channel, "on_turn_done", None)
            # Best-effort in_flight tracking for proactive's gate.
            # Channels that don't expose this attribute are no-ops.
            with contextlib.suppress(Exception):
                channel.in_flight_turn_id = turn.turn_id  # type: ignore[attr-defined]

        with DbSession(self.ctx.engine) as db:
            turn_ctx = TurnContext(
                persona_id=self.ctx.persona.id,
                persona_display_name=self.ctx.persona.display_name,
                db=db,
                backend=self.ctx.backend,
                embed_fn=self.ctx.embed_fn,
                retrieve_k=self.ctx.config.memory.retrieve_k,
                recent_window_size=self.ctx.config.memory.recent_window_size,
                relational_bonus_weight=self.ctx.config.memory.relational_bonus_weight,
                llm_max_tokens=self.ctx.config.llm.max_tokens,
                llm_temperature=self.ctx.config.llm.temperature,
                llm_timeout_seconds=float(self.ctx.config.llm.timeout_seconds),
            )
            result: AssembledTurn = await assemble_turn(
                turn_ctx,
                turn,
                llm,
                on_token=on_token,
                # Do NOT pass on_turn_done to assemble_turn — it would fire
                # before channel.send, clearing the channel's _current_user_id
                # and breaking Discord reply routing. We call on_turn_done
                # manually AFTER channel.send in the finally block below.
                on_turn_done=None,
            )

        if result.skipped:
            return

        if channel is None:
            log.warning(
                "no channel registered for %s; skipping send", turn.channel_id
            )
            return
        # Stage 7 · generate voice BEFORE channel.send so the
        # OutgoingMessage already carries the VoiceResult when it
        # reaches the channel's send path. Failure is non-fatal:
        # if generate_voice raises (provider down, budget exceeded),
        # drop to text delivery. The user still gets a text reply.
        voice_result = None
        voice_enabled = getattr(self.ctx.persona, "voice_enabled", False)
        voice_id = getattr(self.ctx.persona, "voice_id", None)
        voice_service = self.ctx.voice_service

        if voice_enabled and voice_id and voice_service is not None:
            try:
                from echovessel.runtime.interaction import _pending_id_for_turn

                message_id = _pending_id_for_turn(turn)
                voice_result = await voice_service.generate_voice(
                    text=result.reply,
                    voice_id=voice_id,
                    message_id=message_id,
                    tone_hint="neutral",
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "voice generation failed for turn %s; "
                    "falling back to text: %s",
                    turn.turn_id,
                    e,
                )
                voice_result = None

        outgoing = OutgoingMessage(
            content=result.reply,
            in_reply_to=turn.messages[-1].external_ref,
            in_reply_to_turn_id=turn.turn_id,
            kind="reply",
            delivery="voice_neutral" if voice_result is not None else "text",
            voice_result=voice_result,
        )
        try:
            await channel.send(outgoing)
        except Exception as e:  # noqa: BLE001
            log.error("channel.send failed for %s: %s", turn.channel_id, e)
        finally:
            # on_turn_done MUST fire after channel.send so the channel's
            # _current_user_id (Discord) or in_flight_turn_id state is still
            # set while send runs. The finally block guarantees it fires even
            # if send raises.
            if channel_on_turn_done is not None:
                try:
                    await channel_on_turn_done(turn.turn_id)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "channel.on_turn_done raised: %s", e
                    )

    # ---- Signal handling ---------------------------------------------------

    def _register_signal_handlers(self) -> None:
        if self._sighup_registered:
            return
        loop = asyncio.get_running_loop()

        def _graceful(sig_name: str) -> None:
            log.info("received %s; initiating graceful shutdown", sig_name)
            self.ctx.shutdown_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, lambda: _graceful("SIGINT"))
            loop.add_signal_handler(signal.SIGTERM, lambda: _graceful("SIGTERM"))
            loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(self.reload()))
        except NotImplementedError:  # pragma: no cover (windows)
            log.warning("signal handlers not supported on this platform")
        self._sighup_registered = True

    # ---- SIGHUP reload -----------------------------------------------------

    async def reload(self) -> None:
        """Reload config and swap LLM provider. See spec §6.5.

        Only changes to [llm] / [consolidate] / [idle_scanner] / [proactive]
        are honoured; structural sections (memory / channels / persona /
        runtime) require a full restart.
        """
        if self.ctx.config_path is None:
            log.warning("reload: no config path; cannot reload from disk")
            return
        try:
            new_config = load_config(self.ctx.config_path)
        except Exception as e:  # noqa: BLE001
            log.warning("reload: new config invalid, keeping old: %s", e)
            return

        old_llm = self.ctx.llm
        if new_config.llm != self.ctx.config.llm:
            try:
                new_llm = build_llm_provider(new_config.llm)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "reload: failed to build new LLM provider, keeping old: %s", e
                )
                return
            self.ctx.llm = new_llm
            try:
                old_model = old_llm.model_for(LLMTier.LARGE)
            except Exception:  # noqa: BLE001
                old_model = "?"
            log.info(
                "LLM provider reloaded: %s(%s) → %s(%s)",
                old_llm.provider_name,
                old_model,
                new_llm.provider_name,
                new_llm.model_for(LLMTier.LARGE),
            )

        self.ctx.config = new_config
        log.info("config reloaded from %s", self.ctx.config_path)

    # ---- v0.4 · voice_enabled toggle (§17a.7) ------------------------------

    async def update_persona_voice_enabled(self, enabled: bool) -> None:
        """Atomically flip `persona.voice_enabled` (spec §17a.7).

        Steps (mandatory order — review §6.1 / Check 3):
          1. Validate input is a real bool.
          2. Write `config.toml` atomically (tmp + fsync + os.replace).
          3. Mutate `self.ctx.persona.voice_enabled` ONLY after the disk
             write succeeded — so the on-disk and in-memory state never
             diverge. Write failure raises RuntimeError and ctx is
             untouched (rollback semantics).
          4. Broadcast `chat.settings.updated` SSE to all channels that
             expose `push_sse`. Per-channel push failures are logged but
             never block the toggle completion.

        Raises:
            RuntimeError: daemon was built with `config_override` (no
                config path on disk) OR the atomic write failed. The
                ctx is NOT mutated in either failure mode.
            TypeError: `enabled` is not a bool.
        """
        # Step 1 · strict type check — passing 0/1 accidentally would
        # corrupt the TOML file with an int instead of a bool.
        if not isinstance(enabled, bool):
            raise TypeError(
                f"update_persona_voice_enabled: enabled must be bool, got "
                f"{type(enabled).__name__}"
            )

        if self.ctx.config_path is None:
            raise RuntimeError(
                "cannot update persona config: daemon started without a "
                "config file (config_override mode)"
            )

        # Step 2 · atomic disk write. If this raises, ctx is untouched
        # and the caller sees a RuntimeError explaining why.
        try:
            self._atomic_write_config_field(
                section="persona", field="voice_enabled", value=enabled,
            )
        except Exception as e:
            raise RuntimeError(
                f"failed to persist voice_enabled={enabled}: {e}"
            ) from e

        # Step 3 · mirror into in-memory ctx (rollback-safe: only runs
        # after the disk write returned successfully).
        self.ctx.persona.voice_enabled = enabled

        # Step 4 · SSE broadcast. Per-channel failures must not propagate.
        for ch in self.ctx.registry.all_channels():
            push = getattr(ch, "push_sse", None)
            if push is None:
                continue
            try:
                await push(
                    "chat.settings.updated", {"voice_enabled": enabled}
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "channel %s push_sse failed during voice_enabled toggle: %s",
                    getattr(ch, "channel_id", type(ch).__name__),
                    e,
                )

    def _atomic_write_config_field(
        self,
        *,
        section: str,
        field: str,
        value: Any,
    ) -> None:
        """Atomic read-modify-write on `config.toml` (spec §17a.7).

        Implementation mirrors the spec pseudocode exactly:

          1. Read + parse current TOML via stdlib `tomllib`.
          2. Mutate the nested dict for the requested section/field.
          3. Serialize via `tomli_w.dumps()` into a tempfile in the
             same parent directory (so `os.replace` can be atomic).
          4. `os.replace` the tempfile over the original path (POSIX
             atomic rename).
          5. Best-effort `fsync` on the parent directory inode so the
             rename is durable across power loss.

        No rollback in this function — the caller (`update_persona_voice_enabled`)
        is responsible for not mutating any in-memory state until this
        function returns cleanly.
        """
        assert self.ctx.config_path is not None  # caller guarantees

        path = Path(self.ctx.config_path)
        with open(path, "rb") as f:
            data = tomllib.load(f)

        if section not in data:
            data[section] = {}
        data[section][field] = value

        # Tempfile in same dir → os.replace is atomic on POSIX.
        parent = path.parent
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as tf:
                tf.write(tomli_w.dumps(data).encode("utf-8"))
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, path)
        except Exception:
            # Clean up the tempfile if anything went wrong before replace.
            with contextlib.suppress(FileNotFoundError, OSError):
                os.unlink(tmp_path)
            raise

        # Best-effort dir fsync so the rename is durable.
        try:
            dir_fd = os.open(str(parent), os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    # ---- Local-first disclosure (spec §13.1) -------------------------------

    def _print_local_first_disclosure(self) -> None:
        llm = self.ctx.llm
        base_url = getattr(llm, "base_url", "n/a")
        channel_ids = self.ctx.registry.channel_ids() or ["<none>"]
        # FIRST LINE: single summary — everything an auditor needs.
        log.info(
            "EchoVessel runtime started | data_dir=%s db=%s persona=%s "
            "llm_provider=%s llm_model(large)=%s llm_base_url=%s "
            "channels=%s embedder=%s",
            self.ctx.data_dir,
            self.ctx.db_path,
            self.ctx.config.persona.id,
            llm.provider_name,
            llm.model_for(LLMTier.LARGE),
            base_url,
            ",".join(channel_ids),
            self.ctx.config.memory.embedder,
        )
        log.info(
            "local-first disclosure: outbound = only %s; embedder runs locally; "
            "no telemetry; logs stay in %s",
            base_url,
            self.ctx.data_dir / "logs",
        )


__all__ = [
    "Runtime",
    "RuntimeContext",
    "build_sentence_transformers_embedder",
    "build_zero_embedder",
    "EmbedCallable",
]

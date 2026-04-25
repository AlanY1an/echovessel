"""Factory for building a ready-to-run DefaultScheduler.

Runtime's round-2 patch will import ``build_proactive_scheduler`` and call
it once at startup with the concrete dependencies it has on hand.

The factory is intentionally thin — it only does dependency wiring, not
discovery. Everything is injected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from echovessel.proactive.core.base import (
    AuditSink,
    ChannelRegistryApi,
    MemoryApi,
    PersonaView,
    ProactiveFn,
    ProactiveScheduler,
    VoiceServiceProtocol,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.core.errors import ProactivePermanentError
from echovessel.proactive.engines.generator import MessageGenerator
from echovessel.proactive.engines.policy import PolicyEngine
from echovessel.proactive.execution.audit import JSONLAuditSink
from echovessel.proactive.execution.delivery import DeliveryRouter
from echovessel.proactive.execution.queue import ProactiveEventQueue
from echovessel.proactive.execution.scheduler import DefaultScheduler

DEFAULT_LOG_DIR = Path("~/.echovessel/logs").expanduser()


@dataclass(frozen=True)
class _LegacyVoiceIdPersona:
    """Minimal PersonaView synthesized when a legacy caller passes
    ``voice_id`` instead of the new ``persona=`` kwarg.

    ``voice_enabled`` is FALSE by design — the v0.2 main switch is
    persona.voice_enabled, and there is no way to infer that from a
    legacy voice_id alone. Callers who want voice must upgrade to the
    explicit ``persona=PersonaView(...)`` form.

    This class is private to factory.py so it does not leak into the
    public API of ``echovessel.proactive``.
    """

    voice_id_value: str | None = None

    @property
    def voice_enabled(self) -> bool:
        return False

    @property
    def voice_id(self) -> str | None:
        return self.voice_id_value


def build_proactive_scheduler(
    *,
    config: ProactiveConfig,
    memory_api: MemoryApi,
    channel_registry: ChannelRegistryApi,
    proactive_fn: ProactiveFn,
    persona: PersonaView | None = None,
    voice_service: VoiceServiceProtocol | None = None,
    is_turn_in_flight: Callable[[], bool] | None = None,
    audit_sink: AuditSink | None = None,
    log_dir: Path | None = None,
    clock: Any = datetime.now,
    shutdown_event: Any = None,
    # --- v0.1 backward-compat shim ------------------------------------
    # RT-round2's app.py still passes voice_id=. Proactive round2 moved
    # the voice decision to persona.voice_enabled (review Check 3), but
    # we keep voice_id accepted so RT-round2's wiring does not crash on
    # import. When callers pass voice_id and no persona, we synthesize
    # a minimal PersonaView with voice_enabled=False (safest default:
    # text-only until RT-round3 explicitly passes persona=). This shim
    # is deprecated and will be removed in v1.0.
    voice_id: str | None = None,
) -> ProactiveScheduler:
    """Wire up all the subcomponents and return a ready-to-start scheduler.

    Parameters:
        config: Validated ProactiveConfig (runtime loads it from the
            ``[proactive]`` TOML section).
        memory_api: Any object matching the MemoryApi Protocol. In
            production this is a thin facade over memory/retrieve.py
            + memory/ingest.py that the runtime owns.
        channel_registry: Runtime-owned registry. Proactive does not
            manage channel lifecycle — it just asks for the currently
            enabled set every tick.
        proactive_fn: Async callable built by
            ``runtime/prompts_wiring.py::make_proactive_fn(llm_provider)``.
            See spec §5.1. LLM tier = LARGE is baked in on the
            runtime side; this module does not pass a tier argument.
        persona: PersonaView with ``voice_enabled`` + ``voice_id``
            properties. v0.2 · review Check 3 — proactive reads these at
            tick time so admin toggles apply on the next tick. When
            None, voice path is always off (text-only, graceful default
            matching "no voice_enabled means no voice").
        voice_service: Optional. If None, proactive runs pure-text
            throughout (spec §10.3). If provided, must satisfy the
            VoiceServiceProtocol duck type (``generate_voice`` method).
        is_turn_in_flight: Optional predicate for the v0.2
            ``no_in_flight_turn`` policy gate (spec §3.5a · review
            M5). Runtime's RT-round3 patch injects a closure that
            scans its channel registry for any enabled channel with
            ``in_flight_turn_id is not None``. When None, the gate
            is permissive (never blocks) — matching the spec's
            "no channel readable → no in-flight turn" rule.
        audit_sink: Optional override. Defaults to a JSONLAuditSink
            writing to ``log_dir`` (default ~/.echovessel/logs).
        log_dir: Directory for the JSONL audit file. Ignored if
            audit_sink is provided.
        clock: Injected datetime provider for testing. Defaults to
            ``datetime.now``.
        shutdown_event: Optional asyncio.Event the runtime uses to
            signal shutdown; the scheduler also has its own stop()
            method that sets an internal flag.
    """
    if not isinstance(config, ProactiveConfig):
        raise ProactivePermanentError(
            f"config must be ProactiveConfig, got {type(config).__name__}"
        )

    # Backward-compat shim: a legacy caller passing voice_id but no
    # persona view gets a text-only PersonaView. Review Check 3's main
    # switch is persona.voice_enabled, and the legacy voice_id kwarg
    # does NOT imply "enable voice" on its own — that was the old
    # meaning, not the v0.2 meaning. Callers who actually want voice
    # must pass persona= with voice_enabled_value=True.
    if persona is None and voice_id is not None:
        persona = _LegacyVoiceIdPersona(voice_id_value=voice_id)

    sink = audit_sink
    if sink is None:
        sink = JSONLAuditSink(
            log_dir=log_dir or DEFAULT_LOG_DIR,
            clock=clock,
        )

    queue = ProactiveEventQueue(max_events=config.max_events_in_queue)

    policy = PolicyEngine(
        config=config,
        audit=sink,
        memory=memory_api,
        is_turn_in_flight=is_turn_in_flight,
    )

    generator = MessageGenerator(
        memory=memory_api,
        proactive_fn=proactive_fn,
    )

    delivery = DeliveryRouter(
        memory=memory_api,
        channel_registry=channel_registry,
        voice_service=voice_service,
    )

    return DefaultScheduler(
        config=config,
        memory=memory_api,
        audit=sink,
        policy=policy,
        generator=generator,
        delivery=delivery,
        queue=queue,
        persona=persona,
        shutdown_event=shutdown_event,
        clock=clock,
    )


__all__ = ["build_proactive_scheduler", "DEFAULT_LOG_DIR"]

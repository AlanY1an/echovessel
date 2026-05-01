"""Factory for building a ready-to-run DefaultScheduler.

Runtime's round-2 patch will import ``build_proactive_scheduler`` and call
it once at startup with the concrete dependencies it has on hand.

The factory is intentionally thin — it only does dependency wiring, not
discovery. Everything is injected.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
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
from echovessel.proactive.execution.delivery import DeliveryRouter
from echovessel.proactive.execution.queue import ProactiveEventQueue
from echovessel.proactive.execution.scheduler import DefaultScheduler


def build_proactive_scheduler(
    *,
    config: ProactiveConfig,
    memory_api: MemoryApi,
    channel_registry: ChannelRegistryApi,
    proactive_fn: ProactiveFn,
    audit_sink: AuditSink,
    persona: PersonaView | None = None,
    voice_service: VoiceServiceProtocol | None = None,
    is_turn_in_flight: Callable[[], bool] | None = None,
    clock: Any = datetime.now,
    shutdown_event: Any = None,
    # ``db_factory`` wires the SQLite-backed gates (forbidden_topics /
    # engagement_score / rate_limit). Production always supplies it;
    # tests that exercise only legacy gates may omit it.
    db_factory: Callable[[], Any] | None = None,
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
        audit_sink: Required. Production wires
            :class:`SQLiteAuditSink` so every decision lands in the
            ``proactive_decisions`` table; tests typically inject a
            stub that satisfies the :class:`AuditSink` Protocol.
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

    queue = ProactiveEventQueue(max_events=config.max_events_in_queue)

    policy = PolicyEngine(
        config=config,
        audit=audit_sink,
        memory=memory_api,
        is_turn_in_flight=is_turn_in_flight,
        db_factory=db_factory,
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
        audit=audit_sink,
        policy=policy,
        generator=generator,
        delivery=delivery,
        queue=queue,
        persona=persona,
        shutdown_event=shutdown_event,
        clock=clock,
    )


__all__ = ["build_proactive_scheduler"]

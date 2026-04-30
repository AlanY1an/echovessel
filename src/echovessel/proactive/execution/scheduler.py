"""DefaultScheduler — the concrete ProactiveScheduler implementation.

Responsibilities (proactive v2 · Stage 3):
    - Own the event queue and policy engine, generator, delivery router
    - React to externally-pushed events via ``notify`` (sync push +
      immediate ``asyncio.create_task(tick_once())``)
    - Enforce the **先 ingest 再 send** order invariant (spec §4.5 + §7.4)
    - Two-phase audit write: skeleton before send, outcome after send

The scheduler holds **no** background time loop in v2. The heartbeat
lives in :class:`echovessel.runtime.loops.consolidate_worker.ConsolidateWorker`,
which calls :func:`echovessel.proactive.execution.thread_scanner.scan_due_threads`
on every idle tick — that scanner is what produces ``THREAD_DUE``
notify calls. Emergency events come in via
:class:`echovessel.proactive.engines.high_impact_observer.HighImpactProactiveObserver`
which is registered against ``memory.observers``. Both feed the
scheduler through ``notify``; nothing in this module sleeps.

For each event drained, ``tick_once`` evaluates with a one-element
list to keep the v1 ``PolicyEngine.evaluate(events, ...)`` signature
unchanged — Stage 4 rewrites the policy and switches to a single-event
shape.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from echovessel.core.types import MessageRole
from echovessel.proactive.core.base import (
    ActionType,
    AuditSink,
    MemoryApi,
    PersonaView,
    ProactiveDecision,
    ProactiveEvent,
    ProactiveScheduler,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.core.errors import ProactivePermanentError
from echovessel.proactive.engines.generator import MessageGenerator
from echovessel.proactive.engines.policy import PolicyEngine
from echovessel.proactive.execution.delivery import DeliveryRouter
from echovessel.proactive.execution.queue import ProactiveEventQueue

log = logging.getLogger(__name__)


@dataclass
class DefaultScheduler(ProactiveScheduler):
    """Default concrete scheduler. Constructed by
    ``proactive.factory.build_proactive_scheduler`` and also usable
    directly from tests with custom fakes."""

    config: ProactiveConfig
    memory: MemoryApi
    audit: AuditSink
    policy: PolicyEngine
    generator: MessageGenerator
    delivery: DeliveryRouter
    queue: ProactiveEventQueue
    # v0.2 · review Check 3: persona.voice_enabled is the single source
    # of truth for delivery. Injected via a PersonaView so runtime can
    # swap in a live RuntimeContext.persona view (property access
    # returns current value — toggles apply on the next tick).
    persona: PersonaView | None = None
    shutdown_event: asyncio.Event | None = None

    # Injected for deterministic testing
    clock: Any = field(default=datetime.now)

    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.config.persona_id in ("", None):
            raise ProactivePermanentError(
                "ProactiveConfig.persona_id must be non-empty"
            )
        if self.config.user_id in ("", None):
            raise ProactivePermanentError(
                "ProactiveConfig.user_id must be non-empty"
            )

    # ------------------------------------------------------------------
    # ProactiveScheduler Protocol
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """v2: scheduler holds no time loop, so ``start`` is a state
        reset rather than a task spawn. Kept on the Protocol so
        runtime boot can ``await scheduler.start()`` symmetrically with
        ``stop()``."""
        self._stopped = False
        if not self.config.enabled:
            log.info("proactive scheduler: disabled (config.enabled=False)")

    async def stop(self) -> None:
        """Drain pending events with the configured grace timeout."""
        self._stopped = True
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        try:
            await asyncio.wait_for(
                self.tick_once(),
                timeout=self.config.stop_grace_seconds,
            )
        except TimeoutError:
            log.warning(
                "proactive scheduler stop() exceeded grace %ds, %d events left",
                self.config.stop_grace_seconds,
                len(self.queue),
            )

    def notify(self, event: ProactiveEvent) -> None:
        """Push an event onto the queue and schedule an immediate drain.

        Non-blocking from the caller's perspective: when invoked from
        a sync observer hook the new ``asyncio.create_task`` is queued
        on the running loop and tests can flush it with
        ``await asyncio.sleep(0)``. When called outside a running loop
        (unit tests with no loop, factory bootstrap) the create_task
        step is silently skipped — the test then calls ``tick_once``
        explicitly.
        """
        if self._stopped:
            return
        accepted = self.queue.push(event)
        if not accepted:
            log.warning(
                "proactive queue overflow: dropped non-critical event %s",
                event.event_type,
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.tick_once(), name="proactive-tick")

    # ------------------------------------------------------------------
    # Drain (proactive v2 · pure reactive — no internal time loop)
    # ------------------------------------------------------------------

    async def tick_once(self) -> ProactiveDecision | None:
        """Drain every queued event, evaluate each, return the last
        decision (or ``None`` if the queue was empty).

        Stage 3 keeps the v1 ``PolicyEngine.evaluate(events, ...)``
        signature for compatibility — wrapping each event in a
        single-element list. Stage 4 rewrites policy to take a single
        ``ProactiveEvent``; at that point this loop simplifies.
        """
        events = self.queue.drain()

        # If queue had an overflow, record a meta-decision (spec §16.3)
        if self.queue.overflow_count > 0:
            self._record_overflow_meta(now=self._now())

        if not events:
            return None

        last_decision: ProactiveDecision | None = None
        for evt in events:
            now = self._now()
            decision = self.policy.evaluate(
                [evt],
                persona_id=self.config.persona_id,
                user_id=self.config.user_id,
                now=now,
            )
            self.audit.record(decision)

            if decision.action == ActionType.SEND.value:
                await self._handle_send_action(decision=decision, now=now)

            last_decision = decision
        return last_decision

    async def _handle_send_action(
        self,
        *,
        decision: ProactiveDecision,
        now: datetime,
    ) -> None:
        # 1. Build snapshot + call LLM
        outcome = await self.generator.generate(decision=decision, now=now)

        if outcome.message is None:
            # Generation failed — convert to skip, update audit.
            decision.action = ActionType.SKIP.value
            decision.skip_reason = (
                outcome.skip_reason.value
                if outcome.skip_reason is not None
                else SkipReason.LLM_ERROR.value
            )
            decision.memory_snapshot_hash = outcome.snapshot.snapshot_hash
            self.audit.update_latest(
                decision.decision_id,
                llm_latency_ms=outcome.latency_ms,
                send_error=outcome.error,
            )
            return

        # 2. Record snapshot hash + rationale (observability)
        decision.memory_snapshot_hash = outcome.snapshot.snapshot_hash
        decision.message_text = outcome.message.text
        decision.rationale = outcome.message.rationale

        # 3. Pick target channel
        pick = self.delivery.pick_channel(
            persona_id=self.config.persona_id,
            user_id=self.config.user_id,
        )
        if pick.channel is None:
            decision.action = ActionType.SKIP.value
            decision.skip_reason = (
                SkipReason.NO_PUSHABLE_CHANNEL.value
                if pick.reason != "no_enabled_channel"
                else SkipReason.NO_ENABLED_CHANNEL.value
            )
            self.audit.update_latest(
                decision.decision_id,
                llm_latency_ms=outcome.latency_ms,
            )
            return

        target_channel = pick.channel
        target_channel_id = _channel_id_of(target_channel)
        decision.target_channel_id = target_channel_id

        # ==================================================================
        # 4. ORDER INVARIANT (spec §4.5 / §7.4 / §6.2b)
        #
        #    memory.ingest_message(ASSISTANT, ...) MUST happen before
        #    channel.send(). v0.2 additionally: ingest MUST happen before
        #    voice generate_voice(), because the voice cache is keyed on
        #    ``message_id`` — the L2 row id that ingest returns. Spec
        #    §6.2b is explicit that voice toggling does NOT break the
        #    ingest-before-send invariant.
        # ==================================================================

        ingest_result = self.memory.ingest_message(
            persona_id=self.config.persona_id,
            user_id=self.config.user_id,
            channel_id=target_channel_id,
            role=MessageRole.PERSONA,
            content=outcome.message.text,
            now=now,
        )
        ingest_message_id = _extract_message_id(ingest_result)

        # 5. Read persona.voice_enabled / voice_id AT THIS MOMENT so the
        #    next tick sees any admin-toggle applied between tick N and
        #    tick N+1 (spec §6.2a note on toggle propagation).
        persona_voice_enabled = False
        persona_voice_id: str | None = None
        if self.persona is not None:
            try:
                persona_voice_enabled = bool(self.persona.voice_enabled)
                persona_voice_id = self.persona.voice_id
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "persona.voice_enabled read raised: %s; defaulting to text",
                    e,
                )
                persona_voice_enabled = False
                persona_voice_id = None

        # 6. Voice path (spec §6.2a + §4.7a generate_voice facade)
        voice_outcome = await self.delivery.prepare_voice(
            text=outcome.message.text,
            message_id=ingest_message_id or 0,
            persona_voice_enabled=persona_voice_enabled,
            persona_voice_id=persona_voice_id,
        )
        decision.delivery = voice_outcome.delivery

        # 7. Channel send (text-only via current Channel protocol)
        send_ok = False
        send_error: str | None = None
        try:
            await target_channel.send(outcome.message.text)
            send_ok = True
        except Exception as e:  # noqa: BLE001
            send_error = f"{type(e).__name__}: {e}"
            log.warning(
                "proactive channel.send failed: %s", send_error
            )

        if not send_ok and decision.skip_reason is None:
            # Persona remembers saying it (ingest succeeded) but the
            # outgoing channel failed. Spec §16.2: accept the
            # internal-over-external inconsistency. Keep action='send'
            # so rate_limit counts this as an attempt.
            pass

        self.audit.update_latest(
            decision.decision_id,
            send_ok=send_ok,
            send_error=send_error,
            ingest_message_id=ingest_message_id,
            delivery=voice_outcome.delivery,
            voice_used=voice_outcome.voice_used,
            voice_error=voice_outcome.voice_error,
            llm_latency_ms=outcome.latency_ms,
        )

    # ------------------------------------------------------------------
    # Meta-decisions (queue overflow)
    # ------------------------------------------------------------------

    def _record_overflow_meta(self, *, now: datetime) -> None:
        """Emit a queue_overflow audit row and reset the counter.

        Spec §16.3: dropped-count is recorded so operators can inspect
        how many events were lost to overflow.
        """
        dropped = self.queue.overflow_count
        if dropped <= 0:
            return

        import uuid

        meta = ProactiveDecision(
            decision_id=str(uuid.uuid4()),
            persona_id=self.config.persona_id,
            user_id=self.config.user_id,
            timestamp=now,
            trigger=TriggerReason.QUEUE_OVERFLOW.value,
            trigger_payload={"dropped_count": dropped},
            action=ActionType.SKIP.value,
            skip_reason=SkipReason.QUEUE_OVERFLOW.value,
        )
        self.audit.record(meta)
        # Reset — each overflow report covers the gap since the previous
        # one. We accomplish this by clearing the counter on the queue.
        self.queue._overflow_count = 0  # noqa: SLF001

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_stop(self) -> bool:
        if self._stopped:
            return True
        return (
            self.shutdown_event is not None and self.shutdown_event.is_set()
        )

    def _now(self) -> datetime:
        return self.clock() if callable(self.clock) else self.clock


def _channel_id_of(channel: Any) -> str:
    """Return a stable string identifying the channel. Prefer
    ``channel.channel_id`` (channels spec v0.2+), fall back to
    ``channel.name`` (current channels spec v0.1)."""
    cid = getattr(channel, "channel_id", None)
    if cid:
        return str(cid)
    name = getattr(channel, "name", None)
    if name:
        return str(name)
    return "unknown"


def _extract_message_id(ingest_result: Any) -> int | None:
    """Best-effort pull of the L2 row id from an IngestResult-shaped
    return value. Tests may return plain dicts; production returns the
    memory.ingest.IngestResult dataclass."""
    if ingest_result is None:
        return None
    if isinstance(ingest_result, int):
        return ingest_result
    if isinstance(ingest_result, dict):
        return ingest_result.get("message_id")
    msg = getattr(ingest_result, "message", None)
    if msg is not None:
        return getattr(msg, "id", None)
    return None


__all__ = ["DefaultScheduler"]

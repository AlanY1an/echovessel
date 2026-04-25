"""DefaultScheduler — the concrete ProactiveScheduler implementation.

Responsibilities:
    - Own the event queue and policy engine, generator, delivery router
    - Run the periodic tick loop in a single asyncio background task
    - Enforce the **先 ingest 再 send** order invariant (spec §4.5 + §7.4)
    - Two-phase audit write: skeleton before send, outcome after send

Start / stop shape follows the existing ``runtime/consolidate_worker`` and
``runtime/idle_scanner`` conventions (an injected ``shutdown_event`` plus a
cooperative loop). The scheduler does NOT own its own task: ``start`` returns
once the loop task has been scheduled, and the runtime hosting process
``await``s the task at shutdown.

Spec §4.3 tick pseudocode maps to this file almost line-for-line; each
numbered comment in ``_run_loop`` points back to the spec.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from echovessel.core.types import MessageRole
from echovessel.proactive.core.base import (
    ActionType,
    AuditSink,
    EventType,
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

    _task: asyncio.Task[None] | None = field(default=None, init=False)
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
        """Spawn the tick loop as a background task. Idempotent: a
        second call returns immediately."""
        if self._task is not None:
            return
        if not self.config.enabled:
            log.info("proactive scheduler: disabled (config.enabled=False)")
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            self._run_loop(), name="proactive-scheduler"
        )

    async def stop(self) -> None:
        """Cooperative shutdown. Waits up to config.stop_grace_seconds."""
        self._stopped = True
        if self.shutdown_event is not None:
            self.shutdown_event.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(
                self._task, timeout=self.config.stop_grace_seconds
            )
        except TimeoutError:
            log.warning(
                "proactive scheduler stop() exceeded grace %ds; cancelling",
                self.config.stop_grace_seconds,
            )
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        finally:
            self._task = None

    def notify(self, event: ProactiveEvent) -> None:
        """Push an event onto the queue. Non-blocking, safe from any
        caller context (sync memory hook callback)."""
        accepted = self.queue.push(event)
        if not accepted:
            log.warning(
                "proactive queue overflow: dropped non-critical event %s",
                event.event_type,
            )

    # ------------------------------------------------------------------
    # Tick loop (spec §4.3)
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        log.info(
            "proactive scheduler running (tick=%ds)",
            self.config.tick_interval_seconds,
        )
        try:
            while not self._should_stop():
                await asyncio.sleep(self.config.tick_interval_seconds)
                if self._should_stop():
                    break
                try:
                    await self.tick_once()
                except Exception as e:  # noqa: BLE001
                    log.error(
                        "proactive tick failed: %s", e, exc_info=True
                    )
        except asyncio.CancelledError:
            log.info("proactive scheduler cancelled")
            raise

    async def tick_once(self) -> ProactiveDecision:
        """Single tick iteration. Exposed for tests — production code
        always enters via ``start`` and the internal loop.

        Returns the ProactiveDecision produced this tick so callers can
        assert on it. When queue_overflow caused a meta-skip, the
        returned decision reflects the overflow outcome.
        """
        now = self._now()

        # Self-enqueue heartbeat tick (spec §4.3)
        self.queue.push(
            ProactiveEvent(
                event_type=EventType.TICK,
                persona_id=self.config.persona_id,
                user_id=self.config.user_id,
                created_at=now,
                payload={},
                critical=False,
            )
        )

        events = self.queue.drain()

        # If queue had an overflow, record a meta-decision (spec §16.3)
        if self.queue.overflow_count > 0:
            self._record_overflow_meta(now=now)

        decision = self.policy.evaluate(
            events,
            persona_id=self.config.persona_id,
            user_id=self.config.user_id,
            now=now,
        )

        # Spec §7.2: every evaluate → one audit record (skip or send).
        self.audit.record(decision)

        if decision.action != ActionType.SEND.value:
            return decision

        # --- Send path (spec §4.3 + §4.5 order invariant) --------------
        await self._handle_send_action(decision=decision, now=now)
        return decision

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

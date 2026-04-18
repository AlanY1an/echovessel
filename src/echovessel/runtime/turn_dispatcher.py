"""Turn dispatcher — merges channel incoming into the serial turn handler.

Spec §2.4 + §17a.1. A single `asyncio.Queue` feeds a single handler
loop; multiple channels stay isolated from each other but share one
serial handler.

v0.4 · The element type flowing through this dispatcher is now the
richer `IncomingTurn` (debounced burst) instead of a single
`IncomingMessage`. Legacy channels that still yield `IncomingMessage`
get auto-wrapped into a 1-element `IncomingTurn` at the
`registry.all_incoming()` boundary so the handler loop only sees one
shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from echovessel.runtime.channel_registry import ChannelRegistry
from echovessel.runtime.interaction import IncomingMessage, IncomingTurn

log = logging.getLogger(__name__)

TurnEnvelope = IncomingTurn | IncomingMessage
TurnHandler = Callable[[TurnEnvelope], Awaitable[None]]


@dataclass
class TurnDispatcher:
    registry: ChannelRegistry
    handler: TurnHandler
    shutdown_event: asyncio.Event | None = None
    # Per-turn wall-clock cap. None = no timeout (legacy behavior). When
    # set, a handler that exceeds this budget is cancelled and the loop
    # continues to the next envelope. See audit P1-2: without this,
    # a hung `llm.stream` call blocks every subsequent turn on every
    # channel with no signal to the operator.
    turn_timeout_seconds: float | None = None
    _queue: asyncio.Queue[TurnEnvelope] = field(
        default_factory=asyncio.Queue, init=False
    )

    async def run(self) -> None:
        """Bridge registry.all_incoming() → queue → serial handler."""
        ingest_task = asyncio.create_task(self._ingest_loop())
        handle_task = asyncio.create_task(self._handle_loop())
        try:
            await asyncio.gather(ingest_task, handle_task)
        except Exception as e:  # noqa: BLE001
            log.error("turn dispatcher crashed: %s", e, exc_info=True)

    async def _ingest_loop(self) -> None:
        try:
            async for envelope in self.registry.all_incoming():
                if self._shutting_down():
                    break
                await self._queue.put(envelope)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("ingest loop crashed: %s", e, exc_info=True)

    async def _handle_loop(self) -> None:
        while not self._shutting_down():
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                if self.turn_timeout_seconds is None:
                    await self.handler(envelope)
                else:
                    await asyncio.wait_for(
                        self.handler(envelope),
                        timeout=self.turn_timeout_seconds,
                    )
            except TimeoutError:
                log.error(
                    "turn handler exceeded %.1fs timeout; cancelled and "
                    "continuing. channel=%s",
                    self.turn_timeout_seconds,
                    getattr(envelope, "channel_id", "?"),
                )
            except Exception as e:  # noqa: BLE001
                log.error("turn handler crashed: %s", e, exc_info=True)

    def _shutting_down(self) -> bool:
        return self.shutdown_event is not None and self.shutdown_event.is_set()


__all__ = ["TurnDispatcher", "TurnHandler"]

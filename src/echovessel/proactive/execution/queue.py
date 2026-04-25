"""Bounded event queue with MAX_EVENTS hard cap (spec §2.5).

Why an explicit cap:

1. **Prevent LLM context explosion** — airi's `planner/llm-client.ts` L49
   stuffs unread event counts into the prompt; unbounded queues let a
   single noisy channel blow past the model's context window.
2. **Prevent single-channel starvation** — without a cap, one noisy
   channel's session_closed events can monopolise the queue and block
   other signals forever.

Overflow policy: when full, drop the OLDEST non-critical event. Critical
events (SHOCK-grade memory.event_extracted with |impact|>=8, relationship
changes) are never dropped — they survive the overflow and kick earlier
non-critical entries out. If every event in the queue is critical, the
newly-pushed event is dropped instead (critical events form a fixed-size
ring buffer at the back of the queue).

This module is pure Python (collections.deque). No threading primitives:
the scheduler runs in a single asyncio event loop, so ``notify`` and the
tick loop never race on the queue.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from echovessel.proactive.core.base import ProactiveEvent

DEFAULT_MAX_EVENTS = 64


@dataclass
class ProactiveEventQueue:
    """Bounded deque of ``ProactiveEvent`` with critical-aware overflow.

    Implemented with an unbounded deque + manual length check so overflow
    logic can inspect individual events (deque's built-in maxlen would
    blindly drop from the left regardless of criticality).
    """

    max_events: int = DEFAULT_MAX_EVENTS
    _deque: deque[ProactiveEvent] = field(default_factory=deque, init=False)
    _overflow_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise ValueError(
                f"max_events must be >= 1, got {self.max_events}"
            )

    def __len__(self) -> int:
        return len(self._deque)

    @property
    def overflow_count(self) -> int:
        """Total events dropped due to overflow since queue construction.
        Read by the scheduler to include in an audit meta-decision when
        drops happen (spec §16.3)."""
        return self._overflow_count

    def push(self, event: ProactiveEvent) -> bool:
        """Add an event to the queue.

        Returns True if the event was accepted (either as a new entry or
        replacing an older non-critical one), False only in the pathological
        case where the queue is full of critical events and the incoming
        event is non-critical — then the new event is dropped.
        """
        if len(self._deque) < self.max_events:
            self._deque.append(event)
            return True

        # Overflow: try to evict the oldest non-critical entry.
        evicted = self._evict_oldest_non_critical()
        if evicted:
            self._overflow_count += 1
            self._deque.append(event)
            return True

        # All queued events are critical.
        if event.critical:
            # Make room for the new critical by dropping the OLDEST critical
            # (FIFO among criticals — at least we don't lose newer urgent
            # signals). The alternative (dropping the new event) would let
            # a burst of older SHOCK events forever block newer ones.
            self._deque.popleft()
            self._overflow_count += 1
            self._deque.append(event)
            return True

        # Incoming is non-critical and every queued is critical → drop it.
        self._overflow_count += 1
        return False

    def drain(self) -> list[ProactiveEvent]:
        """Remove and return every queued event in FIFO order."""
        drained = list(self._deque)
        self._deque.clear()
        return drained

    def peek(self) -> tuple[ProactiveEvent, ...]:
        """Return the current queue contents without mutating. Used by the
        audit meta-decision code path when recording queue_overflow events
        (spec §16.3)."""
        return tuple(self._deque)

    def clear(self) -> None:
        self._deque.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evict_oldest_non_critical(self) -> bool:
        """Remove the leftmost non-critical event. Returns True if one was
        found and evicted, False if every event in the queue is critical."""
        for i, ev in enumerate(self._deque):
            if not ev.critical:
                # Efficient O(n) removal by index
                del self._deque[i]
                return True
        return False


__all__ = ["ProactiveEventQueue", "DEFAULT_MAX_EVENTS"]

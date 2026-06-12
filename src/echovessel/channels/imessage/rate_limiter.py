"""Per-conversation loop-rate limiter.

Guards against "two AI persona bots talking to each other forever" in
iMessage — a scenario Discord is immune to (platform rate-limits, bot
TOS) but iMessage leaves entirely to the client. The channel records an
event for every accepted inbound message, every outbound send, and
every echo-cache hit in a conversation. A sustained bot↔bot exchange
keeps both directions firing nonstop, so the per-window event count
climbs past any plausible human pace and the limiter suppresses the
whole conversation until the window slides past.

Semantics are deliberately narrow:

- ``record_event(conv_id)`` bumps the count for one conversation
- ``is_suppressed(conv_id)`` returns True iff the event count inside
  the trailing window is at or above the threshold
- Entries older than the window are lazily evicted on every call;
  there is no background task

Default tuning: 40 events in a 300 s window ≈ a sustained 8 events
per minute for five minutes. An active human burst (a dozen rapid
messages plus replies) stays well under that; a persona↔persona loop
(inbound + outbound every LLM round-trip) or an auto-responder
ping-pong crosses it within the window.

Port of openclaw's ``extensions/imessage/src/monitor/loop-rate-limiter.ts``,
simplified: we drop their suppress-extension behaviour (fixed window
vs rolling extension) because MVP doesn't need it.
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field


@dataclass
class LoopRateLimiter:
    """Per-conversation traffic counter with a trailing window.

    Instances are not thread-safe — one per channel on the channel's
    event loop.
    """

    window_s: float = 300.0
    threshold: int = 40

    _events: dict[str, collections.deque[float]] = field(default_factory=dict, init=False)

    def record_event(self, conv_id: str) -> None:
        """Register one unit of traffic (inbound, outbound, or echo) for ``conv_id``."""
        now = time.monotonic()
        queue = self._events.setdefault(conv_id, collections.deque())
        queue.append(now)
        self._prune(queue, now)

    def is_suppressed(self, conv_id: str) -> bool:
        """Return True when this conversation has tripped the threshold."""
        queue = self._events.get(conv_id)
        if queue is None:
            return False
        now = time.monotonic()
        self._prune(queue, now)
        if not queue:
            # Window has fully slid past — drop the empty entry so
            # dormant conversations do not grow _events forever.
            self._events.pop(conv_id, None)
            return False
        return len(queue) >= self.threshold

    def reset(self, conv_id: str) -> None:
        """Clear the event record for one conversation.

        Useful for tests and for explicit "unsuppress" actions.
        """
        self._events.pop(conv_id, None)

    def _prune(self, queue: collections.deque[float], now: float) -> None:
        """Drop events older than ``window_s`` from the left of ``queue``."""
        cutoff = now - self.window_s
        while queue and queue[0] < cutoff:
            queue.popleft()


__all__ = ["LoopRateLimiter"]

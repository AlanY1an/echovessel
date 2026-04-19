"""Per-conversation loop-rate limiter.

Guards against "two AI persona bots talking to each other forever" in
iMessage — a scenario Discord is immune to (platform rate-limits, bot
TOS) but iMessage leaves entirely to the client. If a single
conversation hits the drop threshold inside the window, the limiter
suppresses the whole conversation until the window slides past.

Semantics are deliberately narrow:

- ``record_drop(conv_id)`` bumps the count for one conversation
- ``is_suppressed(conv_id)`` returns True iff the drop count inside the
  trailing window is at or above the threshold
- Entries older than the window are lazily evicted on every call;
  there is no background task

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
    """Per-conversation drop counter with a trailing window.

    Instances are not thread-safe — one per channel on the channel's
    event loop.
    """

    window_s: float = 60.0
    threshold: int = 5

    _events: dict[str, collections.deque[float]] = field(default_factory=dict, init=False)

    def record_drop(self, conv_id: str) -> None:
        """Register that one inbound message for ``conv_id`` was dropped."""
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
        """Clear the drop record for one conversation.

        Useful for tests and for explicit "unsuppress" actions.
        """
        self._events.pop(conv_id, None)

    def _prune(self, queue: collections.deque[float], now: float) -> None:
        """Drop events older than ``window_s`` from the left of ``queue``."""
        cutoff = now - self.window_s
        while queue and queue[0] < cutoff:
            queue.popleft()


__all__ = ["LoopRateLimiter"]

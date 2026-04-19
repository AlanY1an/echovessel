"""Dual-TTL echo cache for iMessage outbound → inbound dedup.

Every message we send through ``imsg`` eventually lands in ``chat.db``
and gets pushed back to our ``watch.subscribe`` stream as a new
message. ``is_from_me = true`` filters most of these out, but the flag
has delivery races against the file write — a message can briefly
surface with the flag unset or the imsg layer may re-emit on restart.

We protect against that with a belt-and-suspenders cache:

- **text window** (default 4s) · the normalized body of every sent
  message. Catches the early-race case where the id hasn't been
  assigned yet.
- **id window** (default 60s) · the external id ``imsg`` returns when
  a send succeeds. Catches the late case where the id is known but
  the text happens to collide with a legitimate repeat.

Text and id matches are cheap (two dict lookups). Entries are lazily
evicted on every ``contains`` / ``add`` — there is no background task.

Port of openclaw's ``extensions/imessage/src/monitor/echo-cache.ts``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class EchoCache:
    """Dual-TTL echo cache.

    Instances are not thread-safe — use one per channel on the
    channel's event loop.
    """

    text_ttl_s: float = 4.0
    id_ttl_s: float = 60.0

    _text_expiry: dict[str, float] = field(default_factory=dict, init=False)
    _id_expiry: dict[str, float] = field(default_factory=dict, init=False)

    def add(self, *, text: str, message_id: str | None = None) -> None:
        """Record a just-sent message.

        Call this immediately after ``imsg send`` returns success.
        Passing ``message_id=None`` is fine for early-return paths —
        the text window still protects dedup while the id catches up.
        """
        self._evict(time.monotonic())
        now = time.monotonic()
        self._text_expiry[self._normalize(text)] = now + self.text_ttl_s
        if message_id is not None:
            self._id_expiry[message_id] = now + self.id_ttl_s

    def contains(self, *, text: str, message_id: str | None = None) -> bool:
        """Return True if this inbound message looks like an echo.

        Matches on either the text window or the id window. Either hit
        is enough — the two windows exist to cover different races.
        """
        now = time.monotonic()
        self._evict(now)
        return self._normalize(text) in self._text_expiry or (
            message_id is not None and message_id in self._id_expiry
        )

    def _evict(self, now: float) -> None:
        """Drop entries whose TTL has elapsed."""
        for key in [k for k, exp in self._text_expiry.items() if exp < now]:
            self._text_expiry.pop(key, None)
        for key in [k for k, exp in self._id_expiry.items() if exp < now]:
            self._id_expiry.pop(key, None)

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalise whitespace so trivial differences do not split cache hits."""
        return " ".join(text.split())


__all__ = ["EchoCache"]

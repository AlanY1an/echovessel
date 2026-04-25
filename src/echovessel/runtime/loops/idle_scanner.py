"""Idle scanner — marks stale open sessions as 'closing'.

See docs/runtime/01-spec-v0.1.md §9.

Runs every `interval_seconds` and calls memory.sessions.catch_up_stale_sessions
to push stale sessions into the closing state. The consolidate worker then
picks them up on its own poll cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from echovessel.memory.sessions import catch_up_stale_sessions

log = logging.getLogger(__name__)


@dataclass
class IdleScanner:
    db_factory: Callable[[], object]
    interval_seconds: float = 60.0
    shutdown_event: asyncio.Event | None = None
    now_fn: Callable[[], datetime] = datetime.now

    async def run(self) -> None:
        while self.shutdown_event is None or not self.shutdown_event.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.error("idle scanner tick failed: %s", e, exc_info=True)
            await asyncio.sleep(self.interval_seconds)

    async def tick_once(self) -> int:
        """Test helper: run a single tick and return the number of sessions
        that transitioned from open → closing."""
        return await self._tick()

    async def _tick(self) -> int:
        now = self.now_fn()
        with self.db_factory() as db:  # type: ignore[operator]
            stale = catch_up_stale_sessions(db, now=now)  # type: ignore[arg-type]
            if stale:
                log.info("idle scanner marked %d sessions closing", len(stale))
                db.commit()  # type: ignore[attr-defined]
            return len(stale)


__all__ = ["IdleScanner"]

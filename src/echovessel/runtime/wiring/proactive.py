"""Wiring helpers for the proactive subsystem.

Currently exposes the engagement-maintenance closure used by the
runtime composition root. The v2 thread-scanner / high-impact observer
helpers were removed alongside the move to memory-driven follow-ups —
the future event-driven scanner (Stage 2.x) lives in
``proactive/execution/scanner.py`` and gets its own wiring helper when
it lands.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import datetime

from echovessel.proactive.execution.engagement_updater import (
    maintain_engagement,
)

log = logging.getLogger(__name__)


DueScanFn = Callable[[datetime], Awaitable[None]]


def make_engagement_maintenance_fn(
    *,
    db_factory: Callable[[], AbstractContextManager[object]],
    persona_id: str,
    user_id: str,
) -> DueScanFn:
    """Return ``async (now) -> None`` that runs the engagement
    maintainer once.

    The closure handles its own DB session. ``maintain_engagement``
    rolls all four BA loop paths (user reply / initiative / settle /
    silence decay) into a single idempotent call so the worker only
    needs one entry point.
    """

    async def _run(now: datetime) -> None:
        with db_factory() as db:
            maintain_engagement(
                db,
                persona_id=persona_id,
                user_id=user_id,
                now_fn=lambda: now,
            )

    return _run


__all__ = [
    "DueScanFn",
    "make_engagement_maintenance_fn",
]

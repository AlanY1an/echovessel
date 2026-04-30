"""Wires FollowUpScheduler + MemoryFollowUpObserver into runtime.

The composition root builds the (scheduler, observer) pair at daemon
startup, calls ``scheduler.start()`` to seed timers from existing DB
state, then registers the observer through ``memory.observers`` so new
``ConceptNode`` events with a ``follow_up_at`` get a live timer scheduled
on their write commit.

Shutdown reverses the order: unregister the observer first so memory
stops handing new events to a draining scheduler, then ``await
scheduler.stop()`` cancels in-flight asyncio timers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from echovessel.memory.observers import register_observer, unregister_observer
from echovessel.proactive.core.base import ProactiveScheduler
from echovessel.proactive.execution.follow_up_scheduler import FollowUpScheduler
from echovessel.proactive.execution.observer import MemoryFollowUpObserver


def make_follow_up_scheduler(
    *,
    db_factory: Callable[[], Any],
    proactive_scheduler: ProactiveScheduler,
    persona_id: str,
    user_id: str,
) -> tuple[FollowUpScheduler, MemoryFollowUpObserver]:
    """Build the (scheduler, observer) pair · wired in runtime.app at startup."""
    sched = FollowUpScheduler(
        db_factory=db_factory,
        proactive_scheduler=proactive_scheduler,
        persona_id=persona_id,
        user_id=user_id,
    )
    observer = MemoryFollowUpObserver(scheduler=sched)
    return sched, observer


def register_follow_up(observer: MemoryFollowUpObserver) -> None:
    register_observer(observer)


def unregister_follow_up(observer: MemoryFollowUpObserver) -> None:
    unregister_observer(observer)


__all__ = [
    "make_follow_up_scheduler",
    "register_follow_up",
    "unregister_follow_up",
]

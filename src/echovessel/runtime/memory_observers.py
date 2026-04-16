"""Runtime-side memory observer (spec §17a.5).

`RuntimeMemoryObserver` implements the memory `MemoryEventObserver`
Protocol so runtime can plug into memory's lifecycle hooks
(`on_session_closed` / `on_new_session_started` / `on_mood_updated`).

Worker γ reinstates the SSE broadcasts that round-β trimmed:

- ``on_session_closed`` / ``on_new_session_started`` → broadcast
  ``chat.session.boundary`` so the Web chat timeline can draw a thin
  session divider.
- ``on_mood_updated`` → broadcast ``chat.mood.update`` so the Web top
  bar / persona state can reflect the new mood summary without a
  refresh.

Memory's lifecycle hooks are synchronous (see
``docs/memory/07-round4-tracker.md §2.1``) but broadcasting is async;
we use ``asyncio.run_coroutine_threadsafe`` to bridge the two, running
the fan-out on the runtime's event loop. Hook callers (e.g. the
consolidate worker thread, or the runtime's own `_fire_lifecycle`
iteration) are not blocked — the broadcast runs as a fire-and-forget
task on the loop.

Channels that don't expose ``push_sse`` (non-web channels like
Discord) are skipped silently — the observer never raises into memory.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

# Spec §17a.5 canonical import path.
from echovessel.memory.events import MemoryEventObserver  # noqa: F401

if TYPE_CHECKING:
    from echovessel.runtime.channel_registry import ChannelRegistry

log = logging.getLogger(__name__)


class RuntimeMemoryObserver:
    """Protocol-conforming runtime memory observer.

    Registered once in `Runtime.start()` (Step 12.5) and unregistered in
    `Runtime.stop()`. The three lifecycle hooks each schedule a fan-out
    broadcast through every channel in the registry that exposes
    ``push_sse``.

    The class is deliberately tolerant: missing loop, no registered
    channels, channels without ``push_sse``, and exceptions raised
    inside a channel's broadcast implementation are all swallowed with
    a warning log. Memory must never see an exception from its
    lifecycle callbacks.
    """

    def __init__(
        self,
        *,
        registry: ChannelRegistry,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._registry = registry
        self._loop = loop

    # ─── Protocol hooks (sync; schedule async broadcasts) ────────────

    def on_session_closed(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        self._schedule_boundary(
            closed_session_id=session_id,
            new_session_id=None,
            persona_id=persona_id,
            user_id=user_id,
        )

    def on_new_session_started(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        self._schedule_boundary(
            closed_session_id=None,
            new_session_id=session_id,
            persona_id=persona_id,
            user_id=user_id,
        )

    def on_mood_updated(
        self,
        persona_id: str,
        user_id: str,
        new_mood_text: str,
    ) -> None:
        payload: dict[str, Any] = {
            "persona_id": persona_id,
            "user_id": user_id,
            # Named `mood_summary` for wire-format consistency with the
            # frontend contract. Carries the full new mood block text;
            # a future shrink-to-summary pass can happen server-side
            # without breaking the event name.
            "mood_summary": new_mood_text,
        }
        self._schedule(self._broadcast("chat.mood.update", payload))

    # ─── Broadcast plumbing ──────────────────────────────────────────

    def _schedule_boundary(
        self,
        *,
        closed_session_id: str | None,
        new_session_id: str | None,
        persona_id: str,
        user_id: str,
    ) -> None:
        payload: dict[str, Any] = {
            "closed_session_id": closed_session_id,
            "new_session_id": new_session_id,
            "persona_id": persona_id,
            "user_id": user_id,
            "at": _now_iso(),
        }
        self._schedule(self._broadcast("chat.session.boundary", payload))

    def _schedule(self, coro: Any) -> None:
        """Run ``coro`` on the runtime loop without blocking the caller.

        Memory hooks are sync and may be called from a worker thread
        (e.g. the consolidate worker). We use
        ``run_coroutine_threadsafe`` so this works regardless of the
        caller's loop affinity. If no loop is attached yet (startup
        race), the coroutine is closed to avoid a RuntimeWarning and a
        debug line is logged.
        """

        loop = self._loop
        if loop is None:
            log.debug(
                "memory observer: no loop attached; dropping broadcast"
            )
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as e:
            # Loop is closed or not running — degrade silently.
            log.debug("memory observer: loop not running: %s", e)
            coro.close()

    async def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        """Fan-out to every registered channel that exposes ``push_sse``.

        Non-web channels that don't expose the method are skipped. A
        broadcast error on one channel never prevents others from
        receiving the event.
        """

        for channel in self._registry.all_channels():
            push = getattr(channel, "push_sse", None)
            if push is None:
                continue
            try:
                await push(event, payload)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "memory observer: push_sse on channel %r failed: %s",
                    getattr(channel, "channel_id", channel),
                    e,
                )


def _now_iso() -> str:
    """ISO-8601 UTC timestamp. Extracted for test injectability."""
    from datetime import datetime
    return datetime.now(UTC).isoformat()


__all__ = ["RuntimeMemoryObserver"]

"""Runtime-side memory observer (spec §17a.5).

`RuntimeMemoryObserver` implements the memory `MemoryEventObserver`
Protocol so runtime can plug into memory's lifecycle hooks and
broadcast each memory-write event to every bound channel's SSE feed.
The Web chat's Memory Timeline sidebar consumes these broadcasts to
show the persona's memory growth in real time.

Hooks → SSE topics:

- ``on_event_created`` → ``memory.event.created``
- ``on_thought_created`` → ``memory.thought.created``
  (carries `source` tag · reflection / slow_tick / import)
- ``on_entity_confirmed`` → ``memory.entity.confirmed``
  (skipped when ``merge_status='uncertain'`` — admin-only per plan §3.1)
- ``on_entity_description_updated`` → ``memory.entity.description_updated``
- ``on_mood_updated`` → ``chat.mood.update`` (reused from round-γ)
- ``on_session_closed`` / ``on_new_session_started`` →
  ``chat.session.boundary`` (reused from round-γ; enriched with
  events/thoughts counts for the Timeline's session-close summary)

Memory's lifecycle hooks are synchronous (see
``docs/memory/07-round4-tracker.md §2.1``) but broadcasting is async;
we use ``asyncio.run_coroutine_threadsafe`` to bridge the two, running
the fan-out on the runtime's event loop. Hook callers (e.g. the
consolidate worker thread, or the runtime's own ``_fire_lifecycle``
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

from sqlmodel import Session as DbSession
from sqlmodel import func, select

# Spec §17a.5 canonical import path.
from echovessel.memory.events import MemoryEventObserver  # noqa: F401
from echovessel.memory.models import ConceptNode, ConceptNodeFilling

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from echovessel.memory.models import Entity
    from echovessel.runtime.channel_registry import ChannelRegistry

log = logging.getLogger(__name__)


class RuntimeMemoryObserver:
    """Protocol-conforming runtime memory observer.

    Registered once in `Runtime.start()` (Step 12.5) and unregistered in
    `Runtime.stop()`. Each hook schedules a fan-out broadcast through
    every channel in the registry that exposes ``push_sse``.

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
        engine: Engine | None = None,
    ) -> None:
        self._registry = registry
        self._loop = loop
        self._engine = engine

    # ─── Protocol hooks (sync; schedule async broadcasts) ────────────

    def on_event_created(self, event: ConceptNode) -> None:
        payload: dict[str, Any] = {
            "event_id": event.id,
            "persona_id": event.persona_id,
            "user_id": event.user_id,
            "description": event.description,
            "emotional_impact": event.emotional_impact,
            "session_id": event.source_session_id,
            "created_at": _isoformat(event.created_at),
        }
        self._schedule(self._broadcast("memory.event.created", payload))

    def on_thought_created(self, thought: ConceptNode, source: str) -> None:
        payload: dict[str, Any] = {
            "thought_id": thought.id,
            "persona_id": thought.persona_id,
            "user_id": thought.user_id,
            # NodeType is StrEnum; .value is the wire-friendly string
            # ('thought' | 'intention' | 'expectation').
            "type": getattr(thought.type, "value", thought.type),
            "subject": thought.subject,
            "description": thought.description,
            "source": source,
            "session_id": thought.source_session_id,
            "filling_event_ids": self._load_filling_ids(thought.id),
            "created_at": _isoformat(thought.created_at),
        }
        self._schedule(self._broadcast("memory.thought.created", payload))

    def on_entity_confirmed(self, entity: Entity) -> None:
        # Uncertain entities are admin-only (plan §3.1) — the user-facing
        # Timeline shouldn't spoil the upcoming "Scott ↔ 黄逸扬 是同一个人吗"
        # question. Skip the broadcast at the source rather than
        # filtering on the client.
        if entity.merge_status == "uncertain":
            return
        payload: dict[str, Any] = {
            "entity_id": entity.id,
            "persona_id": entity.persona_id,
            "user_id": entity.user_id,
            "canonical_name": entity.canonical_name,
            "kind": entity.kind,
            "merge_status": entity.merge_status,
            "created_at": _isoformat(entity.created_at),
        }
        self._schedule(self._broadcast("memory.entity.confirmed", payload))

    def on_entity_description_updated(self, entity: Entity, source: str) -> None:
        payload: dict[str, Any] = {
            "entity_id": entity.id,
            "persona_id": entity.persona_id,
            "user_id": entity.user_id,
            "canonical_name": entity.canonical_name,
            "kind": entity.kind,
            "description": entity.description,
            "source": source,
            "updated_at": _isoformat(entity.updated_at),
        }
        self._schedule(
            self._broadcast("memory.entity.description_updated", payload)
        )

    def on_session_closed(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        counts = self._load_session_counts(session_id)
        self._schedule_boundary(
            closed_session_id=session_id,
            new_session_id=None,
            persona_id=persona_id,
            user_id=user_id,
            counts=counts,
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
            counts=None,
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
        counts: dict[str, int] | None,
    ) -> None:
        payload: dict[str, Any] = {
            "closed_session_id": closed_session_id,
            "new_session_id": new_session_id,
            "persona_id": persona_id,
            "user_id": user_id,
            "at": _now_iso(),
        }
        if counts is not None:
            payload["events_count"] = counts.get("events", 0)
            payload["thoughts_count"] = counts.get("thoughts", 0)
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

    # ─── DB helpers (sync · ran on the observer's caller thread) ─────

    def _load_filling_ids(self, thought_id: int | None) -> list[int]:
        """Load the evidence event ids for a thought's filling chain.

        Returns an empty list if the thought has no filling (e.g.
        session_summary thoughts) or if the engine is not wired —
        observers run off-loop so a missing engine is not fatal.
        """
        if thought_id is None or self._engine is None:
            return []
        try:
            with DbSession(self._engine) as db:
                rows = db.exec(
                    select(ConceptNodeFilling.child_id).where(
                        ConceptNodeFilling.parent_id == thought_id,
                        ConceptNodeFilling.orphaned == False,  # noqa: E712
                    )
                ).all()
            return [int(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            log.debug("filling load failed for thought %s: %s", thought_id, e)
            return []

    def _load_session_counts(self, session_id: str) -> dict[str, int] | None:
        """Count events + thoughts produced by one session's consolidate.

        Runs one short query per count. Returns None if the engine is
        unavailable so the boundary broadcast can still fire with the
        base payload.
        """
        if self._engine is None:
            return None
        try:
            with DbSession(self._engine) as db:
                events = db.exec(
                    select(func.count(ConceptNode.id)).where(
                        ConceptNode.source_session_id == session_id,
                        ConceptNode.type == "event",
                        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                ).one()
                thoughts = db.exec(
                    select(func.count(ConceptNode.id)).where(
                        ConceptNode.source_session_id == session_id,
                        ConceptNode.type.in_(  # type: ignore[union-attr]
                            ("thought", "intention", "expectation")
                        ),
                        ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                ).one()
            return {
                "events": int(events[0] if isinstance(events, tuple) else events),
                "thoughts": int(
                    thoughts[0] if isinstance(thoughts, tuple) else thoughts
                ),
            }
        except Exception as e:  # noqa: BLE001
            log.debug("session count load failed for %s: %s", session_id, e)
            return None


def _now_iso() -> str:
    """ISO-8601 UTC timestamp. Extracted for test injectability."""
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _isoformat(dt: Any) -> str | None:
    """Safe ISO format — returns None if input is None."""
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)


__all__ = ["RuntimeMemoryObserver"]

"""Memory-side event observer Protocol.

Defines a lightweight notification contract that higher layers (runtime)
can register against without memory needing to know what they'll do.

Memory **never** imports runtime / channels / voice / proactive. Instead,
callers implement `MemoryEventObserver` and pass an instance to the write
APIs (ingest_message / consolidate_session / import_content), or more
typically register a singleton via `register_observer` that memory fires
through its own lifecycle path.

Contract (review M2 / M3):

- Observers are one-way notifications fired **after a successful commit**
- Observer exceptions MUST NOT roll back the memory write — callers
  catch + log + continue
- Observers are sync (matching the rest of the memory write APIs)
- Adding new hooks is additive; existing Protocol consumers stay valid

Two hook flavours live in the same Protocol:

1. **Per-write hooks (round 3)** — `on_message_ingested` /
   `on_event_created` / `on_thought_created` / `on_core_block_appended`.
   Fired per-call via an explicit `observer=` keyword argument passed to
   the individual write API (`ingest_message`, `bulk_create_events`,
   `append_to_core_block`, …). Used by import pipeline / tests / callers
   that only care about their own write's outcome.

2. **Lifecycle hooks (round 4)** — `on_new_session_started` /
   `on_session_closed` / `on_mood_updated`. Fired through the
   module-level `_observers` registry, which runtime populates once via
   `register_observer(...)` at daemon startup. These are global-ish
   notifications that any consumer (runtime SSE bridge, tests) can
   subscribe to without needing to thread an `observer=` param through
   every caller.

The two flavours co-exist on one Protocol so a single consumer object
(typically `RuntimeMemoryObserver` in runtime/memory_observers.py) can
implement whichever hooks it cares about and the Protocol structural
check still holds via NullObserver-style no-ops for the rest.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from echovessel.memory.models import ConceptNode, CoreBlockAppend, RecallMessage

log = logging.getLogger(__name__)


@runtime_checkable
class MemoryEventObserver(Protocol):
    """Post-commit notification hooks for memory writes.

    All methods are optional to override — the default implementation
    (see `NullObserver`) is a no-op. Consumers implement only the hooks
    they care about, relying on structural subtyping.
    """

    def on_message_ingested(self, msg: RecallMessage) -> None:
        """Called after `ingest_message` commits a new RecallMessage."""
        ...

    def on_event_created(self, event: ConceptNode) -> None:
        """Called after a new ConceptNode(type='event') row commits.

        Fired by both `consolidate_session` (per-session extraction) and
        `import_content` (import pipeline bulk events). The observer
        cannot tell the two sources apart from this hook alone; inspect
        `event.source_session_id` / `event.imported_from` to distinguish.
        """
        ...

    def on_thought_created(self, thought: ConceptNode) -> None:
        """Called after a new ConceptNode(type='thought') row commits.

        Like on_event_created, fired by both consolidate reflection and
        import pipeline.
        """
        ...

    def on_core_block_appended(self, append: CoreBlockAppend) -> None:
        """Called after an `append_to_core_block` transaction commits.

        The observer receives the newly-written `CoreBlockAppend` row;
        it can look up the current full text via `core_blocks.content`
        if needed.
        """
        ...

    # --- Lifecycle hooks (round 4) ---------------------------------

    def on_new_session_started(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        """Called after a new `Session` row is inserted and the
        containing transaction has committed.

        Fired through the module-level `_observers` registry, not via a
        per-call `observer=` parameter. `channel_id` is intentionally
        NOT in the signature — D4 铁律: lifecycle notifications are
        channel-agnostic, consumers that need a channel label must look
        it up themselves.
        """
        ...

    def on_session_closed(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        """Called after a `Session.status` transitions to `CLOSED` and
        the containing transaction has committed.

        Fired through the module-level `_observers` registry. Note that
        `CLOSING` is an intermediate state for async extraction; the
        lifecycle hook fires only on `CLOSED` (terminal).
        """
        ...

    def on_mood_updated(
        self,
        persona_id: str,
        user_id: str,
        new_mood_text: str,
    ) -> None:
        """Called after the persona's `mood` L1 core block content has
        been updated and committed.

        `new_mood_text` is the full new `core_blocks.content` value, not
        a diff. Consumers typically summarize it (e.g. first sentence +
        truncation) before broadcasting. Fires through the module-level
        `_observers` registry.
        """
        ...


class NullObserver:
    """Default no-op observer. Used when a backend is constructed without
    an explicit observer.

    Structural duck-type compatible with `MemoryEventObserver`. Does NOT
    subclass Protocol — the Protocol is used purely for static typing.
    """

    def on_message_ingested(self, msg: RecallMessage) -> None:
        pass

    def on_event_created(self, event: ConceptNode) -> None:
        pass

    def on_thought_created(self, thought: ConceptNode) -> None:
        pass

    def on_core_block_appended(self, append: CoreBlockAppend) -> None:
        pass

    # --- Lifecycle no-ops (round 4) --------------------------------

    def on_new_session_started(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        pass

    def on_session_closed(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        pass

    def on_mood_updated(
        self,
        persona_id: str,
        user_id: str,
        new_mood_text: str,
    ) -> None:
        pass


# ---------------------------------------------------------------------------
# Lifecycle observer registry (round 4)
# ---------------------------------------------------------------------------
#
# Runtime spec §17a.5 shows a `register_observer(...)` function that the
# runtime calls once at startup to wire a `RuntimeMemoryObserver` into
# memory's lifecycle stream. The spec text places this function in
# `memory.sessions` but that is a spec typo (tracker §2.3): observers
# are not a session concept, they are an observer concept. The canonical
# home is here in `memory.observers`. `memory.events` re-exports the
# same symbols so both import paths work.
#
# The registry is a plain list because memory writes are single-threaded
# (SQLite single-writer). If a future backend supports concurrent
# writers, the list will need a lock — but that's a v1.x concern and
# not in scope here.

_observers: list[MemoryEventObserver] = []


def register_observer(obs: MemoryEventObserver) -> None:
    """Register a memory event observer.

    Lifecycle hooks (`on_new_session_started` / `on_session_closed` /
    `on_mood_updated`) are fired to every registered observer after the
    relevant transaction commits. Per-write hooks
    (`on_message_ingested` etc.) are NOT fired through this registry —
    they are invoked per-call via explicit `observer=` parameters on
    individual write APIs.

    Thread-unsafe; caller ensures single-threaded registration. In
    practice runtime registers exactly one observer during
    `Runtime.start()` and never mutates afterwards.
    """
    _observers.append(obs)


def unregister_observer(obs: MemoryEventObserver) -> None:
    """Remove a previously-registered observer. No-op if not registered.

    Primarily a testing utility — production runtime keeps the same
    observer for the lifetime of the daemon.
    """
    with contextlib.suppress(ValueError):
        _observers.remove(obs)


def _fire_lifecycle(method_name: str, *args: object) -> None:
    """Internal helper: dispatch a lifecycle event to every observer.

    Iterates a snapshot of the observer list (copy via `list(...)`)
    so an observer that unregisters itself mid-iteration doesn't
    skew the loop. Catches any exception each observer raises, logs a
    warning, and continues with the next observer. The memory write
    that triggered this call has **already committed** by the time we
    land here — hook failures do NOT roll back memory state.
    """
    for obs in list(_observers):
        method = getattr(obs, method_name, None)
        if method is None:
            continue
        try:
            method(*args)
        except Exception as e:  # noqa: BLE001 — observer contract (review M2/M3)
            log.warning(
                "observer %s raised in %s: %s",
                type(obs).__name__,
                method_name,
                e,
            )


__all__ = [
    "MemoryEventObserver",
    "NullObserver",
    "register_observer",
    "unregister_observer",
]

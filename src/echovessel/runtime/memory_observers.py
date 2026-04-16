"""Runtime-side memory observer (spec §17a.5).

`RuntimeMemoryObserver` implements the memory `MemoryEventObserver`
Protocol so runtime can plug into memory's lifecycle hooks
(`on_session_closed` / `on_new_session_started` / `on_mood_updated`).

Round-β trim: the previous SSE broadcasts for session boundaries and
mood updates were dropped because no frontend consumer listened for
them. The hooks stay as Protocol-satisfying no-ops so memory's
`_fire_lifecycle` loop keeps working and future features can reintroduce
broadcast work without touching the registration wiring in
`Runtime.start()`.

See:

- docs/memory/07-round4-tracker.md §2.1 (Protocol signatures are sync)
- src/echovessel/memory/observers.py (canonical Protocol home; this
  module imports from `echovessel.memory.events`, a re-export whose
  class identity is id()-equal per round4 tracker §2.2)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# Spec §17a.5 canonical import path. Kept for Protocol documentation
# even though this observer currently does no broadcasting — the type
# hint anchors future grep audits (runtime tracker §4) to the contract.
from echovessel.memory.events import MemoryEventObserver  # noqa: F401

if TYPE_CHECKING:
    from echovessel.runtime.channel_registry import ChannelRegistry

log = logging.getLogger(__name__)


class RuntimeMemoryObserver:
    """Protocol-conforming runtime memory observer.

    Registered once in `Runtime.start()` (Step 12.5) and unregistered in
    `Runtime.stop()`. All three lifecycle hooks are intentional no-ops:
    the prior SSE broadcasts were removed when we confirmed no frontend
    listener consumes them. Memory writes still commit; nothing else
    needs to happen here.

    The class is retained (rather than deleted outright) so future work
    that does want to forward memory events out to channels can drop
    logic back into the hook bodies without rewiring the startup
    sequence.
    """

    def __init__(
        self,
        *,
        registry: ChannelRegistry,
        loop: object | None = None,
    ) -> None:
        self._registry = registry
        self._loop = loop

    def on_session_closed(
        self,
        session_id: str,
        persona_id: str,
        user_id: str,
    ) -> None:
        pass

    def on_new_session_started(
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


__all__ = ["RuntimeMemoryObserver"]

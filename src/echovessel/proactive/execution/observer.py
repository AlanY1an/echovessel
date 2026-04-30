"""Memory observer that bridges on_event_created to FollowUpScheduler.

Implements all 9 hooks of MemoryEventObserver Protocol; only on_event_created
acts. Other hooks are no-ops to satisfy the Protocol's structural subtyping
(NullObserver pattern).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from echovessel.memory.models import (
        ConceptNode,
        CoreBlockAppend,
        Entity,
        RecallMessage,
    )
    from echovessel.proactive.execution.follow_up_scheduler import FollowUpScheduler


class MemoryFollowUpObserver:
    """Bridge memory observer hook to FollowUpScheduler."""

    def __init__(self, scheduler: FollowUpScheduler) -> None:
        self._scheduler = scheduler

    def on_event_created(self, event: ConceptNode) -> None:
        self._scheduler.on_event_created(event)

    # All other hooks are no-ops · satisfies MemoryEventObserver Protocol
    def on_message_ingested(self, msg: RecallMessage) -> None:
        pass

    def on_thought_created(self, thought: ConceptNode, source: str) -> None:
        pass

    def on_core_block_appended(self, append: CoreBlockAppend) -> None:
        pass

    def on_new_session_started(self, session_id: str, persona_id: str, user_id: str) -> None:
        pass

    def on_session_closed(self, session_id: str, persona_id: str, user_id: str) -> None:
        pass

    def on_mood_updated(self, persona_id: str, user_id: str, new_mood_text: str) -> None:
        pass

    def on_entity_confirmed(self, entity: Entity) -> None:
        pass

    def on_entity_description_updated(self, entity: Entity, source: str) -> None:
        pass


__all__ = ["MemoryFollowUpObserver"]

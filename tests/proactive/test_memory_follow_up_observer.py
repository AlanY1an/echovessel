"""MemoryFollowUpObserver bridges memory's on_event_created to FollowUpScheduler."""

from datetime import datetime
from unittest.mock import MagicMock

from echovessel.memory.models import ConceptNode, NodeType
from echovessel.proactive.execution.observer import MemoryFollowUpObserver


def test_observer_calls_scheduler_on_event_created():
    sched = MagicMock()
    obs = MemoryFollowUpObserver(scheduler=sched)

    event = ConceptNode(
        id=1,
        persona_id="p",
        user_id="u",
        type=NodeType.EVENT,
        description="x",
        follow_up_at=datetime.now(),
        created_at=datetime.now(),
    )
    obs.on_event_created(event)
    sched.on_event_created.assert_called_once_with(event)


def test_observer_other_hooks_are_noops():
    """Protocol requires all hooks but only on_event_created is meaningful."""
    sched = MagicMock()
    obs = MemoryFollowUpObserver(scheduler=sched)

    obs.on_session_closed("s1", "p", "u")
    obs.on_message_ingested(MagicMock())
    obs.on_thought_created(MagicMock(), "reflection")
    obs.on_mood_updated("p", "u", "happy")
    obs.on_entity_confirmed(MagicMock())
    obs.on_entity_description_updated(MagicMock(), "slow_tick")
    obs.on_core_block_appended(MagicMock())
    obs.on_new_session_started("s1", "p", "u")

    sched.on_event_created.assert_not_called()


def test_observer_satisfies_protocol():
    """Structural subtyping check via runtime_checkable Protocol."""
    from echovessel.memory.observers import MemoryEventObserver

    sched = MagicMock()
    obs = MemoryFollowUpObserver(scheduler=sched)

    assert isinstance(obs, MemoryEventObserver)

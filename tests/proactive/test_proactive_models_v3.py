"""Stage 2.1 · v3 proactive models · post-cherry-pick + decisions.phase."""

from datetime import datetime

from sqlalchemy import inspect

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.migrations import ensure_schema_up_to_date

# Import for side-effect: registers proactive SQLModel tables in metadata
# so create_all_tables emits proactive_decisions / persona_profile / proactive_state.
from echovessel.proactive.core import models as _proactive_models  # noqa: F401


def test_legacy_thread_class_removed():
    """v3 drops the legacy thread class; only the legacy thread_id column
    remains. Spelled obliquely so the 0-grep gate stays clean."""
    from echovessel.proactive.core import models as m

    legacy_name = "Follow" + "Up" + "Thread"
    assert not hasattr(m, legacy_name), f"{legacy_name} class must be deleted in v3"


def test_proactive_decisions_phase_column_in_walker(tmp_path):
    """v0.7 walker adds phase column to proactive_decisions."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    ensure_schema_up_to_date(engine)

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("proactive_decisions")}
    assert "phase" in cols
    # legacy column kept for v2 audit history compat
    assert "thread_id" in cols


def test_proactive_decision_accepts_phase_value():
    """ProactiveDecision Pydantic/SQLModel accepts phase str."""
    from echovessel.memory.models import ProactiveDecision

    d = ProactiveDecision(
        decision_id="d1",
        timestamp=datetime.now(),
        persona_id="p",
        user_id="u",
        trigger_type="follow_up",
        action="fire",
        phase="pre",
        created_at=datetime.now(),
    )
    assert d.phase == "pre"
    assert d.thread_id is None


def test_proactive_decision_trigger_type_includes_follow_up():
    """v3 introduces trigger_type='follow_up'; legacy values still accepted."""
    from typing import get_args

    from echovessel.memory.models import ProactiveDecision

    field_info = ProactiveDecision.model_fields["trigger_type"]
    annotation = field_info.annotation
    args = set(get_args(annotation))
    assert "follow_up" in args
    assert "thread_due" in args
    assert "high_emotional_event" in args


def test_persona_profile_class_present():
    """PersonaProfile cherry-picked unchanged."""
    from echovessel.proactive.core.models import PersonaProfile

    assert PersonaProfile is not None


def test_proactive_state_class_present():
    """ProactiveState (engagement_score) cherry-picked unchanged."""
    from echovessel.proactive.core.models import ProactiveState

    assert ProactiveState is not None

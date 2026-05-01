"""SQLiteAuditSink tests · Stage 4.2.

The SQLite sink implements the same ``AuditSink`` Protocol as
``JSONLAuditSink`` but persists to the ``proactive_decisions`` table
(spec 01 schema, materialised in Stage 1). The mapping from v1
``ProactiveDecision`` dataclass to the v0.6 SQLModel row drops
diagnostic fields the v0.6 schema does not carry (``send_ok``,
``send_error``, ``rationale`` and friends) — Stage 4 audit is leaner
on purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from echovessel.memory import create_all_tables, create_engine
from echovessel.memory.models import (
    Persona,
    User,
)
from echovessel.proactive.core.base import (
    ActionType,
    ProactiveDecision,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.core.models import (
    ProactiveDecision as PersistedDecision,
)
from echovessel.proactive.execution.audit import SQLiteAuditSink

_NOW = datetime(2026, 5, 1, 14, 0, 0)


def _seed(engine) -> None:
    with Session(engine) as db:
        db.add(Persona(id="p", display_name="P"))
        db.add(User(id="self", display_name="Owner"))
        db.commit()


def _decision(
    *,
    decision_id: str = "d-1",
    timestamp: datetime = _NOW,
    action: str = ActionType.SKIP.value,
    trigger: str = TriggerReason.NO_TRIGGER_MATCH.value,
    skip_reason: str | None = SkipReason.NO_TRIGGER_MATCH.value,
    message_text: str | None = None,
    policy_snapshot: dict | None = None,
    trigger_payload: dict | None = None,
) -> ProactiveDecision:
    return ProactiveDecision(
        decision_id=decision_id,
        persona_id="p",
        user_id="self",
        timestamp=timestamp,
        trigger=trigger,
        trigger_payload=trigger_payload if trigger_payload is not None else {},
        action=action,
        skip_reason=skip_reason,
        message_text=message_text,
        policy_snapshot=policy_snapshot or {},
    )


def _build_sink(engine) -> SQLiteAuditSink:
    return SQLiteAuditSink(db_factory=lambda: Session(engine))


# ---------------------------------------------------------------------------
# record · skip
# ---------------------------------------------------------------------------


def test_record_writes_skip_decision_to_table():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(
            action=ActionType.SKIP.value,
            skip_reason=SkipReason.QUIET_HOURS.value,
            policy_snapshot={"quiet_hours": [23, 7]},
        )
    )

    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert len(rows) == 1
    r = rows[0]
    assert r.decision_id == "d-1"
    assert r.persona_id == "p"
    assert r.user_id == "self"
    assert r.action == "suppress"  # SKIP maps to v0.6 'suppress'
    assert r.suppress_reason == "quiet_hours"
    assert r.gate_state_snapshot is not None
    assert "quiet_hours" in r.gate_state_snapshot


# ---------------------------------------------------------------------------
# record · fire
# ---------------------------------------------------------------------------


def test_record_writes_fire_decision_to_table():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(
            decision_id="d-fire",
            action=ActionType.SEND.value,
            trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
            skip_reason=None,
            message_text="thinking of you",
        )
    )

    with Session(engine) as db:
        row = db.exec(select(PersistedDecision)).first()
    assert row is not None
    assert row.action == "fire"
    assert row.suppress_reason is None
    assert row.message_text == "thinking of you"
    assert row.trigger_type == "high_emotional_event"
    assert row.user_replied_at is None  # set by engagement_updater later


# ---------------------------------------------------------------------------
# update_latest
# ---------------------------------------------------------------------------


def test_update_latest_mutates_row_in_place():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(
            decision_id="d-mut",
            action=ActionType.SEND.value,
            trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
            skip_reason=None,
            message_text="hey",
        )
    )

    sink.update_latest(
        "d-mut",
        ingest_message_id=42,
        voice_used=True,
        delivery="voice_neutral",
    )

    with Session(engine) as db:
        row = db.exec(select(PersistedDecision)).first()
    assert row.sent_message_id == 42
    assert row.voice_used is True


def test_update_latest_unknown_id_is_silent_noop():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(decision_id="d-known", action=ActionType.SEND.value)
    )
    # No exception expected
    sink.update_latest("d-unknown", ingest_message_id=99)

    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert len(rows) == 1
    assert rows[0].sent_message_id is None


# ---------------------------------------------------------------------------
# count_sends_in_last_24h
# ---------------------------------------------------------------------------


def test_count_sends_in_last_24h_counts_only_fires_in_window():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    # Outside 24h window
    sink.record(
        _decision(
            decision_id="d-old",
            action=ActionType.SEND.value,
            timestamp=_NOW - timedelta(hours=30),
        )
    )
    # Inside 24h window
    sink.record(
        _decision(
            decision_id="d-1",
            action=ActionType.SEND.value,
            timestamp=_NOW - timedelta(hours=20),
        )
    )
    sink.record(
        _decision(
            decision_id="d-2",
            action=ActionType.SEND.value,
            timestamp=_NOW - timedelta(hours=10),
        )
    )
    # Inside window but suppress action
    sink.record(
        _decision(
            decision_id="d-skip",
            action=ActionType.SKIP.value,
            timestamp=_NOW - timedelta(hours=5),
        )
    )

    assert sink.count_sends_in_last_24h(now=_NOW) == 2


def test_count_sends_in_last_24h_scoped_to_persona_user():
    """The default sink is single-persona MVP scope; the Protocol
    surface does not include persona_id, so the sink either filters
    via constructor scoping or just returns global counts. Document
    the actual behaviour: counts every fire across the table."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(_decision(action=ActionType.SEND.value))
    assert sink.count_sends_in_last_24h(now=_NOW) == 1


# ---------------------------------------------------------------------------
# recent_sends
# ---------------------------------------------------------------------------


def test_recent_sends_returns_newest_first():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    for i, hours in enumerate([2, 5, 8], start=1):
        sink.record(
            _decision(
                decision_id=f"d-{i}",
                action=ActionType.SEND.value,
                timestamp=_NOW - timedelta(hours=hours),
            )
        )
    sink.record(
        _decision(decision_id="d-skip", action=ActionType.SKIP.value)
    )

    out = sink.recent_sends(last_n=2)

    assert len(out) == 2
    assert out[0].decision_id == "d-1"  # newest fire (2h ago)
    assert out[1].decision_id == "d-2"
    # All rehydrated as v1 ProactiveDecision dataclasses
    assert all(d.action == ActionType.SEND.value for d in out)


def test_recent_sends_zero_returns_empty():
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)
    sink.record(_decision(action=ActionType.SEND.value))

    assert sink.recent_sends(last_n=0) == []


# ---------------------------------------------------------------------------
# phase column · v3 FOLLOW_UP_DUE round-trip
# ---------------------------------------------------------------------------


def test_record_persists_phase_from_trigger_payload():
    """Stage 3.3 · a FOLLOW_UP_DUE fire carries phase via trigger_payload;
    the audit row's ``phase`` column captures it."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(
            decision_id="d-fire-pre",
            action=ActionType.SEND.value,
            trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
            skip_reason=None,
            message_text="early ping",
            trigger_payload={
                "event_id": 42,
                "phase": "pre",
                "decision_id": "d-fire-pre",
            },
        )
    )

    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert len(rows) == 1
    r = rows[0]
    assert r.phase == "pre"
    assert r.source_event_id == 42
    assert r.action == "fire"


def test_record_phase_none_when_payload_missing_phase():
    """Non-FOLLOW_UP_DUE rows (e.g. queue_overflow meta) leave phase null."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    sink.record(
        _decision(
            action=ActionType.SKIP.value,
            skip_reason=SkipReason.QUEUE_OVERFLOW.value,
            trigger_payload={"dropped_count": 3},
        )
    )

    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision)))
    assert len(rows) == 1
    assert rows[0].phase is None


def test_record_persists_each_phase_value():
    """All phase strings the FollowUpScheduler may emit round-trip."""
    engine = create_engine(":memory:")
    create_all_tables(engine)
    _seed(engine)
    sink = _build_sink(engine)

    for i, phase in enumerate(["pre", "on", "post", "check_1", "check_3"]):
        sink.record(
            _decision(
                decision_id=f"d-{phase}",
                action=ActionType.SEND.value,
                trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
                skip_reason=None,
                trigger_payload={"event_id": 100 + i, "phase": phase},
            )
        )

    with Session(engine) as db:
        rows = list(db.exec(select(PersistedDecision).order_by(PersistedDecision.id)))
    assert [r.phase for r in rows] == ["pre", "on", "post", "check_1", "check_3"]

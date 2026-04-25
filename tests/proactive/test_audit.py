"""JSONLAuditSink tests — file round-trip + 24h rolling window."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from echovessel.proactive.core.base import (
    ActionType,
    ProactiveDecision,
    SkipReason,
    TriggerReason,
)
from echovessel.proactive.execution.audit import (
    JSONLAuditSink,
    deserialize_decision,
    serialize_decision,
)


def _decision(
    *,
    timestamp: datetime,
    action: str = ActionType.SKIP.value,
    trigger: str = TriggerReason.NO_TRIGGER_MATCH.value,
    skip_reason: str | None = SkipReason.NO_TRIGGER_MATCH.value,
    decision_id: str | None = None,
) -> ProactiveDecision:
    return ProactiveDecision(
        decision_id=decision_id or f"d_{int(timestamp.timestamp())}",
        persona_id="p",
        user_id="u",
        timestamp=timestamp,
        trigger=trigger,
        action=action,
        skip_reason=skip_reason,
    )


def test_serialize_deserialize_roundtrip():
    original = ProactiveDecision(
        decision_id="abc",
        persona_id="p",
        user_id="u",
        timestamp=datetime(2026, 4, 15, 12, 0),
        trigger=TriggerReason.HIGH_EMOTIONAL_EVENT.value,
        trigger_payload={"trigger_event_id": 7, "emotional_impact": -9},
        action=ActionType.SEND.value,
        target_channel_id="web",
        message_text="hello",
        rationale="because I care",
        voice_used=True,
        send_ok=True,
        ingest_message_id=42,
        llm_latency_ms=150,
        policy_snapshot={"quiet_hours_start": 23, "max_per_24h": 3},
    )
    row = serialize_decision(original)
    assert row["decision_id"] == "abc"
    assert row["timestamp"] == datetime(2026, 4, 15, 12, 0).isoformat()

    restored = deserialize_decision(row)
    assert restored.decision_id == "abc"
    assert restored.action == ActionType.SEND.value
    assert restored.trigger_payload == {
        "trigger_event_id": 7,
        "emotional_impact": -9,
    }
    assert restored.timestamp == datetime(2026, 4, 15, 12, 0)


def test_record_writes_jsonl_file(tmp_path: Path):
    sink = JSONLAuditSink(log_dir=tmp_path)
    d = _decision(timestamp=datetime(2026, 4, 15, 12, 0))
    sink.record(d)

    path = tmp_path / "proactive-2026-04-15.jsonl"
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["decision_id"] == d.decision_id
    assert parsed["skip_reason"] == SkipReason.NO_TRIGGER_MATCH.value


def test_update_latest_rewrites_tail(tmp_path: Path):
    sink = JSONLAuditSink(log_dir=tmp_path)
    d = _decision(
        timestamp=datetime(2026, 4, 15, 12, 0),
        action=ActionType.SEND.value,
        trigger=TriggerReason.LONG_SILENCE.value,
        skip_reason=None,
    )
    sink.record(d)
    sink.update_latest(d.decision_id, send_ok=True, ingest_message_id=99)

    path = tmp_path / "proactive-2026-04-15.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["send_ok"] is True
    assert parsed["ingest_message_id"] == 99


def test_count_sends_in_last_24h_respects_window(tmp_path: Path):
    now = datetime(2026, 4, 15, 12, 0)
    sink = JSONLAuditSink(log_dir=tmp_path, clock=lambda: now)

    # Two sends within 24h, one just outside
    for offset_h, did in [(2, "a"), (10, "b"), (30, "c")]:
        sink.record(
            _decision(
                timestamp=now - timedelta(hours=offset_h),
                action=ActionType.SEND.value,
                trigger=TriggerReason.LONG_SILENCE.value,
                skip_reason=None,
                decision_id=did,
            )
        )

    count = sink.count_sends_in_last_24h(now=now)
    assert count == 2


def test_recent_sends_skips_skip_decisions(tmp_path: Path):
    """Recorded in chronological order (oldest first, like real tick loop).
    ``recent_sends`` returns newest-first."""
    now = datetime(2026, 4, 15, 12, 0)
    sink = JSONLAuditSink(log_dir=tmp_path, clock=lambda: now)

    # Written oldest → newest (how the scheduler actually appends over time)
    sink.record(_decision(timestamp=now - timedelta(hours=4), decision_id="skip1"))
    sink.record(
        _decision(
            timestamp=now - timedelta(hours=3),
            action=ActionType.SEND.value,
            skip_reason=None,
            decision_id="send_old",
        )
    )
    sink.record(_decision(timestamp=now - timedelta(hours=2), decision_id="skip2"))
    sink.record(
        _decision(
            timestamp=now - timedelta(hours=1),
            action=ActionType.SEND.value,
            skip_reason=None,
            decision_id="send_new",
        )
    )

    sends = sink.recent_sends(last_n=5)
    ids = [s.decision_id for s in sends]
    # newest-first, skip entries filtered out
    assert ids == ["send_new", "send_old"]


def test_count_crosses_day_boundary(tmp_path: Path):
    now = datetime(2026, 4, 15, 2, 0)  # 2am today
    sink = JSONLAuditSink(log_dir=tmp_path, clock=lambda: now)

    yesterday_evening = datetime(2026, 4, 14, 23, 0)
    sink.record(
        _decision(
            timestamp=yesterday_evening,
            action=ActionType.SEND.value,
            skip_reason=None,
        )
    )
    this_morning = datetime(2026, 4, 15, 1, 0)
    sink.record(
        _decision(
            timestamp=this_morning,
            action=ActionType.SEND.value,
            skip_reason=None,
        )
    )

    # Two files should exist
    yesterday_path = tmp_path / "proactive-2026-04-14.jsonl"
    today_path = tmp_path / "proactive-2026-04-15.jsonl"
    assert yesterday_path.exists()
    assert today_path.exists()

    count = sink.count_sends_in_last_24h(now=now)
    assert count == 2


def test_record_swallows_oserror(tmp_path: Path):
    # Point the sink at a path that cannot be created (a file posing as a dir)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    sink = JSONLAuditSink(log_dir=blocker / "sub")
    d = _decision(timestamp=datetime(2026, 4, 15, 12, 0))
    # Should NOT raise even though mkdir failed
    sink.record(d)

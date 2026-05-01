"""Admin /api/admin/proactive/* route tests · v3 (memory-driven).

Covers:

- ``GET /api/admin/proactive/decisions`` — audit history (fire/suppress
  rows from ``proactive_decisions``) including the v3 ``phase`` field
  and the ``user_replied_at`` sentinel scrub.
- ``GET /api/admin/proactive/events`` — follow-up annotation listing
  read off ``concept_nodes.follow_up_at IS NOT NULL`` with
  ``state=active|fired|closed|all`` filtering.
- ``DELETE /api/admin/memory/events/{id}/proactive-follow-up`` —
  user-initiated suppress that sets ``proactive_suppressed_at``
  without touching the event itself or ``deleted_at``.

Notable invariants:

* ``user_replied_at == datetime.min`` (sentinel) MUST be mapped to
  ``null`` + ``settled_no_reply: True`` at the API boundary.
* ``state=active`` excludes superseded and user-suppressed events.
* Suppress is idempotent (second call returns 204 with no-op).
* Suppress requires same persona ownership (403 cross-persona).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import NodeType
from echovessel.memory.models import (
    NOT_REPLIED_SENTINEL,
    ConceptNode,
    ProactiveDecision,
)
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider

_FIXED_NOW = datetime(2026, 4, 29, 22, 0, 0)


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "proactive-test"
display_name = "Test Persona"

[memory]
db_path = "memory.db"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


def _build() -> tuple[Runtime, TestClient]:
    tmp = tempfile.mkdtemp(prefix="echovessel-proactive-admin-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="{}"),
        embed_fn=build_zero_embedder(),
    )
    broadcaster = SSEBroadcaster()
    channel = WebChannel(debounce_ms=50)
    channel.attach_broadcaster(broadcaster)
    app = build_web_app(
        channel=channel,
        broadcaster=broadcaster,
        runtime=rt,
        heartbeat_seconds=0.5,
    )
    return rt, TestClient(app)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_event(
    rt: Runtime,
    *,
    description: str = "我下周一有面试",
    impact: int = 4,
    persona_id: str = "proactive-test",
    user_id: str = "self",
    follow_up_at: datetime | None = None,
    follow_up_hint: str | None = None,
    estimated_arc_days: int | None = None,
    advance_pre_hours: int | None = None,
    advance_post_hours: int | None = None,
    superseded_by_id: int | None = None,
    proactive_suppressed_at: datetime | None = None,
    deleted_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    with DbSession(rt.ctx.engine) as db:
        node = ConceptNode(
            persona_id=persona_id,
            user_id=user_id,
            type=NodeType.EVENT,
            description=description,
            emotional_impact=impact,
            follow_up_at=follow_up_at,
            follow_up_hint=follow_up_hint,
            estimated_arc_days=estimated_arc_days,
            advance_pre_hours=advance_pre_hours,
            advance_post_hours=advance_post_hours,
            superseded_by_id=superseded_by_id,
            proactive_suppressed_at=proactive_suppressed_at,
            deleted_at=deleted_at,
            created_at=created_at or _FIXED_NOW - timedelta(days=2),
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        assert node.id is not None
        return node.id


def _seed_decision(
    rt: Runtime,
    *,
    decision_id: str = "d1",
    timestamp: datetime | None = None,
    trigger_type: str = "follow_up",
    source_event_id: int | None = None,
    phase: str | None = None,
    action: str = "fire",
    suppress_reason: str | None = None,
    message_text: str | None = None,
    user_replied_at: datetime | None = None,
    voice_used: bool | None = None,
    voice_error: str | None = None,
    gate_state_snapshot: dict[str, Any] | None = None,
    persona_id: str = "proactive-test",
    user_id: str = "self",
) -> int:
    with DbSession(rt.ctx.engine) as db:
        row = ProactiveDecision(
            decision_id=decision_id,
            timestamp=timestamp or _FIXED_NOW,
            persona_id=persona_id,
            user_id=user_id,
            trigger_type=trigger_type,
            source_event_id=source_event_id,
            phase=phase,
            action=action,
            suppress_reason=suppress_reason,
            message_text=message_text,
            user_replied_at=user_replied_at,
            voice_used=voice_used,
            voice_error=voice_error,
            gate_state_snapshot=(
                json.dumps(gate_state_snapshot) if gate_state_snapshot is not None else None
            ),
            created_at=timestamp or _FIXED_NOW,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.id is not None
        return row.id


# ---------------------------------------------------------------------------
# GET /api/admin/proactive/decisions
# ---------------------------------------------------------------------------


def test_get_decisions_empty_returns_zero_total() -> None:
    _rt, client = _build()
    with client:
        resp = client.get("/api/admin/proactive/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["cursor"] is None


def test_get_decisions_returns_phase_field_for_v3_rows() -> None:
    """Stage 3.3 persisted phase; Stage 5.1 must surface it on the wire."""

    rt, client = _build()
    event_id = _seed_event(rt)
    _seed_decision(
        rt,
        decision_id="d-pre",
        source_event_id=event_id,
        phase="pre",
        action="fire",
        message_text="提前关心",
    )

    with client:
        resp = client.get("/api/admin/proactive/decisions")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["phase"] == "pre"
    assert item["trigger_type"] == "follow_up"


def test_get_decisions_sentinel_user_replied_at_maps_to_null_and_settled_flag() -> None:
    """``datetime.min`` sentinel MUST NOT cross the API boundary as ``year=1``.

    The wire format scrubs it to ``null`` + ``settled_no_reply: True``;
    a real reply is surfaced as the actual datetime + ``False``; an
    unsettled fire (still in the 24h window) is ``null`` + ``False``.
    """

    rt, client = _build()
    event_id = _seed_event(rt)

    _seed_decision(
        rt,
        decision_id="d-replied",
        timestamp=_FIXED_NOW,
        source_event_id=event_id,
        action="fire",
        message_text="hi",
        user_replied_at=_FIXED_NOW + timedelta(minutes=7),
    )
    _seed_decision(
        rt,
        decision_id="d-no-reply-settled",
        timestamp=_FIXED_NOW - timedelta(hours=1),
        source_event_id=event_id,
        action="fire",
        message_text="hi 2",
        user_replied_at=NOT_REPLIED_SENTINEL,
    )
    _seed_decision(
        rt,
        decision_id="d-waiting",
        timestamp=_FIXED_NOW - timedelta(hours=2),
        source_event_id=event_id,
        action="fire",
        message_text="hi 3",
        user_replied_at=None,
    )

    with client:
        resp = client.get("/api/admin/proactive/decisions")

    assert resp.status_code == 200
    items = {item["decision_id"]: item for item in resp.json()["items"]}

    replied = items["d-replied"]
    assert replied["user_replied_at"] is not None
    assert replied["settled_no_reply"] is False

    settled = items["d-no-reply-settled"]
    assert settled["user_replied_at"] is None  # sentinel scrubbed
    assert settled["settled_no_reply"] is True

    waiting = items["d-waiting"]
    assert waiting["user_replied_at"] is None
    assert waiting["settled_no_reply"] is False


def test_get_decisions_action_filter() -> None:
    rt, client = _build()
    event_id = _seed_event(rt)

    _seed_decision(
        rt,
        decision_id="d-fire",
        source_event_id=event_id,
        action="fire",
        message_text="hi",
    )
    _seed_decision(
        rt,
        decision_id="d-suppress",
        timestamp=_FIXED_NOW + timedelta(seconds=1),
        source_event_id=event_id,
        action="suppress",
        suppress_reason="rate_limit",
    )

    with client:
        resp = client.get("/api/admin/proactive/decisions?action=fire")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["decision_id"] == "d-fire"


def test_get_decisions_cursor_pagination_handles_same_timestamp_tie() -> None:
    """Cursor encodes ``(timestamp, id)`` so ties at page edges don't drop rows."""

    rt, client = _build()
    event_id = _seed_event(rt)

    same_ts = _FIXED_NOW
    for label in ("A", "B", "C"):
        _seed_decision(
            rt,
            decision_id=f"d-tie-{label}",
            timestamp=same_ts,
            source_event_id=event_id,
            action="fire",
            message_text=label,
        )

    seen: list[str] = []
    cursor: str | None = None
    with client:
        while True:
            url = "/api/admin/proactive/decisions?limit=2"
            if cursor is not None:
                url += f"&cursor={cursor}"
            resp = client.get(url)
            body = resp.json()
            seen.extend(item["decision_id"] for item in body["items"])
            cursor = body["cursor"]
            if cursor is None:
                break

    assert sorted(seen) == ["d-tie-A", "d-tie-B", "d-tie-C"]
    assert len(set(seen)) == 3


def test_get_decisions_persona_isolation() -> None:
    rt, client = _build()
    _seed_decision(rt, decision_id="d-mine", action="fire")
    _seed_decision(
        rt,
        decision_id="d-other",
        action="fire",
        persona_id="someone-else",
    )

    with client:
        resp = client.get("/api/admin/proactive/decisions")

    items = resp.json()["items"]
    assert {it["decision_id"] for it in items} == {"d-mine"}


# ---------------------------------------------------------------------------
# GET /api/admin/proactive/events
# ---------------------------------------------------------------------------


def test_get_events_state_active_excludes_superseded_and_suppressed() -> None:
    """``active`` is ``follow_up_at IS NOT NULL AND superseded_by_id IS
    NULL AND proactive_suppressed_at IS NULL AND deleted_at IS NULL``.
    """

    rt, client = _build()
    # Need a sentinel "older" event to point superseded_by at; using id=99
    # would be simpler but FK constraints kick in.
    sentinel_id = _seed_event(rt, description="newer outcome", follow_up_at=None)

    active_id = _seed_event(
        rt,
        description="active follow-up",
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        follow_up_hint="提前关心",
    )
    suppressed_id = _seed_event(
        rt,
        description="user suppressed",
        follow_up_at=_FIXED_NOW + timedelta(days=2),
        proactive_suppressed_at=_FIXED_NOW,
    )
    superseded_id = _seed_event(
        rt,
        description="superseded by outcome",
        follow_up_at=_FIXED_NOW + timedelta(days=3),
        superseded_by_id=sentinel_id,
    )
    # No follow_up_at → never appears in this list
    bare_id = _seed_event(
        rt,
        description="no follow-up at all",
        follow_up_at=None,
    )
    # Deleted → excluded even if follow_up_at set
    deleted_id = _seed_event(
        rt,
        description="deleted",
        follow_up_at=_FIXED_NOW + timedelta(days=4),
        deleted_at=_FIXED_NOW,
    )

    with client:
        resp = client.get("/api/admin/proactive/events?state=active")

    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body["items"]}
    assert ids == {active_id}
    assert suppressed_id not in ids
    assert superseded_id not in ids
    assert bare_id not in ids
    assert deleted_id not in ids


def test_get_events_state_closed_includes_superseded_or_suppressed() -> None:
    rt, client = _build()
    sentinel_id = _seed_event(rt, follow_up_at=None)

    suppressed_id = _seed_event(
        rt,
        description="user suppressed",
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        proactive_suppressed_at=_FIXED_NOW,
    )
    superseded_id = _seed_event(
        rt,
        description="superseded",
        follow_up_at=_FIXED_NOW + timedelta(days=2),
        superseded_by_id=sentinel_id,
    )
    _active_id = _seed_event(
        rt,
        description="active",
        follow_up_at=_FIXED_NOW + timedelta(days=3),
    )

    with client:
        resp = client.get("/api/admin/proactive/events?state=closed")

    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {suppressed_id, superseded_id}
    # Closed flag should be true for both
    for item in resp.json()["items"]:
        assert item["closed"] is True


def test_get_events_surfaces_follow_up_annotation_columns() -> None:
    """Wire format must include the v0.7 columns the FE will render."""

    rt, client = _build()
    event_id = _seed_event(
        rt,
        description="exam Monday",
        follow_up_at=datetime(2026, 5, 4, 10, 0),
        follow_up_hint="面试结果",
        estimated_arc_days=14,
        advance_pre_hours=24,
        advance_post_hours=24,
    )

    with client:
        resp = client.get("/api/admin/proactive/events?state=active")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == event_id
    assert item["description"] == "exam Monday"
    assert item["follow_up_hint"] == "面试结果"
    assert item["estimated_arc_days"] == 14
    assert item["advance_pre_hours"] == 24
    assert item["advance_post_hours"] == 24
    assert item["closed"] is False


def test_get_events_fired_at_derives_from_decision_audit() -> None:
    """``fired_at`` is the earliest fire-action decision pointing at the event."""

    rt, client = _build()
    event_id = _seed_event(
        rt,
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        follow_up_hint="check in",
    )

    earliest = _FIXED_NOW
    later = _FIXED_NOW + timedelta(hours=2)
    _seed_decision(
        rt,
        decision_id="d-1",
        timestamp=earliest,
        source_event_id=event_id,
        action="fire",
    )
    _seed_decision(
        rt,
        decision_id="d-2",
        timestamp=later,
        source_event_id=event_id,
        action="fire",
    )

    with client:
        resp = client.get("/api/admin/proactive/events?state=active")

    item = resp.json()["items"][0]
    assert item["fired_at"] is not None
    assert item["fired_at"].startswith("2026-04-29T22:00")


def test_get_events_persona_isolation() -> None:
    rt, client = _build()
    mine = _seed_event(
        rt,
        description="mine",
        follow_up_at=_FIXED_NOW + timedelta(days=1),
    )
    _seed_event(
        rt,
        description="theirs",
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        persona_id="someone-else",
    )

    with client:
        resp = client.get("/api/admin/proactive/events?state=active")

    ids = {it["id"] for it in resp.json()["items"]}
    assert ids == {mine}


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/events/{id}/proactive-follow-up
# ---------------------------------------------------------------------------


def test_suppress_follow_up_sets_proactive_suppressed_at() -> None:
    rt, client = _build()
    event_id = _seed_event(
        rt,
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        follow_up_hint="check in",
    )

    with client:
        resp = client.delete(f"/api/admin/memory/events/{event_id}/proactive-follow-up")

    assert resp.status_code == 204

    with DbSession(rt.ctx.engine) as db:
        row = db.get(ConceptNode, event_id)
        assert row is not None
        assert row.proactive_suppressed_at is not None
        # The event itself is NOT deleted; the user only opted out of follow-up.
        assert row.deleted_at is None
        # follow_up_at column stays — the FE shows it under state=closed.
        assert row.follow_up_at is not None


def test_suppress_follow_up_removes_event_from_active_list() -> None:
    rt, client = _build()
    event_id = _seed_event(
        rt,
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        follow_up_hint="check in",
    )

    with client:
        active_before = client.get("/api/admin/proactive/events?state=active")
        assert event_id in {it["id"] for it in active_before.json()["items"]}

        resp = client.delete(f"/api/admin/memory/events/{event_id}/proactive-follow-up")
        assert resp.status_code == 204

        active_after = client.get("/api/admin/proactive/events?state=active")
        assert event_id not in {it["id"] for it in active_after.json()["items"]}

        closed_after = client.get("/api/admin/proactive/events?state=closed")
        assert event_id in {it["id"] for it in closed_after.json()["items"]}


def test_suppress_follow_up_404_on_missing() -> None:
    _rt, client = _build()
    with client:
        resp = client.delete("/api/admin/memory/events/9999/proactive-follow-up")
    assert resp.status_code == 404


def test_suppress_follow_up_403_on_cross_persona() -> None:
    rt, client = _build()
    foreign_id = _seed_event(
        rt,
        description="theirs",
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        persona_id="someone-else",
    )

    with client:
        resp = client.delete(f"/api/admin/memory/events/{foreign_id}/proactive-follow-up")

    assert resp.status_code == 403


def test_suppress_follow_up_410_on_deleted_event() -> None:
    rt, client = _build()
    event_id = _seed_event(
        rt,
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        deleted_at=_FIXED_NOW - timedelta(hours=1),
    )

    with client:
        resp = client.delete(f"/api/admin/memory/events/{event_id}/proactive-follow-up")

    assert resp.status_code == 410


def test_suppress_follow_up_idempotent_second_call_no_op() -> None:
    """A second suppress on an already-suppressed event is a no-op (204)."""

    rt, client = _build()
    event_id = _seed_event(
        rt,
        follow_up_at=_FIXED_NOW + timedelta(days=1),
        proactive_suppressed_at=_FIXED_NOW - timedelta(hours=1),
    )

    with client:
        resp = client.delete(f"/api/admin/memory/events/{event_id}/proactive-follow-up")

    assert resp.status_code == 204
    # The original suppress timestamp is preserved (not overwritten) — but
    # the public contract is "204, idempotent"; we don't lock the
    # specific timestamp behaviour here.


# ---------------------------------------------------------------------------
# Smoke · routes are registered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/proactive/decisions",
        "/api/admin/proactive/events",
    ],
)
def test_routes_are_registered(path: str) -> None:
    _rt, client = _build()
    with client:
        resp = client.get(path)
    assert resp.status_code == 200

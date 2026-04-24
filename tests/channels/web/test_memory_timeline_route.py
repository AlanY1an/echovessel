"""Spec 3 · GET /api/admin/memory/timeline route tests.

Covers the backfill endpoint that powers the Chat page's Memory
Timeline sidebar:

- Empty DB → empty items list
- Merges events, thoughts, entities, session closes, mood into one
  DESC-ordered timeline
- Respects ``limit`` and the ``since`` pagination cursor
- Filters uncertain entities (admin-only per plan §3.1)
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import NodeType, SessionStatus
from echovessel.memory import ConceptNode
from echovessel.memory.models import Entity, Persona
from echovessel.memory.models import Session as RecallSession
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "timeline-test"
display_name = "Timeline"

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


def _build_rig() -> tuple[Runtime, TestClient]:
    tmp = tempfile.mkdtemp(prefix="echovessel-timeline-")
    cfg = load_config_from_str(_toml(tmp))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback="ok"),
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


def test_timeline_empty() -> None:
    rt, client = _build_rig()
    try:
        r = client.get("/api/admin/memory/timeline")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["oldest_timestamp"] is None
        assert body["limit"] == 50
    finally:
        client.close()


def test_timeline_merges_event_thought_entity_mood_session() -> None:
    rt, client = _build_rig()
    try:
        base = datetime(2026, 4, 24, 10, 0, 0)
        with DbSession(rt.ctx.engine) as db:
            # Bump episodic_state on the persona so the mood row appears.
            persona = db.get(Persona, rt.ctx.persona.id)
            assert persona is not None
            persona.episodic_state = {
                "mood": "warm-curious",
                "energy": "mid",
                "last_user_signal": "none",
                "updated_at": (base + timedelta(minutes=40)).isoformat(),
            }
            db.add(persona)

            # L3 event
            db.add(
                ConceptNode(
                    persona_id=rt.ctx.persona.id,
                    user_id="self",
                    type=NodeType.EVENT,
                    description="用户提到下周要做面试",
                    emotional_impact=2,
                    created_at=base + timedelta(minutes=10),
                )
            )
            # L4 thought
            db.add(
                ConceptNode(
                    persona_id=rt.ctx.persona.id,
                    user_id="self",
                    type=NodeType.THOUGHT,
                    subject="persona",
                    description="我注意到用户在期末期间很紧张",
                    created_at=base + timedelta(minutes=20),
                )
            )
            # L5 entity — confirmed
            db.add(
                Entity(
                    persona_id=rt.ctx.persona.id,
                    user_id="self",
                    canonical_name="温冉",
                    kind="person",
                    merge_status="confirmed",
                    created_at=base + timedelta(minutes=30),
                    updated_at=base + timedelta(minutes=30),
                )
            )
            # L5 entity — uncertain (must be filtered out)
            db.add(
                Entity(
                    persona_id=rt.ctx.persona.id,
                    user_id="self",
                    canonical_name="Scott",
                    kind="person",
                    merge_status="uncertain",
                    merge_target_id=None,
                    created_at=base + timedelta(minutes=35),
                    updated_at=base + timedelta(minutes=35),
                )
            )
            # Closed session (extracted)
            db.add(
                RecallSession(
                    id="sess-closed",
                    persona_id=rt.ctx.persona.id,
                    user_id="self",
                    channel_id="web",
                    status=SessionStatus.CLOSED,
                    started_at=base,
                    last_message_at=base + timedelta(minutes=5),
                    closed_at=base + timedelta(minutes=6),
                    extracted=True,
                    extracted_at=base + timedelta(minutes=50),
                    message_count=12,
                )
            )
            db.commit()

        r = client.get("/api/admin/memory/timeline?limit=50")
        assert r.status_code == 200
        body = r.json()
        kinds = [it["kind"] for it in body["items"]]

        assert "event" in kinds
        assert "thought" in kinds
        assert "entity" in kinds
        assert "mood" in kinds
        assert "session_close" in kinds

        # Uncertain entity MUST NOT surface in the user-facing backfill.
        entity_names = [
            it["data"]["canonical_name"] for it in body["items"] if it["kind"] == "entity"
        ]
        assert "温冉" in entity_names
        assert "Scott" not in entity_names

        # DESC ordering on timestamp.
        timestamps = [it["timestamp"] for it in body["items"]]
        assert timestamps == sorted(timestamps, reverse=True)

        # Session close carries counts.
        sess_items = [it for it in body["items"] if it["kind"] == "session_close"]
        assert len(sess_items) == 1
        assert sess_items[0]["data"]["session_id"] == "sess-closed"
        assert "events_count" in sess_items[0]["data"]
        assert "thoughts_count" in sess_items[0]["data"]
        # Duration 6 minutes → 360 seconds.
        assert sess_items[0]["data"]["duration_seconds"] == 360
    finally:
        client.close()


def test_timeline_respects_limit() -> None:
    rt, client = _build_rig()
    try:
        base = datetime(2026, 4, 24, 10, 0, 0)
        with DbSession(rt.ctx.engine) as db:
            for i in range(8):
                db.add(
                    ConceptNode(
                        persona_id=rt.ctx.persona.id,
                        user_id="self",
                        type=NodeType.EVENT,
                        description=f"event #{i}",
                        created_at=base + timedelta(minutes=i),
                    )
                )
            db.commit()

        r = client.get("/api/admin/memory/timeline?limit=3")
        body = r.json()
        assert len(body["items"]) == 3
        assert body["limit"] == 3
    finally:
        client.close()


def test_timeline_since_filter() -> None:
    rt, client = _build_rig()
    try:
        base = datetime(2026, 4, 24, 10, 0, 0)
        with DbSession(rt.ctx.engine) as db:
            for i in range(6):
                db.add(
                    ConceptNode(
                        persona_id=rt.ctx.persona.id,
                        user_id="self",
                        type=NodeType.EVENT,
                        description=f"event #{i}",
                        created_at=base + timedelta(minutes=i),
                    )
                )
            db.commit()

        # Fetch first page: returns newest 3 (minutes 5, 4, 3).
        r = client.get("/api/admin/memory/timeline?limit=3")
        body = r.json()
        first_descriptions = [it["data"]["description"] for it in body["items"]]
        assert first_descriptions == ["event #5", "event #4", "event #3"]

        # Use oldest_timestamp as cursor for the next older page.
        cursor = body["oldest_timestamp"]
        assert cursor is not None

        r2 = client.get(f"/api/admin/memory/timeline?limit=3&since={cursor}")
        body2 = r2.json()
        second_descriptions = [it["data"]["description"] for it in body2["items"]]
        assert second_descriptions == ["event #2", "event #1", "event #0"]
    finally:
        client.close()

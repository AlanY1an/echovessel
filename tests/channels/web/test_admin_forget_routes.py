"""Worker β · Forget route tests.

Covers the six admin endpoints added for architecture v0.3 §4.12:

- POST   /api/admin/memory/preview-delete
- DELETE /api/admin/memory/events/{node_id}
- DELETE /api/admin/memory/thoughts/{node_id}
- DELETE /api/admin/memory/messages/{message_id}
- DELETE /api/admin/memory/sessions/{session_id}
- DELETE /api/admin/memory/core-blocks/{label}/appends/{append_id}

The fixture pattern is borrowed from test_admin_routes.py:
  - real Runtime built with config_override
  - file-backed SQLite so the TestClient's threadpool can see tables
  - direct DB seeding through a DbSession bound to ctx.engine

Every test is independent — no shared state, no module fixtures.
"""

from __future__ import annotations

import tempfile
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import MessageRole, NodeType
from echovessel.memory import (
    ConceptNode,
    ConceptNodeFilling,
    CoreBlockAppend,
    Persona,
    RecallMessage,
    User,
)
from echovessel.memory import (
    Session as RecallSession,
)
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "p_forget"
display_name = "Forget"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-forget-")
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


def _seed_world(rt: Runtime) -> dict:
    """Seed one session + two messages + one event + one thought + one filling
    link + one core-block append. Returns the primary-key map tests use to
    hit the forget endpoints."""

    with DbSession(rt.ctx.engine) as db:
        # Personas & users.
        if db.get(Persona, "p_forget") is None:
            db.add(Persona(id="p_forget", display_name="Forget"))
        if db.get(User, "self") is None:
            db.add(User(id="self", display_name="Alan"))
        db.commit()

        sess = RecallSession(
            id="s_forget",
            persona_id="p_forget",
            user_id="self",
            channel_id="test",
        )
        db.add(sess)
        db.commit()

        msg1 = RecallMessage(
            session_id="s_forget",
            persona_id="p_forget",
            user_id="self",
            channel_id="test",
            role=MessageRole.USER,
            content="今天发生了一件重要的事",
            day=date.today(),
        )
        msg2 = RecallMessage(
            session_id="s_forget",
            persona_id="p_forget",
            user_id="self",
            channel_id="test",
            role=MessageRole.PERSONA,
            content="我听着",
            day=date.today(),
        )
        db.add(msg1)
        db.add(msg2)
        db.commit()
        db.refresh(msg1)
        db.refresh(msg2)

        event = ConceptNode(
            persona_id="p_forget",
            user_id="self",
            type=NodeType.EVENT,
            description="今天发生了一件重要的事",
            emotional_impact=5,
            source_session_id="s_forget",
        )
        db.add(event)
        db.commit()
        db.refresh(event)

        thought = ConceptNode(
            persona_id="p_forget",
            user_id="self",
            type=NodeType.THOUGHT,
            description="Alan 在关键时刻会主动开口",
            emotional_impact=2,
        )
        db.add(thought)
        db.commit()
        db.refresh(thought)

        link = ConceptNodeFilling(parent_id=thought.id, child_id=event.id)
        db.add(link)
        db.commit()

        append = CoreBlockAppend(
            persona_id="p_forget",
            user_id="self",
            label="user",
            content="Alan 在关键时刻会主动开口",
            provenance_json={"source": "forget-test"},
        )
        db.add(append)
        db.commit()
        db.refresh(append)

        return {
            "session_id": "s_forget",
            "msg1_id": msg1.id,
            "msg2_id": msg2.id,
            "event_id": event.id,
            "thought_id": thought.id,
            "append_id": append.id,
        }


# ---------------------------------------------------------------------------
# POST /api/admin/memory/preview-delete
# ---------------------------------------------------------------------------


def test_preview_delete_reports_no_dependents_for_thought() -> None:
    """A thought (L4) with no parents has an empty dependents list."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.post(
            "/api/admin/memory/preview-delete",
            json={"node_id": ids["thought_id"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_id"] == ids["thought_id"]
    assert body["dependent_thought_ids"] == []
    assert body["has_dependents"] is False


def test_preview_delete_lists_dependent_thought_for_event() -> None:
    """Deleting the seeded event would orphan the dependent thought —
    the preview surfaces both the id and the description."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.post(
            "/api/admin/memory/preview-delete",
            json={"node_id": ids["event_id"]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_id"] == ids["event_id"]
    assert body["dependent_thought_ids"] == [ids["thought_id"]]
    assert body["has_dependents"] is True
    assert "Alan 在关键时刻" in body["dependent_thought_descriptions"][0]


def test_preview_delete_404_on_missing_node() -> None:
    rt, client = _build()
    _seed_world(rt)
    with client:
        resp = client.post(
            "/api/admin/memory/preview-delete",
            json={"node_id": 999_999},
        )
    assert resp.status_code == 404, resp.text
    assert "not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/events/{node_id}
# ---------------------------------------------------------------------------


def test_delete_event_orphan_default_keeps_dependent_thought_alive() -> None:
    """Default choice=orphan soft-deletes the event and marks the
    filling row orphaned but leaves the thought intact."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/events/{ids['event_id']}",
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "deleted": True,
        "node_id": ids["event_id"],
        "choice": "orphan",
    }

    with DbSession(rt.ctx.engine) as db:
        event = db.get(ConceptNode, ids["event_id"])
        thought = db.get(ConceptNode, ids["thought_id"])
        filling = db.exec(
            select(ConceptNodeFilling).where(
                ConceptNodeFilling.parent_id == ids["thought_id"],
                ConceptNodeFilling.child_id == ids["event_id"],
            )
        ).one()
    assert event is not None and event.deleted_at is not None
    assert thought is not None and thought.deleted_at is None
    assert filling.orphaned is True


def test_delete_event_cascade_removes_dependent_thoughts_too() -> None:
    """choice=cascade soft-deletes every L4 dependent."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/events/{ids['event_id']}?choice=cascade",
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choice"] == "cascade"

    with DbSession(rt.ctx.engine) as db:
        event = db.get(ConceptNode, ids["event_id"])
        thought = db.get(ConceptNode, ids["thought_id"])
    assert event is not None and event.deleted_at is not None
    assert thought is not None and thought.deleted_at is not None


def test_delete_event_404_when_id_points_at_a_thought() -> None:
    """Safety rail: you can't delete an L4 thought through the L3 path
    even if the numeric id happens to exist."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/events/{ids['thought_id']}",
        )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/thoughts/{node_id}
# ---------------------------------------------------------------------------


def test_delete_thought_soft_deletes_the_row() -> None:
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/thoughts/{ids['thought_id']}",
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "deleted": True,
        "node_id": ids["thought_id"],
        "choice": "orphan",
    }
    with DbSession(rt.ctx.engine) as db:
        thought = db.get(ConceptNode, ids["thought_id"])
    assert thought is not None and thought.deleted_at is not None


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/messages/{message_id}
# ---------------------------------------------------------------------------


def test_delete_message_soft_deletes_and_flags_source_event() -> None:
    """Deleting any L2 message in a session flips every L3 event sourced
    from that session to source_deleted=True."""
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/messages/{ids['msg1_id']}",
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True, "message_id": ids["msg1_id"]}

    with DbSession(rt.ctx.engine) as db:
        msg = db.get(RecallMessage, ids["msg1_id"])
        event = db.get(ConceptNode, ids["event_id"])
    assert msg is not None and msg.deleted_at is not None
    assert event is not None and event.source_deleted is True
    # 404 on a second call (already soft-deleted).
    with client:
        second = client.delete(
            f"/api/admin/memory/messages/{ids['msg1_id']}",
        )
    assert second.status_code == 404, second.text


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_delete_session_cascades_messages_and_flags_events() -> None:
    rt, client = _build()
    ids = _seed_world(rt)
    with client:
        resp = client.delete(
            f"/api/admin/memory/sessions/{ids['session_id']}",
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["session_id"] == ids["session_id"]
    assert body["messages_deleted"] == 2

    with DbSession(rt.ctx.engine) as db:
        msg1 = db.get(RecallMessage, ids["msg1_id"])
        msg2 = db.get(RecallMessage, ids["msg2_id"])
        event = db.get(ConceptNode, ids["event_id"])
    assert msg1 is not None and msg1.deleted_at is not None
    assert msg2 is not None and msg2.deleted_at is not None
    assert event is not None and event.source_deleted is True


def test_delete_session_404_for_unknown_id() -> None:
    rt, client = _build()
    _seed_world(rt)
    with client:
        resp = client.delete("/api/admin/memory/sessions/s_does_not_exist")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# DELETE /api/admin/memory/core-blocks/{label}/appends/{append_id}
# ---------------------------------------------------------------------------


def test_delete_core_block_append_physical_delete_with_label_guard() -> None:
    """Happy path + wrong-label guard, in one test.

    Step 1. Wrong label on a valid id → 404 (label guard).
    Step 2. Correct label → 200, row physically gone.
    Step 3. Second DELETE → 404 (already physical-deleted).
    """
    rt, client = _build()
    ids = _seed_world(rt)

    with client:
        wrong = client.delete(
            f"/api/admin/memory/core-blocks/persona/appends/{ids['append_id']}"
        )
    assert wrong.status_code == 404, wrong.text
    assert "not" in wrong.json()["detail"].lower()

    with client:
        ok = client.delete(
            f"/api/admin/memory/core-blocks/user/appends/{ids['append_id']}"
        )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {
        "deleted": True,
        "append_id": ids["append_id"],
        "label": "user",
    }

    with DbSession(rt.ctx.engine) as db:
        gone = db.get(CoreBlockAppend, ids["append_id"])
    assert gone is None

    with client:
        second = client.delete(
            f"/api/admin/memory/core-blocks/user/appends/{ids['append_id']}"
        )
    assert second.status_code == 404, second.text

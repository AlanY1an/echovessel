"""Tests for the Worker-α admin memory list routes.

Exercises:

- ``GET /api/admin/memory/events?limit=&offset=``  (paginated)
- ``GET /api/admin/memory/thoughts?limit=&offset=``  (paginated)
- the existing ``POST /api/admin/memory/preview-delete`` is touched
  too so the list-then-delete UI flow has end-to-end coverage at
  the admin-route layer.

Each test builds a real :class:`Runtime` against a file-backed SQLite
DB (``:memory:`` would lose tables across the FastAPI TestClient
threadpool boundary) and seeds ConceptNode rows directly so the test
does not depend on the consolidate pipeline.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import NodeType
from echovessel.memory import ConceptNode
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
id = "memlist-test"
display_name = "MemList"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-memlist-")
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


def _seed_nodes(
    rt: Runtime,
    *,
    n: int,
    node_type: NodeType,
    base_time: datetime | None = None,
) -> list[int]:
    """Insert ``n`` ConceptNode rows of ``node_type`` for the configured
    persona/user. Returns the inserted ids in creation order (oldest
    first) so tests can reason about the DESC-ordered admin response."""

    base_time = base_time or datetime(2026, 4, 16, 12, 0, 0)
    ids: list[int] = []
    with DbSession(rt.ctx.engine) as db:
        for i in range(n):
            row = ConceptNode(
                persona_id=rt.ctx.persona.id,
                user_id="self",
                type=node_type,
                description=f"{node_type.value} #{i}",
                emotional_impact=(i % 11) - 5,
                emotion_tags=["calm"] if i % 2 else ["joy"],
                relational_tags=["family"] if i % 3 == 0 else [],
                created_at=base_time + timedelta(minutes=i),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            ids.append(row.id)
    return ids


# ---------------------------------------------------------------------------
# GET /api/admin/memory/events
# ---------------------------------------------------------------------------


def test_list_events_empty_returns_total_zero() -> None:
    _rt, client = _build_rig()
    with client:
        resp = client.get("/api/admin/memory/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_type"] == "event"
    assert body["total"] == 0
    assert body["items"] == []
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_list_events_returns_descending_order_with_full_payload() -> None:
    rt, client = _build_rig()
    ids = _seed_nodes(rt, n=3, node_type=NodeType.EVENT)
    with client:
        resp = client.get("/api/admin/memory/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3

    # Newest first: the last id seeded is the first item.
    item_ids = [item["id"] for item in body["items"]]
    assert item_ids == list(reversed(ids))

    head = body["items"][0]
    assert head["node_type"] == "event"
    assert head["description"].startswith("event #")
    assert isinstance(head["emotion_tags"], list)
    assert isinstance(head["relational_tags"], list)
    assert "created_at" in head and head["created_at"]


def test_list_events_pagination_offset_and_limit() -> None:
    rt, client = _build_rig()
    ids = _seed_nodes(rt, n=25, node_type=NodeType.EVENT)
    expected_desc = list(reversed(ids))
    with client:
        first = client.get(
            "/api/admin/memory/events", params={"limit": 10, "offset": 0}
        ).json()
        second = client.get(
            "/api/admin/memory/events", params={"limit": 10, "offset": 10}
        ).json()
        third = client.get(
            "/api/admin/memory/events", params={"limit": 10, "offset": 20}
        ).json()
    assert first["total"] == second["total"] == third["total"] == 25
    assert [i["id"] for i in first["items"]] == expected_desc[:10]
    assert [i["id"] for i in second["items"]] == expected_desc[10:20]
    assert [i["id"] for i in third["items"]] == expected_desc[20:25]
    assert len(third["items"]) == 5


def test_list_events_limit_cap_enforced() -> None:
    _rt, client = _build_rig()
    with client:
        # 999 well above the 100 cap; the FastAPI Query validator
        # rejects this with 422 before the handler runs.
        resp = client.get("/api/admin/memory/events", params={"limit": 999})
    assert resp.status_code == 422


def test_list_events_excludes_thoughts() -> None:
    """Events endpoint must filter on type and ignore THOUGHT rows."""

    rt, client = _build_rig()
    _seed_nodes(rt, n=2, node_type=NodeType.EVENT)
    _seed_nodes(rt, n=3, node_type=NodeType.THOUGHT)
    with client:
        resp = client.get("/api/admin/memory/events").json()
    assert resp["total"] == 2
    assert all(i["node_type"] == "event" for i in resp["items"])


# ---------------------------------------------------------------------------
# GET /api/admin/memory/thoughts
# ---------------------------------------------------------------------------


def test_list_thoughts_returns_only_thoughts() -> None:
    rt, client = _build_rig()
    _seed_nodes(rt, n=2, node_type=NodeType.EVENT)
    thought_ids = _seed_nodes(rt, n=4, node_type=NodeType.THOUGHT)
    with client:
        resp = client.get("/api/admin/memory/thoughts").json()
    assert resp["node_type"] == "thought"
    assert resp["total"] == 4
    assert [i["id"] for i in resp["items"]] == list(reversed(thought_ids))
    assert all(i["node_type"] == "thought" for i in resp["items"])


def test_list_thoughts_default_limit_is_20() -> None:
    rt, client = _build_rig()
    _seed_nodes(rt, n=42, node_type=NodeType.THOUGHT)
    with client:
        resp = client.get("/api/admin/memory/thoughts").json()
    assert resp["total"] == 42
    assert len(resp["items"]) == 20
    assert resp["limit"] == 20


# ---------------------------------------------------------------------------
# Preview-then-delete round trip (sanity that W-β + Worker α agree)
# ---------------------------------------------------------------------------


def test_list_then_preview_then_delete_event_roundtrip() -> None:
    rt, client = _build_rig()
    ids = _seed_nodes(rt, n=2, node_type=NodeType.EVENT)
    target_id = ids[0]

    with client:
        # 1. List sees both.
        listing = client.get("/api/admin/memory/events").json()
        assert listing["total"] == 2

        # 2. Preview the delete; with no L4 dependents it has none.
        preview = client.post(
            "/api/admin/memory/preview-delete",
            json={"node_id": target_id},
        )
        assert preview.status_code == 200
        prev_body = preview.json()
        assert prev_body["target_id"] == target_id
        assert prev_body["dependent_thought_ids"] == []
        assert prev_body["has_dependents"] is False

        # 3. Delete via W-β route.
        delete = client.delete(f"/api/admin/memory/events/{target_id}")
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True

        # 4. Re-list shows total dropped + deleted id absent.
        post = client.get("/api/admin/memory/events").json()
    assert post["total"] == 1
    assert all(i["id"] != target_id for i in post["items"])


def test_preview_delete_unknown_node_returns_404() -> None:
    _rt, client = _build_rig()
    with client:
        resp = client.post(
            "/api/admin/memory/preview-delete",
            json={"node_id": 999_999},
        )
    assert resp.status_code == 404

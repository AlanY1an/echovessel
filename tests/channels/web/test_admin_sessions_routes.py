"""Tests for ``GET /api/admin/sessions/failed`` — observability surface
for sessions the consolidate worker marked FAILED.

The admin frontend renders a banner from this endpoint when the list is
non-empty so the operator notices silent data loss instead of having to
``sqlite3`` into the db. Unlike memory list endpoints, this one does NOT
filter by ``user_id`` — a single human shows up under multiple ``user_id``
values across channels (web=self, discord=snowflake, imessage=phone), and
the operator wants to see every failure regardless of which shard owned it.
"""

from __future__ import annotations

import tempfile
from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import SessionStatus
from echovessel.memory.models import Session
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
id = "sess-test"
display_name = "SessTest"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-sess-")
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


def _seed_session(
    rt: Runtime,
    *,
    sid: str,
    status: SessionStatus,
    channel_id: str = "web",
    user_id: str = "self",
    message_count: int = 5,
    close_trigger: str | None = None,
    started_at: datetime | None = None,
) -> None:
    started_at = started_at or datetime(2026, 4, 18, 10, 0, 0)
    with DbSession(rt.ctx.engine) as db:
        db.add(
            Session(
                id=sid,
                persona_id="sess-test",
                user_id=user_id,
                channel_id=channel_id,
                status=status,
                started_at=started_at,
                last_message_at=started_at,
                message_count=message_count,
                total_tokens=message_count * 20,
                close_trigger=close_trigger,
            )
        )
        db.commit()


def test_failed_sessions_endpoint_lists_only_failed():
    rt, client = _build_rig()
    _seed_session(
        rt,
        sid="s-ok",
        status=SessionStatus.CLOSED,
        close_trigger="catchup",
    )
    _seed_session(
        rt,
        sid="s-bad-1",
        status=SessionStatus.FAILED,
        channel_id="discord",
        user_id="753654474022584361",
        message_count=12,
        close_trigger="catchup|failed:unexpected: (sqlite3.OperationalError) database is locked",
        started_at=datetime(2026, 4, 18, 4, 6, 43),
    )
    _seed_session(
        rt,
        sid="s-bad-2",
        status=SessionStatus.FAILED,
        channel_id="web",
        user_id="self",
        message_count=22,
        close_trigger="catchup|failed:unexpected: (sqlite3.OperationalError) database is locked",
        started_at=datetime(2026, 4, 18, 3, 22, 13),
    )

    response = client.get("/api/admin/sessions/failed")
    assert response.status_code == 200
    body = response.json()

    assert body["count"] == 2
    ids = {item["id"] for item in body["items"]}
    assert ids == {"s-bad-1", "s-bad-2"}

    by_id = {item["id"]: item for item in body["items"]}
    bad1 = by_id["s-bad-1"]
    assert bad1["channel_id"] == "discord"
    assert bad1["user_id"] == "753654474022584361"
    assert bad1["message_count"] == 12
    assert "database is locked" in bad1["close_trigger"]
    assert bad1["started_at"].startswith("2026-04-18T04:06:43")


def test_failed_sessions_endpoint_returns_empty_when_none():
    rt, client = _build_rig()
    _seed_session(rt, sid="s-only-ok", status=SessionStatus.CLOSED)

    response = client.get("/api/admin/sessions/failed")
    assert response.status_code == 200
    body = response.json()

    assert body["count"] == 0
    assert body["items"] == []


def test_failed_sessions_endpoint_orders_newest_first():
    rt, client = _build_rig()
    _seed_session(
        rt,
        sid="s-old",
        status=SessionStatus.FAILED,
        started_at=datetime(2026, 4, 18, 3, 0, 0),
        close_trigger="catchup|failed:x",
    )
    _seed_session(
        rt,
        sid="s-new",
        status=SessionStatus.FAILED,
        started_at=datetime(2026, 4, 20, 18, 0, 0),
        close_trigger="catchup|failed:y",
    )

    response = client.get("/api/admin/sessions/failed")
    assert [item["id"] for item in response.json()["items"]] == ["s-new", "s-old"]

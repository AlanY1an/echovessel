"""Worker Y · GET /api/chat/history route tests.

Covers the cross-channel L2 backfill endpoint:

- empty DB
- default limit + DESC ordering
- before=<turn_id> cursor pagination
- cross-channel unification (Web + Discord rows mixed in one response)
- soft-deleted rows excluded
- out-of-range limits → 422
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import MessageRole
from echovessel.memory import (
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


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "p_hist"
display_name = "Hist"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-history-")
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


def _seed_session(rt: Runtime, session_id: str, channel_id: str) -> None:
    """Idempotently create a session + the persona/user foreign keys."""
    with DbSession(rt.ctx.engine) as db:
        if db.get(Persona, "p_hist") is None:
            db.add(Persona(id="p_hist", display_name="Hist"))
        if db.get(User, "self") is None:
            db.add(User(id="self", display_name="Alan"))
        db.commit()

        if db.get(RecallSession, session_id) is None:
            db.add(
                RecallSession(
                    id=session_id,
                    persona_id="p_hist",
                    user_id="self",
                    channel_id=channel_id,
                )
            )
            db.commit()


def _seed_message(
    rt: Runtime,
    *,
    session_id: str,
    channel_id: str,
    turn_id: str,
    content: str,
    role: MessageRole,
    created_at: datetime,
    deleted: bool = False,
) -> int:
    """Insert one recall_messages row and return its PK."""
    with DbSession(rt.ctx.engine) as db:
        msg = RecallMessage(
            session_id=session_id,
            persona_id="p_hist",
            user_id="self",
            channel_id=channel_id,
            role=role,
            content=content,
            day=created_at.date(),
            turn_id=turn_id,
            created_at=created_at,
            deleted_at=created_at if deleted else None,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg.id


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


def test_history_empty_db_returns_empty_list() -> None:
    _rt, client = _build()
    with client:
        resp = client.get("/api/chat/history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "messages": [],
        "has_more": False,
        "oldest_turn_id": None,
    }


# ---------------------------------------------------------------------------
# Default limit + DESC ordering
# ---------------------------------------------------------------------------


def test_history_default_limit_50_desc_order() -> None:
    rt, client = _build()
    _seed_session(rt, "s_web", "web")

    base = datetime(2026, 4, 16, 12, 0, 0)
    # Seed 55 messages so we can verify both the 50 cap and has_more.
    for i in range(55):
        _seed_message(
            rt,
            session_id="s_web",
            channel_id="web",
            turn_id=f"t_{i:03d}",
            content=f"msg {i}",
            role=MessageRole.USER,
            created_at=base + timedelta(minutes=i),
        )

    with client:
        resp = client.get("/api/chat/history")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["messages"]) == 50
    assert body["has_more"] is True
    # DESC order: the newest message (i=54) lands first.
    assert body["messages"][0]["content"] == "msg 54"
    # oldest_turn_id is the turn id of the LAST item (= oldest in the
    # window) — for 50 newest-first rows from a 55-item pool that's i=5.
    assert body["messages"][-1]["content"] == "msg 5"
    assert body["oldest_turn_id"] == "t_005"


# ---------------------------------------------------------------------------
# before=<turn_id> cursor
# ---------------------------------------------------------------------------


def test_history_before_cursor_paginates_correctly() -> None:
    rt, client = _build()
    _seed_session(rt, "s_web", "web")

    base = datetime(2026, 4, 16, 12, 0, 0)
    for i in range(10):
        _seed_message(
            rt,
            session_id="s_web",
            channel_id="web",
            turn_id=f"t_{i:03d}",
            content=f"msg {i}",
            role=MessageRole.USER,
            created_at=base + timedelta(minutes=i),
        )

    with client:
        # Grab first page of 3.
        page1 = client.get("/api/chat/history?limit=3").json()
        assert [m["content"] for m in page1["messages"]] == [
            "msg 9",
            "msg 8",
            "msg 7",
        ]
        assert page1["has_more"] is True
        assert page1["oldest_turn_id"] == "t_007"

        # Paginate before the oldest of page1.
        page2 = client.get(
            f"/api/chat/history?limit=3&before={page1['oldest_turn_id']}"
        ).json()
    assert [m["content"] for m in page2["messages"]] == [
        "msg 6",
        "msg 5",
        "msg 4",
    ]
    # Still 4 older rows after page2 → has_more stays true.
    assert page2["has_more"] is True


def test_history_before_cursor_404_on_missing_turn() -> None:
    rt, client = _build()
    _seed_session(rt, "s_web", "web")
    with client:
        resp = client.get("/api/chat/history?before=t_does_not_exist")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Cross-channel unification (iron rule D4)
# ---------------------------------------------------------------------------


def test_history_cross_channel_unified() -> None:
    """Web + Discord rows interleave in one timeline, newest-first."""
    rt, client = _build()
    _seed_session(rt, "s_web", "web")
    _seed_session(rt, "s_discord", "discord:dm:42")

    base = datetime(2026, 4, 16, 12, 0, 0)
    for i in range(5):
        _seed_message(
            rt,
            session_id="s_web",
            channel_id="web",
            turn_id=f"tw_{i:03d}",
            content=f"web {i}",
            role=MessageRole.USER,
            created_at=base + timedelta(minutes=i * 2),  # even minutes
        )
        _seed_message(
            rt,
            session_id="s_discord",
            channel_id="discord:dm:42",
            turn_id=f"td_{i:03d}",
            content=f"discord {i}",
            role=MessageRole.USER,
            created_at=base + timedelta(minutes=i * 2 + 1),  # odd minutes
        )

    with client:
        resp = client.get("/api/chat/history?limit=50")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["messages"]) == 10

    channel_ids = {m["source_channel_id"] for m in body["messages"]}
    assert channel_ids == {"web", "discord:dm:42"}

    # Interleaved order holds: newest = discord 4 (minute 9), then web
    # 4 (minute 8), etc.
    contents = [m["content"] for m in body["messages"]]
    assert contents[0] == "discord 4"
    assert contents[1] == "web 4"
    assert contents[-1] == "web 0"


# ---------------------------------------------------------------------------
# Soft-deleted exclusion
# ---------------------------------------------------------------------------


def test_history_excludes_soft_deleted() -> None:
    rt, client = _build()
    _seed_session(rt, "s_web", "web")

    base = datetime(2026, 4, 16, 12, 0, 0)
    _seed_message(
        rt,
        session_id="s_web",
        channel_id="web",
        turn_id="t_alive",
        content="alive",
        role=MessageRole.USER,
        created_at=base,
    )
    _seed_message(
        rt,
        session_id="s_web",
        channel_id="web",
        turn_id="t_dead",
        content="dead",
        role=MessageRole.USER,
        created_at=base + timedelta(minutes=1),
        deleted=True,
    )

    with client:
        resp = client.get("/api/chat/history")
    body = resp.json()
    assert [m["content"] for m in body["messages"]] == ["alive"]


# ---------------------------------------------------------------------------
# Limit validation
# ---------------------------------------------------------------------------


def test_history_limit_over_200_returns_422() -> None:
    _rt, client = _build()
    with client:
        resp = client.get("/api/chat/history?limit=201")
    assert resp.status_code == 422, resp.text


def test_history_limit_zero_returns_422() -> None:
    _rt, client = _build()
    with client:
        resp = client.get("/api/chat/history?limit=0")
    assert resp.status_code == 422, resp.text

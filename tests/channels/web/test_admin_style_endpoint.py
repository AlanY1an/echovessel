"""Spec 2 · ``POST /api/admin/persona/style`` + ``POST /api/admin/users/timezone``.

Covers the two admin endpoints that land with the L1 style block + the
users.timezone auto-detect path (plan §6.6 + decision 5).
"""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.core.types import BlockLabel
from echovessel.memory import CoreBlock, User
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
id = "style-test"
display_name = "Initial"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-style-")
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


# ---------------------------------------------------------------------------
# POST /api/admin/persona/style
# ---------------------------------------------------------------------------


def test_style_set_writes_new_block() -> None:
    rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "避免开头说'哈哈'"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "action": "set"}

    with DbSession(rt.ctx.engine) as db:
        rows = list(
            db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == "style-test",
                    CoreBlock.label == BlockLabel.STYLE.value,
                    CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
    assert len(rows) == 1
    assert "哈哈" in rows[0].content
    assert rows[0].user_id is None  # STYLE is a shared block


def test_style_set_twice_replaces_content() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "rule one"},
        )
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "rule two"},
        )
    assert resp.status_code == 200

    with DbSession(rt.ctx.engine) as db:
        active = list(
            db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == "style-test",
                    CoreBlock.label == BlockLabel.STYLE.value,
                    CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
    assert len(active) == 1
    assert active[0].content == "rule two"
    assert "rule one" not in active[0].content


def test_style_append_joins_with_newline() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "first"},
        )
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "append", "text": "second"},
        )
    assert resp.status_code == 200

    with DbSession(rt.ctx.engine) as db:
        row = db.exec(
            select(CoreBlock).where(
                CoreBlock.persona_id == "style-test",
                CoreBlock.label == BlockLabel.STYLE.value,
                CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).one()
    assert "first" in row.content
    assert "second" in row.content


def test_style_clear_soft_deletes_row() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "rule"},
        )
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "clear", "text": ""},
        )
    assert resp.status_code == 200

    with DbSession(rt.ctx.engine) as db:
        active = list(
            db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == "style-test",
                    CoreBlock.label == BlockLabel.STYLE.value,
                    CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )
        )
    assert active == []


def test_style_set_empty_text_rejected_with_400() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "set", "text": "   "},
        )
    assert resp.status_code == 400


def test_style_unknown_action_rejected() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/persona/style",
            json={"action": "toggle", "text": ""},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/admin/users/timezone
# ---------------------------------------------------------------------------


def test_timezone_first_write_sets_column() -> None:
    rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/users/timezone",
            json={"timezone": "America/New_York"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["written"] is True
    assert body["timezone"] == "America/New_York"

    with DbSession(rt.ctx.engine) as db:
        user = db.get(User, "self")
    assert user is not None
    assert user.timezone == "America/New_York"


def test_timezone_second_write_without_override_keeps_original() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/users/timezone",
            json={"timezone": "America/New_York"},
        )
        resp = client.post(
            "/api/admin/users/timezone",
            json={"timezone": "Asia/Taipei"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["written"] is False
    assert body["timezone"] == "America/New_York"


def test_timezone_override_replaces_existing() -> None:
    rt, client = _build()
    with client:
        client.post(
            "/api/admin/users/timezone",
            json={"timezone": "America/New_York"},
        )
        resp = client.post(
            "/api/admin/users/timezone",
            json={"timezone": "Asia/Taipei", "override": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["written"] is True
    assert body["timezone"] == "Asia/Taipei"


def test_timezone_bad_iana_rejected_400() -> None:
    _rt, client = _build()
    with client:
        resp = client.post(
            "/api/admin/users/timezone",
            json={"timezone": "Mars/Red_Planet"},
        )
    assert resp.status_code == 400

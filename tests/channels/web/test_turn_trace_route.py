"""Spec 4 · admin trace endpoints (list / detail / consolidate)."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
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
id = "p_trace4"
display_name = "T4"

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

[dev_trace]
enabled = true
"""


def _build() -> tuple[Runtime, TestClient]:
    tmp = tempfile.mkdtemp(prefix="echovessel-trace4-")
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


def _insert_turn_trace(rt: Runtime, *, turn_id: str, started_at: datetime) -> None:
    with DbSession(rt.ctx.engine) as db:
        db.execute(
            text(
                "INSERT INTO turn_traces ("
                "turn_id, persona_id, user_id, channel_id, "
                "started_at, finished_at, "
                "system_prompt, user_prompt, "
                "retrieval, pinned_thoughts, entity_alias_hits, episodic_state, "
                "llm_model, input_tokens, output_tokens, "
                "duration_ms, first_token_ms, steps"
                ") VALUES ("
                ":turn_id, :persona_id, :user_id, :channel_id, "
                ":started_at, :finished_at, "
                ":system_prompt, :user_prompt, "
                ":retrieval, :pinned_thoughts, :entity_alias_hits, :episodic_state, "
                ":llm_model, :input_tokens, :output_tokens, "
                ":duration_ms, :first_token_ms, :steps)"
            ),
            {
                "turn_id": turn_id,
                "persona_id": "p_trace4",
                "user_id": "self",
                "channel_id": "web",
                "started_at": started_at,
                "finished_at": started_at + timedelta(seconds=2),
                "system_prompt": "# Persona\nyou are X",
                "user_prompt": "# What they said\nhi",
                "retrieval": json.dumps(
                    [
                        {
                            "node_id": 7,
                            "type": "event",
                            "desc_snippet": "ate ramen",
                            "recency": 0.5,
                            "relevance": 0.5,
                            "impact": 0.3,
                            "relational": 0.0,
                            "entity_anchor": 0.0,
                            "total": 0.6,
                            "anchored": False,
                        }
                    ]
                ),
                "pinned_thoughts": json.dumps({"user": [], "persona": []}),
                "entity_alias_hits": json.dumps([]),
                "episodic_state": json.dumps({"mood": "calm"}),
                "llm_model": "claude-haiku",
                "input_tokens": 100,
                "output_tokens": 30,
                "duration_ms": 2000,
                "first_token_ms": 200,
                "steps": json.dumps(
                    [
                        {
                            "stage": "ingest_user",
                            "t_ms": 0,
                            "duration_ms": 5,
                            "detail": {"message_count": 1},
                        }
                    ]
                ),
            },
        )
        db.commit()


def _insert_session_trace(rt: Runtime, *, session_id: str) -> None:
    with DbSession(rt.ctx.engine) as db:
        db.execute(
            text(
                "INSERT INTO session_traces ("
                "session_id, finished_at, "
                "phase_a, phase_b, phase_c, phase_d, phase_e, phase_f, phase_g"
                ") VALUES ("
                ":session_id, :finished_at, "
                ":phase_a, :phase_b, :phase_c, :phase_d, :phase_e, :phase_f, :phase_g)"
            ),
            {
                "session_id": session_id,
                "finished_at": datetime.utcnow(),
                "phase_a": json.dumps(
                    {"is_trivial": False, "reason": "above_threshold"}
                ),
                "phase_b": json.dumps(
                    {
                        "events_created": [{"id": 1, "description": "x"}],
                        "junction_rejects": [],
                    }
                ),
                "phase_c": None,
                "phase_d": json.dumps(
                    {"timer_due": True, "reflections_last_24h": 0}
                ),
                "phase_e": None,
                "phase_f": json.dumps({"status": "closed"}),
                "phase_g": json.dumps({"ran": False}),
            },
        )
        db.commit()


def test_list_turns_returns_recent_headers() -> None:
    rt, client = _build()
    now = datetime.utcnow()
    _insert_turn_trace(rt, turn_id="t-old", started_at=now - timedelta(minutes=5))
    _insert_turn_trace(rt, turn_id="t-new", started_at=now)
    with client:
        resp = client.get("/api/admin/turns?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [item["turn_id"] for item in body["items"]]
    assert ids[0] == "t-new"  # most-recent first
    assert "t-old" in ids
    assert body["limit"] == 10
    # Header-only — full prompt not included in list payload.
    assert "system_prompt" not in body["items"][0]


def test_get_turn_trace_returns_full_payload() -> None:
    rt, client = _build()
    _insert_turn_trace(rt, turn_id="t-x", started_at=datetime.utcnow())
    with client:
        resp = client.get("/api/admin/turns/t-x")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["turn_id"] == "t-x"
    assert body["llm_model"] == "claude-haiku"
    assert body["system_prompt"].startswith("# Persona")
    assert body["retrieval"][0]["node_id"] == 7
    assert body["retrieval"][0]["desc_snippet"] == "ate ramen"
    # plan §4.2 schema columns are all present on each row
    assert {"recency", "relevance", "impact", "relational", "entity_anchor",
            "total", "anchored"}.issubset(body["retrieval"][0])
    assert body["steps"][0]["stage"] == "ingest_user"
    assert body["pinned_thoughts"] == {"user": [], "persona": []}


def test_get_turn_trace_404_for_unknown_id() -> None:
    rt, client = _build()
    with client:
        resp = client.get("/api/admin/turns/never-existed")
    assert resp.status_code == 404


def test_get_consolidate_trace_returns_phases() -> None:
    rt, client = _build()
    _insert_session_trace(rt, session_id="s-1")
    with client:
        resp = client.get("/api/admin/sessions/s-1/consolidate-trace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "s-1"
    assert body["phase_a"]["reason"] == "above_threshold"
    assert body["phase_b"]["junction_rejects"] == []
    assert body["phase_c"] is None
    assert body["phase_g"]["ran"] is False


def test_get_consolidate_trace_404_for_unknown_id() -> None:
    rt, client = _build()
    with client:
        resp = client.get("/api/admin/sessions/never/consolidate-trace")
    assert resp.status_code == 404


def test_list_turns_limit_clamped_by_query_validator() -> None:
    rt, client = _build()
    with client:
        # Above the upper bound → 422 from FastAPI's Query(le=100).
        resp = client.get("/api/admin/turns?limit=999")
    assert resp.status_code == 422

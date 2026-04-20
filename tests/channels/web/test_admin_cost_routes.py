"""Worker ζ · admin Cost tab route shape tests.

Mirrors the test_admin_*.py rigging used elsewhere — file-backed SQLite
+ TestClient against a Runtime built via ``config_override``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.cost_logger import CostRecorder
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "cost-test"
display_name = "CostTest"

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
    tmp = tempfile.mkdtemp(prefix="echovessel-cost-")
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


def _seed_calls(rt: Runtime, *, when: datetime, feature: str, n: int) -> None:
    """Persist ``n`` rows directly via CostRecorder so the route
    response is deterministic."""

    def _factory() -> DbSession:
        return DbSession(rt.ctx.engine)

    recorder = CostRecorder(_factory)
    for i in range(n):
        recorder.record(
            provider="openai_compat",
            model="gpt-4o",
            feature=feature,
            tier="medium",
            input_text=f"input {i} " * 4,
            output_text=f"output {i} " * 4,
            timestamp=when + timedelta(minutes=i),
        )


# ---------------------------------------------------------------------------
# /api/admin/cost/summary
# ---------------------------------------------------------------------------


def test_cost_summary_empty_returns_zero_totals() -> None:
    _rt, client = _build_rig()
    with client:
        resp = client.get("/api/admin/cost/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "30d"
    assert body["total_usd"] == 0.0
    assert body["total_tokens"] == 0
    assert body["by_feature"] == {}
    assert body["by_day"] == []


def test_cost_summary_groups_by_feature_with_seeded_rows() -> None:
    rt, client = _build_rig()
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_calls(rt, when=today, feature="chat", n=2)
    _seed_calls(rt, when=today - timedelta(days=2), feature="import", n=1)
    with client:
        resp = client.get("/api/admin/cost/summary", params={"range": "7d"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "7d"
    assert body["total_usd"] > 0
    assert set(body["by_feature"].keys()) == {"chat", "import"}
    assert body["by_feature"]["chat"]["calls"] == 2
    assert body["by_feature"]["import"]["calls"] == 1
    assert len(body["by_day"]) >= 2


def test_cost_summary_today_window_excludes_yesterday() -> None:
    rt, client = _build_rig()
    today_noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    _seed_calls(rt, when=today_noon, feature="chat", n=1)
    _seed_calls(rt, when=today_noon - timedelta(days=1), feature="chat", n=4)
    with client:
        resp = client.get("/api/admin/cost/summary", params={"range": "today"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["by_feature"]["chat"]["calls"] == 1


def test_cost_summary_invalid_range_returns_422() -> None:
    _rt, client = _build_rig()
    with client:
        resp = client.get("/api/admin/cost/summary", params={"range": "all-time"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /api/admin/cost/recent
# ---------------------------------------------------------------------------


def test_cost_recent_returns_newest_first() -> None:
    rt, client = _build_rig()
    base = datetime(2026, 4, 16, 12, 0, 0)
    _seed_calls(rt, when=base, feature="chat", n=4)
    with client:
        resp = client.get("/api/admin/cost/recent", params={"limit": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 3
    assert len(body["items"]) == 3
    times = [item["timestamp"] for item in body["items"]]
    assert times == sorted(times, reverse=True)
    sample = body["items"][0]
    expected_keys = {
        "id",
        "timestamp",
        "provider",
        "model",
        "feature",
        "tier",
        "tokens_in",
        "tokens_out",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cost_usd",
        "turn_id",
    }
    assert set(sample.keys()) == expected_keys


def test_cost_recent_limit_cap_enforced() -> None:
    _rt, client = _build_rig()
    with client:
        resp = client.get(
            "/api/admin/cost/recent", params={"limit": 9999}
        )
    assert resp.status_code == 422

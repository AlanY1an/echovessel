"""Worker · admin model-catalog route shape tests.

Mirrors the test_admin_*.py rigging used elsewhere — file-backed SQLite
+ TestClient against a Runtime built via ``config_override``.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

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
id = "catalog-test"
display_name = "CatalogTest"

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


@pytest.fixture
def admin_test_client() -> TestClient:
    tmp = tempfile.mkdtemp(prefix="echovessel-catalog-")
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
    return TestClient(app)


def test_get_model_catalog_returns_preset_shape(admin_test_client: TestClient) -> None:
    resp = admin_test_client.get("/api/admin/config/models")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    providers = {row["provider"] for row in payload}
    assert {"anthropic", "openai_compat"} <= providers
    for row in payload:
        assert set(row.keys()) >= {
            "provider",
            "model",
            "display_name",
            "input_per_1k_usd",
            "output_per_1k_usd",
            "cache_read_per_1k_usd",
            "cache_creation_per_1k_usd",
        }


def test_get_model_catalog_attaches_price_for_known_model(
    admin_test_client: TestClient,
) -> None:
    """gpt-4o is in LiteLLM's snapshot reliably; its price fields must
    be populated and positive (not null, not 0)."""
    resp = admin_test_client.get("/api/admin/config/models?provider=openai_compat")
    rows = resp.json()
    gpt4o = next(r for r in rows if r["model"] == "gpt-4o")
    assert gpt4o["input_per_1k_usd"] is not None
    assert gpt4o["input_per_1k_usd"] > 0
    assert gpt4o["output_per_1k_usd"] > gpt4o["input_per_1k_usd"]


def test_get_model_catalog_filters_by_provider(admin_test_client: TestClient) -> None:
    resp = admin_test_client.get("/api/admin/config/models?provider=anthropic")
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert all(row["provider"] == "anthropic" for row in rows)

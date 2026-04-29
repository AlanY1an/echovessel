"""Worker η · Admin Config route tests.

Exercises:

- GET  /api/admin/config — safe subset (no secrets), system-info fold-in
- PATCH /api/admin/config — atomic TOML write + reload

The fixture writes a real ``config.toml`` on disk because the PATCH
path reuses the ``_atomic_write_config_patches`` helper, which requires
``ctx.config_path`` to be set (refuses `config_override` mode).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.runtime import Runtime, build_zero_embedder
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "config-test"
display_name = "Before"

[memory]
db_path = "memory.db"
retrieve_k = 10
recent_window_size = 20

[llm]
provider = "stub"
model = "stub-model"
api_key_env = ""
temperature = 0.7
max_tokens = 1024
timeout_seconds = 60

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1
trivial_message_count = 3
trivial_token_count = 200
reflection_hard_gate_24h = 3

[idle_scanner]
interval_seconds = 60
"""


def _build(tmp_path: Path) -> tuple[Runtime, TestClient, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(_toml(str(data_dir)))
    rt = Runtime.build(
        config_path,
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
    return rt, TestClient(app), config_path


# ---------------------------------------------------------------------------
# GET /api/admin/config
# ---------------------------------------------------------------------------


def test_get_config_returns_safe_subset_with_api_key_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every expected section is present and no secret leaks through."""
    # Force a known state for api_key_present: point api_key_env at an
    # env var we set, so `api_key_present=true` is deterministic.
    monkeypatch.setenv("STUB_CONFIG_KEY", "any-value")

    rt, client, cfg_path = _build(tmp_path)

    with client:
        # Seed llm.api_key_env to STUB_CONFIG_KEY so the GET sees it
        # as "present". PATCH will be exercised below in its own test.
        patch_resp = client.patch(
            "/api/admin/config",
            json={"llm": {"api_key_env": "STUB_CONFIG_KEY"}},
        )
        assert patch_resp.status_code == 200, patch_resp.text

        resp = client.get("/api/admin/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Structural sections present.
    for key in ("llm", "persona", "memory", "consolidate", "system"):
        assert key in body, f"missing section: {key}"

    # api_key_env echoed but NO key material.
    assert body["llm"]["api_key_env"] == "STUB_CONFIG_KEY"
    assert "api_key" not in body["llm"]  # never leak
    assert body["llm"]["api_key_present"] is True

    # System info has everything the UI renders.
    assert body["system"]["data_dir"]
    assert body["system"]["db_path"] == "memory.db"
    assert isinstance(body["system"]["uptime_seconds"], int)
    assert isinstance(body["system"]["db_size_bytes"], int)
    assert body["system"]["config_path"] == str(cfg_path)


def test_get_config_reports_api_key_absent_when_env_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the env var is unset, api_key_present is False — UI uses this
    to show a "missing key" warning dot."""
    monkeypatch.delenv("NOT_SET_IN_ENV", raising=False)

    rt, client, _ = _build(tmp_path)
    with client:
        # Point api_key_env at a var we know isn't set.
        patch_resp = client.patch(
            "/api/admin/config",
            json={"llm": {"api_key_env": "NOT_SET_IN_ENV"}},
        )
        assert patch_resp.status_code == 200, patch_resp.text

        resp = client.get("/api/admin/config")
    body = resp.json()
    assert body["llm"]["api_key_env"] == "NOT_SET_IN_ENV"
    assert body["llm"]["api_key_present"] is False


# ---------------------------------------------------------------------------
# PATCH /api/admin/config — happy paths
# ---------------------------------------------------------------------------


def test_patch_llm_model_persists_to_toml_and_live_ctx(
    tmp_path: Path,
) -> None:
    """Editing llm.model writes the TOML AND reloads ctx.config.llm."""
    rt, client, cfg_path = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"llm": {"model": "gpt-4o-mini"}},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated_fields"] == ["llm.model"]
    assert body["reload_triggered"] is True

    # Disk reflects the change.
    on_disk = tomllib.loads(cfg_path.read_text())
    assert on_disk["llm"]["model"] == "gpt-4o-mini"

    # Live ctx too (reload() swapped ctx.config).
    assert rt.ctx.config.llm.model == "gpt-4o-mini"


def test_patch_persona_display_name_mirrors_into_live_ctx(
    tmp_path: Path,
) -> None:
    """display_name must end up in (a) TOML, (b) ctx.config.persona,
    (c) ctx.persona (the live RuntimePersonaContext object)."""
    rt, client, cfg_path = _build(tmp_path)
    assert rt.ctx.persona.display_name == "Before"

    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"persona": {"display_name": "After"}},
        )
    assert resp.status_code == 200, resp.text

    on_disk = tomllib.loads(cfg_path.read_text())
    assert on_disk["persona"]["display_name"] == "After"
    assert rt.ctx.config.persona.display_name == "After"
    assert rt.ctx.persona.display_name == "After"


def test_patch_multi_section_atomic(tmp_path: Path) -> None:
    """One PATCH call can mix fields from several sections."""
    rt, client, cfg_path = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={
                "memory": {"retrieve_k": 15, "relational_bonus_weight": 1.5},
                "consolidate": {"trivial_message_count": 5},
            },
        )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["updated_fields"]) == {
        "memory.retrieve_k",
        "memory.relational_bonus_weight",
        "consolidate.trivial_message_count",
    }

    on_disk = tomllib.loads(cfg_path.read_text())
    assert on_disk["memory"]["retrieve_k"] == 15
    assert on_disk["memory"]["relational_bonus_weight"] == 1.5
    assert on_disk["consolidate"]["trivial_message_count"] == 5
    assert rt.ctx.config.memory.retrieve_k == 15


# ---------------------------------------------------------------------------
# PATCH /api/admin/config — rejection paths
# ---------------------------------------------------------------------------


def test_patch_restart_required_field_returns_400(tmp_path: Path) -> None:
    """Editing runtime.data_dir or memory.db_path at runtime is refused."""
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"memory": {"db_path": "somewhere-else.db"}},
        )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "memory.db_path" in detail
    assert "restart" in detail.lower()


def test_patch_unknown_field_returns_400(tmp_path: Path) -> None:
    """Fields outside the allowlist (even if valid Pydantic keys) 400."""
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"memory": {"embedder": "all-mpnet-base-v2"}},
        )
    assert resp.status_code == 400, resp.text
    assert "memory.embedder" in resp.json()["detail"]


def test_patch_invalid_provider_returns_422(tmp_path: Path) -> None:
    """Pydantic rejects unknown llm.provider — Literal["anthropic",
    "openai_compat", "stub"]."""
    rt, client, cfg_path = _build(tmp_path)
    before = cfg_path.read_text()
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"llm": {"provider": "palm"}},
        )
    assert resp.status_code == 422, resp.text
    # The TOML must NOT have been mutated on validation failure.
    assert cfg_path.read_text() == before


def test_patch_out_of_range_temperature_returns_422(tmp_path: Path) -> None:
    """llm.temperature is bounded to [0.0, 2.0]; 5.0 → 422."""
    rt, client, cfg_path = _build(tmp_path)
    before_temp = rt.ctx.config.llm.temperature

    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"llm": {"temperature": 5.0}},
        )
    assert resp.status_code == 422, resp.text
    # ctx.config was NOT mutated (reload was never triggered).
    assert rt.ctx.config.llm.temperature == before_temp


def test_patch_empty_body_returns_400(tmp_path: Path) -> None:
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch("/api/admin/config", json={})
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# PATCH /api/admin/config — env-var presence check (Task 6)
# ---------------------------------------------------------------------------


def test_patch_rejects_missing_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the patch sets `llm.api_key_env` to a name that isn't in os.environ,
    the server returns 422 with a field-targeted error so the UI can flag
    the input. We do NOT want the patch to silently succeed and have the
    next LLM call fail at runtime instead."""
    monkeypatch.delenv("DEFINITELY_NOT_SET_KEY", raising=False)
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={
                "llm": {
                    "provider": "anthropic",
                    "api_key_env": "DEFINITELY_NOT_SET_KEY",
                }
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    detail = body["detail"]
    assert any(
        err.get("field") == "llm.api_key_env" and "DEFINITELY_NOT_SET_KEY" in err.get("msg", "")
        for err in detail
    )


def test_patch_accepts_present_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_KEY_THAT_EXISTS", "stub-value")
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={
                "llm": {
                    "provider": "anthropic",
                    "api_key_env": "FAKE_KEY_THAT_EXISTS",
                }
            },
        )
    assert resp.status_code == 200, resp.text


def test_patch_skips_env_check_for_stub_provider(tmp_path: Path) -> None:
    rt, client, _ = _build(tmp_path)
    with client:
        resp = client.patch(
            "/api/admin/config",
            json={"llm": {"provider": "stub", "api_key_env": ""}},
        )
    assert resp.status_code == 200, resp.text

"""Stage 3 admin route tests.

Uses FastAPI's ``TestClient`` against an in-process app built on top
of a real :class:`Runtime` instance. The runtime is constructed with
``config_override`` so nothing touches disk — except the voice-toggle
test that needs a real config.toml to verify the atomic-write path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.memory import CoreBlock
from echovessel.runtime import (
    Runtime,
    build_zero_embedder,
    load_config_from_str,
)
from echovessel.runtime.llm import StubProvider


def _toml(data_dir: str, *, db_path: str = "memory.db") -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "admin-test"
display_name = "Initial"

[memory]
db_path = "{db_path}"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


def _build_runtime_and_app() -> tuple[Runtime, TestClient]:
    # Use a file-backed SQLite (not ":memory:") so FastAPI's TestClient
    # — which runs async handlers in a threadpool bridged via anyio —
    # can see the tables that Runtime.build() created on a different
    # thread. ":memory:" databases are per-connection and would fail
    # on "no such table: core_blocks" the moment TestClient's thread
    # opens its first session.
    tmp = tempfile.mkdtemp(prefix="echovessel-admin-")
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


def _build_runtime_from_file(tmp_path: Path) -> tuple[Runtime, TestClient, Path]:
    """Build a runtime with a real config.toml on disk.

    The voice-toggle tests need this to exercise the atomic write path
    inside :meth:`Runtime.update_persona_voice_enabled`.
    """

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
# GET /api/state
# ---------------------------------------------------------------------------


def test_get_state_fresh_install_onboarding_required() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_required"] is True
    assert data["memory_counts"]["core_blocks"] == 0
    assert data["memory_counts"]["messages"] == 0
    assert data["memory_counts"]["events"] == 0
    assert data["memory_counts"]["thoughts"] == 0
    assert data["persona"]["id"] == "admin-test"
    assert data["persona"]["display_name"] == "Initial"
    assert data["persona"]["voice_enabled"] is False
    assert data["persona"]["has_voice_id"] is False


def test_get_state_channels_lists_known_entries_unregistered() -> None:
    """With nothing in the registry, every known channel is emitted as
    enabled=False / ready=False so the admin UI can show a stable
    "未启用" dim row per channel type."""
    _rt, client = _build_runtime_and_app()
    with client:
        resp = client.get("/api/state")
    assert resp.status_code == 200
    channels = resp.json()["channels"]

    # Stable canonical order (web/discord/imessage) — the UI strip
    # depends on this.
    assert [c["channel_id"] for c in channels] == ["web", "discord", "imessage"]
    assert [c["name"] for c in channels] == ["Web", "Discord", "iMessage"]
    for c in channels:
        assert c["enabled"] is False
        assert c["ready"] is False


def test_get_state_channels_web_registered_reports_enabled_ready() -> None:
    """When the web channel is registered in runtime's registry, its
    entry flips to enabled=True / ready=True (Web.is_ready() is
    always True by contract)."""
    rt, client = _build_runtime_and_app()
    # The TestClient builder hands us a WebChannel but does not wire it
    # into ``rt.ctx.registry`` — register it here so /api/state sees it.
    web_ch = WebChannel(debounce_ms=50)
    rt.ctx.registry.register(web_ch)

    with client:
        resp = client.get("/api/state")
    assert resp.status_code == 200
    by_id = {c["channel_id"]: c for c in resp.json()["channels"]}

    assert by_id["web"] == {
        "channel_id": "web",
        "name": "Web",
        "enabled": True,
        "ready": True,
    }
    # Non-registered siblings stay disabled.
    assert by_id["discord"]["enabled"] is False
    assert by_id["discord"]["ready"] is False
    assert by_id["imessage"]["enabled"] is False


def test_get_state_channels_is_ready_false_reports_not_ready() -> None:
    """A registered channel whose is_ready() returns False (e.g. a
    Discord bot still in the gateway handshake) surfaces as
    enabled=True / ready=False — this is the transient "连接中" state
    the admin UI renders as an orange dot."""

    class _FakeDiscord:
        channel_id = "discord"
        name = "Discord"
        in_flight_turn_id = None

        def is_ready(self) -> bool:
            return False

        async def start(self) -> None: ...
        async def stop(self) -> None: ...
        async def incoming(self):  # pragma: no cover - not exercised
            if False:
                yield
        async def send(self, msg) -> None: ...
        async def on_turn_done(self, turn_id: str) -> None: ...

    rt, client = _build_runtime_and_app()
    rt.ctx.registry.register(_FakeDiscord())

    with client:
        resp = client.get("/api/state")
    assert resp.status_code == 200
    by_id = {c["channel_id"]: c for c in resp.json()["channels"]}

    assert by_id["discord"]["enabled"] is True
    assert by_id["discord"]["ready"] is False


def test_get_state_after_onboarding_reports_not_required() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        r1 = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "Luna is curious and gentle.",
                "user_block": "",
            },
        )
        assert r1.status_code == 200

        r2 = client.get("/api/state")
    assert r2.status_code == 200
    data = r2.json()
    assert data["onboarding_required"] is False
    assert data["memory_counts"]["core_blocks"] >= 1
    assert data["persona"]["display_name"] == "Luna"


# ---------------------------------------------------------------------------
# POST /api/admin/persona/onboarding
# ---------------------------------------------------------------------------


def test_post_onboarding_writes_core_blocks() -> None:
    rt, client = _build_runtime_and_app()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P block",
                "user_block": "U block",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "persona_id": "admin-test"}

    with DbSession(rt.ctx.engine) as db:
        rows = list(
            db.exec(select(CoreBlock).where(CoreBlock.persona_id == "admin-test"))
        )
    labels = {getattr(r.label, "value", r.label): r.content for r in rows}
    assert labels.get("persona") == "P block"
    assert labels.get("user") == "U block"


def test_post_onboarding_duplicate_returns_409() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        body = {
            "display_name": "Luna",
            "persona_block": "P",
            "user_block": "",
        }
        r1 = client.post("/api/admin/persona/onboarding", json=body)
        assert r1.status_code == 200
        r2 = client.post("/api/admin/persona/onboarding", json=body)
    assert r2.status_code == 409
    assert "onboarding already completed" in r2.json()["detail"]


def test_post_onboarding_empty_blocks_are_silently_skipped() -> None:
    """Empty strings in onboarding are OK per §3 — they are just
    skipped at write time. Only non-empty blocks land in memory."""

    rt, client = _build_runtime_and_app()
    with client:
        resp = client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "only this",
                "user_block": "",
            },
        )
    assert resp.status_code == 200

    with DbSession(rt.ctx.engine) as db:
        rows = list(
            db.exec(select(CoreBlock).where(CoreBlock.persona_id == "admin-test"))
        )
    labels = {getattr(r.label, "value", r.label) for r in rows}
    assert labels == {"persona"}  # only the one non-empty block


# ---------------------------------------------------------------------------
# GET /api/admin/persona
# ---------------------------------------------------------------------------


def test_get_persona_returns_three_blocks_empty_for_unwritten() -> None:
    """v0.5 · GET /api/admin/persona returns the 3-block dict only.

    L1.self and L1.relationship were dropped (plan §1); their keys
    must NOT appear in the response. STYLE shows up because it's now
    a shared block alongside persona.
    """
    _rt, client = _build_runtime_and_app()
    with client:
        resp = client.get("/api/admin/persona")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "admin-test"
    assert data["core_blocks"] == {
        "persona": "",
        "user": "",
        "style": "",
    }
    # Belt-and-braces: legacy keys must be absent (not just empty).
    for legacy in ("self", "relationship", "mood"):
        assert legacy not in data["core_blocks"]


def test_get_persona_reflects_onboarding_writes() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "user_block": "U",
            },
        )
        resp = client.get("/api/admin/persona")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Luna"
    assert data["core_blocks"]["persona"] == "P"
    assert data["core_blocks"]["user"] == "U"
    assert data["core_blocks"]["style"] == ""


# ---------------------------------------------------------------------------
# POST /api/admin/persona (partial update)
# ---------------------------------------------------------------------------


def test_post_persona_partial_update_only_touches_present_fields() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        # Seed via onboarding.
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P1",
                "user_block": "U1",
            },
        )
        # Partial update: only persona_block present.
        resp = client.post(
            "/api/admin/persona",
            json={"persona_block": "P2"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        snapshot = client.get("/api/admin/persona").json()

    # persona appended ("P1\nP2"), user unchanged.
    assert "P2" in snapshot["core_blocks"]["persona"]
    assert "P1" in snapshot["core_blocks"]["persona"]
    assert snapshot["core_blocks"]["user"] == "U1"
    # display_name untouched because it was not in the request body.
    assert snapshot["display_name"] == "Luna"


def test_post_persona_display_name_update_applies() -> None:
    rt, client = _build_runtime_and_app()
    with client:
        client.post(
            "/api/admin/persona/onboarding",
            json={
                "display_name": "Luna",
                "persona_block": "P",
                "user_block": "",
            },
        )
        resp = client.post(
            "/api/admin/persona",
            json={"display_name": "Stella"},
        )
    assert resp.status_code == 200
    assert rt.ctx.persona.display_name == "Stella"


# ---------------------------------------------------------------------------
# POST /api/admin/persona/voice-toggle
# ---------------------------------------------------------------------------


def test_post_voice_toggle_config_override_mode_returns_400() -> None:
    """Runtime built via ``config_override`` has no config path — the
    toggle must reject with 400 instead of crashing."""

    _rt, client = _build_runtime_and_app()
    with client:
        resp = client.post(
            "/api/admin/persona/voice-toggle", json={"enabled": True}
        )
    assert resp.status_code == 400
    assert "config file" in resp.json()["detail"]


def test_post_voice_toggle_true_persists_to_config(
    tmp_path: Path,
) -> None:
    rt, client, config_path = _build_runtime_from_file(tmp_path)
    with client:
        resp = client.post(
            "/api/admin/persona/voice-toggle", json={"enabled": True}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "voice_enabled": True}
    assert rt.ctx.persona.voice_enabled is True
    # The atomic writer updated config.toml on disk.
    assert "voice_enabled = true" in config_path.read_text()


def test_post_voice_toggle_false_persists_to_config(
    tmp_path: Path,
) -> None:
    rt, client, config_path = _build_runtime_from_file(tmp_path)
    with client:
        client.post(
            "/api/admin/persona/voice-toggle", json={"enabled": True}
        )
        resp = client.post(
            "/api/admin/persona/voice-toggle", json={"enabled": False}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "voice_enabled": False}
    assert rt.ctx.persona.voice_enabled is False
    assert "voice_enabled = false" in config_path.read_text()


def test_post_voice_toggle_missing_enabled_returns_422() -> None:
    _rt, client = _build_runtime_and_app()
    with client:
        resp = client.post("/api/admin/persona/voice-toggle", json={})
    assert resp.status_code == 422

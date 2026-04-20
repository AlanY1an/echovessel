"""`Runtime.update_persona_voice_enabled` (spec §17a.7) tests.

Four cases cover:
    - happy path: atomic write + ctx update + SSE broadcast
    - rollback when atomic write fails: ctx stays on old value
    - config_override mode (no config_path): raises RuntimeError
    - live read: interaction reads the current value on the next turn
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from echovessel.channels.base import OutgoingMessage
from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str
from echovessel.runtime.llm import StubProvider

TOML = """
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "p"
display_name = "Sage"
voice_enabled = false

[memory]
db_path = ":memory:"

[llm]
provider = "stub"
api_key_env = ""

[consolidate]
worker_poll_seconds = 1
worker_max_retries = 1

[idle_scanner]
interval_seconds = 60
"""


def _write_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(TOML.format(data_dir=str(tmp_path / "data")))
    return cfg_path


class _SSEChannel:
    channel_id = "web"
    name = "Web"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.in_flight_turn_id: str | None = None

    async def push_sse(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload)))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def incoming(self):
        async def _gen():
            if False:
                yield None

        return _gen()

    async def send(self, msg: OutgoingMessage) -> None:
        pass

    async def on_turn_done(self, turn_id: str) -> None:
        self.in_flight_turn_id = None


async def test_update_persona_voice_enabled_atomic(tmp_path: Path):
    cfg_path = _write_config(tmp_path)
    rt = Runtime.build(cfg_path, llm=StubProvider(fallback=""), embed_fn=build_zero_embedder())
    channel = _SSEChannel()
    await rt.start(channels=[channel], register_signals=False)
    try:
        assert rt.ctx.persona.voice_enabled is False

        await rt.update_persona_voice_enabled(True)

        # ctx reflects the new value.
        assert rt.ctx.persona.voice_enabled is True

        # config.toml on disk was updated.
        with open(cfg_path, "rb") as f:
            on_disk = tomllib.load(f)
        assert on_disk["persona"]["voice_enabled"] is True

        # SSE broadcast fired.
        assert any(
            ev == "chat.settings.updated" and pl == {"voice_enabled": True}
            for ev, pl in channel.events
        )
    finally:
        await rt.stop()


async def test_update_persona_voice_enabled_rollback_on_write_fail(tmp_path: Path):
    cfg_path = _write_config(tmp_path)
    rt = Runtime.build(cfg_path, llm=StubProvider(fallback=""), embed_fn=build_zero_embedder())
    await rt.start(channels=[], register_signals=False)
    try:
        assert rt.ctx.persona.voice_enabled is False

        # Force _atomic_write_config_field to blow up.
        def _boom(**_kwargs):
            raise OSError("disk full")

        rt._atomic_write_config_field = _boom  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="failed to persist"):
            await rt.update_persona_voice_enabled(True)

        # Rollback: ctx stayed on the old value.
        assert rt.ctx.persona.voice_enabled is False
    finally:
        await rt.stop()


async def test_update_persona_voice_enabled_config_override_mode():
    cfg = load_config_from_str(TOML.format(data_dir="/tmp/ev-override"))
    rt = Runtime.build(
        None,
        config_override=cfg,
        llm=StubProvider(fallback=""),
        embed_fn=build_zero_embedder(),
    )
    await rt.start(channels=[], register_signals=False)
    try:
        with pytest.raises(RuntimeError, match="config_override mode"):
            await rt.update_persona_voice_enabled(True)
        assert rt.ctx.persona.voice_enabled is False
    finally:
        await rt.stop()


async def test_interaction_reads_voice_enabled_live(tmp_path: Path):
    """The `RuntimeContextPersonaView` used by proactive must re-read
    `voice_enabled` on every property access so an admin toggle is
    visible on the next tick without any reload plumbing."""
    cfg_path = _write_config(tmp_path)
    rt = Runtime.build(cfg_path, llm=StubProvider(fallback=""), embed_fn=build_zero_embedder())
    await rt.start(channels=[], register_signals=False)
    try:
        view = rt._make_persona_view()
        assert view.voice_enabled is False

        await rt.update_persona_voice_enabled(True)
        # Same view object — no re-instantiation — reads the new value.
        assert view.voice_enabled is True

        await rt.update_persona_voice_enabled(False)
        assert view.voice_enabled is False
    finally:
        await rt.stop()

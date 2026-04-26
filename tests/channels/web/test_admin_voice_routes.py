"""Worker λ · voice clone wizard HTTP routes.

Exercises the six ``/api/admin/voice/*`` endpoints through FastAPI's
:class:`TestClient` with a real :class:`Runtime` plus a stub-provider
:class:`VoiceService`. No network, no FishAudio SDK, no real TTS.

What the tests pin down:

- Upload validates non-empty payload and hands back a ``sample_id``
- ``GET`` lists every surviving sample with the ``minimum_required=3`` gate
- ``DELETE`` removes the draft directory on disk
- ``POST /clone`` refuses with 400 below the sample floor, returns a
  ``voice_id`` once the floor is met
- ``POST /preview`` streams non-empty bytes marked as ``audio/mpeg``
- ``POST /activate`` flips ``[persona].voice_id`` in config.toml
  via the atomic-write path and mirrors the change in memory
"""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from echovessel.channels.web.app import build_web_app
from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.routes.admin.helpers import _VOICE_SAMPLE_MIN_COUNT
from echovessel.channels.web.sse import SSEBroadcaster
from echovessel.runtime import Runtime, build_zero_embedder, load_config_from_str
from echovessel.runtime.llm import StubProvider
from echovessel.voice.service import VoiceService
from echovessel.voice.stub import StubVoiceProvider


def _toml(data_dir: str) -> str:
    return f"""
[runtime]
data_dir = "{data_dir}"
log_level = "warn"

[persona]
id = "voice-test"
display_name = "V"

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


def _voice_service(cache_dir: Path) -> VoiceService:
    """Build a stub-backed VoiceService. Same stub is used for TTS + STT."""
    stub = StubVoiceProvider()
    return VoiceService(tts=stub, stt=stub, voice_cache_dir=cache_dir)


def _build(tmp_path: Path) -> tuple[Runtime, TestClient, Path]:
    """Build a runtime from a real config.toml on disk so activate's
    atomic-write path can actually reach the file."""
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
        voice_service=_voice_service(data_dir / "voice_cache"),
        heartbeat_seconds=0.5,
    )
    return rt, TestClient(app), config_path


def _build_without_voice(tmp_path: Path) -> TestClient:
    """Build an app without a voice_service to test the 503 gate."""
    tmp = tempfile.mkdtemp(dir=str(tmp_path))
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


# ---------------------------------------------------------------------------
# Upload / list / delete
# ---------------------------------------------------------------------------


def test_post_voice_sample_stores_file_and_returns_metadata(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        resp = client.post(
            "/api/admin/voice/samples",
            files={"file": ("clip.wav", b"RIFFxxxxWAVEfmt pretend-audio", "audio/wav")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["sample_id"].startswith("s-")
    assert body["size_bytes"] == len(b"RIFFxxxxWAVEfmt pretend-audio")


def test_post_voice_sample_rejects_empty_upload(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        resp = client.post(
            "/api/admin/voice/samples",
            files={"file": ("empty.wav", b"", "audio/wav")},
        )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_post_voice_sample_rejects_oversize_before_reading_body(
    tmp_path: Path, monkeypatch
) -> None:
    """Oversize uploads must 413 via the Content-Length header BEFORE the
    body is materialized into memory.

    Regression test for audit P1-6. The handler used to call
    ``await file.read()`` unconditionally, then check the size — which
    meant a 10 GB upload fully allocated memory before being rejected.
    The fix reads ``Content-Length`` from the request headers and
    short-circuits with 413 before any body read.
    """
    from starlette.datastructures import UploadFile as StarletteUploadFile

    from echovessel.channels.web.routes.admin import voice as voice_module

    # Dial the cap down so we don't need a 50 MB body in the test.
    monkeypatch.setattr(voice_module, "_VOICE_SAMPLE_MAX_BYTES", 10)

    read_calls: list[int] = []
    original_read = StarletteUploadFile.read

    async def tracking_read(self, *args, **kwargs):
        read_calls.append(1)
        return await original_read(self, *args, **kwargs)

    monkeypatch.setattr(StarletteUploadFile, "read", tracking_read)

    _rt, client, _cfg = _build(tmp_path)
    with client:
        # Body is 100 bytes; cap is 10. The Content-Length of the whole
        # multipart body will be >> 10, so the early check fires.
        resp = client.post(
            "/api/admin/voice/samples",
            files={"file": ("big.wav", b"x" * 100, "audio/wav")},
        )

    assert resp.status_code == 413, resp.text
    assert read_calls == [], (
        "UploadFile.read must NOT be called when Content-Length exceeds the "
        "size cap — the header check has to short-circuit the handler"
    )


def test_get_voice_samples_lists_uploads_with_min_required(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        # Start empty.
        r0 = client.get("/api/admin/voice/samples")
        assert r0.status_code == 200
        assert r0.json()["count"] == 0
        assert r0.json()["minimum_required"] == _VOICE_SAMPLE_MIN_COUNT

        # Upload two samples — count reflects it.
        client.post(
            "/api/admin/voice/samples",
            files={"file": ("a.wav", b"one", "audio/wav")},
        )
        client.post(
            "/api/admin/voice/samples",
            files={"file": ("b.wav", b"two", "audio/wav")},
        )

        r2 = client.get("/api/admin/voice/samples")
    body = r2.json()
    assert body["count"] == 2
    assert body["minimum_required"] == _VOICE_SAMPLE_MIN_COUNT
    assert len(body["samples"]) == 2
    filenames = sorted(s["filename"] for s in body["samples"])
    assert filenames == ["a.wav", "b.wav"]


def test_delete_voice_sample_removes_from_disk(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        r_up = client.post(
            "/api/admin/voice/samples",
            files={"file": ("c.wav", b"delete-me", "audio/wav")},
        )
        sid = r_up.json()["sample_id"]

        r_del = client.delete(f"/api/admin/voice/samples/{sid}")
        assert r_del.status_code == 200
        assert r_del.json() == {"deleted": True, "sample_id": sid}

        r_list = client.get("/api/admin/voice/samples")
    assert r_list.json()["count"] == 0


def test_delete_voice_sample_404_on_unknown_id(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        r = client.delete("/api/admin/voice/samples/s-doesnotexist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Clone gating
# ---------------------------------------------------------------------------


def test_clone_refuses_below_minimum_samples(tmp_path: Path) -> None:
    """The minimum-sample gate must reject a clone call whenever the
    sample count is below ``_VOICE_SAMPLE_MIN_COUNT``. Zero samples is
    the only value guaranteed to be below any non-negative floor, so
    the test exercises the gate independent of its exact numeric value.
    """
    _rt, client, _cfg = _build(tmp_path)
    with client:
        # Don't upload anything — 0 samples is below every possible floor.
        r = client.post(
            "/api/admin/voice/clone", json={"display_name": "my voice"}
        )
    assert r.status_code == 400
    assert "samples" in r.json()["detail"].lower()


def test_clone_succeeds_with_three_samples_and_returns_preview(
    tmp_path: Path,
) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        for i, body in enumerate((b"alpha", b"beta", b"gamma")):
            client.post(
                "/api/admin/voice/samples",
                files={"file": (f"s{i}.wav", body, "audio/wav")},
            )
        r = client.post(
            "/api/admin/voice/clone",
            json={"display_name": "Luna-voice-1"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["voice_id"].startswith("stub-voice-")
    assert body["display_name"] == "Luna-voice-1"
    assert isinstance(body["preview_text"], str) and body["preview_text"]
    # The stub provider's speak() path yields bytes, so preview_audio_url
    # should be populated (cached via VoiceService.generate_voice).
    assert body["preview_audio_url"] is not None


# ---------------------------------------------------------------------------
# Preview streaming
# ---------------------------------------------------------------------------


def test_preview_streams_audio_bytes(tmp_path: Path) -> None:
    _rt, client, _cfg = _build(tmp_path)
    with client:
        r = client.post(
            "/api/admin/voice/preview",
            json={"voice_id": "stub-voice-xyz", "text": "hello"},
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    # StubVoiceProvider.speak emits a deterministic byte blob.
    assert len(r.content) > 0


# ---------------------------------------------------------------------------
# Activate
# ---------------------------------------------------------------------------


def test_activate_writes_voice_id_to_config_toml_and_memory(
    tmp_path: Path,
) -> None:
    rt, client, cfg_path = _build(tmp_path)
    assert rt.ctx.persona.voice_id is None  # sanity

    with client:
        r = client.post(
            "/api/admin/voice/activate",
            json={"voice_id": "stub-voice-activated"},
        )
    assert r.status_code == 200
    assert r.json() == {"activated": True, "voice_id": "stub-voice-activated"}

    # In-memory mirror.
    assert rt.ctx.persona.voice_id == "stub-voice-activated"

    # On-disk atomic write landed in the [persona] section.
    with open(cfg_path, "rb") as f:
        parsed = tomllib.load(f)
    assert parsed["persona"]["voice_id"] == "stub-voice-activated"


def test_activate_returns_400_without_config_file(tmp_path: Path) -> None:
    """A daemon booted with ``config_override`` has no file to rewrite;
    the atomic-write path must refuse rather than silently succeed."""
    tmp = tempfile.mkdtemp(dir=str(tmp_path))
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
        voice_service=_voice_service(Path(tmp) / "voice_cache"),
        heartbeat_seconds=0.5,
    )
    client = TestClient(app)
    with client:
        r = client.post(
            "/api/admin/voice/activate",
            json={"voice_id": "stub-voice-x"},
        )
    assert r.status_code == 400
    assert "config_override" in r.json()["detail"] or "config" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Voice service disabled (no VoiceService supplied)
# ---------------------------------------------------------------------------


def test_clone_returns_503_when_voice_service_missing(tmp_path: Path) -> None:
    """The wizard is a no-op surface when [voice].enabled = false. Routes
    that need the VoiceService must 503 rather than 500."""
    client = _build_without_voice(tmp_path)
    with client:
        r = client.post(
            "/api/admin/voice/clone", json={"display_name": "x"}
        )
    assert r.status_code == 503
    assert "voice" in r.json()["detail"].lower()

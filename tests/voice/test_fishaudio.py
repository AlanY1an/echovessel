"""Tests for FishAudioProvider.

All fish-audio-sdk calls are mocked by injecting a fake module into
sys.modules before the lazy `_get_client()` import. Tests run without
the real SDK being installed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from echovessel.voice.base import VoiceMeta
from echovessel.voice.errors import (
    VoiceBudgetError,
    VoicePermanentError,
    VoiceTransientError,
)
from echovessel.voice.fishaudio import (
    FishAudioProvider,
    _classify_fishaudio_error,
    _sync_collect_tts_chunks,
    build_fishaudio_from_env,
)

# ---------------------------------------------------------------------------
# Identity / lazy import
# ---------------------------------------------------------------------------


def test_provider_name():
    p = FishAudioProvider(api_key="fake")
    assert p.provider_name == "fishaudio"


def test_is_cloud():
    p = FishAudioProvider(api_key="fake")
    assert p.is_cloud is True


def test_supports_cloning():
    p = FishAudioProvider(api_key="fake")
    assert p.supports_cloning is True


def test_top_level_does_not_import_fish_audio_sdk():
    """fishaudio.py must lazy-import fish_audio_sdk.

    AST-walk the module body to verify no top-level import statement
    references fish_audio_sdk. Docstrings mentioning the name are OK.
    """
    import ast

    import echovessel.voice.fishaudio as mod

    with open(mod.__file__) as f:
        tree = ast.parse(f.read())

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "fish_audio_sdk" not in alias.name, (
                    f"top-level `import {alias.name}` is forbidden"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "fish_audio_sdk" not in node.module, (
                f"top-level `from {node.module} import ...` is forbidden"
            )


# ---------------------------------------------------------------------------
# Empty / missing api_key
# ---------------------------------------------------------------------------


def test_get_client_without_api_key_raises():
    p = FishAudioProvider(api_key=None)
    with pytest.raises(VoicePermanentError, match="api_key is empty"):
        p._get_client()


def test_get_client_with_empty_api_key_raises():
    p = FishAudioProvider(api_key="")
    with pytest.raises(VoicePermanentError, match="api_key is empty"):
        p._get_client()


# ---------------------------------------------------------------------------
# Fake fish_audio_sdk module fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fish_sdk(monkeypatch):
    """Inject a fake `fish_audio_sdk` module for the duration of a test."""
    fake_mod = types.ModuleType("fish_audio_sdk")

    class FakeSession:
        def __init__(self, apikey: str) -> None:
            self.apikey = apikey
            self.voices = MagicMock()
            self._tts_chunks: list[bytes] = [b"chunk-1", b"chunk-2", b"chunk-3"]
            self._tts_exception: Exception | None = None
            self._last_tts_request: object | None = None

        def tts(self, request):
            self._last_tts_request = request
            if self._tts_exception is not None:
                raise self._tts_exception
            return iter(self._tts_chunks)

    class FakeTTSRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_mod.Session = FakeSession
    fake_mod.TTSRequest = FakeTTSRequest
    monkeypatch.setitem(sys.modules, "fish_audio_sdk", fake_mod)
    yield fake_mod


# ---------------------------------------------------------------------------
# speak() — happy path
# ---------------------------------------------------------------------------


async def test_speak_yields_chunks_in_order(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake-key")

    out: list[bytes] = []
    async for chunk in p.speak("hello world"):
        out.append(chunk)

    assert out == [b"chunk-1", b"chunk-2", b"chunk-3"]


async def test_speak_passes_text_and_voice_id(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake-key")

    async for _ in p.speak("the quick brown fox", voice_id="v_42"):
        pass

    session: object = p._client  # type: ignore[assignment]
    req = session._last_tts_request  # type: ignore[attr-defined]
    assert req.text == "the quick brown fox"
    assert req.reference_id == "v_42"
    assert req.format == "mp3"


async def test_speak_default_voice_id_passes_no_reference_id(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake-key")

    async for _ in p.speak("hi"):
        pass

    session = p._client
    req = session._last_tts_request  # type: ignore[attr-defined]
    assert not hasattr(req, "reference_id") or req.__dict__.get("reference_id") is None


async def test_speak_empty_text_raises():
    p = FishAudioProvider(api_key="fake")
    with pytest.raises(ValueError, match="non-empty"):
        async for _ in p.speak(""):
            pass


# ---------------------------------------------------------------------------
# speak() — asyncio.to_thread is actually used
# ---------------------------------------------------------------------------


async def test_speak_uses_asyncio_to_thread(fake_fish_sdk, monkeypatch):
    """The sync SDK call MUST go through asyncio.to_thread.
    We replace `asyncio.to_thread` with a spy to verify it's invoked.
    """
    import asyncio as asyncio_module

    call_log: list[str] = []
    real_to_thread = asyncio_module.to_thread

    async def spy_to_thread(fn, /, *args, **kwargs):
        call_log.append(getattr(fn, "__name__", str(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(
        "echovessel.voice.fishaudio.asyncio.to_thread", spy_to_thread
    )

    p = FishAudioProvider(api_key="fake-key")
    async for _ in p.speak("hello"):
        pass

    assert any("sync_collect_tts_chunks" in name for name in call_log), call_log


# ---------------------------------------------------------------------------
# speak() — error classification
# ---------------------------------------------------------------------------


async def test_speak_server_error_maps_to_transient(fake_fish_sdk):
    class FakeServerError(Exception):
        status_code = 503

    p = FishAudioProvider(api_key="fake")
    p._get_client()  # prime self._client
    p._client._tts_exception = FakeServerError("upstream")  # type: ignore[attr-defined]

    with pytest.raises(VoiceTransientError, match="server error 503"):
        async for _ in p.speak("hi"):
            pass


async def test_speak_auth_error_maps_to_permanent(fake_fish_sdk):
    class FakeAuthError(Exception):
        status_code = 401

    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client._tts_exception = FakeAuthError("bad key")  # type: ignore[attr-defined]

    with pytest.raises(VoicePermanentError, match="auth error 401"):
        async for _ in p.speak("hi"):
            pass


async def test_speak_quota_error_maps_to_budget(fake_fish_sdk):
    class FakeQuotaError(Exception):
        status_code = 402

    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client._tts_exception = FakeQuotaError("quota")  # type: ignore[attr-defined]

    with pytest.raises(VoiceBudgetError):
        async for _ in p.speak("hi"):
            pass


async def test_speak_rate_limit_maps_to_transient(fake_fish_sdk):
    class FakeRateError(Exception):
        status_code = 429

    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client._tts_exception = FakeRateError("slow")  # type: ignore[attr-defined]

    with pytest.raises(VoiceTransientError, match="rate limited"):
        async for _ in p.speak("hi"):
            pass


# ---------------------------------------------------------------------------
# clone_voice()
# ---------------------------------------------------------------------------


async def test_clone_voice_calls_sdk_correctly(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    # Prime client and set up the voices mock
    p._get_client()
    p._client.create_model = MagicMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(id="fishmodel_new123")
    )

    voice_id = await p.clone_voice(b"wav data", name="alice")

    assert voice_id == "fishmodel_new123"
    p._client.create_model.assert_called_once()  # type: ignore[attr-defined]
    call_kwargs = p._client.create_model.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["title"] == "alice"
    assert call_kwargs["voices"] == [b"wav data"]


async def test_clone_voice_from_path(fake_fish_sdk, tmp_path: Path):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.create_model = MagicMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(id="fishmodel_path")
    )

    sample_path = tmp_path / "me.wav"
    sample_path.write_bytes(b"file content")
    voice_id = await p.clone_voice(sample_path, name="me")

    assert voice_id == "fishmodel_path"
    call_kwargs = p._client.create_model.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["voices"] == [b"file content"]


async def test_clone_voice_empty_sample_raises(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()

    with pytest.raises(VoicePermanentError, match="sample is empty"):
        await p.clone_voice(b"", name="x")


async def test_clone_voice_fallback_attribute_name(fake_fish_sdk):
    """Some SDK versions return `_id` instead of `id`."""
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    fake_voice = SimpleNamespace(_id="fallback_id")
    p._client.create_model = MagicMock(return_value=fake_voice)  # type: ignore[attr-defined]

    voice_id = await p.clone_voice(b"data", name="x")
    assert voice_id == "fallback_id"


async def test_clone_voice_missing_id_raises(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    # Object with neither `id` nor `_id`
    p._client.create_model = MagicMock(return_value=object())  # type: ignore[attr-defined]

    with pytest.raises(VoicePermanentError, match="no id"):
        await p.clone_voice(b"data", name="x")


async def test_clone_voice_sdk_exception_classified(fake_fish_sdk):
    class FakeForbiddenError(Exception):
        status_code = 403

    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.create_model = MagicMock(side_effect=FakeForbiddenError("nope"))  # type: ignore[attr-defined]

    with pytest.raises(VoicePermanentError, match="auth error 403"):
        await p.clone_voice(b"data", name="x")


# ---------------------------------------------------------------------------
# list_voices()
# ---------------------------------------------------------------------------


async def test_list_voices_returns_voice_meta(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    fake_voices = [
        SimpleNamespace(id="v1", title="Alice", language="en"),
        SimpleNamespace(id="v2", title="Bob", language="zh"),
    ]
    p._client.list_models = MagicMock(return_value=iter(fake_voices))  # type: ignore[attr-defined]

    voices = await p.list_voices()
    assert len(voices) == 2
    assert all(isinstance(v, VoiceMeta) for v in voices)
    assert voices[0].voice_id == "v1"
    assert voices[0].display_name == "Alice"
    assert voices[0].provider_name == "fishaudio"
    assert voices[0].language == "en"


async def test_list_voices_handles_missing_fields(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.list_models = MagicMock(  # type: ignore[attr-defined]
        return_value=iter([SimpleNamespace(id="v1")])
    )

    voices = await p.list_voices()
    assert voices[0].voice_id == "v1"
    assert voices[0].display_name == "unknown"
    assert voices[0].language is None


async def test_list_voices_sdk_error_classified(fake_fish_sdk):
    class FakeServerError(Exception):
        status_code = 500

    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.list_models = MagicMock(side_effect=FakeServerError("boom"))  # type: ignore[attr-defined]

    with pytest.raises(VoiceTransientError):
        await p.list_voices()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def test_health_check_success(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.list_models = MagicMock(return_value=iter([]))  # type: ignore[attr-defined]

    assert await p.health_check() is True


async def test_health_check_failure(fake_fish_sdk):
    p = FishAudioProvider(api_key="fake")
    p._get_client()
    p._client.list_models = MagicMock(  # type: ignore[attr-defined]
        side_effect=Exception("unreachable")
    )

    assert await p.health_check() is False


async def test_health_check_without_key_returns_false():
    p = FishAudioProvider(api_key=None)
    assert await p.health_check() is False


# ---------------------------------------------------------------------------
# _sync_collect_tts_chunks unit behavior
# ---------------------------------------------------------------------------


def test_sync_collect_rejects_non_bytes_chunk(fake_fish_sdk):
    class WeirdClient:
        def tts(self, req):
            yield "not-bytes"  # type: ignore[misc]

    with pytest.raises(VoicePermanentError, match="non-bytes chunk"):
        _sync_collect_tts_chunks(
            WeirdClient(), text="hi", voice_id=None, format="mp3"
        )


# ---------------------------------------------------------------------------
# Error classifier
# ---------------------------------------------------------------------------


def test_classify_via_response_attribute():
    class SDKError(Exception):
        def __init__(self, status):
            super().__init__("sdk")
            self.response = SimpleNamespace(status_code=status)

    assert isinstance(_classify_fishaudio_error(SDKError(500)), VoiceTransientError)
    assert isinstance(_classify_fishaudio_error(SDKError(401)), VoicePermanentError)
    assert isinstance(_classify_fishaudio_error(SDKError(402)), VoiceBudgetError)


def test_classify_connection_error_maps_to_transient():
    class FakeConnectionError(Exception):
        pass

    result = _classify_fishaudio_error(FakeConnectionError("dns"))
    assert isinstance(result, VoiceTransientError)


def test_classify_unknown_error_defaults_to_permanent():
    class MysteryError(Exception):
        pass

    result = _classify_fishaudio_error(MysteryError("???"))
    assert isinstance(result, VoicePermanentError)


def test_classify_passes_through_voice_errors():
    err = VoiceTransientError("already classified")
    assert _classify_fishaudio_error(err) is err


# ---------------------------------------------------------------------------
# build_fishaudio_from_env
# ---------------------------------------------------------------------------


def test_build_from_env_reads_environment_variable(monkeypatch):
    monkeypatch.setenv("FISH_API_KEY_TEST", "fish-key-xyz")
    p = build_fishaudio_from_env(api_key_env="FISH_API_KEY_TEST")
    assert p._api_key == "fish-key-xyz"


def test_build_from_env_missing_is_none(monkeypatch):
    monkeypatch.delenv("MISSING_FISH_KEY_VAR", raising=False)
    p = build_fishaudio_from_env(api_key_env="MISSING_FISH_KEY_VAR")
    assert p._api_key is None

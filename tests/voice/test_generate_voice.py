"""Tests for `VoiceService.generate_voice` — the v0.2 facade method.

Spec: docs/voice/01-spec-v0.1.md §4.7a. All tests here exercise the
new facade only; they MUST NOT change the existing `speak()` / provider
contracts (round2 tracker §3 R3 hard rule).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from echovessel.voice import VoiceResult
from echovessel.voice.errors import VoicePermanentError
from echovessel.voice.pricing import COST_PER_CHAR_USD, estimate_tts_cost
from echovessel.voice.service import VoiceService
from echovessel.voice.stub import StubVoiceProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    tmp_path: Path,
    *,
    tts=None,
) -> VoiceService:
    tts = tts if tts is not None else StubVoiceProvider()
    stt = StubVoiceProvider()
    return VoiceService(
        tts=tts,
        stt=stt,
        default_voice_id="v_persona",
        voice_cache_dir=tmp_path / "voice_cache",
    )


class _FakeFishLikeTTS:
    """Minimal stand-in that reports `provider_name == "fishaudio"`.

    Intentionally does NOT subclass `StubVoiceProvider` so the pricing
    path is exercised against the exact provider label the real
    `FishAudioProvider` uses.
    """

    provider_name = "fishaudio"
    is_cloud = True
    supports_cloning = True

    def speak(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        format: str = "mp3",
    ) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"FAKE-FISH-AUDIO-"
            yield text.encode("utf-8")

        return _gen()

    async def clone_voice(self, sample, *, name):  # pragma: no cover
        raise NotImplementedError

    async def list_voices(self):  # pragma: no cover
        return []

    async def health_check(self) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# 1. returns VoiceResult with all fields populated
# ---------------------------------------------------------------------------


async def test_generate_voice_returns_voice_result(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    result = await svc.generate_voice(
        "hello world",
        voice_id="v_persona",
        message_id=42,
    )

    assert isinstance(result, VoiceResult)
    assert result.url == "/api/chat/voice/42.mp3"
    assert result.provider == "stub"
    assert result.cached is False
    assert result.cost_usd == 0.0  # stub is free
    assert isinstance(result.duration_seconds, float)
    assert result.duration_seconds >= 0.0

    # Cache artifact must have been written atomically.
    cache_file = tmp_path / "voice_cache" / "42.mp3"
    assert cache_file.exists()
    assert cache_file.read_bytes().startswith(b"STUB-mp3-")
    # No lingering .tmp file after a successful atomic write.
    assert not (tmp_path / "voice_cache" / "42.mp3.tmp").exists()


async def test_voice_result_is_frozen(tmp_path: Path) -> None:
    """Spec §4.7a: VoiceResult is a frozen dataclass — immutable shape."""
    svc = _make_service(tmp_path)
    result = await svc.generate_voice("hi", voice_id="v", message_id=1)
    with pytest.raises((AttributeError, TypeError)):
        result.cost_usd = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. cost_usd via hard-coded fishaudio rate
# ---------------------------------------------------------------------------


async def test_generate_voice_cost_fishaudio(tmp_path: Path) -> None:
    svc = _make_service(tmp_path, tts=_FakeFishLikeTTS())
    text = "hello fishaudio cost"  # len == 20
    result = await svc.generate_voice(
        text,
        voice_id="fish_voice",
        message_id=7,
    )

    assert result.provider == "fishaudio"
    assert result.cached is False
    assert result.cost_usd == pytest.approx(len(text) * COST_PER_CHAR_USD["fishaudio"])
    assert result.cost_usd == pytest.approx(estimate_tts_cost("fishaudio", text))


async def test_generate_voice_cost_cache_hit_is_zero(tmp_path: Path) -> None:
    """Second call for same message_id must not touch the provider and
    MUST return cost_usd=0.0 / cached=True (spec §4.7a.1)."""
    fake = _FakeFishLikeTTS()
    call_count = 0
    original_speak = fake.speak

    def counting_speak(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_speak(*args, **kwargs)

    fake.speak = counting_speak  # type: ignore[assignment]

    svc = _make_service(tmp_path, tts=fake)
    first = await svc.generate_voice(
        "some text", voice_id="fv", message_id=9
    )
    second = await svc.generate_voice(
        "some text", voice_id="fv", message_id=9
    )

    assert first.cached is False
    assert first.cost_usd > 0.0  # fishaudio non-zero
    assert second.cached is True
    assert second.cost_usd == 0.0
    assert second.url == first.url
    assert call_count == 1, "cache hit must skip provider.speak"


# ---------------------------------------------------------------------------
# 3. stub provider cost is zero
# ---------------------------------------------------------------------------


async def test_generate_voice_cost_stub_is_zero(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    result = await svc.generate_voice(
        "plenty of text here to charge by character",
        voice_id="v_persona",
        message_id=1,
    )
    assert result.provider == "stub"
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# 4. tone_hint="neutral" works
# ---------------------------------------------------------------------------


async def test_generate_voice_tone_hint_neutral(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    result = await svc.generate_voice(
        "neutral test",
        voice_id="v_persona",
        message_id=100,
        tone_hint="neutral",
    )
    assert isinstance(result, VoiceResult)
    assert result.cached is False
    # Provider field does not embed any tone label (spec §4.7a.4).
    assert result.provider == "stub"


# ---------------------------------------------------------------------------
# 5. non-neutral tone_hint degrades + warns (spec §4.7a.4)
# ---------------------------------------------------------------------------


async def test_generate_voice_tone_hint_other_degrades(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _make_service(tmp_path)

    with caplog.at_level(logging.WARNING, logger="echovessel.voice.service"):
        result = await svc.generate_voice(
            "whisper please",
            voice_id="v_persona",
            message_id=200,
            tone_hint="whisper",
        )

    # Spec §4.7a.4: degrade to neutral, log warning, provider field has
    # no tone tag.
    assert isinstance(result, VoiceResult)
    assert result.provider == "stub"
    assert any(
        "tone_hint" in record.message and "whisper" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 6. round1 speak() surface is unchanged (regression fence)
# ---------------------------------------------------------------------------


async def test_speak_unchanged(tmp_path: Path) -> None:
    """Explicit guard: round2 must not alter `VoiceService.speak()`.

    Exercises the exact round1 call shape — positional text, keyword
    voice_id/format — and asserts the streaming AsyncIterator contract.
    """
    svc = _make_service(tmp_path, tts=StubVoiceProvider())

    # Default voice path.
    chunks: list[bytes] = []
    async for chunk in svc.speak("hello"):
        chunks.append(chunk)
    out_default = b"".join(chunks)
    assert out_default.startswith(b"STUB-mp3-")

    # Explicit voice override path.
    chunks = []
    async for chunk in svc.speak("hello", voice_id="v_other", format="wav"):
        chunks.append(chunk)
    out_override = b"".join(chunks)
    assert out_override.startswith(b"STUB-wav-")
    assert out_override != out_default

    # Sanity check: generate_voice and speak are separate code paths.
    # speak() does not touch the voice cache directory.
    assert not (tmp_path / "voice_cache").exists()


# ---------------------------------------------------------------------------
# 7. errors bubble up without leaving partial cache artifacts
# ---------------------------------------------------------------------------


async def test_generate_voice_provider_error_leaves_no_cache_file(
    tmp_path: Path,
) -> None:
    broken = MagicMock()
    broken.provider_name = "fishaudio"

    async def _bad_speak(*args, **kwargs):
        raise RuntimeError("upstream exploded")
        yield b""  # unreachable; marker for async-generator typing

    broken.speak = _bad_speak

    svc = VoiceService(
        tts=broken,
        stt=StubVoiceProvider(),
        voice_cache_dir=tmp_path / "voice_cache",
    )

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await svc.generate_voice("boom", voice_id="v", message_id=500)

    # Spec §4.7a.6: no partial artifact left behind.
    cache_file = tmp_path / "voice_cache" / "500.mp3"
    assert not cache_file.exists()
    tmp_file = tmp_path / "voice_cache" / "500.mp3.tmp"
    assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# 8. cache write runs off the event-loop thread (CLAUDE.md blocking-I/O rule)
# ---------------------------------------------------------------------------


async def test_generate_voice_cache_write_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atomic write (open/write/fsync/replace) must execute in a
    worker thread via asyncio.to_thread, never on the loop thread."""
    import threading

    from echovessel.voice import service as service_mod

    loop_thread = threading.current_thread()
    writer_threads: list[threading.Thread] = []
    real_writer = service_mod._write_cache_atomic

    def spying_writer(*args, **kwargs):
        writer_threads.append(threading.current_thread())
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(service_mod, "_write_cache_atomic", spying_writer)

    svc = _make_service(tmp_path)
    result = await svc.generate_voice(
        "off the loop",
        voice_id="v_persona",
        message_id=314,
    )

    assert result.cached is False
    assert len(writer_threads) == 1
    assert writer_threads[0] is not loop_thread
    # Atomic-replace semantics preserved: final file, no .tmp leftover.
    assert (tmp_path / "voice_cache" / "314.mp3").exists()
    assert not (tmp_path / "voice_cache" / "314.mp3.tmp").exists()


# ---------------------------------------------------------------------------
# 9. cache write failure raises VoicePermanentError (spec §4.7a.7)
# ---------------------------------------------------------------------------


async def test_generate_voice_cache_write_failure_raises_permanent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _make_service(tmp_path)

    def _broken_mkdir(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _broken_mkdir)

    with pytest.raises(VoicePermanentError, match="voice cache write failed"):
        await svc.generate_voice(
            "some text",
            voice_id="v_persona",
            message_id=999,
        )

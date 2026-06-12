"""build_voice_service — factory from a VoiceServiceConfig to VoiceService.

Called from:
- `runtime/app.py` at startup (§3 step 10.5 per voice spec §7.3.1)
- Future CLI subcommand `echovessel voice clone` (Thread RT Round 2)
- Tests that want a real VoiceService composed from config

Style aligned with `runtime/llm/factory.build_llm_provider`: a tiny
function that maps string provider names to concrete classes, with lazy
imports for any SDK-backed provider.

See docs/voice/01-spec-v0.1.md §4.7 and §6.2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from echovessel.voice.base import AudioFormat, STTProvider, TTSProvider
from echovessel.voice.cloning import FingerprintCache
from echovessel.voice.service import VoiceService
from echovessel.voice.stub import StubVoiceProvider


@dataclass(frozen=True, slots=True)
class VoiceServiceConfig:
    """Minimal config shape for building a VoiceService.

    This is a voice-module-internal dataclass. Runtime's `VoiceSection`
    Pydantic model (Thread RT Round 2) will map from `[voice]` in
    `config.toml` to this shape and call `build_voice_service(cfg)`.

    See voice spec §6.2 for the authoritative field semantics.
    """

    enabled: bool = True
    tts_provider: str = "fishaudio"  # 'fishaudio' | 'stub'
    stt_provider: str = "whisper_api"  # 'whisper_api' | 'stub'
    tts_api_key_env: str = "FISH_AUDIO_KEY"
    stt_api_key_env: str = "OPENAI_API_KEY"
    default_audio_format: AudioFormat = "mp3"
    default_voice_id: str | None = None
    clone_cache_path: Path | None = None  # ~/.echovessel/voice-cache.json


# ---------------------------------------------------------------------------
# Sub-factories
# ---------------------------------------------------------------------------


def build_tts_provider(
    *,
    provider: str,
    api_key_env: str,
) -> TTSProvider:
    """Construct a TTSProvider from a provider name string."""
    if provider == "stub":
        return StubVoiceProvider()

    if provider == "fishaudio":
        from echovessel.voice.fishaudio import FishAudioProvider

        api_key = os.environ.get(api_key_env) if api_key_env else None
        return FishAudioProvider(api_key=api_key)

    raise ValueError(
        f"Unknown TTS provider: {provider!r}. "
        f"Supported: 'fishaudio' | 'stub'."
    )


def build_stt_provider(
    *,
    provider: str,
    api_key_env: str,
) -> STTProvider:
    """Construct an STTProvider from a provider name string."""
    if provider == "stub":
        return StubVoiceProvider()

    if provider == "whisper_api":
        from echovessel.voice.whisper_api import WhisperAPIProvider

        api_key = os.environ.get(api_key_env) if api_key_env else None
        return WhisperAPIProvider(api_key=api_key)

    raise ValueError(
        f"Unknown STT provider: {provider!r}. "
        f"Supported: 'whisper_api' | 'stub'."
    )


# ---------------------------------------------------------------------------
# Top-level factory
# ---------------------------------------------------------------------------


def build_voice_service(config: VoiceServiceConfig) -> VoiceService:
    """Build a `VoiceService` from a `VoiceServiceConfig`.

    Runtime calls this at startup (§3 step 10.5 per voice spec §7.3.1).
    Callers are responsible for checking `config.enabled` before
    instantiating — disabled configs should still produce a service that
    channels can call without crashing, but that's a runtime layer
    decision, not this factory's.
    """
    tts = build_tts_provider(
        provider=config.tts_provider,
        api_key_env=config.tts_api_key_env,
    )
    stt = build_stt_provider(
        provider=config.stt_provider,
        api_key_env=config.stt_api_key_env,
    )

    clone_cache: FingerprintCache | None = None
    if config.clone_cache_path is not None:
        clone_cache = FingerprintCache(config.clone_cache_path)

    return VoiceService(
        tts=tts,
        stt=stt,
        default_voice_id=config.default_voice_id,
        default_format=config.default_audio_format,
        clone_cache=clone_cache,
    )


__all__ = [
    "VoiceServiceConfig",
    "build_tts_provider",
    "build_stt_provider",
    "build_voice_service",
]

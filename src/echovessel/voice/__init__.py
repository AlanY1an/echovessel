"""Voice subsystem — TTS + STT + voice cloning.

Layer 2 module. Only imports from `echovessel.core`. Never imports from
`echovessel.memory`, `echovessel.channels`, `echovessel.runtime`,
`echovessel.proactive`, or `echovessel.prompts`. The boundary is
enforced in CI by import-linter.

Public API (spec §4.7 + factory contract):

    from echovessel.voice import (
        # Protocols
        TTSProvider, STTProvider,
        # Value types
        TranscriptResult, VoiceMeta, AudioFormat, InputAudioFormat,
        # Errors
        VoiceError, VoiceTransientError, VoicePermanentError, VoiceBudgetError,
        # Service facade + factory
        VoiceService, VoiceServiceConfig,
        build_voice_service, build_tts_provider, build_stt_provider,
        # Stub (tests + dry-run)
        StubVoiceProvider,
        # Cloning
        CloneEntry, FingerprintCache, compute_fingerprint,
    )

Concrete cloud providers (`FishAudioProvider`, `WhisperAPIProvider`) are
NOT re-exported at the package top level — they are lazy-imported by
the factory so that consumers who only use Stub never have to install
the cloud SDKs. Import them directly if needed:

    from echovessel.voice.fishaudio import FishAudioProvider
    from echovessel.voice.whisper_api import WhisperAPIProvider

See:
- `docs/voice/01-spec-v0.1.md` — authoritative spec
- `docs/voice/02-voice-code-tracker.md` — code implementation tracker
"""

from echovessel.voice.base import (
    AudioFormat,
    InputAudioFormat,
    STTProvider,
    TranscriptResult,
    TTSProvider,
    VoiceMeta,
)
from echovessel.voice.cloning import (
    CloneEntry,
    FingerprintCache,
    compute_fingerprint,
)
from echovessel.voice.errors import (
    VoiceBudgetError,
    VoiceError,
    VoicePermanentError,
    VoiceTransientError,
)
from echovessel.voice.factory import (
    VoiceServiceConfig,
    build_stt_provider,
    build_tts_provider,
    build_voice_service,
)
from echovessel.voice.service import VoiceService
from echovessel.voice.stub import StubVoiceProvider
from echovessel.voice.types import VoiceResult

__all__ = [
    # Types
    "AudioFormat",
    "InputAudioFormat",
    "TranscriptResult",
    "VoiceMeta",
    # Protocols
    "TTSProvider",
    "STTProvider",
    # Errors
    "VoiceError",
    "VoiceTransientError",
    "VoicePermanentError",
    "VoiceBudgetError",
    # Service facade + factory
    "VoiceService",
    "VoiceServiceConfig",
    "VoiceResult",
    "build_voice_service",
    "build_tts_provider",
    "build_stt_provider",
    # Stub
    "StubVoiceProvider",
    # Cloning
    "CloneEntry",
    "FingerprintCache",
    "compute_fingerprint",
]

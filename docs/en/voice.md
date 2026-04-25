# Voice

## Overview

Voice is a first-class identity carrier in EchoVessel, not a decorative extra. A persona that only speaks through text is only half-present — the voice module exists so that the same persona can pronounce its own words, recognise spoken input from the user, and carry a unique timbre across every channel that supports audio. Because the rest of the daemon already treats each persona as a single cross-channel entity, voice slots into that design by exposing one small facade (`VoiceService`) whose public shape stays identical whether the underlying provider is a cloud synthesiser, a local binary, or an in-process stub.

The module covers three capabilities. **TTS** (text-to-speech) converts an outgoing persona message into audio bytes that channels can stream to the user. **STT** (speech-to-text) turns an incoming voice note — a browser `MediaRecorder` blob, a Discord voice message, an `.m4a` from a phone — into the text that the rest of the pipeline consumes. **Voice cloning** takes a short reference sample and registers it with a cloud provider so the persona speaks in that timbre. These three paths are deliberately kept separate: TTS and cloning share one provider Protocol because they ride on the same vendor account, while STT lives behind its own Protocol because the real providers rarely overlap (FishAudio does TTS but no STT; OpenAI Whisper does STT but no TTS).

`VoiceService.generate_voice()` is the single entry point that channels and the proactive scheduler actually call. It is a facade over the lower-level `TTSProvider.speak()` primitive — it does not replace it. Internally it looks up an on-disk cache keyed by `message_id`, calls `speak()` only on a cache miss, writes the result atomically, estimates the cost, and returns a `VoiceResult` whose fields map 1-to-1 onto the `chat.message.voice_ready` SSE payload that the Web channel emits. Everything the facade adds — cache, cost estimate, idempotency, graceful fallback on unsupported tone hints — can be added in one place exactly because `speak()` stayed a plain streaming primitive underneath.

## Core Concepts

**TTS provider.** The abstraction over any external text-to-speech synthesiser. Defined as a `runtime_checkable` Protocol in `src/echovessel/voice/base.py`. A provider exposes a streaming `speak()` method that yields audio bytes plus a handful of identity properties (`provider_name`, `is_cloud`, `supports_cloning`). FishAudio is the current cloud implementation; `StubVoiceProvider` is the deterministic no-network implementation used in tests and dry runs.

**STT provider.** A separate Protocol for speech-to-text, used when importing voice messages from the user or from an offline transcript. It only has one real method, `transcribe()`, which accepts raw bytes or an async iterator of chunks and returns a `TranscriptResult`. STT is split from TTS because real providers almost never ship both — forcing them into one combined Protocol would leave half the methods empty on every concrete class.

**Voice clone.** A short reference audio sample (typically 10–60 seconds) uploaded to a cloud provider that supports voice reference models. The provider returns a stable `voice_id` that EchoVessel then uses as the synthesis voice for that persona. The cloning path is deliberately kept off the hot per-turn codepath — it runs once via a CLI subcommand and the resulting id is written into persona config.

**Voice profile.** The pair `(provider_name, voice_id)` stored in the persona's section of `config.toml`. Runtime reads it at startup, passes the `voice_id` as the `default_voice_id` parameter to `VoiceService`, and from then on the rest of the daemon synthesises with that voice without ever having to care which provider minted it.

**`VoiceResult`.** The frozen dataclass returned by `generate_voice()`, defined in `src/echovessel/voice/types.py`. Five fields, all load-bearing: `url` (the relative path the Web channel serves the audio from), `duration_seconds` (a best-effort estimate for progress bars), `provider` (opaque label for audit logs), `cost_usd` (a hard-coded per-call estimate, `0.0` on cache hit), and `cached` (whether the result skipped the underlying provider call).

**Voice cache.** An on-disk cache at `~/.echovessel/voice_cache/<message_id>.mp3`, created lazily the first time `generate_voice()` runs. It makes the method idempotent per message id: a second call for the same message returns the cached file, marks `cached=True`, and reports `cost_usd=0.0` without touching the provider. It is a different file location from the voice-clone fingerprint cache (`~/.echovessel/voice-cache.json`), and wiping one never affects the other.

## Architecture

Voice lives in Layer 2 of the five-module stack, alongside Memory. Runtime, Channels, and Proactive all sit above Voice and may import from it; Voice itself may only import from the core types directly below it. The layering is enforced by `import-linter` in CI, so the dependency direction is a build-time guarantee, not a convention.

```
┌────────────────────────────────────────────────────┐
│        runtime  |  channels  |  proactive          │
└────────────────────────────┬───────────────────────┘
                             │  constructor injection
                             ▼
┌────────────────────────────────────────────────────┐
│                VoiceService  (facade)              │
│  ┌──────────────────────────────────────────────┐  │
│  │  generate_voice(text, voice_id, message_id) │  │
│  │     · cache check  · speak()  · atomic write │  │
│  │     · cost estimate  · VoiceResult           │  │
│  └──────────────────────────────────────────────┘  │
│        │                              │            │
│        ▼                              ▼            │
│   TTSProvider                    STTProvider       │
│   (Protocol)                     (Protocol)        │
└────────┼──────────────────────────────┼────────────┘
         │                              │
         ▼                              ▼
   FishAudioProvider              WhisperAPIProvider
   StubVoiceProvider              StubVoiceProvider
```

Two Protocols, one facade. `TTSProvider.speak()` is the low-level primitive: it takes a text string, an optional `voice_id`, and an `AudioFormat` literal, and returns an `AsyncIterator[bytes]` of audio chunks. Even providers that read the whole HTTP response before yielding still expose the streaming signature, so the interface stays stable when a real streaming provider lands later. `STTProvider.transcribe()` is the mirror primitive on the STT side: it accepts bytes or an async chunk iterator plus an `InputAudioFormat` hint and returns a `TranscriptResult`. The broader `InputAudioFormat` set (`mp3`, `wav`, `pcm16`, `webm`, `m4a`, `ogg`) reflects the real range of container formats that browsers, phones, and voice-note apps produce — providers that cannot handle a given format raise `VoicePermanentError` rather than silently coerce.

`VoiceService` (in `src/echovessel/voice/service.py`) composes one `TTSProvider` and one `STTProvider` with a cache directory, an optional `FingerprintCache` for clone idempotency, and a default audio format. Runtime constructs exactly one instance at startup and passes it to channels and the proactive scheduler by constructor injection; upstream code never touches provider instances directly, which is what makes provider swaps (e.g. FishAudio to stub for an offline demo) contained to one config edit.

The `generate_voice(text, *, voice_id, message_id, tone_hint="neutral") -> VoiceResult` method is the high-level entry point that channels call once per persona reply. Its contract is:

1. **Idempotent per `message_id`.** A second call for the same message id returns the cached audio with `cached=True` and `cost_usd=0.0`, and must not touch the underlying provider at all.
2. **Atomic cache writes.** The synthesis path writes to `<cache_dir>/<message_id>.mp3.tmp`, `fsync`s the file handle, then `os.replace`s onto the final name. A crash mid-write never leaves a truncated cache artifact.
3. **Hard-coded cost estimate.** `estimate_tts_cost(provider, text)` multiplies `len(text)` by a per-character USD rate from a small table in `src/echovessel/voice/pricing.py`. The estimate is deliberately not a live billing query — the authoritative number is the provider's own dashboard, and `VoiceService` logs a disclaimer string at construction time so every process leaves a breadcrumb that the cost field is approximate. Web UI surfaces that display `cost_usd` must label it as an estimate.
4. **MVP tone hint handling.** `tone_hint` only honours `"neutral"`. Passing `"tender"` or `"whisper"` logs a warning and silently falls back to the neutral path; the resulting `VoiceResult.provider` carries no tone decoration. Honouring additional values is deferred.
5. **Errors bubble through untouched.** `ValueError` on empty text, `VoiceTransientError` on 5xx/timeout/rate-limit, `VoicePermanentError` on 4xx/auth/invalid voice id, and `VoiceBudgetError` on quota exhaustion all propagate out of `generate_voice()` unchanged. If `speak()` raises, no partial cache file is left behind.

The FishAudio path is the one legal exception to the module's async-I/O rule. `fish-audio-sdk` is sync-only, so `FishAudioProvider.speak()` collects chunks inside `asyncio.to_thread(...)` to avoid blocking the event loop. This is documented at `src/echovessel/voice/fishaudio.py` and is the only blocking-I/O exception permitted anywhere in the voice module — every other network call uses an async client (`httpx.AsyncClient` or `openai.AsyncOpenAI`).

Voice cloning is a separate path from per-message synthesis. `VoiceService.clone_voice_interactive(sample, *, name)` loads sample bytes, computes a stable fingerprint (`sha256:<hex>:<size>`), checks a local `FingerprintCache` at `~/.echovessel/voice-cache.json`, and either returns the cached `CloneEntry` or uploads the sample via `TTSProvider.clone_voice()` and caches the result. Running `echovessel voice clone sample.wav` twice on the same file is therefore a single network round-trip. Writing the resulting `voice_id` into `config.toml` is the CLI subcommand's job, not the service's.

The error hierarchy is intentionally shallow and mirrors the LLM module's retry semantics so upstream code can share a single `try/except` pattern:

```
VoiceError
  ├── VoiceTransientError   (retry: 5xx, timeout, rate limit)
  └── VoicePermanentError   (do not retry: 4xx, auth, invalid voice_id)
        └── VoiceBudgetError (quota exhausted — disable voice until next start)
```

Upstream channels catch `VoiceError` around every voice call and gracefully degrade to sending the plain text reply. Runtime additionally catches `VoiceBudgetError` and flips an in-memory switch that disables further voice operations for the lifetime of the daemon; a restart is required to clear it.

### Data flow of `generate_voice`

```
          text  voice_id  message_id  tone_hint
            │      │         │          │
            │      │         │          │ (warn + fallback if non-neutral)
            ▼      ▼         ▼          ▼
        ┌──────────────────────────────────┐
        │   VoiceService.generate_voice    │
        └──────────────┬───────────────────┘
                       │
              ┌────────┴────────┐
              │  cache lookup   │  <voice_cache_dir>/<message_id>.mp3
              └────────┬────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
       HIT │                         │ MISS
          │                         │
          ▼                         ▼
   VoiceResult(              TTSProvider.speak(text, voice_id, format="mp3")
     cached=True,                    │
     cost_usd=0.0,                   ▼
     ... )                 collect async chunks → audio_bytes
                                     │
                                     ▼
                     atomic write: tmp + fsync + os.replace
                                     │
                                     ▼
                         estimate_tts_cost(provider, text)
                                     │
                                     ▼
                              VoiceResult(
                                url="/api/chat/voice/<message_id>.mp3",
                                duration_seconds=<heuristic>,
                                provider=<provider_name>,
                                cost_usd=<estimate>,
                                cached=False,
                              )
```

## How to Extend

### 1. Add a new TTS provider

A new provider is any class that satisfies the `TTSProvider` Protocol (`src/echovessel/voice/base.py`). There is no base class to inherit from — the Protocol is `runtime_checkable` and structural, so implementing the right methods is enough. The minimum surface is `provider_name`, `is_cloud`, `supports_cloning`, `speak()`, `clone_voice()`, `list_voices()`, and `health_check()`.

```python
# src/echovessel/voice/myprovider.py
from collections.abc import AsyncIterator
from pathlib import Path

from echovessel.voice.base import AudioFormat, VoiceMeta
from echovessel.voice.errors import VoicePermanentError


class MyTTSProvider:
    def __init__(self, *, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "myprovider"

    @property
    def is_cloud(self) -> bool:
        return True

    @property
    def supports_cloning(self) -> bool:
        return False

    async def speak(
        self, text: str, *, voice_id: str | None = None,
        format: AudioFormat = "mp3",
    ) -> AsyncIterator[bytes]:
        if not text:
            raise ValueError("speak: text is empty")
        # ... call your async HTTP client, yield chunks ...
        yield b""  # placeholder

    async def clone_voice(self, sample, *, name: str) -> str:
        raise NotImplementedError

    async def list_voices(self) -> list[VoiceMeta]:
        return []

    async def health_check(self) -> bool:
        return bool(self._api_key)
```

Then register it in `src/echovessel/voice/factory.py` by adding one branch to `build_tts_provider`:

```python
if provider == "myprovider":
    from echovessel.voice.myprovider import MyTTSProvider
    api_key = os.environ.get(api_key_env) if api_key_env else None
    return MyTTSProvider(api_key=api_key)
```

Set `[voice] tts_provider = "myprovider"` in `config.toml` and the factory picks up the new class at next startup. Add tests under `tests/voice/` mirroring the layout of the FishAudio tests — the stub is the reference for what a "minimal green" suite looks like.

### 2. Add a new STT provider

Same structural-Protocol story, but targeting `STTProvider`. The surface is smaller: `provider_name`, `is_cloud`, `transcribe()`, and `health_check()`.

```python
# src/echovessel/voice/mystt.py
from collections.abc import AsyncIterator

from echovessel.voice.base import InputAudioFormat, TranscriptResult
from echovessel.voice.errors import VoicePermanentError


class MySTTProvider:
    def __init__(self, *, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "mystt"

    @property
    def is_cloud(self) -> bool:
        return True

    async def transcribe(
        self,
        audio: bytes | AsyncIterator[bytes],
        *,
        language: str | None = None,
        format: InputAudioFormat = "wav",
    ) -> TranscriptResult:
        if isinstance(audio, (bytes, bytearray)):
            data = bytes(audio)
        else:
            data = b"".join([chunk async for chunk in audio])
        if not data:
            raise VoicePermanentError("no speech detected")
        text = await self._call_api(data, language=language, fmt=format)
        return TranscriptResult(text=text, language=language)

    async def health_check(self) -> bool:
        return bool(self._api_key)
```

Register it in `build_stt_provider` in `src/echovessel/voice/factory.py` with an `if provider == "mystt":` branch mirroring the `whisper_api` case. Honour the contract that a silent / no-speech input raises `VoicePermanentError` rather than returning an empty string — channels rely on that to show an accurate "no speech detected" message to the user.

### 3. Clone a voice

Cloning uses the same `VoiceService` instance that handles per-message synthesis. Call `clone_voice_interactive` with the sample (as raw bytes or a `Path`) and a human-readable name, then write the returned `voice_id` into the persona's config.

```python
from pathlib import Path

from echovessel.voice.factory import (
    VoiceServiceConfig,
    build_voice_service,
)

async def register_voice(sample_path: Path, label: str) -> str:
    cfg = VoiceServiceConfig(
        tts_provider="fishaudio",
        stt_provider="whisper_api",
        tts_api_key_env="FISH_API_KEY",
        stt_api_key_env="OPENAI_API_KEY",
        clone_cache_path=Path.home() / ".echovessel" / "voice-cache.json",
    )
    svc = build_voice_service(cfg)
    entry = await svc.clone_voice_interactive(sample_path, name=label)
    # `entry.voice_id` is now cached on disk. Running this function a
    # second time with the same sample file is a no-op network-wise.
    return entry.voice_id
```

Then patch `config.toml` under the persona section:

```toml
[persona]
voice_id = "v_abc123"   # paste the id returned above
```

The next daemon start picks up the new id via `VoiceServiceConfig.default_voice_id`, and every subsequent `generate_voice()` call synthesises in that voice. Because `clone_voice_interactive` is fingerprint-cached, re-running the registration on the same sample — for example in a script that provisions several environments — costs nothing after the first upload.

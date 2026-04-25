"""VoiceService — single-instance facade over TTS + STT providers.

Runtime constructs one `VoiceService` at startup and passes it to
channels / proactive scheduler via constructor injection. Upstream code
NEVER touches provider instances directly — that lets us swap providers
(e.g. fishaudio → stub) without recompiling consumers.

Spec: docs/voice/01-spec-v0.1.md §4.7 (speak / transcribe / clone) and
§4.7a (v0.2 `generate_voice` facade method).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from echovessel.voice.base import (
    AudioFormat,
    InputAudioFormat,
    STTProvider,
    TranscriptResult,
    TTSProvider,
)
from echovessel.voice.cloning import CloneEntry, FingerprintCache, compute_fingerprint
from echovessel.voice.errors import VoicePermanentError
from echovessel.voice.pricing import COST_ESTIMATE_DISCLAIMER, estimate_tts_cost
from echovessel.voice.types import VoiceResult

log = logging.getLogger(__name__)


# ~/.echovessel/voice_cache/ — see spec §4.7a. Separate from the clone
# fingerprint cache (`voice-cache.json`), which is deliberately a
# different file so wiping one does not affect the other.
_DEFAULT_VOICE_CACHE_DIR = Path.home() / ".echovessel" / "voice_cache"


ToneHint = Literal["neutral", "tender", "whisper"]


class VoiceService:
    """Single-instance facade over TTS + STT providers.

    Not thread-local; shared across all channels and proactive scheduler.
    Holds exactly one `TTSProvider` and one `STTProvider`. The default
    persona voice_id and audio format are captured at construction time
    so per-call parameters can be omitted in the common path.
    """

    def __init__(
        self,
        *,
        tts: TTSProvider,
        stt: STTProvider,
        default_voice_id: str | None = None,
        default_format: AudioFormat = "mp3",
        clone_cache: FingerprintCache | None = None,
        voice_cache_dir: Path | None = None,
    ) -> None:
        self._tts = tts
        self._stt = stt
        self._default_voice_id = default_voice_id
        self._default_format: AudioFormat = default_format
        self._clone_cache = clone_cache
        # spec §4.7a: on-disk cache for generate_voice results. Tests
        # override this with a tmp_path; runtime uses the default under
        # ~/.echovessel/voice_cache/. Directory creation is lazy so a
        # VoiceService that never calls generate_voice never touches the
        # filesystem.
        self._voice_cache_dir: Path = (
            voice_cache_dir if voice_cache_dir is not None else _DEFAULT_VOICE_CACHE_DIR
        )
        # spec §4.7a Cost estimation · one-line startup warning so every
        # process leaves a log breadcrumb that the numbers are estimates.
        log.warning("%s", COST_ESTIMATE_DISCLAIMER)

    # --- Introspection --------------------------------------------

    @property
    def tts_provider_name(self) -> str:
        return self._tts.provider_name

    @property
    def stt_provider_name(self) -> str:
        return self._stt.provider_name

    @property
    def is_fully_local(self) -> bool:
        """True if both providers run entirely on the local machine.

        Used by the startup disclosure (spec §8.4) to decide whether to
        label the voice row `(cloud)` or `(local)`.
        """
        return not self._tts.is_cloud and not self._stt.is_cloud

    @property
    def default_voice_id(self) -> str | None:
        return self._default_voice_id

    @property
    def default_format(self) -> AudioFormat:
        return self._default_format

    @property
    def supports_cloning(self) -> bool:
        return self._tts.supports_cloning

    # --- Public API -----------------------------------------------

    async def speak(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        format: AudioFormat | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize `text` into audio bytes.

        `voice_id` defaults to the persona's configured id (provided at
        construction). `format` defaults to `self.default_format`.

        Errors bubble up from the TTS provider unchanged. The facade does
        NOT do retry or fallback — those are runtime-level concerns.
        """
        effective_voice = voice_id if voice_id is not None else self._default_voice_id
        effective_format: AudioFormat = format or self._default_format
        async for chunk in self._tts.speak(
            text,
            voice_id=effective_voice,
            format=effective_format,
        ):
            yield chunk

    async def transcribe(
        self,
        audio: bytes | AsyncIterator[bytes],
        *,
        language: str | None = None,
        format: InputAudioFormat = "wav",
    ) -> TranscriptResult:
        return await self._stt.transcribe(
            audio,
            language=language,
            format=format,
        )

    async def clone_voice_interactive(
        self,
        sample: bytes | Path,
        *,
        name: str,
    ) -> CloneEntry:
        """End-to-end cloning flow with fingerprint idempotency (spec §5).

        Steps:
          1. Load sample bytes (if a Path was passed)
          2. Compute fingerprint
          3. Check local cache → return early on hit
          4. Delegate to provider.clone_voice
          5. Store result in cache
          6. Return the CloneEntry

        Does NOT write to persona config — the CLI subcommand is
        responsible for that (Thread RT Round 2 wires the CLI).
        """
        if not self._tts.supports_cloning:
            raise NotImplementedError(
                f"TTS provider {self._tts.provider_name!r} does not support cloning"
            )

        sample_bytes = (
            sample.read_bytes() if isinstance(sample, Path) else bytes(sample)
        )

        if not sample_bytes:
            raise VoicePermanentError("clone sample is empty")

        fingerprint = compute_fingerprint(sample_bytes)

        # Step 3: cache check
        if self._clone_cache is not None:
            hit = self._clone_cache.lookup(fingerprint)
            if hit is not None:
                log.info(
                    "voice clone cache hit for fingerprint=%s voice_id=%s; "
                    "skipping upload",
                    fingerprint[:24],
                    hit.voice_id,
                )
                return hit

        # Step 4: actual upload
        voice_id = await self._tts.clone_voice(sample_bytes, name=name)

        # Step 5: cache the result
        if self._clone_cache is not None:
            return self._clone_cache.store(
                fingerprint,
                voice_id=voice_id,
                name=name,
                provider=self._tts.provider_name,
            )

        # No cache configured: synthesize an entry for the caller
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return CloneEntry(
            voice_id=voice_id,
            name=name,
            provider=self._tts.provider_name,
            created_at=created_at,
            fingerprint=fingerprint,
        )

    async def health_check(self) -> dict[str, bool]:
        """Return `{"tts": bool, "stt": bool}`.

        Non-fatal: individual provider failures are logged but do not
        raise. Runtime calls this at startup to populate the local-first
        disclosure banner.
        """
        try:
            tts_ok = await self._tts.health_check()
        except Exception as e:  # noqa: BLE001
            log.warning("TTS health check raised: %s", e)
            tts_ok = False
        try:
            stt_ok = await self._stt.health_check()
        except Exception as e:  # noqa: BLE001
            log.warning("STT health check raised: %s", e)
            stt_ok = False
        return {"tts": tts_ok, "stt": stt_ok}

    # --- v0.2 · generate_voice facade (review R3) ------------------

    async def generate_voice(
        self,
        text: str,
        *,
        voice_id: str,
        message_id: int,
        tone_hint: ToneHint = "neutral",
    ) -> VoiceResult:
        """Produce a playable audio artifact for a persona message.

        High-level facade that wraps `TTSProvider.speak()` with:

          - idempotent on-disk cache keyed by `message_id`
          - hard-coded cost estimate (方案 Z, spec §4.7a)
          - returns URL + metadata ready for the Web channel
            `chat.message.voice_ready` SSE payload

        Semantics per spec §4.7a:

          1. The second call for the same `message_id` MUST return the
             cached result with `cost_usd=0.0` and `cached=True`, and MUST
             NOT touch the underlying TTS provider.
          2. Cache file lives at `<voice_cache_dir>/<message_id>.mp3` and
             is written atomically (tmp + fsync + os.replace).
          3. `tone_hint` only honours `"neutral"` in MVP. Other values log
             a warning and silently fall back to neutral.
          4. Errors raised by `speak()` bubble up untouched; no partial
             cache artifact is left behind.

        The underlying `TTSProvider.speak()` signature is NOT changed —
        this method purely composes existing provider primitives (see
        review R3).
        """
        if tone_hint != "neutral":
            log.warning(
                "generate_voice: tone_hint=%r not yet supported, "
                "falling back to neutral",
                tone_hint,
            )
            # Spec §4.7a.4: silently fall back. Provider is called with
            # the neutral path, and VoiceResult.provider is NOT decorated
            # with any tone tag.

        cache_path = self._voice_cache_dir / f"{message_id}.mp3"
        provider_name = self._tts.provider_name

        # Step 1 · cache hit → free, cached=True, zero provider traffic.
        if cache_path.exists():
            duration_seconds = _estimate_duration_seconds(cache_path.stat().st_size)
            return VoiceResult(
                url=f"/api/chat/voice/{message_id}.mp3",
                cache_path=cache_path,
                duration_seconds=duration_seconds,
                provider=provider_name,
                cost_usd=0.0,
                cached=True,
            )

        # Step 2 · cache miss → call speak(), collect bytes.
        chunks: list[bytes] = []
        async for chunk in self._tts.speak(
            text,
            voice_id=voice_id,
            format="mp3",
        ):
            chunks.append(chunk)
        audio_bytes = b"".join(chunks)

        # Step 3 · atomic write to <voice_cache_dir>/<message_id>.mp3.
        try:
            self._voice_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            with open(tmp_path, "wb") as fh:
                fh.write(audio_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, cache_path)
        except OSError as e:
            raise VoicePermanentError(
                f"voice cache write failed: {e}"
            ) from e

        # Step 4 · assemble VoiceResult. Cost is a hard-coded estimate
        # based on input character count (spec §4.7a 方案 Z).
        cost_usd = estimate_tts_cost(provider_name, text)
        duration_seconds = _estimate_duration_seconds(len(audio_bytes))
        return VoiceResult(
            url=f"/api/chat/voice/{message_id}.mp3",
            cache_path=cache_path,
            duration_seconds=duration_seconds,
            provider=provider_name,
            cost_usd=cost_usd,
            cached=False,
        )


# ---------------------------------------------------------------------------
# Duration heuristic
# ---------------------------------------------------------------------------


# Assumed bit-rate for "rough" duration estimation when the provider's
# speak() stream does not carry any timing metadata. 128 kbps is a safe
# average for FishAudio / standard MP3 TTS output — the number is only
# used to populate VoiceResult.duration_seconds for UI progress bars, it
# is NOT load-bearing and it is NOT a spec-normative value. See spec
# §4.7a — the spec only says the field must be present, it does not
# mandate accuracy.
_ASSUMED_MP3_BITRATE_BPS = 128_000


def _estimate_duration_seconds(num_bytes: int) -> float:
    """Best-effort MP3 duration estimate from byte count.

    Returns 0.0 for zero-byte input so `VoiceResult.duration_seconds`
    stays a real `float` and not a `NaN` / sentinel. Real providers with
    streaming duration metadata should be upgraded in a later round to
    feed the true value through — this is a MVP placeholder.
    """
    if num_bytes <= 0:
        return 0.0
    return round(num_bytes * 8 / _ASSUMED_MP3_BITRATE_BPS, 3)


__all__ = ["VoiceService"]

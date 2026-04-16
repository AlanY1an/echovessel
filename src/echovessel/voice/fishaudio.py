"""FishAudioProvider — FishAudio cloud TTS + voice cloning.

FishAudio's Python SDK (`fish-audio-sdk`) is synchronous. Wrapping it in
`asyncio.to_thread(...)` is the ONE legal blocking-I/O exception allowed
in the voice module (spec §4.2 / §11.8). Every other network call in
this package uses an async client.

Lazy-imports `fish_audio_sdk` inside `_get_client` so that importing
`echovessel.voice` does not require the SDK to be installed (matches the
`[voice]` optional extra — spec §4.2).

See docs/voice/01-spec-v0.1.md §4.2 for the reference.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from echovessel.voice.base import AudioFormat, VoiceMeta
from echovessel.voice.errors import (
    VoiceBudgetError,
    VoicePermanentError,
    VoiceTransientError,
)

log = logging.getLogger(__name__)

#: Default FishAudio model. Their current flagship is "s2-pro".
_DEFAULT_MODEL: str = "s2-pro"


class FishAudioProvider:
    """FishAudio cloud provider for TTS + voice cloning.

    Not STT-capable — FishAudio does not offer STT. Production configs
    pair this with `WhisperAPIProvider` for STT.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        default_model: str = _DEFAULT_MODEL,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise VoicePermanentError(
                "FishAudioProvider: api_key is empty. "
                "Set FISH_API_KEY environment variable."
            )
        try:
            from fish_audio_sdk import Session
        except ImportError as e:
            raise ImportError(
                "fish-audio-sdk not installed. Install the [voice] extra: "
                "`uv sync --extra voice` or `pip install fish-audio-sdk`."
            ) from e
        self._client = Session(apikey=self._api_key)
        return self._client

    # --- Identity / capability ------------------------------------

    @property
    def provider_name(self) -> str:
        return "fishaudio"

    @property
    def is_cloud(self) -> bool:
        return True

    @property
    def supports_cloning(self) -> bool:
        return True

    # --- Synthesis ------------------------------------------------

    async def speak(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        format: AudioFormat = "mp3",
    ) -> AsyncIterator[bytes]:
        """Synthesize `text` into audio bytes.

        MVP implementation: the SDK's sync `client.tts(req)` returns an
        iterator of bytes. We collect all chunks inside a single
        `asyncio.to_thread` call, then yield them in the async generator.
        This is a degenerate streaming case but matches the Protocol
        signature (spec §2.4) and keeps the sync/async boundary clean.

        v1.0 real streaming providers can replace this with a proper
        yield-per-chunk-across-threads implementation.
        """
        if not text:
            raise ValueError("FishAudioProvider.speak: text must be non-empty")

        client = self._get_client()
        chunks = await asyncio.to_thread(
            _sync_collect_tts_chunks,
            client,
            text=text,
            voice_id=voice_id,
            format=format,
        )
        for chunk in chunks:
            yield chunk

    # --- Cloning --------------------------------------------------

    async def clone_voice(
        self,
        sample: bytes | Path,
        *,
        name: str,
    ) -> str:
        """Upload a sample as a FishAudio reference model and return its id.

        `sample` may be bytes or a Path. Path-based samples are read
        synchronously inside the worker thread to avoid blocking the
        event loop with file I/O.
        """
        client = self._get_client()

        def _sync_clone() -> str:
            sample_bytes = (
                sample.read_bytes() if isinstance(sample, Path) else sample
            )

            if not sample_bytes:
                raise VoicePermanentError(
                    "FishAudioProvider.clone_voice: sample is empty"
                )

            try:
                # fish_audio_sdk >= 2024.x · flat method on Session.
                # Older '.voices.create' is gone.
                voice = client.create_model(  # type: ignore[attr-defined]
                    title=name,
                    voices=[sample_bytes],
                    description=f"EchoVessel clone: {name}",
                )
            except Exception as e:  # noqa: BLE001
                raise _classify_fishaudio_error(e) from e

            # ModelEntity exposes id (aliased from _id). Probe both for safety.
            voice_id = getattr(voice, "id", None) or getattr(voice, "_id", None)
            if not voice_id:
                raise VoicePermanentError(
                    f"FishAudio clone returned no id (got {voice!r})"
                )
            return str(voice_id)

        return await asyncio.to_thread(_sync_clone)

    async def list_voices(self) -> list[VoiceMeta]:
        client = self._get_client()

        def _sync_list() -> list[VoiceMeta]:
            try:
                # fish_audio_sdk >= 2024.x · flat list_models on Session.
                # Returns PaginatedResponse[ModelEntity]; iterate .items.
                page = client.list_models(self_only=True, page_size=100)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                raise _classify_fishaudio_error(e) from e

            voices_iter = getattr(page, "items", page)
            result: list[VoiceMeta] = []
            for v in voices_iter:
                raw_id = getattr(v, "id", None) or getattr(v, "_id", None) or ""
                raw_name = (
                    getattr(v, "title", None)
                    or getattr(v, "name", None)
                    or "unknown"
                )
                result.append(
                    VoiceMeta(
                        voice_id=str(raw_id),
                        display_name=str(raw_name),
                        provider_name="fishaudio",
                        language=getattr(v, "language", None),
                        preview_url=getattr(v, "preview_url", None),
                    )
                )
            return result

        return await asyncio.to_thread(_sync_list)

    # --- Health ---------------------------------------------------

    async def health_check(self) -> bool:
        """Cheap reachability check — list voices and see if it works.

        Any exception (transport, auth, SDK missing) → False.
        """
        try:
            await self.list_voices()
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("FishAudioProvider health_check failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# Sync worker helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _sync_collect_tts_chunks(
    client: object,
    *,
    text: str,
    voice_id: str | None,
    format: AudioFormat,
) -> list[bytes]:
    """Run inside a worker thread. Builds the TTSRequest, iterates the
    sync SDK generator, and returns all chunks. Errors are classified
    into the voice error hierarchy before being raised.
    """
    try:
        from fish_audio_sdk import TTSRequest
    except ImportError as e:
        raise VoicePermanentError(f"fish_audio_sdk.TTSRequest import failed: {e}") from e

    req_kwargs: dict[str, object] = {"text": text, "format": format}
    if voice_id:
        req_kwargs["reference_id"] = voice_id
    req = TTSRequest(**req_kwargs)

    chunks: list[bytes] = []
    try:
        for chunk in client.tts(req):  # type: ignore[attr-defined]
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise VoicePermanentError(
                    f"FishAudio SDK yielded non-bytes chunk: {type(chunk).__name__}"
                )
            chunks.append(bytes(chunk))
    except (VoiceTransientError, VoicePermanentError):
        raise
    except Exception as e:  # noqa: BLE001
        raise _classify_fishaudio_error(e) from e
    return chunks


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_fishaudio_error(e: Exception) -> Exception:
    """Map fish-audio-sdk exceptions to the voice error hierarchy.

    SDK-level exceptions are not uniform; we inspect `.status_code`,
    `.response.status_code`, and class name heuristics. Mirrors
    `runtime/llm/anthropic._classify_anthropic_error`.
    """
    if isinstance(e, (VoiceTransientError, VoicePermanentError)):
        return e

    status = _extract_status(e)
    cls_name = e.__class__.__name__

    if status is None:
        if any(
            hint in cls_name
            for hint in ("Timeout", "Connection", "Network", "Socket")
        ):
            return VoiceTransientError(f"{cls_name}: {e}")
        return VoicePermanentError(f"{cls_name}: {e}")

    if status == 429:
        return VoiceTransientError(f"rate limited: {e}")
    if status >= 500:
        return VoiceTransientError(f"server error {status}: {e}")
    if status in (401, 403):
        return VoicePermanentError(f"auth error {status}: {e}")
    if status == 402:
        return VoiceBudgetError(f"budget/quota error {status}: {e}")
    return VoicePermanentError(f"client error {status}: {e}")


def _extract_status(e: Exception) -> int | None:
    status = getattr(e, "status_code", None)
    if status is not None:
        return status
    response = getattr(e, "response", None)
    if response is not None:
        return getattr(response, "status_code", None)
    return None


def build_fishaudio_from_env(
    *, api_key_env: str = "FISH_API_KEY"
) -> FishAudioProvider:
    api_key = os.environ.get(api_key_env) if api_key_env else None
    return FishAudioProvider(api_key=api_key)


__all__ = [
    "FishAudioProvider",
    "build_fishaudio_from_env",
]

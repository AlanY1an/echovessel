"""Admin voice routes — sample CRUD + clone wizard + activate.

Flow: upload ≥3 samples → POST /clone produces a voice_id → caller
previews via POST /preview (streamed mp3) → POST /activate writes
persona.voice_id to config.toml via the existing atomic-write helper
used by voice-toggle.

Samples live under <data_dir>/voice_samples/<sample_id>/ with an
audio.bin + meta.json pair. See ``_voice_samples_dir`` /
``_VoiceSampleStore`` in ``helpers``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from echovessel.channels.web.routes.admin.helpers import (
    _VOICE_PREVIEW_TEXT,
    _VOICE_SAMPLE_MAX_BYTES,
    _VOICE_SAMPLE_MIN_COUNT,
    _voice_samples_dir,
    _VoiceSampleStore,
)
from echovessel.channels.web.routes.admin.models import (
    VoiceActivateRequest,
    VoiceCloneRequest,
    VoicePreviewRequest,
)
from echovessel.voice.errors import VoicePermanentError

log = logging.getLogger(__name__)


def register_voice_routes(
    router: APIRouter,
    *,
    runtime: Any,
    voice_service: Any | None,
) -> None:
    def _require_voice() -> Any:
        if voice_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "voice service is not enabled on this daemon; set "
                    "[voice].enabled = true in config.toml"
                ),
            )
        return voice_service

    def _sample_store() -> _VoiceSampleStore:
        data_dir = Path(runtime.ctx.config.runtime.data_dir).expanduser()
        return _VoiceSampleStore(_voice_samples_dir(data_dir))

    @router.post("/api/admin/voice/samples")
    async def post_voice_sample(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008 - FastAPI marker
    ) -> dict[str, Any]:
        # Reject oversize uploads from the Content-Length header BEFORE
        # reading the body — otherwise a multi-GB misclick would fully
        # materialize in RAM before we rejected it. Audit P1-6.
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > _VOICE_SAMPLE_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(f"sample exceeds {_VOICE_SAMPLE_MAX_BYTES // 1_000_000} MB"),
                    )
            except ValueError:
                # Malformed header — let the bounded read below catch it.
                pass

        data = await file.read()
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="uploaded sample is empty",
            )
        if len(data) > _VOICE_SAMPLE_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(f"sample exceeds {_VOICE_SAMPLE_MAX_BYTES // 1_000_000} MB"),
            )

        saved = _sample_store().save(
            data,
            filename=file.filename or "sample",
            content_type=file.content_type or "application/octet-stream",
        )
        return {
            "sample_id": saved.sample_id,
            "duration_seconds": saved.duration_seconds,
            "size_bytes": saved.size_bytes,
            "accepted": True,
        }

    @router.get("/api/admin/voice/samples")
    async def get_voice_samples() -> dict[str, Any]:
        items = _sample_store().list()
        return {
            "samples": [
                {
                    "sample_id": s.sample_id,
                    "filename": s.filename,
                    "size_bytes": s.size_bytes,
                    "duration_seconds": s.duration_seconds,
                    "created_at": s.created_at,
                }
                for s in items
            ],
            "count": len(items),
            "minimum_required": _VOICE_SAMPLE_MIN_COUNT,
        }

    @router.delete("/api/admin/voice/samples/{sample_id}")
    async def delete_voice_sample(sample_id: str) -> dict[str, Any]:
        ok = _sample_store().delete(sample_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"voice sample not found: {sample_id}",
            )
        return {"deleted": True, "sample_id": sample_id}

    @router.post("/api/admin/voice/clone")
    async def post_voice_clone(req: VoiceCloneRequest) -> dict[str, Any]:
        voice = _require_voice()
        store = _sample_store()
        samples = store.list()
        if len(samples) < _VOICE_SAMPLE_MIN_COUNT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"need at least {_VOICE_SAMPLE_MIN_COUNT} samples to "
                    f"clone a voice (have {len(samples)})"
                ),
            )

        # MVP clone strategy · concatenate every draft sample's raw bytes
        # into a single blob. ``VoiceService.clone_voice_interactive``
        # still takes one sample — revisit in v0.2 when the voice
        # abstraction gets a multi-sample variant. The concat still
        # gives FishAudio meaningfully more audio than a single upload
        # and keeps the stub provider's deterministic hash working.
        blob = b"".join(store.read_bytes(s.sample_id) for s in samples)
        try:
            entry = await voice.clone_voice_interactive(blob, name=req.display_name)
        except VoicePermanentError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

        # Stub providers return strings that are fine for
        # ``generate_voice``; real providers return a proper id. Try to
        # render a preview clip so the user hears the result immediately;
        # if the TTS round-trip errors we still return the voice_id so
        # the UI can flip to the "preview failed, retry?" state.
        preview_text = _VOICE_PREVIEW_TEXT
        preview_url: str | None = None
        try:
            preview_result = await voice.generate_voice(
                preview_text,
                voice_id=entry.voice_id,
                message_id=abs(hash(entry.voice_id)) & 0x7FFFFFFF,
            )
            preview_url = preview_result.url
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "voice clone preview generation failed (%s: %s); "
                "returning voice_id without preview_audio_url",
                type(exc).__name__,
                exc,
            )

        return {
            "voice_id": entry.voice_id,
            "display_name": entry.name,
            "preview_text": preview_text,
            "preview_audio_url": preview_url,
        }

    @router.post("/api/admin/voice/preview")
    async def post_voice_preview(req: VoicePreviewRequest) -> StreamingResponse:
        voice = _require_voice()

        async def _stream():
            try:
                async for chunk in voice.speak(req.text, voice_id=req.voice_id, format="mp3"):
                    yield chunk
            except VoicePermanentError as e:
                # Once the response has started we can't switch to an
                # error status code; close the stream and rely on the
                # client noticing the short body. Log so the daemon
                # operator sees the failure.
                log.warning(
                    "voice preview stream aborted: %s: %s",
                    type(e).__name__,
                    e,
                )
                return

        return StreamingResponse(_stream(), media_type="audio/mpeg")

    @router.post("/api/admin/voice/activate")
    async def post_voice_activate(req: VoiceActivateRequest) -> dict[str, Any]:
        if runtime.ctx.config_path is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot activate voice without a config file "
                    "(daemon started in config_override mode)"
                ),
            )
        try:
            runtime._atomic_write_config_field(
                section="persona", field="voice_id", value=req.voice_id
            )
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"failed to write config.toml: {e}",
            ) from e

        # Mirror the on-disk write in memory so subsequent
        # /api/admin/persona reads and outgoing turns use the new voice
        # without waiting for a daemon restart.
        runtime.ctx.persona.voice_id = req.voice_id

        return {"activated": True, "voice_id": req.voice_id}

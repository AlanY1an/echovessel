"""Admin HTTP routes for the Import pipeline (v0.0.2).

Implements the core subset of the admin import surface:

- ``POST /api/admin/import/upload``   — accept multipart file OR JSON
  ``{"text": "..."}`` paste, persist under
  ``<data_dir>/imports/<upload_id>/``
- ``POST /api/admin/import/estimate`` — return token + USD estimates
  for the LLM extraction stage
- ``POST /api/admin/import/start``    — spawn the import pipeline via
  :meth:`ImporterFacade.start_pipeline`
- ``POST /api/admin/import/cancel``   — idempotent cancel
- ``GET  /api/admin/import/events``   — SSE stream of
  :class:`PipelineEvent` items scoped to one ``pipeline_id``

The v0.0.3 endpoints (``/transcribe`` / ``/resume`` / ``/dropped``)
are deliberately omitted from this router; add them alongside these
when they land.

The router is built by :func:`build_admin_import_router`, which
closes over a live :class:`Runtime` and the runtime-constructed
:class:`ImporterFacade`. It is mounted by
:func:`echovessel.channels.web.app.build_web_app` only when both a
runtime and a facade are supplied — tests that exercise the chat
surface in isolation can drop them entirely without the import
router raising on missing dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from echovessel.import_.pricing import estimate_llm_cost

log = logging.getLogger(__name__)


# 50 MiB upload ceiling — spec §9 caps the pre-flight path at this size
# and we'd rather reject early than buffer the whole file then error.
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class UploadTextBody(BaseModel):
    """JSON body for the text-paste path of ``POST /api/admin/import/upload``."""

    text: str = Field(..., min_length=1, max_length=MAX_UPLOAD_BYTES)
    source_label: str | None = Field(default=None, max_length=256)


class EstimateRequest(BaseModel):
    upload_id: str = Field(..., min_length=1, max_length=64)
    # "llm" is the only stage the estimator knows about in v0.0.2.
    # Future stages (chunking / embed / voice) can add their own rates
    # without breaking this contract.
    stage: str = Field(default="llm", max_length=32)


class StartRequest(BaseModel):
    upload_id: str = Field(..., min_length=1, max_length=64)
    force_duplicate: bool = False


class CancelRequest(BaseModel):
    pipeline_id: str = Field(..., min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Upload store
# ---------------------------------------------------------------------------


class _UploadStore:
    """Filesystem-backed store for admin uploads.

    Layout on disk::

        <data_dir>/imports/<upload_id>/
            raw<suffix>    # the original bytes, verbatim
            meta.json      # {file_hash, suffix, source_label,
                           #  size_bytes, received_at}

    ``upload_id`` is a UUID4 hex so collisions are effectively impossible
    and the value is safe to embed in URLs unescaped.
    """

    META_FILENAME = "meta.json"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def write(
        self,
        *,
        data: bytes,
        suffix: str,
        source_label: str,
    ) -> dict[str, Any]:
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="empty upload",
            )
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"upload exceeds {MAX_UPLOAD_BYTES} byte ceiling "
                    f"(got {len(data)})"
                ),
            )

        upload_id = uuid.uuid4().hex
        upload_dir = self._root / upload_id
        upload_dir.mkdir(parents=True, exist_ok=False)

        raw_path = upload_dir / f"raw{suffix}"
        raw_path.write_bytes(data)

        file_hash = hashlib.sha256(data).hexdigest()
        meta = {
            "upload_id": upload_id,
            "file_hash": file_hash,
            "suffix": suffix,
            "source_label": source_label,
            "size_bytes": len(data),
            "received_at": datetime.now().isoformat(),
        }
        (upload_dir / self.META_FILENAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta

    def load(self, upload_id: str) -> tuple[bytes, dict[str, Any]]:
        meta_path = self._root / upload_id / self.META_FILENAME
        if not meta_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown upload_id: {upload_id}",
            )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw_path = self._root / upload_id / f"raw{meta.get('suffix', '')}"
        data = raw_path.read_bytes()
        return data, meta


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _batch_embed_adapter(embed_fn: Any) -> Any:
    """Adapt the runtime's single-string embed_fn to the pipeline's
    batch-of-strings contract.

    Runtime's ``EmbedCallable = Callable[[str], list[float]]``;
    :mod:`echovessel.import_.embed` expects
    ``EmbedFn = Callable[[list[str]], list[list[float]]]``. We iterate
    once per batch so no extra state leaks between calls.
    """

    if embed_fn is None:
        return None

    def _batch(texts: list[str]) -> list[list[float]]:
        return [embed_fn(t) for t in texts]

    return _batch


def _decode_text_or_none(data: bytes) -> str | None:
    """Best-effort UTF-8 decode for the estimate path.

    Binary uploads (PDF, mp3) get ``None`` back — the estimator treats
    them as zero-cost for the LLM stage since extraction only runs on
    decoded text.
    """

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_admin_import_router(
    *,
    runtime: Any,
    importer_facade: Any,
) -> APIRouter:
    """Wire the five admin-import endpoints onto a fresh :class:`APIRouter`.

    Parameters
    ----------
    runtime
        Live :class:`echovessel.runtime.app.Runtime`. Consumed for
        ``ctx.config.runtime.data_dir``, ``ctx.persona.id``,
        ``ctx.embed_fn``, and ``ctx.backend``.
    importer_facade
        Live :class:`echovessel.runtime.wiring.importer.ImporterFacade`.
        Typed as ``Any`` to avoid a channel→runtime import chain that
        would violate the layered-architecture contract.
    """

    router = APIRouter(prefix="/api/admin/import", tags=["admin-import"])

    data_dir = Path(runtime.ctx.config.runtime.data_dir).expanduser()
    imports_root = data_dir / "imports"
    imports_root.mkdir(parents=True, exist_ok=True)
    store = _UploadStore(imports_root)

    user_id = "self"  # MVP single-user daemon.

    # ------------------------------------------------------------------
    # POST /api/admin/import/upload
    # ------------------------------------------------------------------

    @router.post(
        "/upload",
        status_code=status.HTTP_200_OK,
    )
    async def upload(
        file: Annotated[UploadFile | None, File()] = None,
        source_label: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Persist either the uploaded file or ``{"text": ...}`` body.

        FastAPI handles the content-type split: when the client sends
        ``multipart/form-data`` the ``file`` form field populates; when
        it sends ``application/json`` we fall through and parse via
        :class:`UploadTextBody`.
        """

        if file is None:
            # No file — the client needs the JSON-only path so the
            # multipart parser doesn't swallow its body first. Give a
            # precise redirect rather than a generic 422.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "upload requires either a multipart `file` field or "
                    "the JSON paste endpoint POST /api/admin/import/upload_text"
                ),
            )

        data = await file.read()
        raw_name = file.filename or ""
        suffix = Path(raw_name).suffix  # includes leading "." or ""
        meta = store.write(
            data=data,
            suffix=suffix,
            source_label=source_label or raw_name or "",
        )
        return {
            "upload_id": meta["upload_id"],
            "file_hash": meta["file_hash"],
            "suffix": meta["suffix"],
            "source_label": meta["source_label"],
            "size_bytes": meta["size_bytes"],
        }

    @router.post(
        "/upload_text",
        status_code=status.HTTP_200_OK,
    )
    async def upload_text(req: UploadTextBody) -> dict[str, Any]:
        """JSON-only paste path. Mirror of ``/upload`` for clients
        that prefer ``application/json`` over multipart.

        A dedicated path keeps the FastAPI schema clean (OpenAPI
        generators don't cope well with a single endpoint accepting
        both multipart and JSON).
        """

        data = req.text.encode("utf-8")
        meta = store.write(
            data=data,
            suffix=".txt",
            source_label=req.source_label or "pasted_text",
        )
        return {
            "upload_id": meta["upload_id"],
            "file_hash": meta["file_hash"],
            "suffix": meta["suffix"],
            "source_label": meta["source_label"],
            "size_bytes": meta["size_bytes"],
        }

    # ------------------------------------------------------------------
    # POST /api/admin/import/estimate
    # ------------------------------------------------------------------

    @router.post("/estimate")
    async def estimate(req: EstimateRequest) -> dict[str, Any]:
        if req.stage != "llm":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"unknown stage {req.stage!r}; only 'llm' is "
                    f"supported in v0.0.2"
                ),
            )

        data, _meta = store.load(req.upload_id)
        text = _decode_text_or_none(data)
        if text is None:
            # Binary upload — zero tokens / zero cost because the LLM
            # extraction stage only runs over decoded text. Give the
            # front-end a clear signal rather than a silent zero.
            return {
                "tokens_in": 0,
                "tokens_out_est": 0,
                "cost_usd_est": 0.0,
                "note": (
                    "upload is binary; LLM extraction skipped until the "
                    "pipeline decodes it"
                ),
            }
        return estimate_llm_cost(text)

    # ------------------------------------------------------------------
    # POST /api/admin/import/start
    # ------------------------------------------------------------------

    @router.post(
        "/start",
        status_code=status.HTTP_200_OK,
    )
    async def start(req: StartRequest) -> dict[str, Any]:
        data, meta = store.load(req.upload_id)

        embed_fn_batch = _batch_embed_adapter(
            getattr(runtime.ctx, "embed_fn", None)
        )
        backend = getattr(runtime.ctx, "backend", None)
        vector_writer = (
            backend.insert_vector if backend is not None else None
        )

        pipeline_id = await importer_facade.start_pipeline(
            req.upload_id,
            force_duplicate=req.force_duplicate,
            raw_bytes=data,
            suffix=meta.get("suffix", "") or "",
            source_label=meta.get("source_label", "") or "",
            file_hash=meta.get("file_hash", "") or "",
            persona_id=runtime.ctx.persona.id,
            user_id=user_id,
            persona_context="",
            embed_fn=embed_fn_batch,
            vector_writer=vector_writer,
        )
        return {"pipeline_id": pipeline_id}

    # ------------------------------------------------------------------
    # POST /api/admin/import/cancel
    # ------------------------------------------------------------------

    @router.post("/cancel")
    async def cancel(req: CancelRequest) -> dict[str, Any]:
        await importer_facade.cancel_pipeline(req.pipeline_id)
        return {"status": "cancelled"}

    # ------------------------------------------------------------------
    # GET /api/admin/import/events?pipeline_id=...
    # ------------------------------------------------------------------

    @router.get("/events")
    async def events(pipeline_id: str) -> EventSourceResponse:
        """Stream :class:`PipelineEvent` items for a single pipeline.

        Each event is emitted as one SSE frame::

            event: import.progress
            data: {"pipeline_id":"...","type":"...","payload":{...}}

        The stream terminates when the facade pushes its internal
        ``None`` sentinel — cancelled pipelines and naturally-finished
        pipelines both trigger that.
        """

        try:
            iterator = importer_facade.subscribe_events(pipeline_id)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            ) from e

        async def _stream():
            try:
                async for ev in iterator:
                    yield ServerSentEvent(
                        event="import.progress",
                        data=json.dumps(
                            {
                                "pipeline_id": ev.pipeline_id,
                                "type": ev.type,
                                "payload": ev.payload,
                            },
                            default=str,
                            ensure_ascii=False,
                        ),
                    )
            except asyncio.CancelledError:
                raise

        return EventSourceResponse(_stream())

    return router


__all__ = [
    "build_admin_import_router",
    "MAX_UPLOAD_BYTES",
]

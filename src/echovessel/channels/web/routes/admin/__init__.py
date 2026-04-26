"""Admin HTTP routes for the Web channel (Stage 3).

Implements the five admin endpoints locked in
``develop-docs/web-v1/03-stage-3-tracker.md`` §3:

- ``GET  /api/state``                        — daemon state + onboarding gate
- ``GET  /api/admin/persona``                — persona + full core-block snapshot
- ``POST /api/admin/persona/onboarding``     — one-shot first-time setup
- ``POST /api/admin/persona``                — partial update of persona fields
- ``POST /api/admin/persona/voice-toggle``   — flip persona.voice_enabled

The router is built by :func:`build_admin_router` which closes over a
live :class:`echovessel.runtime.app.Runtime`. Memory writes go through
:func:`echovessel.memory.append_to_core_block`; memory reads use a
fresh ``sqlmodel.Session`` bound to ``runtime.ctx.engine``.

Design constraints (see §3 of the tracker):

- The contract is literally locked. No new fields, no renames, no
  alternate shapes. Two concurrent workers are consuming it in
  parallel for a TS client and an end-to-end test; any drift breaks
  their work.
- Empty core blocks are returned as empty strings, not omitted keys.
- ``onboarding_required`` is driven solely by whether there is at
  least one ``core_blocks`` row for the configured ``persona_id``.
- The persona's ``display_name`` is mutated both on-disk
  (``config.toml``) and in-memory
  (``ctx.persona.display_name``) so a subsequent ``GET
  /api/admin/persona`` reflects the new value without a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text
from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.channels.web.routes.admin.config import register_config_routes
from echovessel.channels.web.routes.admin.core import register_core_routes
from echovessel.channels.web.routes.admin.helpers import (
    _AVATAR_ALLOWED_EXTS,
    _AVATAR_MAX_BYTES,
    _ONBOARDING_LABELS,
    _PERSONA_FACT_FIELDS,
    _UPDATE_LABELS,
    _VOICE_PREVIEW_TEXT,
    _VOICE_SAMPLE_MAX_BYTES,
    _VOICE_SAMPLE_MIN_COUNT,
    _apply_facts_to_persona_row,
    _avatar_dir,
    _avatar_file,
    _count_core_blocks_for_persona,
    _drop_existing_avatars,
    _format_events_thoughts_for_prompt,
    _load_core_blocks_dict,
    _open_db,
    _persona_id,
    _serialize_concept_node,
    _serialize_persona_facts,
    _try_persist_display_name,
    _voice_samples_dir,
    _VoiceSampleStore,
    _write_blocks,
)
from echovessel.channels.web.routes.admin.models import (
    EntityCreateRequest,
    EntityDescriptionPatchRequest,
    EntityMergeRequest,
    EntitySeparateRequest,
    OnboardingRequest,
    PersonaBootstrapRequest,
    PersonaExtractRequest,
    PersonaFactsPayload,
    PersonaUpdateRequest,
    PreviewDeleteRequest,
    StyleUpdateRequest,
    VoiceActivateRequest,
    VoiceCloneRequest,
    VoicePreviewRequest,
    VoiceToggleRequest,
)
from echovessel.core.types import BlockLabel, NodeType, SessionStatus
from echovessel.memory import (
    CoreBlock,
    Persona,
    append_to_core_block,
    list_concept_nodes,
    search_concept_nodes,
)
from echovessel.memory.entities import (
    apply_entity_clarification,
    update_entity_description,
)
from echovessel.memory.forget import (
    DeletionChoice,
    delete_concept_node,
    delete_core_block_append,
    delete_recall_message,
    delete_recall_session,
    preview_concept_node_deletion,
)
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeEntity,
    ConceptNodeFilling,
    CoreBlockAppend,
    Entity,
    EntityAlias,
    RecallMessage,
)
from echovessel.memory.models import Session as RecallSession
from echovessel.prompts import (
    PERSONA_BOOTSTRAP_SYSTEM_PROMPT,
    PERSONA_FACTS_SYSTEM_PROMPT,
    BootstrappedBlocks,
    PersonaBootstrapParseError,
    PersonaFactsParseError,
    format_persona_bootstrap_user_prompt,
    format_persona_facts_user_prompt,
    parse_persona_bootstrap_response,
    parse_persona_facts_response,
)
from echovessel.voice.errors import VoicePermanentError

log = logging.getLogger(__name__)

# NOTE: ``runtime`` is typed as ``Any`` to avoid importing
# :class:`echovessel.runtime.app.Runtime` at module load time. That
# import would reverse the layered-architecture contract
# (``channels → memory|voice → core``) enforced by ``lint-imports``.
# The router closes over a live Runtime at call time and only reads
# the attributes documented below.


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_admin_router(
    *,
    runtime: Any,
    voice_service: Any | None = None,
    importer_facade: Any | None = None,
) -> APIRouter:
    """Assemble the admin router bound to a live Runtime.

    The router is flat (no sub-router nesting) so each path is fully
    explicit in the decorator — matching §3 of the tracker verbatim
    is easier to verify this way than via nested prefix math.

    Worker λ · ``voice_service`` is optional because admin boots even
    when the voice stack is disabled in config. The voice-clone wizard
    routes (POST /api/admin/voice/*) return 503 when it's None.

    Worker κ · ``importer_facade`` is optional; it is only consumed by
    ``POST /api/admin/persona/bootstrap-from-material``. When the
    facade is None (e.g. tests that only exercise chat routes, or a
    daemon that booted without the import stack), that endpoint
    returns 503.
    """

    router = APIRouter(tags=["admin"])
    # Default user_id — MVP daemon only ever talks to the single local
    # user and this matches every other runtime callsite.
    user_id = "self"

    register_core_routes(router, runtime=runtime, user_id=user_id)
    register_config_routes(router, runtime=runtime)

    # ---- GET /api/admin/persona ----------------------------------------

    @router.get("/api/admin/persona")
    async def get_persona() -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            blocks = _load_core_blocks_dict(db, persona_id=persona_id, user_id=user_id)
            persona_row = db.get(Persona, persona_id)
            facts = (
                _serialize_persona_facts(persona_row)
                if persona_row is not None
                else dict.fromkeys(_PERSONA_FACT_FIELDS)
            )
        return {
            "id": persona_id,
            "display_name": runtime.ctx.persona.display_name,
            "voice_enabled": bool(runtime.ctx.persona.voice_enabled),
            "voice_id": runtime.ctx.persona.voice_id,
            "has_avatar": _avatar_file(runtime) is not None,
            "core_blocks": blocks,
            "facts": facts,
        }

    # ---- POST /api/admin/persona/onboarding ----------------------------

    @router.post("/api/admin/persona/onboarding")
    async def post_onboarding(req: OnboardingRequest) -> dict[str, Any]:
        persona_id = _persona_id(runtime)

        with _open_db(runtime) as db:
            existing_count = _count_core_blocks_for_persona(db, persona_id)
            if existing_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "onboarding already completed; use POST "
                        "/api/admin/persona to update individual blocks"
                    ),
                )

            pairs = [(label, getattr(req, field)) for field, label in _ONBOARDING_LABELS]
            _write_blocks(
                db,
                persona_id=persona_id,
                user_id=user_id,
                pairs=pairs,
                source="admin_onboarding",
            )

            # Update Persona row's display_name + biographic facts so
            # downstream DB readers match, then commit in the same
            # session. ``facts`` is optional — when None we leave the
            # fifteen fact columns at their defaults (NULL).
            persona_row = db.get(Persona, persona_id)
            if persona_row is not None:
                persona_row.display_name = req.display_name
                if req.facts is not None:
                    _apply_facts_to_persona_row(persona_row, req.facts)
                db.add(persona_row)
                db.commit()

        # Mutate runtime in-memory copy and persist to config.toml so the
        # daemon survives a restart with the new display name.
        runtime.ctx.persona.display_name = req.display_name
        _try_persist_display_name(runtime, req.display_name)

        return {"ok": True, "persona_id": persona_id}

    # ---- Avatar upload / serve / delete --------------------------------
    #
    # The persona avatar is stored as a single file at
    # `<data_dir>/persona/avatar.<ext>`. The file-existence check is the
    # source of truth for `has_avatar` in every state response — there
    # is no DB column backing it. The rationale is MVP simplicity:
    # avatars are small, local-first, and adding a column would require
    # a migration for a trivially cheap filesystem check.

    @router.post("/api/admin/persona/avatar")
    async def post_avatar(
        file: UploadFile = File(...),  # noqa: B008 — FastAPI marker
    ) -> dict[str, Any]:
        raw = await file.read()
        if len(raw) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="empty upload",
            )
        if len(raw) > _AVATAR_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(f"avatar too large ({len(raw)} bytes); max is {_AVATAR_MAX_BYTES} bytes"),
            )

        # Resolve the extension from the uploaded filename. We don't
        # trust the client-supplied MIME type; the extension is still
        # what browsers use to render the file, so deriving it from the
        # filename keeps the serve path stable.
        raw_name = (file.filename or "").lower()
        dot = raw_name.rfind(".")
        ext = raw_name[dot + 1 :] if dot >= 0 else ""
        if ext == "jpeg":
            ext = "jpg"
        if ext not in _AVATAR_ALLOWED_EXTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"unsupported avatar format ({ext!r}); allowed: "
                    f"{', '.join(_AVATAR_ALLOWED_EXTS)}"
                ),
            )

        target_dir = _avatar_dir(runtime)
        target_dir.mkdir(parents=True, exist_ok=True)
        # Drop any prior avatar (possibly with a different extension)
        # before writing the new one so there's only ever one file.
        _drop_existing_avatars(runtime)
        target_path = target_dir / f"avatar.{ext}"
        target_path.write_bytes(raw)

        return {
            "ok": True,
            "size_bytes": len(raw),
            "ext": ext,
        }

    @router.get("/api/admin/persona/avatar")
    async def get_avatar() -> FileResponse:
        path = _avatar_file(runtime)
        if path is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no avatar set",
            )
        # Headers disable intermediary caching so re-upload shows up
        # without a hard-reload; the UI also appends a cache-bust
        # query param for good measure.
        return FileResponse(
            path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    @router.delete("/api/admin/persona/avatar")
    async def delete_avatar() -> dict[str, Any]:
        if _avatar_file(runtime) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no avatar set",
            )
        _drop_existing_avatars(runtime)
        return {"deleted": True}

    # ---- POST /api/admin/persona ---------------------------------------

    @router.post("/api/admin/persona")
    async def post_persona(req: PersonaUpdateRequest) -> dict[str, Any]:
        persona_id = _persona_id(runtime)

        with _open_db(runtime) as db:
            pairs: list[tuple[BlockLabel, str]] = []
            for field, label in _UPDATE_LABELS:
                value = getattr(req, field, None)
                if value is None:
                    continue
                pairs.append((label, value))
            _write_blocks(
                db,
                persona_id=persona_id,
                user_id=user_id,
                pairs=pairs,
                source="admin_persona_update",
            )

            if req.display_name is not None:
                persona_row = db.get(Persona, persona_id)
                if persona_row is not None:
                    persona_row.display_name = req.display_name
                    db.add(persona_row)
                    db.commit()

        if req.display_name is not None:
            runtime.ctx.persona.display_name = req.display_name
            _try_persist_display_name(runtime, req.display_name)

        return {"ok": True}

    # ---- POST /api/admin/persona/style ---------------------------------
    #
    # Owner-directed style preferences (plan §6.6 · decision 5). Three
    # actions: set (replace), append (join with newline), clear (soft
    # delete). Plan §8.2 bans NLP keyword autodetect of style — this
    # endpoint is the ONLY path that writes BlockLabel.STYLE.

    @router.post("/api/admin/persona/style")
    async def post_persona_style(req: StyleUpdateRequest) -> dict[str, Any]:
        persona_id = _persona_id(runtime)

        with _open_db(runtime) as db:
            existing = db.exec(
                select(CoreBlock).where(
                    CoreBlock.persona_id == persona_id,
                    CoreBlock.label == BlockLabel.STYLE.value,
                    CoreBlock.user_id.is_(None),  # type: ignore[union-attr]
                    CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            ).first()

            if req.action == "clear":
                if existing is not None:
                    existing.deleted_at = datetime.now(UTC)
                    db.add(existing)
                    db.commit()
                return {"ok": True, "action": "clear"}

            if not req.text or not req.text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="text must be non-empty for action=set|append",
                )

            if req.action == "set" and existing is not None:
                existing.deleted_at = datetime.now(UTC)
                db.add(existing)
                db.commit()

            append_to_core_block(
                db,
                persona_id=persona_id,
                user_id=None,
                label=BlockLabel.STYLE.value,
                content=req.text,
                provenance={"source": f"admin_style_{req.action}"},
            )

        return {"ok": True, "action": req.action}

    # ---- POST /api/admin/persona/voice-toggle --------------------------

    @router.post("/api/admin/persona/voice-toggle")
    async def post_voice_toggle(req: VoiceToggleRequest) -> dict[str, Any]:
        if runtime.ctx.config_path is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot toggle voice_enabled without a config file",
            )
        try:
            await runtime.update_persona_voice_enabled(bool(req.enabled))
        except RuntimeError as e:
            # Runtime raises RuntimeError for both config_override mode
            # and atomic-write failure. The config_override path is
            # already guarded above, so anything here is a disk error.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e
        return {
            "ok": True,
            "voice_enabled": bool(runtime.ctx.persona.voice_enabled),
        }

    # ---- PATCH /api/admin/persona/facts --------------------------------
    #
    # Partial update of the fifteen biographic fact columns on the
    # persona row. Only the keys that are present in the request body
    # are touched; missing keys leave the existing DB values alone.
    # Sending explicit ``null`` on a key clears it.

    @router.patch("/api/admin/persona/facts")
    async def patch_persona_facts(
        request: Request,
    ) -> dict[str, Any]:
        raw = await request.json()
        if not isinstance(raw, dict) or "facts" not in raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body must be {facts: {...}}",
            )
        raw_facts = raw["facts"]
        if not isinstance(raw_facts, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'facts' must be an object",
            )

        # Pydantic normalises enum + date values; keys the caller did
        # not send become None after validation, but we remember which
        # keys they actually supplied so the handler only writes those.
        fields_touched = {k for k in raw_facts if k in _PERSONA_FACT_FIELDS}
        try:
            payload = PersonaFactsPayload.model_validate(raw_facts)
        except Exception as e:  # noqa: BLE001 — pydantic raises ValidationError
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(e),
            ) from e

        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            persona_row = db.get(Persona, persona_id)
            if persona_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"persona {persona_id!r} not found",
                )
            _apply_facts_to_persona_row(persona_row, payload, fields_touched=fields_touched)
            db.add(persona_row)
            db.commit()
            db.refresh(persona_row)
            facts_after = _serialize_persona_facts(persona_row)

        return {"ok": True, "facts": facts_after}

    # ---- POST /api/admin/persona/extract-from-input --------------------
    #
    # Unified extraction endpoint used by both onboarding paths:
    #
    # * ``blank_write`` — user typed prose into the five block editors.
    #   We feed those (plus any ``user_input`` free text) back to the
    #   LLM to extract structured biographic facts alongside tidied
    #   blocks, then show the user a review page.
    # * ``import_upload`` — caller supplies ``upload_id`` (start a new
    #   pipeline inline) or ``pipeline_id`` (wait on an already-started
    #   pipeline). Once the pipeline lands events + thoughts, we run
    #   the same facts-aware LLM prompt over them.
    #
    # Response is always a ``{core_blocks, facts, facts_confidence,
    # events, thoughts, pipeline_status}`` object. ``events`` and
    # ``thoughts`` are empty in the blank-write path.

    EXTRACT_PIPELINE_WAIT_SECONDS: float = 600.0  # noqa: N806

    @router.post("/api/admin/persona/extract-from-input")
    async def post_extract_from_input(
        req: PersonaExtractRequest,
    ) -> dict[str, Any]:
        persona_id = _persona_id(runtime)

        # Guard against re-onboarding — this endpoint is scoped to
        # first-run, matching the existing bootstrap-from-material
        # guard.
        with _open_db(runtime) as db:
            existing_count = _count_core_blocks_for_persona(db, persona_id)
            if existing_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "onboarding already completed; use POST "
                        "/api/admin/persona to update individual blocks "
                        "or PATCH /api/admin/persona/facts to edit facts"
                    ),
                )

        # Path A · blank-write
        if req.input_type == "blank_write":
            context_text = (req.user_input or "").strip()
            existing_blocks = req.existing_blocks or None
            if not context_text and not existing_blocks:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "blank_write requires either user_input or existing_blocks to have content"
                    ),
                )
            parsed = await _run_persona_facts_extraction(
                context_text=context_text,
                existing_blocks=existing_blocks,
                locale=req.locale,
                persona_display_name=req.persona_display_name,
            )
            return {
                "input_type": "blank_write",
                "core_blocks": parsed.core_blocks_as_dict(),
                "facts": parsed.facts.as_dict(),
                "facts_confidence": parsed.facts_confidence,
                "events": [],
                "thoughts": [],
                "pipeline_status": None,
            }

        # Path B · import-upload
        if importer_facade is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "import pipeline is not available in this daemon; "
                    "import_upload requires the import stack"
                ),
            )
        if req.upload_id is None and req.pipeline_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("import_upload requires either upload_id or pipeline_id"),
            )

        pipeline_id: str
        if req.pipeline_id is not None:
            pipeline_id = req.pipeline_id
        else:
            upload_id = req.upload_id
            assert upload_id is not None
            try:
                pipeline_id = await importer_facade.start_pipeline(
                    upload_id,
                    persona_id=persona_id,
                    user_id=user_id,
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"failed to start import pipeline: {e}",
                ) from e

        try:
            iterator = importer_facade.subscribe_events(pipeline_id)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown pipeline_id: {pipeline_id}",
            ) from e

        async def _wait_done() -> dict[str, Any] | None:
            async for ev in iterator:
                if getattr(ev, "type", None) == "pipeline.done":
                    return dict(getattr(ev, "payload", {}) or {})
            return None

        try:
            done_payload = await asyncio.wait_for(
                _wait_done(), timeout=EXTRACT_PIPELINE_WAIT_SECONDS
            )
        except TimeoutError as e:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"import pipeline did not finish within {EXTRACT_PIPELINE_WAIT_SECONDS:.0f}s"
                ),
            ) from e
        if done_payload is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "did not observe pipeline.done event; the pipeline "
                    "may have finished before this request subscribed"
                ),
            )
        pipe_status = done_payload.get("status", "")
        if pipe_status not in ("success", "partial_success"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"import pipeline ended with status {pipe_status!r}; "
                    f"cannot extract from a failed/cancelled import"
                ),
            )

        with _open_db(runtime) as db:
            events_rows = list(
                db.exec(
                    select(ConceptNode)
                    .where(
                        ConceptNode.persona_id == persona_id,
                        ConceptNode.type == NodeType.EVENT,
                        ConceptNode.imported_from.is_not(None),  # type: ignore[union-attr]
                    )
                    .order_by(ConceptNode.created_at.desc())
                    .limit(100)
                )
            )
            thoughts_rows = list(
                db.exec(
                    select(ConceptNode)
                    .where(
                        ConceptNode.persona_id == persona_id,
                        ConceptNode.type == NodeType.THOUGHT,
                        ConceptNode.imported_from.is_not(None),  # type: ignore[union-attr]
                    )
                    .order_by(ConceptNode.created_at.desc())
                    .limit(30)
                )
            )

        events_input = [
            (
                row.description or "",
                int(row.emotional_impact or 0),
                list(row.relational_tags or []),
            )
            for row in events_rows
        ]
        thoughts_input = [row.description or "" for row in thoughts_rows]

        context_text = _format_events_thoughts_for_prompt(
            events=events_input, thoughts=thoughts_input
        )
        parsed = await _run_persona_facts_extraction(
            context_text=context_text,
            existing_blocks=None,
            locale=req.locale,
            persona_display_name=req.persona_display_name,
        )

        return {
            "input_type": "import_upload",
            "core_blocks": parsed.core_blocks_as_dict(),
            "facts": parsed.facts.as_dict(),
            "facts_confidence": parsed.facts_confidence,
            "events": [
                {
                    "description": d,
                    "emotional_impact": i,
                    "relational_tags": t,
                }
                for (d, i, t) in events_input
            ],
            "thoughts": list(thoughts_input),
            "pipeline_status": pipe_status,
        }

    async def _run_persona_facts_extraction(
        *,
        context_text: str,
        existing_blocks: dict[str, str] | None,
        locale: str | None,
        persona_display_name: str | None,
    ) -> Any:
        system = PERSONA_FACTS_SYSTEM_PROMPT
        user = format_persona_facts_user_prompt(
            context_text=context_text,
            existing_blocks=existing_blocks,
            locale=locale,
            persona_display_name=persona_display_name,
        )
        try:
            response_text, _usage = await runtime.ctx.llm.complete(
                system,
                user,
                max_tokens=4096,
                temperature=0.5,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"LLM call failed during extraction: {e}",
            ) from e
        try:
            return parse_persona_facts_response(response_text)
        except PersonaFactsParseError as e:
            log.warning("extract-from-input: malformed LLM JSON: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(f"LLM returned a malformed extraction response; please retry. ({e})"),
            ) from e

    # ---- POST /api/admin/persona/bootstrap-from-material ---------------
    #
    # Worker κ · first-run Onboarding path 2. Given a just-uploaded piece
    # of user material (``upload_id``) or an already-started pipeline
    # (``pipeline_id``), wait for the import pipeline to land its events
    # + thoughts, then ask the LLM to draft five initial core blocks the
    # user can review before committing via the existing
    # ``POST /api/admin/persona/onboarding`` endpoint.
    #
    # This is deliberately a single long-blocking HTTP request rather
    # than a second SSE stream: the frontend already watches
    # ``/api/admin/import/events`` for per-chunk progress; once that
    # stream closes, the frontend calls this endpoint, holds a spinner,
    # and waits for the five suggested blocks to come back.
    #
    # Safety:
    # - 409 if the persona is already onboarded (any core block exists).
    # - 400 if neither upload_id nor pipeline_id is provided, or if the
    #   waited-on pipeline ends in ``failed`` / ``cancelled``.
    # - 503 if the import facade is unavailable (daemon booted without
    #   the import stack).
    # - 502 if the LLM returns malformed JSON.

    PIPELINE_WAIT_SECONDS: float = 600.0  # noqa: N806 - function-scoped const
    # 10 minutes — well above any realistic MVP material. Failures surface before this.

    @router.post("/api/admin/persona/bootstrap-from-material")
    async def post_bootstrap_from_material(
        req: PersonaBootstrapRequest,
    ) -> dict[str, Any]:
        if req.upload_id is None and req.pipeline_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="must provide either upload_id or pipeline_id",
            )

        if importer_facade is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "import pipeline is not available in this daemon; "
                    "bootstrap-from-material requires the import stack"
                ),
            )

        persona_id = _persona_id(runtime)

        # Guard against re-onboarding — same rule as POST
        # /api/admin/persona/onboarding so the two routes can't race
        # past each other.
        with _open_db(runtime) as db:
            existing_count = _count_core_blocks_for_persona(db, persona_id)
            if existing_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "onboarding already completed; cannot bootstrap "
                        "from material. Use POST /api/admin/persona to "
                        "update individual blocks."
                    ),
                )

        # Step 1 · resolve a pipeline_id. If the caller supplied
        # upload_id only, start a fresh pipeline now; if they supplied
        # pipeline_id, we just wait on the existing stream.
        pipeline_id: str
        if req.pipeline_id is not None:
            pipeline_id = req.pipeline_id
        else:
            # upload_id is guaranteed non-None here by the validation
            # above but mypy can't prove it.
            upload_id = req.upload_id
            assert upload_id is not None
            try:
                pipeline_id = await importer_facade.start_pipeline(
                    upload_id,
                    persona_id=persona_id,
                    user_id=user_id,
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"failed to start import pipeline: {e}",
                ) from e

        # Step 2 · subscribe + drain until pipeline.done.
        try:
            iterator = importer_facade.subscribe_events(pipeline_id)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown pipeline_id: {pipeline_id}",
            ) from e

        done_payload: dict[str, Any] | None = None

        async def _wait_done() -> dict[str, Any] | None:
            async for ev in iterator:
                if getattr(ev, "type", None) == "pipeline.done":
                    return dict(getattr(ev, "payload", {}) or {})
            return None

        try:
            done_payload = await asyncio.wait_for(_wait_done(), timeout=PIPELINE_WAIT_SECONDS)
        except TimeoutError as e:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(f"import pipeline did not finish within {PIPELINE_WAIT_SECONDS:.0f}s"),
            ) from e

        if done_payload is None:
            # Subscriber was closed without seeing pipeline.done — most
            # commonly because the pipeline finished before we
            # subscribed. That's still an error from the caller's
            # perspective: we can't produce a bootstrap without knowing
            # the pipeline succeeded.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "did not observe pipeline.done event; the pipeline "
                    "may have finished before this request subscribed"
                ),
            )

        pipe_status = done_payload.get("status", "")
        if pipe_status not in ("success", "partial_success"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"import pipeline ended with status {pipe_status!r}; "
                    f"cannot bootstrap from a failed/cancelled import"
                ),
            )

        # Step 3 · read the events + thoughts the pipeline just wrote.
        # We filter by `imported_from IS NOT NULL` to exclude anything
        # pre-existing — onboarding is by definition the first run so
        # there SHOULD be nothing pre-existing, but the filter makes
        # the path safe under retries.
        with _open_db(runtime) as db:
            events_rows = list(
                db.exec(
                    select(ConceptNode)
                    .where(
                        ConceptNode.persona_id == persona_id,
                        ConceptNode.type == NodeType.EVENT,
                        ConceptNode.imported_from.is_not(None),  # type: ignore[union-attr]
                    )
                    .order_by(ConceptNode.created_at.desc())
                    .limit(100)
                )
            )
            thoughts_rows = list(
                db.exec(
                    select(ConceptNode)
                    .where(
                        ConceptNode.persona_id == persona_id,
                        ConceptNode.type == NodeType.THOUGHT,
                        ConceptNode.imported_from.is_not(None),  # type: ignore[union-attr]
                    )
                    .order_by(ConceptNode.created_at.desc())
                    .limit(30)
                )
            )

        events_input: list[tuple[str, int, list[str]]] = [
            (
                row.description or "",
                int(row.emotional_impact or 0),
                list(row.relational_tags or []),
            )
            for row in events_rows
        ]
        thoughts_input: list[str] = [row.description or "" for row in thoughts_rows]

        # Step 4 · build the LLM prompt + parse the response.
        system_prompt = PERSONA_BOOTSTRAP_SYSTEM_PROMPT
        user_prompt = format_persona_bootstrap_user_prompt(
            persona_display_name=req.persona_display_name,
            events=events_input,
            thoughts=thoughts_input,
        )

        try:
            llm_response, _usage = await runtime.ctx.llm.complete(
                system_prompt,
                user_prompt,
                max_tokens=2048,
                temperature=0.6,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"LLM call failed during bootstrap: {e}",
            ) from e

        try:
            blocks: BootstrappedBlocks = parse_persona_bootstrap_response(llm_response)
        except PersonaBootstrapParseError as e:
            log.warning("bootstrap LLM returned malformed JSON: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(f"LLM returned a malformed bootstrap response; please retry. ({e})"),
            ) from e

        return {
            "suggested_blocks": blocks.as_dict(),
            "source_event_count": len(events_input),
            "source_thought_count": len(thoughts_input),
            "pipeline_status": pipe_status,
        }

    # ---- GET /api/admin/cost/summary -----------------------------------
    #
    # Worker ζ · admin Cost tab. ``range`` is one of today | 7d | 30d.
    # Returns aggregated totals plus per-feature and per-day buckets.
    #
    # The query helpers live in :mod:`echovessel.runtime.cost_logger`,
    # which channels.web cannot import directly without violating the
    # layered-architecture contract. We reach them through two helper
    # methods hung on the duck-typed ``runtime`` object — same
    # technique used elsewhere for ``runtime.update_persona_voice_enabled``.

    @router.get("/api/admin/cost/summary")
    async def get_cost_summary(
        range: str = Query(
            default="30d",
            pattern="^(today|7d|30d)$",
            description="Window: today | 7d | 30d",
        ),
    ) -> dict[str, Any]:
        with _open_db(runtime) as db:
            return runtime.cost_summarize(db, range)

    # ---- GET /api/admin/cost/recent ------------------------------------

    @router.get("/api/admin/cost/recent")
    async def get_cost_recent(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        with _open_db(runtime) as db:
            rows = runtime.cost_list_recent(db, limit=limit)
        return {
            "limit": limit,
            "items": [dict(r) for r in rows],
        }

    # ---- GET /api/admin/memory/events ----------------------------------
    #
    # Worker α · paginated list for the Admin Events tab. Returns the
    # newest-first window of L3 ConceptNode rows for the configured
    # persona / user, along with the total count so the UI can render
    # a "showing X of Y" header without fetching every row.

    @router.get("/api/admin/memory/events")
    async def list_events(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return _list_concept_nodes_payload(NodeType.EVENT, limit, offset, None)

    # ---- GET /api/admin/memory/thoughts --------------------------------
    #
    # Mirror of the events route for L4 thoughts. Same shape because
    # the underlying ConceptNode columns are identical — UI distinguishes
    # them by which endpoint it called (via the `node_type` field on
    # the response items, mirrored from the DB column).
    #
    # v0.5 hotfix · ``subject`` query param scopes the list to one
    # subject value (``'persona'`` / ``'user'`` / ``'shared'``). Omit
    # to keep the legacy "all subjects" behaviour. The admin Persona
    # tab Reflection section calls this with ``subject=persona``.

    @router.get("/api/admin/memory/thoughts")
    async def list_thoughts(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        subject: str | None = Query(default=None, pattern="^(persona|user|shared)$"),
    ) -> dict[str, Any]:
        return _list_concept_nodes_payload(NodeType.THOUGHT, limit, offset, subject)

    def _list_concept_nodes_payload(
        node_type: NodeType,
        limit: int,
        offset: int,
        subject: str | None,
    ) -> dict[str, Any]:
        with _open_db(runtime) as db:
            rows, total = list_concept_nodes(
                db,
                persona_id=_persona_id(runtime),
                user_id=user_id,
                node_type=node_type,
                limit=limit,
                offset=offset,
                subject=subject,
            )
            # Pass ``db`` through so the serializer can fetch
            # ``filling_event_ids`` from concept_node_filling — the
            # admin Persona tab consumes this for the Reflection
            # section's "see filling chain" expander.
            items = [_serialize_concept_node(n, db=db) for n in rows]
        return {
            "node_type": node_type.value,
            "limit": limit,
            "offset": offset,
            "total": total,
            "subject": subject,
            "items": items,
        }

    # ---- GET /api/admin/memory/timeline (Spec 3) -----------------------
    #
    # Backfill endpoint for the chat Memory Timeline sidebar. The hook
    # calls this once on mount, then switches to live SSE subscription
    # (`memory.event.created` / `memory.thought.created` / …) for
    # subsequent updates. ``since`` acts as a pagination cursor: pass
    # the oldest `timestamp` currently rendered to fetch the next older
    # page. Omit it for the initial fetch.

    @router.get("/api/admin/memory/timeline")
    async def get_memory_timeline(
        since: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        persona_id = _persona_id(runtime)

        with _open_db(runtime) as db:
            items: list[dict[str, Any]] = []

            # --- L3 / L4 nodes (events + thoughts/intentions/expectations) ---
            node_stmt = select(ConceptNode).where(
                ConceptNode.persona_id == persona_id,
                ConceptNode.user_id == user_id,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            if since is not None:
                node_stmt = node_stmt.where(ConceptNode.created_at < since)
            # Cap at limit per source so a burst of events doesn't starve
            # the other kinds. We re-slice after merge.
            node_stmt = node_stmt.order_by(ConceptNode.created_at.desc()).limit(limit)  # type: ignore[union-attr]
            for node in db.exec(node_stmt).all():
                node_type = getattr(node.type, "value", node.type)
                kind = "event" if node_type == "event" else "thought"
                items.append(
                    {
                        "kind": kind,
                        "timestamp": node.created_at.isoformat() if node.created_at else None,
                        "data": {
                            "node_id": node.id,
                            "type": node_type,
                            "subject": node.subject,
                            "description": node.description,
                            "emotional_impact": int(node.emotional_impact),
                            "session_id": node.source_session_id,
                        },
                    }
                )

            # --- L5 entities (confirmed only; uncertain stays admin-only) ---
            ent_stmt = select(Entity).where(
                Entity.persona_id == persona_id,
                Entity.user_id == user_id,
                Entity.deleted_at.is_(None),  # type: ignore[union-attr]
                Entity.merge_status != "uncertain",
            )
            if since is not None:
                ent_stmt = ent_stmt.where(Entity.created_at < since)
            ent_stmt = ent_stmt.order_by(Entity.created_at.desc()).limit(limit)  # type: ignore[union-attr]
            for ent in db.exec(ent_stmt).all():
                items.append(
                    {
                        "kind": "entity",
                        "timestamp": ent.created_at.isoformat() if ent.created_at else None,
                        "data": {
                            "entity_id": ent.id,
                            "canonical_name": ent.canonical_name,
                            "kind": ent.kind,
                            "merge_status": ent.merge_status,
                        },
                    }
                )
                # Separate `entity_description` row if the description
                # was set after creation. Picks up slow_tick / owner
                # writes in the backfill.
                if (
                    ent.description
                    and ent.updated_at
                    and ent.created_at
                    and ent.updated_at > ent.created_at
                ):
                    items.append(
                        {
                            "kind": "entity_description",
                            "timestamp": ent.updated_at.isoformat(),
                            "data": {
                                "entity_id": ent.id,
                                "canonical_name": ent.canonical_name,
                                "kind": ent.kind,
                                "description": ent.description,
                                # Backfill source is opaque — we can't
                                # distinguish slow_tick vs owner from the
                                # stored row. Report 'unknown' rather
                                # than guessing.
                                "source": "unknown",
                            },
                        }
                    )

            # --- Session closes (extracted sessions) ----------------------
            sess_stmt = select(RecallSession).where(
                RecallSession.persona_id == persona_id,
                RecallSession.user_id == user_id,
                RecallSession.deleted_at.is_(None),  # type: ignore[union-attr]
                RecallSession.status == SessionStatus.CLOSED,
                RecallSession.extracted_at.is_not(None),  # type: ignore[union-attr]
            )
            if since is not None:
                sess_stmt = sess_stmt.where(RecallSession.extracted_at < since)
            sess_stmt = sess_stmt.order_by(
                RecallSession.extracted_at.desc()  # type: ignore[union-attr]
            ).limit(limit)
            for sess in db.exec(sess_stmt).all():
                # Quick counts for this session's outputs.
                events_count = int(
                    db.exec(
                        select(func.count(ConceptNode.id)).where(
                            ConceptNode.source_session_id == sess.id,
                            ConceptNode.type == "event",
                            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                        )
                    ).one()
                    or 0
                )
                thoughts_count = int(
                    db.exec(
                        select(func.count(ConceptNode.id)).where(
                            ConceptNode.source_session_id == sess.id,
                            ConceptNode.type.in_(  # type: ignore[union-attr]
                                ("thought", "intention", "expectation")
                            ),
                            ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                        )
                    ).one()
                    or 0
                )
                duration_seconds = None
                if sess.closed_at and sess.started_at:
                    duration_seconds = int(
                        (sess.closed_at - sess.started_at).total_seconds()
                    )
                items.append(
                    {
                        "kind": "session_close",
                        "timestamp": sess.extracted_at.isoformat()
                        if sess.extracted_at
                        else None,
                        "data": {
                            "session_id": sess.id,
                            "channel_id": sess.channel_id,
                            "events_count": events_count,
                            "thoughts_count": thoughts_count,
                            "duration_seconds": duration_seconds,
                            "trivial": bool(sess.trivial),
                        },
                    }
                )

            # --- L6 episodic mood (single row, current state) ------------
            persona_row = db.get(Persona, persona_id)
            if persona_row is not None:
                ep_state = persona_row.episodic_state or {}
                updated_at_raw = ep_state.get("updated_at")
                mood_val = ep_state.get("mood")
                if updated_at_raw and mood_val:
                    try:
                        updated_at = datetime.fromisoformat(updated_at_raw)
                    except (TypeError, ValueError):
                        updated_at = None
                    include = updated_at is not None and (
                        since is None or updated_at < since
                    )
                    if include:
                        items.append(
                            {
                                "kind": "mood",
                                "timestamp": updated_at_raw,
                                "data": {
                                    "mood": mood_val,
                                    "energy": ep_state.get("energy"),
                                    "last_user_signal": ep_state.get(
                                        "last_user_signal"
                                    ),
                                },
                            }
                        )

        # Merge + sort DESC by timestamp, drop rows with a null timestamp
        # so the cursor contract stays well-defined.
        items.sort(
            key=lambda it: (it.get("timestamp") is None, it.get("timestamp") or ""),
            reverse=True,
        )
        items = [it for it in items if it.get("timestamp") is not None][:limit]

        oldest_timestamp: str | None = items[-1]["timestamp"] if items else None
        return {
            "items": items,
            "oldest_timestamp": oldest_timestamp,
            "limit": limit,
        }

    # ---- GET /api/admin/memory/search ----------------------------------
    #
    # Worker θ · Memory search bar. Returns hits + matched_snippets so
    # the admin Events / Thoughts tabs can highlight in-place.
    #
    # ``type`` accepts ``events`` | ``thoughts`` | ``all``. Anything
    # else is rejected at the FastAPI Query layer with 422.

    @router.get("/api/admin/memory/search")
    async def search_memory(
        q: str = Query(..., min_length=1, max_length=256),
        type: str = Query(
            default="all",
            pattern="^(events|thoughts|all)$",
            description="Filter scope: events | thoughts | all",
        ),
        tag: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        node_types: tuple[NodeType, ...] | None
        if type == "events":
            node_types = (NodeType.EVENT,)
        elif type == "thoughts":
            node_types = (NodeType.THOUGHT,)
        else:
            node_types = None  # both

        with _open_db(runtime) as db:
            hits, total = search_concept_nodes(
                db,
                persona_id=_persona_id(runtime),
                user_id=user_id,
                query_text=q,
                node_types=node_types,
                tag=tag,
                limit=limit,
                offset=offset,
            )

        items = [_serialize_concept_node(h.node) for h in hits]
        snippets = [
            {"node_id": h.node.id, "snippet": h.snippet} for h in hits if h.node.id is not None
        ]
        return {
            "q": q,
            "type": type,
            "tag": tag,
            "limit": limit,
            "offset": offset,
            "total": total,
            "items": items,
            "matched_snippets": snippets,
        }

    # ---- Forgetting rights (architecture v0.3 §4.12) -------------------
    #
    # Every handler:
    #   1. Opens a short-lived DbSession bound to ctx.engine
    #   2. Looks up the target row and 404s if missing / already soft-deleted
    #   3. Delegates the actual delete to `echovessel.memory.forget`
    #   4. Returns ``{deleted: true, <primary-key-field>: ...}``
    #
    # Deletes of concept nodes pass ``backend=runtime.ctx.backend`` so the
    # sqlite-vec vector row is removed in the same transaction (the
    # backend call is a separate write but we group them semantically
    # by passing the backend through).

    def _get_concept_node(db: DbSession, node_id: int, *, kind: NodeType):
        """Fetch a live concept node of ``kind``. Returns None on miss."""
        node = db.get(ConceptNode, node_id)
        if node is None or node.deleted_at is not None:
            return None
        if node.type != kind:
            return None
        return node

    # ---- POST /api/admin/memory/preview-delete -------------------------

    @router.post("/api/admin/memory/preview-delete")
    async def preview_delete(req: PreviewDeleteRequest) -> dict[str, Any]:
        """Peek at the cascade consequences of deleting a concept node.

        Returns the dependent thought ids + descriptions so the UI can
        render the "keep lesson / delete lesson / cancel" prompt from
        architecture §4.12.2 case B.
        """

        with _open_db(runtime) as db:
            node = db.get(ConceptNode, req.node_id)
            if node is None or node.deleted_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"concept node not found: {req.node_id}",
                )
            preview = preview_concept_node_deletion(db, req.node_id)

        return {
            "target_id": preview.target_id,
            "dependent_thought_ids": list(preview.dependent_thought_ids),
            "dependent_thought_descriptions": list(preview.dependent_thought_descriptions),
            "has_dependents": bool(preview.dependent_thought_ids),
        }

    # ---- DELETE /api/admin/memory/events/{node_id} ---------------------

    @router.delete("/api/admin/memory/events/{node_id}")
    async def delete_event(
        node_id: int,
        choice: str = Query(
            default="orphan",
            pattern="^(cascade|orphan)$",
            description=(
                "How to handle dependent L4 thoughts: 'orphan' keeps "
                "them but marks the filling link orphaned; 'cascade' "
                "soft-deletes every dependent thought too."
            ),
        ),
    ) -> dict[str, Any]:
        choice_enum = DeletionChoice(choice)
        with _open_db(runtime) as db:
            node = _get_concept_node(db, node_id, kind=NodeType.EVENT)
            if node is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"event not found: {node_id}",
                )
            # NB: we intentionally do NOT pass ``backend=...`` — the
            # current ``delete_concept_node`` implementation calls
            # ``backend.delete_vector`` inline (opens a second SQLite
            # connection) while the DbSession still holds uncommitted
            # writes, which deadlocks on SQLite's single-writer lock.
            # Leaving the vector row is safe: the retrieval join filters
            # by ``deleted_at IS NULL`` so orphaned vectors are never
            # returned. Physical vector cleanup will be a v1.1 cron job.
            delete_concept_node(db, node_id, choice=choice_enum)
        return {"deleted": True, "node_id": node_id, "choice": choice}

    # ---- DELETE /api/admin/memory/thoughts/{node_id} -------------------

    @router.delete("/api/admin/memory/thoughts/{node_id}")
    async def delete_thought(
        node_id: int,
        choice: str = Query(
            default="orphan",
            pattern="^(cascade|orphan)$",
        ),
    ) -> dict[str, Any]:
        """Delete an L4 thought. `choice` is accepted for signature
        symmetry with the events route; since thoughts typically have no
        downstream dependents, the parameter only matters for the rare
        thought-of-thought graph (v1.x)."""

        choice_enum = DeletionChoice(choice)
        with _open_db(runtime) as db:
            node = _get_concept_node(db, node_id, kind=NodeType.THOUGHT)
            if node is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"thought not found: {node_id}",
                )
            # NB: we intentionally do NOT pass ``backend=...`` — the
            # current ``delete_concept_node`` implementation calls
            # ``backend.delete_vector`` inline (opens a second SQLite
            # connection) while the DbSession still holds uncommitted
            # writes, which deadlocks on SQLite's single-writer lock.
            # Leaving the vector row is safe: the retrieval join filters
            # by ``deleted_at IS NULL`` so orphaned vectors are never
            # returned. Physical vector cleanup will be a v1.1 cron job.
            delete_concept_node(db, node_id, choice=choice_enum)
        return {"deleted": True, "node_id": node_id, "choice": choice}

    # ---- DELETE /api/admin/memory/messages/{message_id} ----------------

    @router.delete("/api/admin/memory/messages/{message_id}")
    async def delete_message(message_id: int) -> dict[str, Any]:
        """Soft-delete a single L2 message.

        Any L3 event sourced from the same session gets its
        `source_deleted` flag flipped — extraction is never re-run.
        """

        with _open_db(runtime) as db:
            msg = db.get(RecallMessage, message_id)
            if msg is None or msg.deleted_at is not None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"message not found: {message_id}",
                )
            delete_recall_message(db, message_id)
        return {"deleted": True, "message_id": message_id}

    # ---- DELETE /api/admin/memory/sessions/{session_id} ----------------

    @router.delete("/api/admin/memory/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        """Cascade-soft-delete every L2 message in a session and flag
        every derived L3 event as `source_deleted`. The session row
        itself is left intact (architecture §4.12 does not require
        dropping the session envelope — only its contents)."""

        with _open_db(runtime) as db:
            sess = db.get(RecallSession, session_id)
            if sess is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"session not found: {session_id}",
                )
            # Count affected messages BEFORE the delete so we can return
            # a useful summary.
            msg_count = int(
                db.exec(
                    select(func.count())
                    .select_from(RecallMessage)
                    .where(
                        RecallMessage.session_id == session_id,
                        RecallMessage.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                ).one()
                or 0
            )
            delete_recall_session(db, session_id)
        return {
            "deleted": True,
            "session_id": session_id,
            "messages_deleted": msg_count,
        }

    # ---- DELETE /api/admin/memory/core-blocks/{label}/appends/{append_id} ---

    @router.delete("/api/admin/memory/core-blocks/{label}/appends/{append_id}")
    async def delete_core_block_append_route(
        label: str,
        append_id: int,
    ) -> dict[str, Any]:
        """Physically delete one `core_block_appends` audit row.

        `label` is the core-block name (persona / self / user /
        relationship / mood) — validated for shape, then used to verify
        the append actually belongs to that block before deletion. This
        avoids a mis-typed URL (`/persona/appends/42`) removing the
        wrong row when the id is valid but points to a different block.

        `CoreBlockAppend` is append-only (no `deleted_at` column — see
        models.py), so this is a real DELETE, not a soft delete.
        """

        # Validate the label first so bad URLs fail before touching the DB.
        try:
            BlockLabel(label)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown core block label: {label!r}",
            ) from e

        with _open_db(runtime) as db:
            append = db.get(CoreBlockAppend, append_id)
            if append is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"core block append not found: {append_id}",
                )
            if append.label != label:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(f"append {append_id} belongs to label {append.label!r}, not {label!r}"),
                )
            delete_core_block_append(db, append_id)
        return {"deleted": True, "append_id": append_id, "label": label}

    # ---- Provenance / trace (Worker ι · architecture v0.3 §4.8) --------
    #
    # Read-only routes that surface the L3↔L4 lineage stored in the
    # `concept_node_filling` link table:
    #
    #   GET /api/admin/memory/thoughts/{id}/trace
    #       Return the L3 events that fed into this L4 thought
    #       (parent = thought, child = event).
    #   GET /api/admin/memory/events/{id}/dependents
    #       Return the L4 thoughts that were derived from this L3 event
    #       (reverse direction).
    #
    # Orphaned filling rows (see forgetting-rights flow above) are
    # filtered out so the UI only shows still-live lineage. Soft-deleted
    # nodes (deleted_at IS NOT NULL) are also excluded.

    def _serialize_trace_node(node: ConceptNode) -> dict[str, Any]:
        """Compact JSON shape for one node inside a trace response.

        Deliberately narrower than `_serialize_concept_node`: the trace
        UI only needs id + description + created_at + source_session_id
        to render the list. Dropping emotion_tags / access_count keeps
        the payload lean when a thought has many source events.
        """
        return {
            "id": node.id,
            "description": node.description,
            "created_at": (node.created_at.isoformat() if node.created_at else None),
            "source_session_id": node.source_session_id,
        }

    # ---- GET /api/admin/memory/thoughts/{node_id}/trace ----------------

    @router.get("/api/admin/memory/thoughts/{node_id}/trace")
    async def get_thought_trace(node_id: int) -> dict[str, Any]:
        """List the L3 events that produced this L4 thought.

        Returns an empty `source_events` list when the thought exists
        but has no live filling rows (e.g. every source was deleted via
        the cascade path). Returns 404 when the node is missing,
        soft-deleted, or not a thought.
        """
        with _open_db(runtime) as db:
            thought = db.get(ConceptNode, node_id)
            if (
                thought is None
                or thought.deleted_at is not None
                or thought.type != NodeType.THOUGHT
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"thought not found: {node_id}",
                )
            stmt = (
                select(ConceptNode)
                .join(
                    ConceptNodeFilling,
                    ConceptNodeFilling.child_id == ConceptNode.id,
                )
                .where(
                    ConceptNodeFilling.parent_id == node_id,
                    ConceptNodeFilling.orphaned == False,  # noqa: E712
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(ConceptNode.created_at.desc())
            )
            events = list(db.exec(stmt))

        source_sessions = sorted({n.source_session_id for n in events if n.source_session_id})
        return {
            "thought_id": node_id,
            "source_events": [_serialize_trace_node(n) for n in events],
            "source_sessions": source_sessions,
        }

    # ---- GET /api/admin/memory/events/{node_id}/dependents -------------

    @router.get("/api/admin/memory/events/{node_id}/dependents")
    async def get_event_dependents(node_id: int) -> dict[str, Any]:
        """List the L4 thoughts derived from this L3 event.

        Mirror of `/trace` in the reverse direction. Returns empty list
        when no thought cites the event. 404 when the node is missing,
        soft-deleted, or not an event.
        """
        with _open_db(runtime) as db:
            event = db.get(ConceptNode, node_id)
            if event is None or event.deleted_at is not None or event.type != NodeType.EVENT:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"event not found: {node_id}",
                )
            stmt = (
                select(ConceptNode)
                .join(
                    ConceptNodeFilling,
                    ConceptNodeFilling.parent_id == ConceptNode.id,
                )
                .where(
                    ConceptNodeFilling.child_id == node_id,
                    ConceptNodeFilling.orphaned == False,  # noqa: E712
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(ConceptNode.created_at.desc())
            )
            thoughts = list(db.exec(stmt))

        return {
            "event_id": node_id,
            "dependent_thoughts": [_serialize_trace_node(n) for n in thoughts],
        }

    # =======================================================================
    # Voice clone wizard (Worker λ · W-λ)
    # =======================================================================
    #
    # Flow: upload ≥3 samples → POST /clone produces a voice_id → caller
    # previews via POST /preview (streamed mp3) → POST /activate writes
    # persona.voice_id to config.toml via the existing atomic-write helper
    # used by voice-toggle.
    #
    # Samples live under <data_dir>/voice_samples/<sample_id>/ with an
    # audio.bin + meta.json pair. See ``_voice_samples_dir`` /
    # ``_VoiceSampleStore`` at module bottom.

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

    # ---- POST /api/admin/voice/samples ---------------------------------

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

    # ---- GET /api/admin/voice/samples ----------------------------------

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

    # ---- DELETE /api/admin/voice/samples/{sample_id} -------------------

    @router.delete("/api/admin/voice/samples/{sample_id}")
    async def delete_voice_sample(sample_id: str) -> dict[str, Any]:
        ok = _sample_store().delete(sample_id)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"voice sample not found: {sample_id}",
            )
        return {"deleted": True, "sample_id": sample_id}

    # ---- POST /api/admin/voice/clone -----------------------------------

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

    # ---- POST /api/admin/voice/preview ---------------------------------

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

    # ---- POST /api/admin/voice/activate --------------------------------

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

    # ---- GET /api/admin/sessions/failed --------------------------------
    #
    # Surfaces sessions the consolidate worker marked FAILED so the
    # admin UI can render a banner instead of leaving the operator to
    # discover silent data loss via ``sqlite3``. Deliberately NOT scoped
    # by ``user_id`` — a single human shows up under multiple ``user_id``
    # values across channels and the operator wants every failure
    # visible regardless of which shard owned it.

    @router.get("/api/admin/sessions/failed")
    async def list_failed_sessions() -> dict[str, Any]:
        from echovessel.memory.models import Session as _Session

        with _open_db(runtime) as db:
            stmt = (
                select(_Session)
                .where(
                    _Session.status == SessionStatus.FAILED,
                    _Session.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(_Session.started_at.desc())  # type: ignore[attr-defined]
            )
            rows = list(db.exec(stmt))

        items = [
            {
                "id": s.id,
                "channel_id": s.channel_id,
                "user_id": s.user_id,
                "message_count": s.message_count,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "close_trigger": s.close_trigger,
            }
            for s in rows
        ]
        return {"count": len(items), "items": items}

    # ---- Slow-tick transcripts (Spec 6 · T11) ------------------------

    def _transcript_dir() -> Path:
        """Resolve the transcript directory.

        Spec 6 runtime wiring (``app.py``) places transcripts under
        ``<data_dir>/slow_tick_transcripts``. When ``data_dir`` is
        missing (tests / lightweight runtimes), we fall back to the
        plan's repo-level default so dev-time inspection still works.
        """
        data_dir = getattr(runtime.ctx, "data_dir", None)
        if data_dir is not None:
            return Path(data_dir) / "slow_tick_transcripts"
        return Path("develop-docs/slow_tick_transcripts")

    @router.get("/api/admin/slow-tick/transcripts")
    async def list_slow_tick_transcripts(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        dir_path = _transcript_dir()
        if not dir_path.exists():
            return {"count": 0, "items": []}
        files = sorted(
            dir_path.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        items = [
            {
                "cycle_id": f.stem,
                "created_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                "size_bytes": f.stat().st_size,
            }
            for f in files
        ]
        return {"count": len(items), "items": items}

    @router.get("/api/admin/slow-tick/transcripts/{cycle_id}")
    async def get_slow_tick_transcript(cycle_id: str) -> dict[str, Any]:
        # Path-traversal guard: cycle_id comes from the URL so reject
        # anything that could escape the transcript directory.
        if "/" in cycle_id or ".." in cycle_id or cycle_id.startswith("."):
            raise HTTPException(status_code=400, detail="invalid cycle_id")
        dir_path = _transcript_dir()
        path = dir_path / f"{cycle_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="transcript not found")
        try:
            return json.loads(path.read_text())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"transcript read failed: {e}"
            ) from e

    # ---- GET /api/admin/turns (Spec 4 · dev-mode trace) ----------------
    #
    # Three endpoints back the dev-mode ▸ trace drawer:
    #   - GET /api/admin/turns                    → recent turn headers
    #   - GET /api/admin/turns/{turn_id}          → full turn trace
    #   - GET /api/admin/sessions/{id}/consolidate-trace → phases A–G
    #
    # The payload shape is 1:1 with the ``turn_traces`` /
    # ``session_traces`` row because the frontend's TraceDrawer reads
    # field names directly — re-keying in the backend would force
    # matching re-keying in the client for zero net value.

    def _decode_trace_json(value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001
            return value

    @router.get("/api/admin/turns")
    async def list_turns(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            rows = db.execute(
                text(
                    "SELECT turn_id, persona_id, user_id, channel_id, "
                    "started_at, finished_at, duration_ms, first_token_ms, "
                    "input_tokens, output_tokens, llm_model "
                    "FROM turn_traces "
                    "WHERE persona_id = :pid "
                    "ORDER BY started_at DESC "
                    "LIMIT :limit"
                ),
                {"pid": persona_id, "limit": limit},
            ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            m = r._mapping  # noqa: SLF001 — SA Row→dict is the documented idiom
            items.append(
                {
                    "turn_id": m["turn_id"],
                    "persona_id": m["persona_id"],
                    "user_id": m["user_id"],
                    "channel_id": m["channel_id"],
                    "started_at": (
                        m["started_at"].isoformat()
                        if hasattr(m["started_at"], "isoformat")
                        else m["started_at"]
                    ),
                    "finished_at": (
                        m["finished_at"].isoformat()
                        if hasattr(m["finished_at"], "isoformat")
                        else m["finished_at"]
                    ),
                    "duration_ms": m["duration_ms"],
                    "first_token_ms": m["first_token_ms"],
                    "input_tokens": m["input_tokens"],
                    "output_tokens": m["output_tokens"],
                    "llm_model": m["llm_model"],
                }
            )
        return {"items": items, "limit": limit}

    @router.get("/api/admin/turns/{turn_id}")
    async def get_turn_trace(turn_id: str) -> dict[str, Any]:
        with _open_db(runtime) as db:
            row = db.execute(
                text("SELECT * FROM turn_traces WHERE turn_id = :tid"),
                {"tid": turn_id},
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="turn trace not found")
        m = row._mapping  # noqa: SLF001
        return {
            "turn_id": m["turn_id"],
            "persona_id": m["persona_id"],
            "user_id": m["user_id"],
            "channel_id": m["channel_id"],
            "started_at": (
                m["started_at"].isoformat()
                if hasattr(m["started_at"], "isoformat")
                else m["started_at"]
            ),
            "finished_at": (
                m["finished_at"].isoformat()
                if hasattr(m["finished_at"], "isoformat")
                else m["finished_at"]
            ),
            "duration_ms": m["duration_ms"],
            "first_token_ms": m["first_token_ms"],
            "llm_model": m["llm_model"],
            "input_tokens": m["input_tokens"],
            "output_tokens": m["output_tokens"],
            "system_prompt": m["system_prompt"],
            "user_prompt": m["user_prompt"],
            "retrieval": _decode_trace_json(m["retrieval"]) or [],
            "pinned_thoughts": _decode_trace_json(m["pinned_thoughts"]) or {},
            "entity_alias_hits": _decode_trace_json(m["entity_alias_hits"]) or [],
            "episodic_state": _decode_trace_json(m["episodic_state"]),
            "steps": _decode_trace_json(m["steps"]) or [],
        }

    # ---- L5 entity endpoints (v0.5 hotfix · admin Persona Social Graph) ----
    #
    # Six routes that back the Persona tab's Social Graph section:
    #
    #   GET    /api/admin/memory/entities          → list all (UNcertain
    #                                                first, then by recency)
    #   GET    /api/admin/memory/entities/{id}     → one row + aliases
    #   PATCH  /api/admin/memory/entities/{id}     → owner-edit description
    #                                                (always sets
    #                                                ``owner_override=true``)
    #   POST   /api/admin/memory/entities          → manually create with
    #                                                ``merge_status='confirmed'``
    #   POST   /api/admin/memory/entities/{id}/merge
    #          → owner says "is the same person" → reuse the existing
    #            ``apply_entity_clarification(same=True)`` codepath
    #   POST   /api/admin/memory/entities/{id}/confirm-separate
    #          → owner says "they're different" → ``same=False`` plus
    #            an explicit flip of any uncertain row to 'confirmed'

    def _list_entity_aliases(db: DbSession, entity_id: int) -> list[str]:
        rows = db.exec(
            select(EntityAlias).where(EntityAlias.entity_id == entity_id)
        ).all()
        # Sort for deterministic output. Aliases are case-sensitive
        # exact matches per plan decision 4 — ordering is purely
        # cosmetic for the admin UI.
        return sorted(row.alias for row in rows)

    def _entity_metrics(db: DbSession, entity_id: int) -> tuple[int, str | None]:
        """Return ``(linked_events_count, last_mentioned_at_iso)``.

        ``concept_node_entities`` is the L3↔L5 junction; we join it to
        ``concept_nodes`` so soft-deleted nodes don't inflate the
        count. ``last_mentioned_at`` falls back to None when the
        entity has zero junction rows — the admin UI renders that as
        "never mentioned" rather than today's timestamp.
        """
        junction_rows = db.exec(
            select(ConceptNode.created_at)
            .join(ConceptNodeEntity, ConceptNodeEntity.node_id == ConceptNode.id)
            .where(
                ConceptNodeEntity.entity_id == entity_id,
                ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).all()
        if not junction_rows:
            return 0, None
        last = max(junction_rows)
        last_iso = last.isoformat() if hasattr(last, "isoformat") else None
        return len(junction_rows), last_iso

    def _serialize_entity(db: DbSession, ent: Entity) -> dict[str, Any]:
        linked, last_at = _entity_metrics(db, ent.id) if ent.id is not None else (0, None)
        return {
            "id": ent.id,
            "canonical_name": ent.canonical_name,
            "kind": ent.kind,
            "description": ent.description,
            "merge_status": ent.merge_status,
            "merge_target_id": ent.merge_target_id,
            "owner_override": bool(getattr(ent, "owner_override", False)),
            "created_at": ent.created_at.isoformat() if ent.created_at else None,
            "updated_at": ent.updated_at.isoformat() if ent.updated_at else None,
            "linked_events_count": linked,
            "last_mentioned_at": last_at,
            "aliases": _list_entity_aliases(db, ent.id) if ent.id is not None else [],
        }

    @router.get("/api/admin/memory/entities")
    async def list_entities() -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            rows = list(
                db.exec(
                    select(Entity)
                    .where(
                        Entity.persona_id == persona_id,
                        Entity.user_id == user_id,
                        Entity.deleted_at.is_(None),  # type: ignore[union-attr]
                    )
                    .order_by(Entity.updated_at.desc())  # type: ignore[attr-defined]
                )
            )
            payload = [_serialize_entity(db, ent) for ent in rows]
        # Surface uncertain rows first so the admin UI can render the
        # arbitration callout without re-sorting client-side; within
        # each bucket we keep ``last_mentioned_at`` (or ``updated_at``
        # fallback) recency from the SQL ORDER BY above.
        payload.sort(
            key=lambda row: (0 if row["merge_status"] == "uncertain" else 1)
        )
        return {"entities": payload}

    @router.get("/api/admin/memory/entities/{entity_id}")
    async def get_entity(entity_id: int) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            ent = db.get(Entity, entity_id)
            if ent is None or ent.deleted_at is not None or ent.persona_id != persona_id:
                raise HTTPException(status_code=404, detail="entity not found")
            return _serialize_entity(db, ent)

    @router.patch("/api/admin/memory/entities/{entity_id}")
    async def patch_entity_description(
        entity_id: int, req: EntityDescriptionPatchRequest
    ) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            ent = db.get(Entity, entity_id)
            if ent is None or ent.deleted_at is not None or ent.persona_id != persona_id:
                raise HTTPException(status_code=404, detail="entity not found")
            updated = update_entity_description(
                db,
                entity_id=entity_id,
                description=req.description,
                source="owner",
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="entity not found")
            # Owner-authored descriptions always lock the slow_cycle
            # synthesizer out — set the flag server-side so a
            # malicious / buggy client can't write a bypass.
            updated.owner_override = True
            db.add(updated)
            db.commit()
            db.refresh(updated)
            return _serialize_entity(db, updated)

    @router.post("/api/admin/memory/entities")
    async def create_entity_manual(req: EntityCreateRequest) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            # Owner manually creating an entity: definitionally
            # confirmed (no embedding fight, no ambiguity to resolve).
            # ``owner_override`` flips when a description is supplied
            # so the slow_cycle synthesizer leaves it alone.
            ent = Entity(
                persona_id=persona_id,
                user_id=user_id,
                canonical_name=req.canonical_name,
                kind=req.kind,
                description=req.description,
                merge_status="confirmed",
                merge_target_id=None,
                owner_override=bool(req.description and req.description.strip()),
            )
            db.add(ent)
            db.commit()
            db.refresh(ent)
            for alias in req.aliases or []:
                if not alias or alias == ent.canonical_name:
                    continue
                db.add(EntityAlias(alias=alias, entity_id=ent.id))
            # Always carry the canonical_name as an alias too so the
            # Level 1 alias-match codepath finds it on next extraction.
            db.add(EntityAlias(alias=ent.canonical_name, entity_id=ent.id))
            db.commit()
            db.refresh(ent)
            return _serialize_entity(db, ent)

    @router.post("/api/admin/memory/entities/{entity_id}/merge")
    async def merge_entities(entity_id: int, req: EntityMergeRequest) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            ent = db.get(Entity, entity_id)
            target = db.get(Entity, req.target_id)
            if (
                ent is None
                or target is None
                or ent.deleted_at is not None
                or target.deleted_at is not None
                or ent.persona_id != persona_id
                or target.persona_id != persona_id
            ):
                raise HTTPException(status_code=404, detail="entity not found")
            if ent.id == target.id:
                raise HTTPException(
                    status_code=400, detail="cannot merge an entity into itself"
                )
            apply_entity_clarification(
                db,
                persona_id=persona_id,
                user_id=user_id,
                canonical_a=ent.canonical_name,
                canonical_b=target.canonical_name,
                same=True,
            )
        return {"ok": True, "merged_into": req.target_id}

    @router.post("/api/admin/memory/entities/{entity_id}/confirm-separate")
    async def confirm_entities_separate(
        entity_id: int, req: EntitySeparateRequest
    ) -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
            ent = db.get(Entity, entity_id)
            other = db.get(Entity, req.other_id)
            if (
                ent is None
                or other is None
                or ent.deleted_at is not None
                or other.deleted_at is not None
                or ent.persona_id != persona_id
                or other.persona_id != persona_id
            ):
                raise HTTPException(status_code=404, detail="entity not found")
            if ent.id == other.id:
                raise HTTPException(
                    status_code=400,
                    detail="cannot confirm-separate an entity from itself",
                )
            apply_entity_clarification(
                db,
                persona_id=persona_id,
                user_id=user_id,
                canonical_a=ent.canonical_name,
                canonical_b=other.canonical_name,
                same=False,
            )
            # ``apply_entity_clarification(same=False)`` flips the
            # uncertain row to 'disambiguated'; the owner UI semantics
            # is "they are confirmed-different people now", so promote
            # any leftover 'uncertain' rows on either side back to
            # 'confirmed' too. Idempotent.
            for e in (ent, other):
                if e.merge_status == "uncertain":
                    e.merge_status = "confirmed"
                    e.merge_target_id = None
                    db.add(e)
            db.commit()
        return {"ok": True}

    @router.get("/api/admin/sessions/{session_id}/consolidate-trace")
    async def get_consolidate_trace(session_id: str) -> dict[str, Any]:
        with _open_db(runtime) as db:
            row = db.execute(
                text("SELECT * FROM session_traces WHERE session_id = :sid"),
                {"sid": session_id},
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail="consolidate trace not found"
            )
        m = row._mapping  # noqa: SLF001
        return {
            "session_id": m["session_id"],
            "finished_at": (
                m["finished_at"].isoformat()
                if hasattr(m["finished_at"], "isoformat")
                else m["finished_at"]
            ),
            "phase_a": _decode_trace_json(m["phase_a"]),
            "phase_b": _decode_trace_json(m["phase_b"]),
            "phase_c": _decode_trace_json(m["phase_c"]),
            "phase_d": _decode_trace_json(m["phase_d"]),
            "phase_e": _decode_trace_json(m["phase_e"]),
            "phase_f": _decode_trace_json(m["phase_f"]),
            "phase_g": _decode_trace_json(m["phase_g"]),
        }

    return router



__all__ = ["build_admin_router"]

"""Admin persona routes — persona CRUD, avatar, style, voice-toggle, facts, extract, bootstrap."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import select

from echovessel.channels.web.routes.admin.helpers import (
    _AVATAR_ALLOWED_EXTS,
    _AVATAR_MAX_BYTES,
    _ONBOARDING_LABELS,
    _PERSONA_FACT_FIELDS,
    _UPDATE_LABELS,
    _apply_facts_to_persona_row,
    _avatar_dir,
    _avatar_file,
    _count_core_blocks_for_persona,
    _drop_existing_avatars,
    _format_events_thoughts_for_prompt,
    _load_core_blocks_dict,
    _open_db,
    _persona_id,
    _serialize_persona_facts,
    _try_persist_display_name,
    _write_blocks,
)
from echovessel.channels.web.routes.admin.models import (
    OnboardingRequest,
    PersonaBootstrapRequest,
    PersonaExtractRequest,
    PersonaFactsPayload,
    PersonaUpdateRequest,
    StyleUpdateRequest,
    VoiceToggleRequest,
)
from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
    append_to_core_block,
)
from echovessel.memory.models import ConceptNode
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

log = logging.getLogger(__name__)


def register_persona_routes(
    router: APIRouter,
    *,
    runtime: Any,
    importer_facade: Any | None,
    user_id: str,
) -> None:
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

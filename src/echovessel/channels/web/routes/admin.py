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
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session as DbSession
from sqlmodel import func, select

# NOTE: the allowlists live in `echovessel.core.config_paths`, NOT in
# `echovessel.runtime.config`, because channels → runtime would break
# the layered-architecture contract (channels imports core, runtime
# imports everything above core).
from echovessel.core.config_paths import (
    HOT_RELOADABLE_CONFIG_PATHS,
    RESTART_REQUIRED_CONFIG_PATHS,
)
from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
    append_to_core_block,
    list_concept_nodes,
    search_concept_nodes,
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
    ConceptNodeFilling,
    CoreBlockAppend,
    RecallMessage,
)
from echovessel.memory.models import Session as RecallSession
from echovessel.prompts import (
    PERSONA_BOOTSTRAP_SYSTEM_PROMPT,
    BootstrappedBlocks,
    PersonaBootstrapParseError,
    format_persona_bootstrap_user_prompt,
    parse_persona_bootstrap_response,
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
# Request models
# ---------------------------------------------------------------------------


class OnboardingRequest(BaseModel):
    """Body for ``POST /api/admin/persona/onboarding``.

    All fields are required (the frontend sends them even when empty),
    but empty strings are accepted and silently skipped at write time.
    """

    display_name: str = Field(..., min_length=1, max_length=256)
    persona_block: str = Field(...)
    self_block: str = Field(...)
    user_block: str = Field(...)
    mood_block: str = Field(...)


class PersonaUpdateRequest(BaseModel):
    """Body for ``POST /api/admin/persona``.

    Every field is optional — the server applies only the keys that
    are actually present in the request body.
    """

    display_name: str | None = Field(default=None, max_length=256)
    persona_block: str | None = None
    self_block: str | None = None
    user_block: str | None = None
    mood_block: str | None = None
    relationship_block: str | None = None


class VoiceToggleRequest(BaseModel):
    """Body for ``POST /api/admin/persona/voice-toggle``."""

    enabled: bool


class PersonaBootstrapRequest(BaseModel):
    """Body for ``POST /api/admin/persona/bootstrap-from-material``.

    At least one of ``upload_id`` or ``pipeline_id`` MUST be supplied:

    - ``upload_id``   — the caller has uploaded material but has not
      started a pipeline. This endpoint will start one, wait for
      ``pipeline.done``, then bootstrap.
    - ``pipeline_id`` — the caller already started a pipeline (via
      ``POST /api/admin/import/start``) and is ready to consume its
      output. This endpoint subscribes to the existing stream and
      waits for ``pipeline.done``.

    ``persona_display_name`` is an optional hint passed to the LLM so
    the generated blocks can reference the persona by name where
    natural. The ACTUAL display_name is set later via
    ``POST /api/admin/persona/onboarding``.
    """

    upload_id: str | None = Field(default=None, min_length=1, max_length=64)
    pipeline_id: str | None = Field(default=None, min_length=1, max_length=64)
    persona_display_name: str | None = Field(default=None, max_length=256)


class VoiceCloneRequest(BaseModel):
    """Body for ``POST /api/admin/voice/clone``.

    Worker λ. ``display_name`` is the user-facing label for the new
    cloned voice (e.g. "我的声音 2026-04-16"). The backend takes every
    current draft sample, concatenates the raw bytes, and passes the
    blob to :meth:`VoiceService.clone_voice_interactive`.
    """

    display_name: str = Field(..., min_length=1, max_length=128)


class VoicePreviewRequest(BaseModel):
    """Body for ``POST /api/admin/voice/preview``."""

    voice_id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=500)


class VoiceActivateRequest(BaseModel):
    """Body for ``POST /api/admin/voice/activate``."""

    voice_id: str = Field(..., min_length=1, max_length=128)


class PreviewDeleteRequest(BaseModel):
    """Body for ``POST /api/admin/memory/preview-delete``.

    ``node_id`` is the L3 event or L4 thought the admin UI wants to
    inspect before committing a delete. Used to show the user how many
    (if any) derivative thoughts would be affected and let them pick
    between cascade / orphan.
    """

    node_id: int = Field(..., ge=1)


# Map the JSON keys used on the wire to the memory BlockLabel values.
# ``onboarding`` has no relationship_block (§3 locked shape) so it is
# absent here; ``persona_update`` has the full set.
_ONBOARDING_LABELS: tuple[tuple[str, BlockLabel], ...] = (
    ("persona_block", BlockLabel.PERSONA),
    ("self_block", BlockLabel.SELF),
    ("user_block", BlockLabel.USER),
    ("mood_block", BlockLabel.MOOD),
)

_UPDATE_LABELS: tuple[tuple[str, BlockLabel], ...] = (
    ("persona_block", BlockLabel.PERSONA),
    ("self_block", BlockLabel.SELF),
    ("user_block", BlockLabel.USER),
    ("mood_block", BlockLabel.MOOD),
    ("relationship_block", BlockLabel.RELATIONSHIP),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_id_for_label(label: BlockLabel, user_id: str) -> str | None:
    """Return the per-row ``user_id`` for a given block label.

    Shared blocks (persona/self/mood) use NULL; per-user blocks
    (user/relationship) carry the actual user_id. Mirrors the business
    rule in :mod:`echovessel.memory.models.CoreBlock`.
    """

    if label in (BlockLabel.PERSONA, BlockLabel.SELF, BlockLabel.MOOD):
        return None
    return user_id


def _load_core_blocks_dict(
    db: DbSession, *, persona_id: str, user_id: str
) -> dict[str, str]:
    """Return every label → content mapping, defaulting missing labels to ''."""

    stmt = select(CoreBlock).where(
        CoreBlock.persona_id == persona_id,
        CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
        (CoreBlock.user_id.is_(None)) | (CoreBlock.user_id == user_id),  # type: ignore[union-attr]
    )
    rows = list(db.exec(stmt))

    out: dict[str, str] = {
        BlockLabel.PERSONA.value: "",
        BlockLabel.SELF.value: "",
        BlockLabel.USER.value: "",
        BlockLabel.MOOD.value: "",
        BlockLabel.RELATIONSHIP.value: "",
    }
    for row in rows:
        label_value = getattr(row.label, "value", row.label)
        if label_value in out:
            out[label_value] = row.content or ""
    return out


def _count_rows(db: DbSession, model: type) -> int:
    return int(db.exec(select(func.count()).select_from(model)).one() or 0)


def _serialize_concept_node(node: ConceptNode) -> dict[str, Any]:
    """Convert a ConceptNode SQLModel row into the JSON shape the
    admin Events / Thoughts tabs render.

    Field naming mirrors the DB columns 1:1 — the frontend's
    ``MemoryEvent`` / ``MemoryThought`` types in
    ``api/types.ts`` consume this exact shape.
    """

    type_value = getattr(node.type, "value", node.type)
    return {
        "id": node.id,
        "node_type": type_value,
        "description": node.description,
        "emotional_impact": int(node.emotional_impact),
        "emotion_tags": list(node.emotion_tags or []),
        "relational_tags": list(node.relational_tags or []),
        "source_session_id": node.source_session_id,
        "source_turn_id": node.source_turn_id,
        "imported_from": node.imported_from,
        "source_deleted": bool(node.source_deleted),
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "access_count": int(node.access_count),
    }


def _count_core_blocks_for_persona(db: DbSession, persona_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(CoreBlock)
        .where(
            CoreBlock.persona_id == persona_id,
            CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    )
    return int(db.exec(stmt).one() or 0)


def _write_blocks(
    db: DbSession,
    *,
    persona_id: str,
    user_id: str,
    pairs: list[tuple[BlockLabel, str]],
    source: str,
) -> None:
    """Write a batch of label/content pairs via ``append_to_core_block``.

    Empty content strings are skipped — the import API rejects empty
    writes and the spec allows callers to send empty blocks in
    onboarding / partial update payloads. The provenance payload is
    the minimum the import audit log expects.
    """

    for label, content in pairs:
        if not content or not content.strip():
            continue
        append_to_core_block(
            db,
            persona_id=persona_id,
            user_id=_user_id_for_label(label, user_id),
            label=label.value,
            content=content,
            provenance={"source": source},
        )


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

    def _persona_id() -> str:
        return runtime.ctx.persona.id

    def _open_db() -> DbSession:
        return DbSession(runtime.ctx.engine)

    # ---- GET /api/state -------------------------------------------------

    @router.get("/api/state")
    async def get_state() -> dict[str, Any]:
        persona_id = _persona_id()
        with _open_db() as db:
            core_block_count = _count_core_blocks_for_persona(db, persona_id)
            message_count = _count_rows(db, RecallMessage)
            event_count = int(
                db.exec(
                    select(func.count())
                    .select_from(ConceptNode)
                    .where(ConceptNode.type == NodeType.EVENT)
                ).one()
                or 0
            )
            thought_count = int(
                db.exec(
                    select(func.count())
                    .select_from(ConceptNode)
                    .where(ConceptNode.type == NodeType.THOUGHT)
                ).one()
                or 0
            )

        return {
            "persona": {
                "id": persona_id,
                "display_name": runtime.ctx.persona.display_name,
                "voice_enabled": bool(runtime.ctx.persona.voice_enabled),
                "has_voice_id": runtime.ctx.persona.voice_id is not None,
            },
            "onboarding_required": core_block_count == 0,
            "memory_counts": {
                "core_blocks": core_block_count,
                "messages": message_count,
                "events": event_count,
                "thoughts": thought_count,
            },
            "channels": _collect_channel_status(runtime),
        }

    # ---- GET /api/admin/persona ----------------------------------------

    @router.get("/api/admin/persona")
    async def get_persona() -> dict[str, Any]:
        persona_id = _persona_id()
        with _open_db() as db:
            blocks = _load_core_blocks_dict(
                db, persona_id=persona_id, user_id=user_id
            )
        return {
            "id": persona_id,
            "display_name": runtime.ctx.persona.display_name,
            "voice_enabled": bool(runtime.ctx.persona.voice_enabled),
            "voice_id": runtime.ctx.persona.voice_id,
            "core_blocks": blocks,
        }

    # ---- POST /api/admin/persona/onboarding ----------------------------

    @router.post("/api/admin/persona/onboarding")
    async def post_onboarding(req: OnboardingRequest) -> dict[str, Any]:
        persona_id = _persona_id()

        with _open_db() as db:
            existing_count = _count_core_blocks_for_persona(db, persona_id)
            if existing_count > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "onboarding already completed; use POST "
                        "/api/admin/persona to update individual blocks"
                    ),
                )

            pairs = [
                (label, getattr(req, field))
                for field, label in _ONBOARDING_LABELS
            ]
            _write_blocks(
                db,
                persona_id=persona_id,
                user_id=user_id,
                pairs=pairs,
                source="admin_onboarding",
            )

            # Update Persona row's display_name so downstream DB readers
            # match the new name, then commit inside the same session.
            persona_row = db.get(Persona, persona_id)
            if persona_row is not None:
                persona_row.display_name = req.display_name
                db.add(persona_row)
                db.commit()

        # Mutate runtime in-memory copy and persist to config.toml so the
        # daemon survives a restart with the new display name.
        runtime.ctx.persona.display_name = req.display_name
        _try_persist_display_name(runtime, req.display_name)

        return {"ok": True, "persona_id": persona_id}

    # ---- POST /api/admin/persona ---------------------------------------

    @router.post("/api/admin/persona")
    async def post_persona(req: PersonaUpdateRequest) -> dict[str, Any]:
        persona_id = _persona_id()

        with _open_db() as db:
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

        persona_id = _persona_id()

        # Guard against re-onboarding — same rule as POST
        # /api/admin/persona/onboarding so the two routes can't race
        # past each other.
        with _open_db() as db:
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
            done_payload = await asyncio.wait_for(
                _wait_done(), timeout=PIPELINE_WAIT_SECONDS
            )
        except TimeoutError as e:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"import pipeline did not finish within "
                    f"{PIPELINE_WAIT_SECONDS:.0f}s"
                ),
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
        with _open_db() as db:
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
        thoughts_input: list[str] = [
            row.description or "" for row in thoughts_rows
        ]

        # Step 4 · build the LLM prompt + parse the response.
        system_prompt = PERSONA_BOOTSTRAP_SYSTEM_PROMPT
        user_prompt = format_persona_bootstrap_user_prompt(
            persona_display_name=req.persona_display_name,
            events=events_input,
            thoughts=thoughts_input,
        )

        try:
            llm_response = await runtime.ctx.llm.complete(
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
            blocks: BootstrappedBlocks = parse_persona_bootstrap_response(
                llm_response
            )
        except PersonaBootstrapParseError as e:
            log.warning("bootstrap LLM returned malformed JSON: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "LLM returned a malformed bootstrap response; please "
                    f"retry. ({e})"
                ),
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
        with _open_db() as db:
            return runtime.cost_summarize(db, range)

    # ---- GET /api/admin/cost/recent ------------------------------------

    @router.get("/api/admin/cost/recent")
    async def get_cost_recent(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        with _open_db() as db:
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
        return _list_concept_nodes_payload(NodeType.EVENT, limit, offset)

    # ---- GET /api/admin/memory/thoughts --------------------------------
    #
    # Mirror of the events route for L4 thoughts. Same shape because
    # the underlying ConceptNode columns are identical — UI distinguishes
    # them by which endpoint it called (via the `node_type` field on
    # the response items, mirrored from the DB column).

    @router.get("/api/admin/memory/thoughts")
    async def list_thoughts(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return _list_concept_nodes_payload(NodeType.THOUGHT, limit, offset)

    def _list_concept_nodes_payload(
        node_type: NodeType, limit: int, offset: int
    ) -> dict[str, Any]:
        with _open_db() as db:
            rows, total = list_concept_nodes(
                db,
                persona_id=_persona_id(),
                user_id=user_id,
                node_type=node_type,
                limit=limit,
                offset=offset,
            )
        items = [_serialize_concept_node(n) for n in rows]
        return {
            "node_type": node_type.value,
            "limit": limit,
            "offset": offset,
            "total": total,
            "items": items,
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

        with _open_db() as db:
            hits, total = search_concept_nodes(
                db,
                persona_id=_persona_id(),
                user_id=user_id,
                query_text=q,
                node_types=node_types,
                tag=tag,
                limit=limit,
                offset=offset,
            )

        items = [_serialize_concept_node(h.node) for h in hits]
        snippets = [
            {"node_id": h.node.id, "snippet": h.snippet}
            for h in hits
            if h.node.id is not None
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

        with _open_db() as db:
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
            "dependent_thought_descriptions": list(
                preview.dependent_thought_descriptions
            ),
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
        with _open_db() as db:
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
        with _open_db() as db:
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

        with _open_db() as db:
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

        with _open_db() as db:
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

    @router.delete(
        "/api/admin/memory/core-blocks/{label}/appends/{append_id}"
    )
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

        with _open_db() as db:
            append = db.get(CoreBlockAppend, append_id)
            if append is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"core block append not found: {append_id}",
                )
            if append.label != label:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"append {append_id} belongs to label "
                        f"{append.label!r}, not {label!r}"
                    ),
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
            "created_at": (
                node.created_at.isoformat() if node.created_at else None
            ),
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
        with _open_db() as db:
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

        source_sessions = sorted(
            {n.source_session_id for n in events if n.source_session_id}
        )
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
        with _open_db() as db:
            event = db.get(ConceptNode, node_id)
            if (
                event is None
                or event.deleted_at is not None
                or event.type != NodeType.EVENT
            ):
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

    # ---- GET /api/admin/config -----------------------------------------
    #
    # Worker η · Config tab. Returns the "safe subset" of the daemon's
    # live config — never the API key material, only whether it's
    # present in the environment. Sections the UI displays but cannot
    # edit (system info) are folded into the same response so the
    # frontend only makes one round trip.

    @router.get("/api/admin/config")
    async def get_config() -> dict[str, Any]:
        cfg = runtime.ctx.config
        llm = cfg.llm

        # System-info card · data_dir + db_path + size + uptime + version.
        data_dir = Path(cfg.runtime.data_dir).expanduser()
        db_path = data_dir / cfg.memory.db_path
        try:
            db_size_bytes = int(db_path.stat().st_size)
        except (FileNotFoundError, OSError):
            db_size_bytes = 0
        try:
            version = pkg_version("echovessel")
        except PackageNotFoundError:
            version = "unknown"
        uptime_seconds = 0
        if runtime._started_at is not None:
            uptime_seconds = int(
                (datetime.now() - runtime._started_at).total_seconds()
            )

        return {
            "llm": {
                "provider": llm.provider,
                "model": llm.model,
                "api_key_env": llm.api_key_env,
                "timeout_seconds": int(llm.timeout_seconds),
                "temperature": float(llm.temperature),
                "max_tokens": int(llm.max_tokens),
                # `api_key_present` is a boolean presence check against
                # os.environ so the UI can render "🟢 key loaded" /
                # "🔴 missing" without ever seeing the actual key.
                "api_key_present": bool(os.environ.get(llm.api_key_env)),
            },
            "persona": {
                "display_name": runtime.ctx.persona.display_name,
                "voice_enabled": bool(runtime.ctx.persona.voice_enabled),
                "voice_id": runtime.ctx.persona.voice_id,
            },
            "memory": {
                "retrieve_k": int(cfg.memory.retrieve_k),
                "relational_bonus_weight": float(
                    cfg.memory.relational_bonus_weight
                ),
                "recent_window_size": int(cfg.memory.recent_window_size),
            },
            "consolidate": {
                "trivial_message_count": int(
                    cfg.consolidate.trivial_message_count
                ),
                "trivial_token_count": int(
                    cfg.consolidate.trivial_token_count
                ),
                "reflection_hard_gate_24h": int(
                    cfg.consolidate.reflection_hard_gate_24h
                ),
            },
            "system": {
                "data_dir": str(cfg.runtime.data_dir),
                "db_path": cfg.memory.db_path,
                "version": version,
                "uptime_seconds": uptime_seconds,
                "db_size_bytes": db_size_bytes,
                "config_path": (
                    str(runtime.ctx.config_path)
                    if runtime.ctx.config_path is not None
                    else None
                ),
            },
        }

    # ---- PATCH /api/admin/config ---------------------------------------
    #
    # Validates the patch body against HOT_RELOADABLE_CONFIG_PATHS,
    # rejects RESTART_REQUIRED_CONFIG_PATHS with 400, then delegates the
    # atomic write + reload to ``Runtime.apply_config_patches``. All
    # pydantic validation errors (invalid provider, out-of-range slider,
    # etc.) translate to 422.

    @router.patch("/api/admin/config")
    async def patch_config(
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        # Guard: the daemon must have a config file we can rewrite.
        if runtime.ctx.config_path is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot patch config: daemon started without a "
                    "config file (config_override mode)"
                ),
            )

        # Normalise + validate body shape — must be {section: {field: value}}.
        if not isinstance(body, dict) or not body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "request body must be a non-empty object like "
                    '{"section": {"field": value}}'
                ),
            )
        for section, fields in body.items():
            if not isinstance(fields, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"section {section!r} must be an object, got "
                        f"{type(fields).__name__}"
                    ),
                )

        # Classify every path as hot / restart-required / unknown.
        restart_required: list[str] = []
        unknown: list[str] = []
        for section, fields in body.items():
            for field in fields:
                path = f"{section}.{field}"
                if path in RESTART_REQUIRED_CONFIG_PATHS:
                    restart_required.append(path)
                elif path not in HOT_RELOADABLE_CONFIG_PATHS:
                    unknown.append(path)

        if restart_required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "these fields require a daemon restart and cannot "
                    "be patched at runtime: "
                    + ", ".join(sorted(restart_required))
                ),
            )
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "unknown or read-only config fields: "
                    + ", ".join(sorted(unknown))
                ),
            )

        # Delegate the atomic write + validate + reload path to the
        # runtime. ValueError → 422 (pydantic validation failed);
        # RuntimeError → 400 (config_override); OSError → 500.
        try:
            applied = await runtime.apply_config_patches(body)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(e),
            ) from e
        except RuntimeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"failed to write config.toml: {e}",
            ) from e

        return {
            "updated_fields": applied,
            "reload_triggered": True,
            "restart_required": [],
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
        file: UploadFile = File(...),  # noqa: B008 - FastAPI marker
    ) -> dict[str, Any]:
        data = await file.read()
        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="uploaded sample is empty",
            )
        if len(data) > _VOICE_SAMPLE_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"sample exceeds {_VOICE_SAMPLE_MAX_BYTES // 1_000_000} MB"
                ),
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
            entry = await voice.clone_voice_interactive(
                blob, name=req.display_name
            )
        except VoicePermanentError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
            ) from e

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
                async for chunk in voice.speak(
                    req.text, voice_id=req.voice_id, format="mp3"
                ):
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

    return router


# ---------------------------------------------------------------------------
# Channel status (W-δ)
# ---------------------------------------------------------------------------

# Canonical display order for the admin UI's channel status strip. Each
# entry is ``(channel_id, human_readable_name)``. The strip renders
# every row in this list even when the channel is not registered —
# registered-but-not-started rows surface as "未启用" rather than
# vanishing, so the user can see at a glance which channels they have
# the option to enable.
#
# Adding a new channel: append ``(channel_id, name)`` here AND
# ensure the concrete Channel implementation exposes ``is_ready()``.
# ``channel.py`` docs describe the ``is_ready`` contract.
_KNOWN_CHANNELS: tuple[tuple[str, str], ...] = (
    ("web", "Web"),
    ("discord", "Discord"),
    ("imessage", "iMessage"),
)


def _collect_channel_status(runtime: Any) -> list[dict[str, Any]]:
    """Return ``[{channel_id, name, enabled, ready}]`` for the admin UI.

    - ``enabled``: runtime actually registered the channel (config
      turned it on AND the init succeeded).
    - ``ready``: ``is_ready()`` returned True at the moment of this
      call. For channels without the method, assume ready when enabled.

    The list is always the full canonical order (``_KNOWN_CHANNELS``)
    so the frontend status strip has a stable shape — disabled rows are
    emitted as ``enabled=False, ready=False``.
    """

    registry = getattr(runtime.ctx, "registry", None)
    out: list[dict[str, Any]] = []
    for channel_id, name in _KNOWN_CHANNELS:
        ch = registry.get(channel_id) if registry is not None else None
        if ch is None:
            out.append(
                {
                    "channel_id": channel_id,
                    "name": name,
                    "enabled": False,
                    "ready": False,
                }
            )
            continue

        is_ready_fn = getattr(ch, "is_ready", None)
        if callable(is_ready_fn):
            try:
                ready = bool(is_ready_fn())
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "is_ready raised for channel %s: %s: %s",
                    channel_id,
                    type(exc).__name__,
                    exc,
                )
                ready = False
        else:
            # Backward-compatible default for channels that predate the
            # capability — they register, start, and that is the whole
            # readiness signal available to us.
            ready = True

        out.append(
            {
                "channel_id": channel_id,
                "name": getattr(ch, "name", name),
                "enabled": True,
                "ready": ready,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Voice clone sample store (W-λ)
# ---------------------------------------------------------------------------
#
# Draft voice-clone samples live on disk under
# ``<data_dir>/voice_samples/<sample_id>/`` so they survive a daemon
# restart mid-wizard. Each sample directory has:
#
#   audio.bin   - raw uploaded bytes (format-agnostic; we never re-encode)
#   meta.json   - {filename, content_type, size_bytes, duration_seconds, created_at}
#
# The store is intentionally minimal — no database table, no in-memory
# cache, no cross-sample dedup. The wizard is short-lived (user uploads
# a handful of clips over a few minutes) and samples are deleted either
# explicitly via DELETE or implicitly when the user leaves them around
# (a future cron can sweep anything older than 7 days; for MVP we leave
# housekeeping to the user).

_VOICE_SAMPLE_MIN_COUNT = 3
_VOICE_SAMPLE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per sample
_VOICE_PREVIEW_TEXT = "你好，我是你刚刚克隆出的声音。"


@dataclass(frozen=True)
class _VoiceSampleEntry:
    sample_id: str
    filename: str
    content_type: str
    size_bytes: int
    duration_seconds: float | None
    created_at: str


def _voice_samples_dir(data_dir: Path) -> Path:
    return data_dir.expanduser() / "voice_samples"


class _VoiceSampleStore:
    """Filesystem-backed draft-sample store for the voice-clone wizard."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _sample_dir(self, sample_id: str) -> Path:
        return self._root / sample_id

    def save(
        self, data: bytes, *, filename: str, content_type: str
    ) -> _VoiceSampleEntry:
        import uuid as _uuid

        sample_id = f"s-{_uuid.uuid4().hex[:12]}"
        sdir = self._sample_dir(sample_id)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "audio.bin").write_bytes(data)

        entry = _VoiceSampleEntry(
            sample_id=sample_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            # Duration probing requires an audio library (mutagen /
            # ffprobe); MVP leaves it as None and lets the UI render
            # "—". The "建议 10-30s" guidance is advisory anyway.
            duration_seconds=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        _json_dump(sdir / "meta.json", _entry_to_dict(entry))
        return entry

    def list(self) -> list[_VoiceSampleEntry]:
        if not self._root.exists():
            return []
        entries: list[_VoiceSampleEntry] = []
        for sdir in sorted(self._root.iterdir()):
            if not sdir.is_dir():
                continue
            meta_path = sdir / "meta.json"
            audio_path = sdir / "audio.bin"
            if not (meta_path.exists() and audio_path.exists()):
                continue
            try:
                meta = _json_load(meta_path)
            except (OSError, ValueError):
                # Corrupt meta · skip rather than blow up the list.
                continue
            entries.append(_entry_from_dict(meta))
        return entries

    def read_bytes(self, sample_id: str) -> bytes:
        return (self._sample_dir(sample_id) / "audio.bin").read_bytes()

    def delete(self, sample_id: str) -> bool:
        sdir = self._sample_dir(sample_id)
        if not sdir.is_dir():
            return False
        import shutil as _shutil

        _shutil.rmtree(sdir)
        return True


def _entry_to_dict(entry: _VoiceSampleEntry) -> dict[str, Any]:
    return {
        "sample_id": entry.sample_id,
        "filename": entry.filename,
        "content_type": entry.content_type,
        "size_bytes": entry.size_bytes,
        "duration_seconds": entry.duration_seconds,
        "created_at": entry.created_at,
    }


def _entry_from_dict(d: dict[str, Any]) -> _VoiceSampleEntry:
    return _VoiceSampleEntry(
        sample_id=str(d["sample_id"]),
        filename=str(d.get("filename") or "sample"),
        content_type=str(d.get("content_type") or "application/octet-stream"),
        size_bytes=int(d.get("size_bytes") or 0),
        duration_seconds=(
            float(d["duration_seconds"])
            if d.get("duration_seconds") is not None
            else None
        ),
        created_at=str(d.get("created_at") or ""),
    )


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    import json as _json

    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_load(path: Path) -> dict[str, Any]:
    import json as _json

    return _json.loads(path.read_text(encoding="utf-8"))


def _try_persist_display_name(runtime: Any, new_name: str) -> None:
    """Best-effort atomic write of ``[persona].display_name`` to config.toml.

    Matches the same rollback-safe pattern as
    :meth:`Runtime.update_persona_voice_enabled`: on failure, log a
    warning and leave the in-memory mutation in place. Admin write
    routes never refuse on a disk error — the user's edit is
    preserved for the current process lifetime.
    """

    if runtime.ctx.config_path is None:
        # `config_override` mode — nothing to persist. In-memory
        # mutation already happened in the caller; a future restart
        # with the same override will obviously revert it, but that's
        # fine because config_override is a tests-only path.
        return
    try:
        runtime._atomic_write_config_field(
            section="persona", field="display_name", value=new_name
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "failed to persist persona.display_name=%r to config.toml: %s",
            new_name,
            e,
        )


__all__ = ["build_admin_router"]

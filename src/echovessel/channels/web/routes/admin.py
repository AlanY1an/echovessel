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

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.core.types import BlockLabel, NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
    append_to_core_block,
    list_concept_nodes,
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
    CoreBlockAppend,
    RecallMessage,
)
from echovessel.memory.models import Session as RecallSession

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


def build_admin_router(*, runtime: Any) -> APIRouter:
    """Assemble the admin router bound to a live Runtime.

    The router is flat (no sub-router nesting) so each path is fully
    explicit in the decorator — matching §3 of the tracker verbatim
    is easier to verify this way than via nested prefix math.
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

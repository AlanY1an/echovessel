"""Admin memory routes — events, thoughts, timeline, search, deletes, traces, entities."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.channels.web.routes.admin.helpers import (
    _open_db,
    _persona_id,
    _serialize_concept_node,
)
from echovessel.channels.web.routes.admin.models import (
    EntityCreateRequest,
    EntityDescriptionPatchRequest,
    EntityMergeRequest,
    EntitySeparateRequest,
    PreviewDeleteRequest,
)
from echovessel.core.types import BlockLabel, NodeType, SessionStatus
from echovessel.memory import (
    Persona,
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


def register_memory_routes(
    router: APIRouter,
    *,
    runtime: Any,
    user_id: str,
) -> None:
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

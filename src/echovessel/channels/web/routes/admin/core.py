"""Admin core routes — daemon state, reset, and user timezone.

Three orphan endpoints that don't fit a domain:

- ``GET  /api/state``                  — daemon state + onboarding gate
- ``POST /api/admin/reset``            — wipe everything for the persona
- ``POST /api/admin/users/timezone``   — record the local user's IANA tz
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlmodel import func, select

from echovessel.channels.web.routes.admin.helpers import (
    _PERSONA_FACT_FIELDS,
    _avatar_file,
    _collect_channel_status,
    _count_core_blocks_for_persona,
    _count_rows,
    _drop_existing_avatars,
    _open_db,
    _persona_id,
    _voice_samples_dir,
)
from echovessel.channels.web.routes.admin.models import UserTimezoneRequest
from echovessel.core.types import NodeType
from echovessel.memory import (
    CoreBlock,
    Persona,
)
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
    CoreBlockAppend,
    RecallMessage,
    User,
)
from echovessel.memory.models import Session as RecallSession


def register_core_routes(
    router: APIRouter,
    *,
    runtime: Any,
    user_id: str,
) -> None:
    @router.get("/api/state")
    async def get_state() -> dict[str, Any]:
        persona_id = _persona_id(runtime)
        with _open_db(runtime) as db:
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
                "has_avatar": _avatar_file(runtime) is not None,
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

    # ---- POST /api/admin/reset -----------------------------------------
    #
    # Nuclear reset: wipe everything the user has accumulated for this
    # persona and return the daemon to a fresh-onboarding state.
    # Specifically, for the current persona_id we delete:
    #   - every core_block row (so `onboarding_required` flips to True)
    #   - every core_block_append row
    #   - every concept_node + concept_node_filling row
    #   - every recall_message + session row
    # Then we clear the Persona row's display_name, voice_id, and 15
    # biographic facts; drop every voice sample file on disk; mirror the
    # voice_id/display_name clears into the live runtime state; and, if
    # the daemon has a writable config.toml, null out `persona.voice_id`
    # there so a subsequent restart doesn't resurrect the old voice.
    #
    # The endpoint is intentionally idempotent — calling it twice in a
    # row on an empty daemon is a no-op that still returns 200.

    @router.post("/api/admin/reset")
    async def post_reset() -> dict[str, Any]:
        from sqlalchemy import delete as sa_delete

        persona_id = _persona_id(runtime)

        with _open_db(runtime) as db:
            # Delete child tables first to avoid FK violations. Order
            # mirrors the creation graph in reverse: appends + fillings
            # before concept_nodes, messages before sessions, everything
            # before the Persona row reset. We use the underlying
            # sqlalchemy Session.execute — sqlmodel.Session.exec is
            # shaped for SELECT, not bulk DELETE.
            db.execute(sa_delete(CoreBlockAppend))
            db.execute(sa_delete(ConceptNodeFilling))
            db.execute(sa_delete(ConceptNode))
            db.execute(sa_delete(RecallMessage))
            db.execute(sa_delete(RecallSession))
            db.execute(sa_delete(CoreBlock))

            persona_row = db.get(Persona, persona_id)
            if persona_row is not None:
                persona_row.display_name = persona_id
                persona_row.voice_id = None
                for field_name in _PERSONA_FACT_FIELDS:
                    setattr(persona_row, field_name, None)
                db.add(persona_row)

            db.commit()

        # Nuke every on-disk voice sample. The store is keyed to a
        # directory under the daemon's data_dir; delete the directory
        # tree and let the next upload recreate it lazily.
        try:
            data_dir = Path(runtime.ctx.config.runtime.data_dir).expanduser()
            samples_dir = _voice_samples_dir(data_dir)
            if samples_dir.exists():
                import shutil

                shutil.rmtree(samples_dir)
        except OSError:
            # Non-fatal — the DB side of the reset already succeeded.
            pass

        # Drop the avatar file too, since "reset everything" includes
        # the profile picture. This is best-effort and doesn't block
        # success if the filesystem call fails.
        _drop_existing_avatars(runtime)

        # Mirror clears into runtime in-memory state so subsequent
        # /api/state and outgoing turns reflect the reset without a
        # daemon restart.
        runtime.ctx.persona.display_name = persona_id
        runtime.ctx.persona.voice_id = None

        # Best-effort clear of voice_id in config.toml. If the daemon
        # was booted in config_override mode there is no file to write
        # — we swallow the error since the in-memory clear above is
        # already authoritative for this process.
        if runtime.ctx.config_path is not None:
            with contextlib.suppress(OSError):
                runtime._atomic_write_config_field(section="persona", field="voice_id", value=None)

        return {"ok": True, "persona_id": persona_id}

    # ---- POST /api/admin/users/timezone --------------------------------
    #
    # Plan decision 5 · browser-supplied IANA timezone for the local
    # owner. Web channel POSTs this on first connect from
    # ``Intl.DateTimeFormat().resolvedOptions().timeZone``.
    # ``override=True`` flips "only write if null" to "always write"
    # (admin UI manual-edit path).

    @router.post("/api/admin/users/timezone")
    async def post_user_timezone(req: UserTimezoneRequest) -> dict[str, Any]:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(req.timezone)
        except ZoneInfoNotFoundError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown IANA timezone {req.timezone!r}",
            ) from e

        with _open_db(runtime) as db:
            user_row = db.get(User, user_id)
            if user_row is None:
                user_row = User(id=user_id, display_name=user_id)
                db.add(user_row)
                db.commit()
                db.refresh(user_row)

            if user_row.timezone is None or req.override:
                user_row.timezone = req.timezone
                db.add(user_row)
                db.commit()
                return {"ok": True, "timezone": req.timezone, "written": True}

            return {"ok": True, "timezone": user_row.timezone, "written": False}

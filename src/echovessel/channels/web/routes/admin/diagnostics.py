"""Admin diagnostics routes — cost, failed sessions, slow-tick, turns, consolidate trace."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from sqlmodel import select

from echovessel.channels.web.routes.admin.helpers import _open_db, _persona_id
from echovessel.core.types import SessionStatus
from echovessel.memory.models import Session as RecallSession


def register_diagnostics_routes(router: APIRouter, *, runtime: Any) -> None:
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
        with _open_db(runtime) as db:
            stmt = (
                select(RecallSession)
                .where(
                    RecallSession.status == SessionStatus.FAILED,
                    RecallSession.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(RecallSession.started_at.desc())  # type: ignore[attr-defined]
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

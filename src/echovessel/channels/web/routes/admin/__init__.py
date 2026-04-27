"""Admin HTTP routes for the Web channel.

Composition entry-point: :func:`build_admin_router` creates the shared
``APIRouter`` and delegates registration to one ``register_*_routes``
function per domain. Domain modules are siblings of this file:

- ``core``        — daemon state, reset, user timezone
- ``config``      — config GET/PATCH, channels PATCH
- ``voice``       — voice samples + clone wizard + activate
- ``diagnostics`` — cost, failed sessions, slow-tick, turns, traces
- ``persona``     — persona CRUD, avatar, style, voice-toggle, facts,
                    extract, bootstrap
- ``memory``      — events, thoughts, timeline, search, deletes,
                    traces, entities

Shared helpers + Pydantic request models live in ``helpers`` and
``models``. ``runtime`` is typed as ``Any`` throughout to avoid
``channels → runtime`` reversing the layered-architecture contract
enforced by ``lint-imports``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from echovessel.channels.web.routes.admin.config import register_config_routes
from echovessel.channels.web.routes.admin.core import register_core_routes
from echovessel.channels.web.routes.admin.diagnostics import register_diagnostics_routes
from echovessel.channels.web.routes.admin.memory import register_memory_routes
from echovessel.channels.web.routes.admin.persona import register_persona_routes
from echovessel.channels.web.routes.admin.voice import register_voice_routes


def build_admin_router(
    *,
    runtime: Any,
    voice_service: Any | None = None,
    importer_facade: Any | None = None,
) -> APIRouter:
    """Assemble the admin router bound to a live Runtime.

    The router is flat (no sub-router nesting) so each path is fully
    explicit in the decorator — matching the locked admin spec verbatim
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
    register_voice_routes(router, runtime=runtime, voice_service=voice_service)
    register_diagnostics_routes(router, runtime=runtime)
    register_persona_routes(
        router,
        runtime=runtime,
        importer_facade=importer_facade,
        user_id=user_id,
    )
    register_memory_routes(router, runtime=runtime, user_id=user_id)

    return router


__all__ = ["build_admin_router"]

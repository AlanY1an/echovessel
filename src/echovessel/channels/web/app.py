"""FastAPI app factory for the Web channel (Stage 2).

Builds a FastAPI application pre-wired with:

- ``/api/chat/*`` routes from :mod:`echovessel.channels.web.routes.chat`
- A lifespan that starts the :class:`SSEBroadcaster` heartbeat task on
  startup and cancels it on shutdown
- The live :class:`WebChannel` + :class:`SSEBroadcaster` bound into
  the routes via closure — no global state

Stage 3 will add an ``admin`` router and call
``app.include_router(build_admin_router(...))`` on the same app
instance. The factory signature is designed so Stage 3 can add
optional kwargs without breaking Stage 2 callers.

The factory does NOT start the uvicorn server. That is the
runtime's responsibility — see ``echovessel.runtime.app`` Stage 2
wiring. Keeping the two concerns separate lets tests build the app
and hit it with ``httpx.AsyncClient`` without any socket binding.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from echovessel.channels.web.channel import WebChannel
from echovessel.channels.web.routes.admin import build_admin_router
from echovessel.channels.web.routes.admin_import import build_admin_import_router
from echovessel.channels.web.routes.chat import build_chat_router
from echovessel.channels.web.sse import SSEBroadcaster

log = logging.getLogger(__name__)


def build_web_app(
    *,
    channel: WebChannel,
    broadcaster: SSEBroadcaster,
    runtime: Any = None,
    voice_service: Any | None = None,
    importer_facade: Any | None = None,
    heartbeat_seconds: float = 30.0,
) -> FastAPI:
    """Assemble the FastAPI application for the Web channel.

    Parameters
    ----------
    channel
        Live :class:`WebChannel` instance. The factory assumes the
        caller has already attached the broadcaster via
        ``channel.attach_broadcaster(broadcaster)``.
    broadcaster
        Live :class:`SSEBroadcaster`. The factory starts the
        heartbeat task on this broadcaster in the app's lifespan.
    voice_service
        Optional :class:`VoiceService` instance. Stage 7 threads this
        to the chat router so the ``GET /api/chat/voice/{id}.mp3``
        endpoint can locate the voice cache directory. When ``None``
        (voice disabled), the endpoint returns 404.
    importer_facade
        Optional :class:`ImporterFacade` instance. When supplied
        alongside ``runtime``, the admin-import router (upload /
        estimate / start / cancel / events) is mounted. When either
        is missing, the endpoints are not registered — so tests that
        exercise the chat surface alone still work.
    heartbeat_seconds
        Interval for the heartbeat broadcast. Tests can use a small
        value to exercise the heartbeat path without waiting 30s.
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        import asyncio
        import contextlib

        hb_task = asyncio.create_task(
            broadcaster.heartbeat_task(interval_seconds=heartbeat_seconds),
            name="web_sse_heartbeat",
        )
        try:
            yield
        finally:
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hb_task

    app = FastAPI(
        title="EchoVessel Web",
        version="v1",
        lifespan=_lifespan,
    )
    app.include_router(
        build_chat_router(
            channel=channel,
            broadcaster=broadcaster,
            voice_service=voice_service,
            runtime=runtime,
        )
    )
    # Stage 3 · admin routes are mounted only when the caller passes a
    # Runtime. Tests that exercise chat routes in isolation can pass
    # ``runtime=None`` and skip the admin surface entirely.
    if runtime is not None:
        app.include_router(
            build_admin_router(
                runtime=runtime,
                voice_service=voice_service,
                importer_facade=importer_facade,
            )
        )

    # Worker E (v0.0.2) · admin-import routes are mounted only when
    # both the runtime and a live ImporterFacade are available. The
    # facade construction in runtime/app.py is best-effort; if it
    # failed at boot (no LLM, etc.) we simply skip the router rather
    # than 503-ing every handler.
    if runtime is not None and importer_facade is not None:
        app.include_router(
            build_admin_import_router(
                runtime=runtime, importer_facade=importer_facade
            )
        )

    # Serve the built React frontend as static files. The vite build
    # outputs into channels/web/static/ (see vite.config.ts build.outDir).
    # StaticFiles is mounted AFTER the API routers so /api/* routes take
    # priority.
    #
    # Starlette's ``html=True`` only serves ``index.html`` when the path
    # points to a directory — it does NOT fall back to ``index.html`` on
    # 404, which means direct navigation to a client-side route like
    # ``/admin`` returns a hard 404. Since React Router owns ``/admin``,
    # ``/chat``, ``/onboarding`` and friends, we subclass StaticFiles to
    # rewrite every missing-file 404 into a fresh ``index.html`` response
    # so the SPA picks up the route on the client.
    import os
    from pathlib import Path

    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.staticfiles import StaticFiles

    class _SPAStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code == 404:
                    return await super().get_response("index.html", scope)
                raise

    # Resolve the static directory relative to this file's location.
    # In a wheel install: site-packages/echovessel/channels/web/static/
    # In a dev checkout: src/echovessel/channels/web/static/
    _this_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    _static_dir = _this_dir / "static"

    if _static_dir.is_dir() and any(_static_dir.iterdir()):
        app.mount("/", _SPAStaticFiles(directory=str(_static_dir), html=True), name="static")
        log.info("static frontend: mounted from %s", _static_dir)
    else:
        log.warning(
            "static frontend: directory %s is empty or missing. "
            "Run `cd src/echovessel/channels/web/frontend && npm run build` "
            "to build the frontend, or use `npm run dev` for development mode.",
            _static_dir,
        )

    return app


__all__ = ["build_web_app"]

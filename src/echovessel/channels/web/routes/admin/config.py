"""Admin config routes — read + patch the daemon config + channels block."""

from __future__ import annotations

import os
import tomllib
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status

from echovessel.channels.web.routes.admin.helpers import (
    _CHANNEL_PATCH_FIELDS,
    _collect_channels_config,
)
from echovessel.core.config_paths import (
    HOT_RELOADABLE_CONFIG_PATHS,
    RESTART_REQUIRED_CONFIG_PATHS,
)


def register_config_routes(router: APIRouter, *, runtime: Any) -> None:
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
            uptime_seconds = int((datetime.now() - runtime._started_at).total_seconds())

        # Channels section · include scrubbed config for every known
        # channel plus their live ready/enabled state. Secrets are
        # never returned — only the env-var name and a presence bool.
        channels_section = _collect_channels_config(cfg, runtime)

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
                "relational_bonus_weight": float(cfg.memory.relational_bonus_weight),
                "recent_window_size": int(cfg.memory.recent_window_size),
            },
            "consolidate": {
                "trivial_message_count": int(cfg.consolidate.trivial_message_count),
                "trivial_token_count": int(cfg.consolidate.trivial_token_count),
                "reflection_hard_gate_24h": int(cfg.consolidate.reflection_hard_gate_24h),
            },
            "system": {
                "data_dir": str(cfg.runtime.data_dir),
                "db_path": cfg.memory.db_path,
                "version": version,
                "uptime_seconds": uptime_seconds,
                "db_size_bytes": db_size_bytes,
                "config_path": (
                    str(runtime.ctx.config_path) if runtime.ctx.config_path is not None else None
                ),
            },
            "channels": channels_section,
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
                    'request body must be a non-empty object like {"section": {"field": value}}'
                ),
            )
        for section, fields in body.items():
            if not isinstance(fields, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(f"section {section!r} must be an object, got {type(fields).__name__}"),
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
                    "be patched at runtime: " + ", ".join(sorted(restart_required))
                ),
            )
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("unknown or read-only config fields: " + ", ".join(sorted(unknown))),
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

    # ---- PATCH /api/admin/channels -------------------------------------
    #
    # Separate from PATCH /api/admin/config because channel fields are
    # never hot-reloadable — flipping ``enabled`` has to spawn or tear
    # down a subprocess (imsg) / gateway connection (Discord) / HTTP
    # server (web). The admin PATCH /api/admin/config route enforces a
    # strict hot-reload-only contract so we mint a dedicated route here
    # that atomically writes the TOML and tells the caller "restart
    # daemon to apply". No secret fields accept input — secrets are
    # environment-driven, not TOML-driven.

    @router.patch("/api/admin/channels")
    async def patch_channels(
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        if runtime.ctx.config_path is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot patch channels: daemon started without a "
                    "config file (config_override mode)"
                ),
            )

        if not isinstance(body, dict) or not body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "request body must be a non-empty object like "
                    '{"imessage": {"enabled": true, ...}}'
                ),
            )

        # Validate channel names + field names before touching disk.
        unknown_channels: list[str] = []
        unknown_fields: list[str] = []
        patches: dict[str, dict[str, Any]] = {}
        for channel_id, fields in body.items():
            if channel_id not in _CHANNEL_PATCH_FIELDS:
                unknown_channels.append(channel_id)
                continue
            if not isinstance(fields, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"channel {channel_id!r} body must be an object, "
                        f"got {type(fields).__name__}"
                    ),
                )
            allowed = _CHANNEL_PATCH_FIELDS[channel_id]
            for fname in fields:
                if fname not in allowed:
                    unknown_fields.append(f"{channel_id}.{fname}")
            patches[f"channels.{channel_id}"] = dict(fields)

        if unknown_channels:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown channels: {sorted(unknown_channels)}",
            )
        if unknown_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "unknown or read-only channel fields: " + ", ".join(sorted(unknown_fields))
                ),
            )

        # Merge the channel sub-blocks into a single {"channels": {...}}
        # patch that the runtime's atomic writer understands. We read
        # the current channels block so untouched sub-keys (e.g. the
        # discord section when only imessage is being patched) survive.
        config_path = Path(runtime.ctx.config_path)
        try:
            with open(config_path, "rb") as f:
                current = tomllib.load(f)
        except OSError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"failed to read config.toml: {e}",
            ) from e

        channels_block = dict(current.get("channels") or {})
        for channel_id, fields in body.items():
            existing = dict(channels_block.get(channel_id) or {})
            existing.update(fields)
            channels_block[channel_id] = existing

        try:
            runtime.write_channel_config_patches({"channels": channels_block})
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"invalid channel config: {e}",
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

        updated = sorted(
            f"channels.{ch}.{fname}" for ch, fields in body.items() for fname in fields
        )
        return {
            "updated_fields": updated,
            "reload_triggered": False,
            "restart_required": True,
        }

"""Resolve a transport-native identity to the internal user_id.

The memory layer stays scoped by ``internal_user_id`` so retrieve /
consolidate / core_blocks remain coherent across channels. Channels
keep using their native identity tokens (Discord snowflakes, phone
handles, web "self"); this module is the single conversion point.

MVP semantics: every external id auto-bootstraps to ``"self"`` — the
local daemon has one human owner. Future multi-user / group-chat work
will rebind rows in ``external_identities`` without touching anything
else.
"""

from __future__ import annotations

from sqlmodel import Session as DbSession

from echovessel.memory.models import ExternalIdentity


def resolve_internal_user_id(
    db: DbSession,
    channel_id: str,
    external_id: str,
    *,
    default_internal: str = "self",
) -> str:
    """Return the ``internal_user_id`` mapped from ``(channel_id, external_id)``.

    First call for a given pair inserts a new row defaulting to
    ``default_internal`` and commits it. Subsequent calls read the
    stored row and return its ``internal_user_id``. The function is
    idempotent for the same pair within a process.
    """
    existing = db.get(ExternalIdentity, (channel_id, external_id))
    if existing is not None:
        return existing.internal_user_id

    db.add(
        ExternalIdentity(
            channel_id=channel_id,
            external_id=external_id,
            internal_user_id=default_internal,
        )
    )
    db.commit()
    return default_internal


__all__ = ["resolve_internal_user_id"]

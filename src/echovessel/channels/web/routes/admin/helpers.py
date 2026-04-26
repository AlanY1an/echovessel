"""Shared helpers + constants for the admin route handlers.

Functions here are private (underscore-prefixed) and consumed only by
the route modules in this package. Splitting them out of __init__.py
keeps the router factory focused on routing and lets per-domain route
files import only the helpers they actually use.

Categories:

- Block-label mappings (`_ONBOARDING_LABELS`, `_UPDATE_LABELS`)
- Persona block / fact helpers (`_user_id_for_label`,
  `_load_core_blocks_dict`, `_PERSONA_FACT_FIELDS`,
  `_apply_facts_to_persona_row`, `_serialize_persona_facts`,
  `_count_core_blocks_for_persona`, `_write_blocks`)
- ConceptNode serializer (`_serialize_concept_node`, `_count_rows`)
- Avatar storage (`_AVATAR_ALLOWED_EXTS`, `_AVATAR_MAX_BYTES`,
  `_avatar_dir`, `_avatar_file`, `_drop_existing_avatars`)
- Channel config + status (`_CHANNEL_PATCH_FIELDS`,
  `_collect_channels_config`, `_KNOWN_CHANNELS`,
  `_collect_channel_status`)
- Voice clone sample store (`_VOICE_SAMPLE_*`, `_VoiceSampleEntry`,
  `_VoiceSampleStore`, `_voice_samples_dir`, `_entry_to_dict`,
  `_entry_from_dict`)
- Misc (`_format_events_thoughts_for_prompt`, `_json_dump`,
  `_json_load`, `_try_persist_display_name`)
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import func, select

from echovessel.channels.web.routes.admin.models import PersonaFactsPayload
from echovessel.core.types import BlockLabel
from echovessel.memory import (
    CoreBlock,
    Persona,
    append_to_core_block,
)
from echovessel.memory.models import (
    ConceptNode,
    ConceptNodeFilling,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime accessors
# ---------------------------------------------------------------------------


def _persona_id(runtime: Any) -> str:
    return runtime.ctx.persona.id


def _open_db(runtime: Any) -> DbSession:
    return DbSession(runtime.ctx.engine)


# ---------------------------------------------------------------------------
# Block-label mappings
# ---------------------------------------------------------------------------


# Map the JSON keys used on the wire to the memory BlockLabel values.
# v0.5 · L1 collapsed to ``persona`` + ``user`` (+ ``style`` via its
# own endpoint). ``self`` and ``relationship`` are gone (plan §1).
_ONBOARDING_LABELS: tuple[tuple[str, BlockLabel], ...] = (
    ("persona_block", BlockLabel.PERSONA),
    ("user_block", BlockLabel.USER),
)

_UPDATE_LABELS: tuple[tuple[str, BlockLabel], ...] = (
    ("persona_block", BlockLabel.PERSONA),
    ("user_block", BlockLabel.USER),
)


# ---------------------------------------------------------------------------
# Persona block / fact helpers
# ---------------------------------------------------------------------------


def _user_id_for_label(label: BlockLabel, user_id: str) -> str | None:
    """Return the per-row ``user_id`` for a given block label.

    v0.5: shared blocks are ``persona`` and ``style`` (both with
    ``user_id=NULL``). The ``user`` block is the only per-user row and
    carries the actual ``user_id``. Mirrors the business rule in
    :mod:`echovessel.memory.models.CoreBlock`.
    """

    if label in (BlockLabel.PERSONA, BlockLabel.STYLE):
        return None
    return user_id


def _load_core_blocks_dict(db: DbSession, *, persona_id: str, user_id: str) -> dict[str, str]:
    """Return every label → content mapping, defaulting missing labels to ''.

    v0.5 · only the three live labels are populated. Soft-deleted
    rows from the v0.4 ``self`` / ``relationship`` labels are ignored
    by the WHERE clause and never reach this dict.
    """

    stmt = select(CoreBlock).where(
        CoreBlock.persona_id == persona_id,
        CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
        (CoreBlock.user_id.is_(None)) | (CoreBlock.user_id == user_id),  # type: ignore[union-attr]
    )
    rows = list(db.exec(stmt))

    out: dict[str, str] = {
        BlockLabel.PERSONA.value: "",
        BlockLabel.USER.value: "",
        BlockLabel.STYLE.value: "",
    }
    for row in rows:
        label_value = getattr(row.label, "value", row.label)
        if label_value in out:
            out[label_value] = row.content or ""
    return out


# The 15 biographic fact columns on Persona. Kept as a tuple so the
# apply-facts helper and the serializer stay in lockstep — if a field is
# added to the model, add it here too.
_PERSONA_FACT_FIELDS: tuple[str, ...] = (
    "full_name",
    "gender",
    "birth_date",
    "ethnicity",
    "nationality",
    "native_language",
    "locale_region",
    "education_level",
    "occupation",
    "occupation_field",
    "location",
    "timezone",
    "relationship_status",
    "life_stage",
    "health_status",
)


def _apply_facts_to_persona_row(
    persona_row: Persona,
    payload: PersonaFactsPayload,
    *,
    fields_touched: set[str] | None = None,
) -> None:
    """Copy the supplied facts onto the ORM row in place.

    ``fields_touched`` — when provided, only those field names are
    applied. Unset fields are left untouched. When ``None`` (the
    onboarding case), every field on the payload is written: a
    ``None`` in the payload means "clear it".

    ``birth_date`` is parsed from the ISO string the payload carries
    onto a :class:`datetime.date` before it hits the model.
    """

    data = payload.model_dump()
    for field_name in _PERSONA_FACT_FIELDS:
        if fields_touched is not None and field_name not in fields_touched:
            continue
        value: Any = data.get(field_name)
        if field_name == "birth_date" and value is not None:
            value = date.fromisoformat(value)
        setattr(persona_row, field_name, value)


def _serialize_persona_facts(persona_row: Persona) -> dict[str, Any]:
    """Render the 15 facts as a plain-JSON dict (ISO date, None preserved)."""

    out: dict[str, Any] = {}
    for field_name in _PERSONA_FACT_FIELDS:
        value = getattr(persona_row, field_name, None)
        if field_name == "birth_date" and value is not None:
            out[field_name] = value.isoformat()
        else:
            out[field_name] = value
    return out


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
# ConceptNode helpers
# ---------------------------------------------------------------------------


def _count_rows(db: DbSession, model: type) -> int:
    return int(db.exec(select(func.count()).select_from(model)).one() or 0)


def _serialize_concept_node(
    node: ConceptNode,
    *,
    db: DbSession | None = None,
) -> dict[str, Any]:
    """Convert a ConceptNode SQLModel row into the JSON shape the
    admin Events / Thoughts tabs render.

    Field naming mirrors the DB columns 1:1 — the frontend's
    ``MemoryEvent`` / ``MemoryThought`` types in
    ``api/types.ts`` consume this exact shape.

    v0.5 hotfix · the optional ``db`` kwarg unlocks two extra fields
    needed by the admin Persona tab (Spec 2):

    - ``subject`` — ``'persona'`` or ``'user'`` (already on the
      ConceptNode column; surfaced unconditionally).
    - ``filling_event_ids`` — IDs of the L3 events this node was
      reflected from, queried from ``concept_node_filling`` WHERE
      ``parent_id == node.id``. Requires ``db``; an empty list is
      returned when ``db`` is None.
    - ``source`` — only on ``type == 'thought'``. Heuristic per spec
      ST A: ``subject='persona' AND source_session_id IS NULL`` →
      ``'slow_tick'``; otherwise ``'reflection'``. ``None`` when the
      node isn't a thought.

    Old callers that don't pass ``db`` still get the original schema
    plus the always-cheap ``subject`` column — additive only.
    """

    type_value = getattr(node.type, "value", node.type)
    subject = getattr(node, "subject", None) or "user"

    filling_event_ids: list[int] = []
    if db is not None and node.id is not None:
        filling_event_ids = [
            int(row.child_id)
            for row in db.exec(
                select(ConceptNodeFilling).where(
                    ConceptNodeFilling.parent_id == node.id,
                    ConceptNodeFilling.orphaned == False,  # noqa: E712
                )
            )
        ]

    source: str | None = None
    if type_value == "thought":
        source = (
            "slow_tick"
            if subject == "persona" and node.source_session_id is None
            else "reflection"
        )

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
        # v0.5 hotfix additions ↓
        "subject": subject,
        "filling_event_ids": filling_event_ids,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Avatar storage
# ---------------------------------------------------------------------------


# Avatar — stored as a single image file under `<data_dir>/persona/`.
# We don't pin the extension in code: the filename always starts with
# `avatar.` and carries whatever extension the user uploaded, so both
# serve and delete operate by glob.
_AVATAR_ALLOWED_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp", "gif")
_AVATAR_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB cap — plenty for any reasonable avatar.


def _avatar_dir(runtime: Any) -> Path:
    """Absolute path to `<data_dir>/persona/` — created on demand."""
    data_dir = Path(runtime.ctx.config.runtime.data_dir).expanduser()
    return data_dir / "persona"


def _avatar_file(runtime: Any) -> Path | None:
    """Return the single `avatar.<ext>` in `<data_dir>/persona/`, or None."""
    d = _avatar_dir(runtime)
    if not d.exists():
        return None
    for ext in _AVATAR_ALLOWED_EXTS:
        candidate = d / f"avatar.{ext}"
        if candidate.exists():
            return candidate
    return None


def _drop_existing_avatars(runtime: Any) -> None:
    """Delete every `avatar.*` file in the persona dir (before a re-upload)."""
    d = _avatar_dir(runtime)
    if not d.exists():
        return
    for ext in _AVATAR_ALLOWED_EXTS:
        p = d / f"avatar.{ext}"
        if p.exists():
            with contextlib.suppress(OSError):
                p.unlink()


# ---------------------------------------------------------------------------
# Channel config + status
# ---------------------------------------------------------------------------


# Channel config schema · allowlists per channel for PATCH /api/admin/channels.
# Every field listed here is valid input; unknown fields → 400. Secrets
# (Discord token, future iMessage creds) are explicitly NOT in this set —
# secrets only live in environment variables, never in the TOML.
_CHANNEL_PATCH_FIELDS: dict[str, frozenset[str]] = {
    "web": frozenset({"enabled", "host", "port", "static_dir", "debounce_ms"}),
    "discord": frozenset({"enabled", "token_env", "allowed_user_ids", "debounce_ms"}),
    "imessage": frozenset(
        {
            "enabled",
            "persona_apple_id",
            "cli_path",
            "db_path",
            "allowed_handles",
            "default_service",
            "region",
            "debounce_ms",
        }
    ),
}


# The canonical list of channel ids the admin status strip cares about.
# Currently MVP-equivalent: web, discord, imessage. wechat lands behind
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


def _collect_channels_config(cfg: Any, runtime: Any) -> dict[str, dict[str, Any]]:
    """Build the scrubbed ``channels`` section for GET /api/admin/config.

    Returns one entry per known channel with its config fields plus two
    live-state fields (``ready``, ``registered``) sourced from the
    runtime's registry. Secrets are returned only as a presence bool
    (e.g. ``token_loaded``); the actual token string never leaves the
    daemon process.
    """
    # Map channel_id → live state dict (ready / registered). We collect
    # this once so we can decorate every channel's config blob with its
    # current runtime status.
    live_status: dict[str, dict[str, bool]] = {}
    for status_row in _collect_channel_status(runtime):
        live_status[status_row["channel_id"]] = {
            "ready": bool(status_row.get("ready", False)),
            "registered": bool(status_row.get("enabled", False)),
        }

    def live(channel_id: str) -> dict[str, bool]:
        return live_status.get(channel_id, {"ready": False, "registered": False})

    web_cfg = cfg.channels.web
    discord_cfg = cfg.channels.discord
    imessage_cfg = cfg.channels.imessage

    return {
        "web": {
            "enabled": bool(web_cfg.enabled),
            "channel_id": web_cfg.channel_id,
            "host": web_cfg.host,
            "port": int(web_cfg.port),
            "static_dir": web_cfg.static_dir,
            "debounce_ms": int(web_cfg.debounce_ms),
            **live(web_cfg.channel_id),
        },
        "discord": {
            "enabled": bool(discord_cfg.enabled),
            "channel_id": discord_cfg.channel_id,
            "token_env": discord_cfg.token_env,
            "token_loaded": bool(os.environ.get(discord_cfg.token_env)),
            "allowed_user_ids": list(discord_cfg.allowed_user_ids or []),
            "debounce_ms": int(discord_cfg.debounce_ms),
            **live(discord_cfg.channel_id),
        },
        "imessage": {
            "enabled": bool(imessage_cfg.enabled),
            "channel_id": imessage_cfg.channel_id,
            "persona_apple_id": imessage_cfg.persona_apple_id,
            "cli_path": imessage_cfg.cli_path,
            "db_path": imessage_cfg.db_path,
            "allowed_handles": list(imessage_cfg.allowed_handles),
            "default_service": imessage_cfg.default_service,
            "region": imessage_cfg.region,
            "debounce_ms": int(imessage_cfg.debounce_ms),
            **live(imessage_cfg.channel_id),
        },
    }


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _format_events_thoughts_for_prompt(
    *,
    events: list[tuple[str, int, list[str]]],
    thoughts: list[str],
) -> str:
    """Render imported events + thoughts as the LLM's context material.

    Used by the persona-facts extraction route to feed the structured
    import output back to the LLM as free-form text; the prompt does
    not care about the wire format — just that both sides read the
    same thing.
    """

    lines: list[str] = []
    lines.append(f"EVENTS ({len(events)} total):")
    if not events:
        lines.append("  (none — the import produced no events)")
    for i, (desc, impact, rel_tags) in enumerate(events, start=1):
        tag_str = f" [{','.join(rel_tags)}]" if rel_tags else ""
        lines.append(f"  {i}. impact={impact:+d}{tag_str} · {desc}")
    lines.append("")
    lines.append(f"THOUGHTS ({len(thoughts)} total):")
    if not thoughts:
        lines.append("  (none — the import produced no long-term thoughts)")
    for i, t in enumerate(thoughts, start=1):
        lines.append(f"  {i}. {t}")
    return "\n".join(lines)


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
        runtime._atomic_write_config_field(section="persona", field="display_name", value=new_name)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "failed to persist persona.display_name=%r to config.toml: %s",
            new_name,
            e,
        )


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

_VOICE_SAMPLE_MIN_COUNT = (
    1  # FishAudio accepts a single sample · more is better quality but not required
)
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

    def save(self, data: bytes, *, filename: str, content_type: str) -> _VoiceSampleEntry:
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
            float(d["duration_seconds"]) if d.get("duration_seconds") is not None else None
        ),
        created_at=str(d.get("created_at") or ""),
    )


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    import json as _json

    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_load(path: Path) -> dict[str, Any]:
    import json as _json

    return _json.loads(path.read_text(encoding="utf-8"))

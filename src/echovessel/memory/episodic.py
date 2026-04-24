"""L6 · Episodic state update path (v0.4).

The persona carries a single-row JSON snapshot of how they feel right
now — mood, energy, last user signal — on ``personas.episodic_state``.
Replaces the Phase 1 ``core_blocks`` MOOD block (physically removed in
the v0.4 migration). Extraction derives the snapshot from each closed
session's conversation arc and writes it through the single entry
point below; the 12h decay to ``neutral`` lives on ``assemble_turn``'s
entry path (see ``runtime.interaction``).

Design notes:

- **Replace in place.** Episodic state is a snapshot, not an append
  log. The update overwrites the JSON column.
- **Single LLM call.** The ``session_mood_signal`` field is emitted by
  the extraction LLM alongside events, so no new model round-trip is
  spent on mood updates (plan §5.3).
- **Lifecycle hook.** The same ``on_mood_updated`` hook that powered
  the old mood block keeps firing. Third argument semantics shift from
  "prose text" to ``repr`` of the new state dict; Protocol signature
  stays ``str`` so existing observers keep working.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session as DbSession

from echovessel.memory.models import Persona
from echovessel.memory.observers import MemoryEventObserver, _fire_lifecycle

log = logging.getLogger(__name__)

# Allowed values for ``last_user_signal``. Mirrors the extraction
# prompt's closed vocabulary; anything outside collapses to ``None``.
_LAST_USER_SIGNAL_VOCAB: frozenset[str] = frozenset(
    {"warm", "cool", "tired", "frustrated"}
)


def update_episodic_state(
    db: DbSession,
    *,
    persona_id: str,
    signal: dict[str, Any],
    now: datetime | None = None,
    observer: MemoryEventObserver | None = None,
) -> dict[str, Any]:
    """Replace ``personas.episodic_state`` with a freshly derived snapshot.

    Args:
        db: Active SQLModel session. Commits before returning.
        persona_id: Which persona row to update.
        signal: The extraction LLM's ``session_mood_signal`` output.
            Expected keys:
                ``mood``             — free-form hyphenated phrase (required)
                ``energy``           — int 0-10 (clamped)
                ``last_user_signal`` — one of ``{'warm','cool','tired',
                                       'frustrated'}`` or ``None``
        now: Timestamp for the ``updated_at`` field. Defaults to UTC now.
        observer: Per-call hook; receives ``on_mood_updated`` after
            commit in addition to the module-level observer registry.

    Returns:
        The new state dict that was written.

    Raises:
        ValueError: ``mood`` is missing or empty.
        LookupError: ``persona_id`` does not resolve to a row.
    """
    mood = signal.get("mood")
    if not isinstance(mood, str) or not mood.strip():
        raise ValueError(
            "update_episodic_state: signal['mood'] must be non-empty"
        )

    persona = db.get(Persona, persona_id)
    if persona is None:
        raise LookupError(f"no persona with id={persona_id!r}")

    now_dt = now or datetime.now(UTC)

    energy_raw = signal.get("energy", 5)
    try:
        energy = int(energy_raw)
    except (TypeError, ValueError):
        energy = 5
    energy = max(0, min(10, energy))

    raw_signal = signal.get("last_user_signal")
    last_user_signal: str | None = (
        raw_signal
        if isinstance(raw_signal, str) and raw_signal in _LAST_USER_SIGNAL_VOCAB
        else None
    )

    new_state: dict[str, Any] = {
        "mood": mood.strip(),
        "energy": energy,
        "last_user_signal": last_user_signal,
        "updated_at": now_dt.isoformat(),
    }
    persona.episodic_state = new_state
    db.add(persona)
    db.commit()

    if observer is not None:
        try:
            observer.on_mood_updated(persona_id, "self", repr(new_state))
        except Exception as e:  # noqa: BLE001
            log.warning("observer.on_mood_updated raised: %s", e)

    _fire_lifecycle("on_mood_updated", persona_id, "self", repr(new_state))

    return new_state


__all__ = ["update_episodic_state"]

"""Mood L1 core-block update path.

MVP has no automatic mood-block writer yet — the persona's mood is
supposed to decay over time as a side effect of reflection (v1.0 work)
or to be directly edited via the runtime admin UI (round 3 in
flight). Either way, runtime needs a **single** entry point that:

1. Finds or creates the mood `core_blocks` row (shared across users,
   so `user_id = NULL`).
2. Replaces its `.content` with the new text (mood is a replacement,
   not an append — that's the key difference from persona/self/user
   blocks which `append_to_core_block` handles).
3. Commits the transaction as one unit.
4. Fires the `on_mood_updated` lifecycle hook via the memory-level
   observer registry so downstream subscribers (e.g. the runtime-side
   observer, or future analytics consumers) learn the mood row
   changed.

Intentionally a separate function (not a special case in
`append_to_core_block`) because the semantics differ:

    append_to_core_block — append + audit log in core_block_appends
    update_mood_block    — replace-in-place + lifecycle hook fire

The `on_core_block_appended` hook from round 3 does **not** fire from
this path (mood updates are not append-audit events). Round 4 adds the
dedicated `on_mood_updated` lifecycle hook instead.

Related spec:
- docs/memory/07-round4-tracker.md §3.2 (mood row in modified files)
- docs/runtime/01-spec-v0.1.md §17a.5 (RuntimeMemoryObserver.on_mood_updated)
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import BlockLabel
from echovessel.memory.models import CoreBlock
from echovessel.memory.observers import _fire_lifecycle

log = logging.getLogger(__name__)


def update_mood_block(
    db: DbSession,
    *,
    persona_id: str,
    new_mood_text: str,
    user_id: str = "self",
    now: datetime | None = None,
) -> CoreBlock:
    """Replace the persona's mood L1 core block content and fire the
    `on_mood_updated` lifecycle hook.

    Args:
        db: Active SQLModel session. The function commits before
            returning.
        persona_id: Which persona's mood to update.
        new_mood_text: Full replacement text for
            `core_blocks.content`. Not an append — the previous value
            is overwritten in place. Must be non-empty.
        user_id: User-ID label passed through to the `on_mood_updated`
            lifecycle hook. Defaults to the MVP single-user "self".
            The mood block itself is stored as SHARED (`user_id=NULL`)
            regardless of this value; this parameter exists purely to
            satisfy the Protocol signature (tracker §2.1, spec §17a.5)
            which mandates a `user_id: str` argument on the hook. v1.x
            may split mood into per-user rows, at which point this
            parameter will drive both DB storage and hook dispatch.
        now: Override timestamp for tests.

    Returns:
        The freshly-committed `CoreBlock` row.

    Raises:
        ValueError: `new_mood_text` is empty / whitespace-only.

    Lifecycle hook semantics (round 4):
        After the commit succeeds, every observer registered via
        `memory.register_observer` receives
        `on_mood_updated(persona_id, user_id, new_mood_text)`.
        Runtime's observer summarizes and pushes to SSE via the Web
        channel; see `docs/runtime/01-spec-v0.1.md` §17a.5.

        Observer exceptions do NOT roll back this update — the write
        has already committed and the lifecycle dispatcher catches +
        logs any observer failures (review M2/M3).
    """
    if not new_mood_text or not new_mood_text.strip():
        raise ValueError("update_mood_block: new_mood_text must be non-empty")

    now = now or datetime.now()

    stmt = select(CoreBlock).where(
        CoreBlock.persona_id == persona_id,
        CoreBlock.label == BlockLabel.MOOD.value,
        CoreBlock.user_id.is_(None),  # type: ignore[union-attr]
        CoreBlock.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    block = db.exec(stmt).one_or_none()
    if block is None:
        block = CoreBlock(
            persona_id=persona_id,
            user_id=None,
            label=BlockLabel.MOOD,
            content=new_mood_text,
            char_count=len(new_mood_text),
            last_edited_by="runtime",
        )
        db.add(block)
    else:
        block.content = new_mood_text
        block.char_count = len(new_mood_text)
        block.last_edited_by = "runtime"
        db.add(block)

    db.commit()
    db.refresh(block)

    # Round 4 lifecycle hook. Fires strictly after commit, via the
    # module-level observer registry — downstream subscribers learn the
    # mood row changed. Observer exceptions are swallowed by
    # `_fire_lifecycle`; mood write stays committed regardless.
    _fire_lifecycle(
        "on_mood_updated",
        persona_id,
        user_id,
        new_mood_text,
    )

    return block


__all__ = ["update_mood_block"]

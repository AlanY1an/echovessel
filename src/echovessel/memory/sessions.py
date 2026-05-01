"""Session boundary management.

A Session is an internal unit of extraction, NOT a user-visible concept.
The user experiences "one continuous conversation", the system decides when
to close a session for extraction purposes based on:

    IDLE: idle > SESSION_IDLE_MINUTES minutes
    MAX_LENGTH: messages > SESSION_MAX_MESSAGES or tokens > SESSION_MAX_TOKENS
    EXPLICIT: user clicked a "goodbye" (not in MVP)
    LIFECYCLE: app close, etc. (handled by runtime layer, not here)

Sessions are sharded by (persona_id, user_id, channel_id). Each channel's
lifecycle signals fire independently — see docs/DISCUSSION.md 2026-04-14 D6.
The channel sharding applies ONLY to session boundaries; downstream memory
(L3/L4) is unified across channels and never filtered by channel at retrieve
time (D4).

See docs/memory/02-architecture-v0.3.md §3.4.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.core.types import SessionStatus
from echovessel.memory.models import Session
from echovessel.memory.observers import _fire_lifecycle

log = logging.getLogger(__name__)

# --- Tunables (MVP defaults from architecture v0.3) ------------------------

SESSION_IDLE_MINUTES = 10  # v0.7 · was 30, lowered for short reminder support
SESSION_MAX_MESSAGES = 200
SESSION_MAX_TOKENS = 20_000


def set_session_idle_minutes(minutes: int) -> None:
    """Override default. Called by runtime wiring at startup from config."""
    global SESSION_IDLE_MINUTES
    if minutes < 1 or minutes > 1440:
        raise ValueError(f"session_idle_minutes must be 1..1440, got {minutes}")
    SESSION_IDLE_MINUTES = minutes


# --- Round 4: lifecycle fire tracking --------------------------------------
#
# Memory writes happen across multiple functions (get_or_create_open_session
# flushes new Session rows; ingest_message commits; consolidate_session marks
# CLOSED and commits). The lifecycle hooks MUST fire strictly AFTER commit
# (tracker §4 rule 7), so the creator can't fire immediately — the
# committing caller has to.
#
# We use a tiny module-level pending list to relay new-session / closed-
# session events between creator and committer without changing any
# existing function signature. The committing caller drains it via
# `drain_and_fire_pending_lifecycle_events()` right after `db.commit()`.
#
# This is single-threaded-only (SQLite MVP is single-writer). A future
# concurrent backend would need a ContextVar instead; that's v1.x.

_pending_new_sessions: list[tuple[str, str, str]] = []
"""(session_id, persona_id, user_id) tuples queued by
`get_or_create_open_session` when it flushed a fresh Session row.
Drained by `drain_and_fire_pending_lifecycle_events`."""

_pending_closed_sessions: list[tuple[str, str, str]] = []
"""(session_id, persona_id, user_id) tuples queued when a session is
transitioned to `CLOSED`. Drained by
`drain_and_fire_pending_lifecycle_events`."""


def track_pending_session_closed(session: Session) -> None:
    """Queue a session for `on_session_closed` lifecycle dispatch.

    Called by the code path that transitions `session.status` to
    `CLOSED` (currently `consolidate.consolidate_session`) AFTER it has
    committed. The caller then invokes
    `drain_and_fire_pending_lifecycle_events` to actually fire the
    hook; batching via the pending list lets a single commit trigger
    hooks for multiple sessions in one drain, and keeps the fire path
    uniform with new-session events.
    """
    if session.id is None or session.persona_id is None or session.user_id is None:
        return
    _pending_closed_sessions.append(
        (session.id, session.persona_id, session.user_id)
    )


def drain_and_fire_pending_lifecycle_events() -> None:
    """Fire every pending lifecycle event and clear the queues.

    Callers that have just committed a write involving session lifecycle
    transitions MUST call this exactly once after the commit returns.
    Safe to call when no events are pending (it's a no-op).

    Order guarantee: new-session events fire before closed-session
    events within a single drain. This matches the ordering in a
    natural flow where a user's first message in a new session creates
    the new session first, then (at the next boundary) closes it.
    """
    while _pending_new_sessions:
        sid, pid, uid = _pending_new_sessions.pop(0)
        _fire_lifecycle("on_new_session_started", sid, pid, uid)
    while _pending_closed_sessions:
        sid, pid, uid = _pending_closed_sessions.pop(0)
        _fire_lifecycle("on_session_closed", sid, pid, uid)


def _new_session_id() -> str:
    return f"s_{uuid.uuid4().hex[:12]}"


def is_session_stale(session: Session, now: datetime) -> bool:
    """A session is stale if idle exceeds the threshold."""
    return session.last_message_at < now - timedelta(minutes=SESSION_IDLE_MINUTES)


def _should_close_for_length(session: Session) -> bool:
    return (
        session.message_count >= SESSION_MAX_MESSAGES
        or session.total_tokens >= SESSION_MAX_TOKENS
    )


def get_or_create_open_session(
    db: DbSession,
    persona_id: str,
    user_id: str,
    channel_id: str,
    now: datetime | None = None,
) -> Session:
    """Return the open session to write the next message into.

    Scoped to the (persona_id, user_id, channel_id) triple. Each channel has
    its own lifecycle: a stale Discord session will not affect an active
    iMessage session.

    Side effects:
      - If the currently open session for this channel is stale (idle >
        threshold), marks it closing with trigger='idle' and creates a
        fresh one.
      - If no open session exists for this channel, creates one.

    Does NOT commit — the caller is responsible for db.commit() after ingest.
    """
    now = now or datetime.now()

    stmt = select(Session).where(
        Session.persona_id == persona_id,
        Session.user_id == user_id,
        Session.channel_id == channel_id,
        Session.status == SessionStatus.OPEN,
        Session.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    open_sessions = list(db.exec(stmt))

    # Close any that are stale
    fresh_open: Session | None = None
    for s in open_sessions:
        if is_session_stale(s, now):
            _mark_closing(s, trigger="idle", now=now)
        else:
            fresh_open = s

    if fresh_open is not None:
        return fresh_open

    new_session = Session(
        id=_new_session_id(),
        persona_id=persona_id,
        user_id=user_id,
        channel_id=channel_id,
        status=SessionStatus.OPEN,
        started_at=now,
        last_message_at=now,
    )
    # Insert inside a savepoint so a concurrent transaction that already
    # created an OPEN session for this triple trips the partial unique
    # index (uq_sessions_one_open_per_channel) without rolling back the
    # caller's outer transaction. On IntegrityError we re-query and
    # return whichever row won the race. Audit P1-5.
    try:
        with db.begin_nested():
            db.add(new_session)
            db.flush()
    except IntegrityError:
        log.debug(
            "get_or_create_open_session: lost race on (%s, %s, %s); "
            "re-querying for the winner",
            persona_id,
            user_id,
            channel_id,
        )
        winner = db.exec(stmt).first()
        if winner is None:
            # Extremely unlikely: the winner was soft-deleted between
            # our failed flush and the re-query. Surface the original
            # error to the caller rather than silently return None.
            raise
        return winner
    # Queue the new-session lifecycle event for the committer to fire
    # after its `db.commit()` lands. The event is NOT fired here because
    # the write is not yet durable. The append is INSIDE the post-flush
    # path so a failed insert never leaves a stale pending event.
    _pending_new_sessions.append(
        (new_session.id, persona_id, user_id)
    )
    return new_session


def mark_session_closing(
    db: DbSession,
    session: Session,
    trigger: str,
    now: datetime | None = None,
) -> None:
    """Transition an open session to closing.

    Called by:
      - `get_or_create_open_session` when idle threshold crossed
      - INGEST when MAX_LENGTH triggered
      - Startup catch-up
      - Runtime lifecycle events
    """
    _mark_closing(session, trigger=trigger, now=now)


def _mark_closing(
    session: Session, trigger: str, now: datetime | None = None
) -> None:
    now = now or datetime.now()
    if session.status != SessionStatus.CLOSED:
        session.status = SessionStatus.CLOSING
        session.closed_at = now
        session.close_trigger = trigger


def catch_up_stale_sessions(
    db: DbSession,
    now: datetime | None = None,
) -> list[Session]:
    """At startup: find every open session that's already gone stale and
    mark it closing. Returns the list so the caller can enqueue for
    extraction.

    Runs across ALL channels — IDLE is a physical signal that applies
    independently per channel, but the scan just walks (status, last_message_at)
    without needing a channel filter.
    """
    now = now or datetime.now()
    stmt = select(Session).where(
        Session.status == SessionStatus.OPEN,
        Session.deleted_at.is_(None),  # type: ignore[union-attr]
    )
    stale: list[Session] = []
    for s in db.exec(stmt):
        if is_session_stale(s, now):
            _mark_closing(s, trigger="catchup", now=now)
            stale.append(s)
    return stale


def check_length_trigger(
    db: DbSession,
    session: Session,
    now: datetime | None = None,
) -> bool:
    """Called after each message write. If the session exceeded the length
    budget, mark it closing and return True. The caller should then
    create a new session for the next message.
    """
    if _should_close_for_length(session):
        _mark_closing(session, trigger="max_length", now=now)
        return True
    return False

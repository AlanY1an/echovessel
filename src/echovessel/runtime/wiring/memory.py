"""Runtime-side adapters that translate between memory/channel APIs and
the Protocols the proactive subsystem expects.

Two jobs in one file:

1. **`MemoryFacade`** · implements `echovessel.proactive.base.MemoryApi`
   by delegating to the existing `echovessel.memory` public API. The
   memory layer exposes free functions that take `DbSession` as the
   first argument; the proactive layer expects a stateful object that
   manages DB access internally. The facade is the bridge.

2. **`ProactiveChannelRegistry` + `_ProactiveChannelAdapter`** · wraps
   the runtime's in-memory `ChannelRegistry` into the shape proactive
   expects (`list_enabled()` returning objects with `.name` and
   `async def send(text)`). Runtime channels use a richer
   `send(envelope_ref, content)` signature; the adapter translates.

Design rules enforced here (both are tested in `test_memory_facade.py`):

- **D4 铁律**: no `channel_id=...` parameter is ever passed to a memory
  query function. The memory reads must return unified cross-channel
  data. Grep + an explicit unit test guard this.
- **No DbSession leakage**: proactive receives a MemoryFacade instance,
  not a raw DbSession. Each facade method opens its own session, runs
  the query, and closes it before returning.

Spec references:
- Voice spec §7.3.2 (runtime instantiates VoiceService before channels)
- Proactive spec §11.3 (runtime supplies MemoryApi adapter)
- DISCUSSION.md 2026-04-14 D4 (铁律: memory never filters by channel)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlmodel import Session as DbSession
from sqlmodel import select

from echovessel.channels.base import OutgoingMessage
from echovessel.core.types import MessageRole, NodeType
from echovessel.memory import (
    list_recall_messages as _list_recall_messages,
)
from echovessel.memory.ingest import IngestResult, ingest_message
from echovessel.memory.models import ConceptNode
from echovessel.memory.models import Session as SessionRow
from echovessel.memory.retrieve import load_core_blocks
from echovessel.proactive.core.base import ChannelProtocol
from echovessel.runtime.channel_registry import ChannelRegistry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MemoryFacade
# ---------------------------------------------------------------------------


DbSessionFactory = Callable[[], DbSession]


class MemoryFacade:
    """Proactive's view of the memory subsystem.

    Wraps the free-function memory API with a stateful facade that opens
    a short-lived `DbSession` per call. Proactive uses this exactly like
    the `MemoryApi` Protocol describes (spec §11.3 #4).

    Every READ method here MUST NOT pass `channel_id=` to any downstream
    call. That invariant is the D4 铁律 — verified by
    `tests/runtime/test_memory_facade.py::test_no_channel_id_kwarg_in_reads`.
    """

    def __init__(self, db_factory: DbSessionFactory) -> None:
        self._db_factory = db_factory

    # --- MemoryApi: reads (D4 — no channel filter) ---------------

    def load_core_blocks(self, persona_id: str, user_id: str) -> list[Any]:
        with self._db_factory() as db:
            return list(load_core_blocks(db, persona_id, user_id))

    def list_recall_messages(
        self,
        persona_id: str,
        user_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[Any]:
        with self._db_factory() as db:
            return list(
                _list_recall_messages(
                    db,
                    persona_id,
                    user_id,
                    limit=limit,
                    before=before,
                )
            )

    def get_recent_events(
        self,
        persona_id: str,
        user_id: str,
        *,
        since: datetime,
        limit: int = 20,
    ) -> list[Any]:
        """Return L3 events (ConceptNode type='event') newer than `since`.

        No `channel_id` filter by design (D4). Memory does not currently
        export a top-level `get_recent_events` helper, so we run the
        SQLModel query directly here — this is the only spot in the
        runtime where we bypass the memory facade API, and it's justified
        because the query is purely a subset of the generic ConceptNode
        schema and requires no new memory-layer abstraction.
        """
        with self._db_factory() as db:
            stmt = (
                select(ConceptNode)
                .where(
                    ConceptNode.persona_id == persona_id,
                    ConceptNode.user_id == user_id,
                    ConceptNode.type == NodeType.EVENT.value,
                    ConceptNode.created_at >= since,
                    ConceptNode.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(ConceptNode.created_at.desc())  # type: ignore[attr-defined]
                .limit(max(1, min(limit, 200)))
            )
            return list(db.exec(stmt).all())

    def get_session_status(self, session_id: str) -> Any | None:
        """Look up a session by id. Returns the SessionRow or None.

        Proactive uses this to decide whether a closed session has just
        been processed and its events are worth reflecting on.
        """
        with self._db_factory() as db:
            stmt = select(SessionRow).where(SessionRow.id == session_id)
            return db.exec(stmt).one_or_none()

    # --- MemoryApi: single write path ----------------------------

    def ingest_message(
        self,
        *,
        persona_id: str,
        user_id: str,
        channel_id: str,
        role: Any,
        content: str,
        now: datetime | None = None,
    ) -> IngestResult:
        """Write a persona-authored message into L2.

        ``channel_id`` here is **delivery metadata** (which pipe was used
        to push the message out) — not a memory filter. D4 only applies
        to reads. Proactive uses this after its delivery router has
        picked a target channel to keep memory and channel state in sync
        (spec §6.2 send-order invariant).
        """
        role_value = role
        if not isinstance(role_value, MessageRole):
            # Accept string or enum; normalize to the Python enum so
            # memory.ingest_message's sa_column=String handles it.
            role_value = MessageRole(str(role))

        with self._db_factory() as db:
            return ingest_message(
                db,
                persona_id=persona_id,
                user_id=user_id,
                channel_id=channel_id,
                role=role_value,
                content=content,
                now=now,
            )


# ---------------------------------------------------------------------------
# Channel adapter (runtime ChannelLike → proactive ChannelProtocol)
# ---------------------------------------------------------------------------


class _ProactiveChannelAdapter:
    """Adapt a runtime `ChannelLike` to proactive's `ChannelProtocol`.

    Interface differences the adapter bridges:

    | runtime                         | proactive          |
    |---------------------------------|--------------------|
    | `channel_id: str`               | `name: str`        |
    | `send(envelope_ref, content)`   | `send(text)`       |

    Runtime channels also may expose optional capability flags
    (`supports_audio`, `supports_outgoing_push`) that proactive's
    delivery router checks via `getattr(... , default)`. We pass those
    through unchanged.
    """

    def __init__(self, raw_channel: Any) -> None:
        self._raw = raw_channel

    @property
    def name(self) -> str:
        return getattr(
            self._raw, "channel_id", self._raw.__class__.__name__
        )

    @property
    def channel_id(self) -> str:
        # Proactive's delivery router falls back to `channel_id` when
        # `name` is unset, but we populate both for clarity.
        return self.name

    @property
    def supports_audio(self) -> bool:
        return bool(getattr(self._raw, "supports_audio", False))

    @property
    def supports_outgoing_push(self) -> bool:
        return bool(getattr(self._raw, "supports_outgoing_push", True))

    async def send(self, text: str) -> None:
        """Deliver a proactive message to the underlying runtime channel.

        Proactive's contract passes just the text; runtime channels
        now speak :class:`echovessel.channels.base.OutgoingMessage` (v0.2).
        Proactive pushes are unthreaded by nature — they are not
        replying to an incoming message — so both ``in_reply_to`` and
        ``in_reply_to_turn_id`` are ``None`` and ``kind`` is
        ``"proactive"``.

        Stage 7 note: proactive has its own voice delivery path inside
        ``proactive/delivery.py::prepare_voice`` which computes a
        ``VoiceOutcome`` (including calling ``generate_voice``).
        However, the ``VoiceOutcome.voice_result`` is currently NOT
        threaded through this adapter's ``send(text)`` call — the
        proactive scheduler only passes text. For MVP, proactive
        messages are always ``delivery="text"`` at the channel level;
        the voice artifact (if produced) is consumed by proactive's
        audit trail but not forwarded to the channel. A follow-up
        round can extend this adapter to accept and forward
        voice_result alongside text.
        """
        raw_send = getattr(self._raw, "send", None)
        if raw_send is None:
            log.warning(
                "proactive channel adapter: underlying channel %r has no send()",
                self.name,
            )
            return
        outgoing = OutgoingMessage(
            content=text,
            in_reply_to=None,
            in_reply_to_turn_id=None,
            kind="proactive",
            delivery="text",
        )
        await raw_send(outgoing)


class ProactiveChannelRegistry:
    """Proactive-side view of the runtime ChannelRegistry.

    Structurally satisfies `echovessel.proactive.base.ChannelRegistryApi`
    (not a runtime_checkable Protocol, so we don't declare inheritance —
    structural typing is enough for the scheduler to consume it).

    Returns fresh adapters each time `list_enabled` is called. Holding
    long-term references would leak runtime-side channel lifecycle state
    into proactive; we don't.
    """

    def __init__(self, runtime_registry: ChannelRegistry) -> None:
        self._reg = runtime_registry

    def list_enabled(self) -> list[ChannelProtocol]:
        return [
            _ProactiveChannelAdapter(ch)
            for ch in self._reg.all_channels()
        ]


__all__ = [
    "MemoryFacade",
    "DbSessionFactory",
    "ProactiveChannelRegistry",
]

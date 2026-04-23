"""Shared enum types used across all EchoVessel modules.

Kept in `core/` because every module may need to reference these (e.g., an
L3 event uses NodeType, a channel produces a MessageRole, etc.). Nothing else
lives in core/ unless it's similarly foundational and dependency-free.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MessageRole(StrEnum):
    """Role of a recall message. Stored as TEXT in SQLite."""

    USER = "user"
    PERSONA = "persona"
    SYSTEM = "system"


class SessionStatus(StrEnum):
    """Lifecycle state of a conversation session.

    - `open`: accepting new messages
    - `closing`: idle/length/lifecycle triggered; waiting for extraction
    - `closed`: extraction complete (or trivial-skipped)
    - `failed`: consolidate worker exhausted retries (see spec §8.3 / §11 #5)
    """

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class NodeType(StrEnum):
    """Provenance of a ConceptNode (L3 event vs L4 thought vs reserved chat).

    Note: `type` is *provenance*, not *topic*. Emotion is never a type — it's
    an attribute (see emotional_impact / emotion_tags fields on ConceptNode).
    """

    EVENT = "event"  # Extracted from L2 by the consolidate pipeline
    THOUGHT = "thought"  # Produced by reflection from other ConceptNodes
    CHAT = "chat"  # Reserved; unused in MVP
    # v0.4 · L3 sub-type · persona-side commitment ("I promised I'd remind
    # you at 9"). Extraction writes under PART C strict commitment guard.
    INTENTION = "intention"
    # v0.4 · L4 sub-type · forward-looking expectation produced by the
    # slow_tick reflection phase ("she'll probably update grad school
    # progress next week").
    EXPECTATION = "expectation"


class BlockLabel(StrEnum):
    """Identifies which core block a row represents.

    Shared blocks (persona/self/style) have user_id = NULL.
    Per-user blocks (user/relationship) must have a non-null user_id.
    """

    PERSONA = "persona"
    SELF = "self"
    USER = "user"
    RELATIONSHIP = "relationship"
    STYLE = "style"


# Blocks that are shared across users for a given persona.
SHARED_BLOCK_LABELS: frozenset[BlockLabel] = frozenset(
    {BlockLabel.PERSONA, BlockLabel.SELF, BlockLabel.STYLE}
)

# Blocks that are per-user for a given persona.
PER_USER_BLOCK_LABELS: frozenset[BlockLabel] = frozenset({BlockLabel.USER, BlockLabel.RELATIONSHIP})


@dataclass(frozen=True, slots=True)
class EventTime:
    """Resolved absolute time window for an L3 event (R4).

    Both bounds carry their original timezone offset — extraction MUST
    NOT shift to UTC because the renderer downstream uses the user's
    local wall clock. ``end is None`` means "instant event": the
    consumer treats start and end as the same moment.

    Lives in core because both ``prompts`` (parses LLM output) and
    ``memory`` (persists into ``concept_nodes``) handle this shape, and
    neither layer is allowed to import the other.
    """

    start: datetime
    end: datetime | None = None

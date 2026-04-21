"""SQLModel table definitions for the EchoVessel memory subsystem.

Matches `docs/memory/04-schema-v0.2.md` (content labelled v0.3 after the
Thread SYS-exec round). Schema upgrades from already-existing databases
are handled by `memory.migrations.ensure_schema_up_to_date`, which is
called in `db.py::create_all_tables` before the ORM `create_all`.

Eight entity tables (v0.3 adds `core_block_appends`):
    1. personas
    2. users
    3. core_blocks             (L1)
    4. sessions
    5. recall_messages         (L2, plus separate FTS5 virtual table)
    6. concept_nodes           (L3 + L4 unified, plus separate sqlite-vec table)
    7. concept_node_filling    (L4 provenance chain)
    8. core_block_appends      (L1 append-only audit log, v0.3)

Virtual tables (FTS5, sqlite-vec) are NOT SQLModel — they are created via raw
DDL in `db.py` after metadata creation.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlmodel import Field, SQLModel

from echovessel.core.types import BlockLabel, MessageRole, NodeType, SessionStatus


def _str_enum_column() -> Column:
    """SQLModel stores Python enums by NAME by default, not VALUE.

    We want the stored values to match the enum .value ('event', 'thought'
    etc.) so that SQL queries written against string literals work and so
    that the DB is human-readable. Declare enum columns with a plain String
    type; Pydantic/SQLModel will still validate via the enum on the Python
    side because enum(str, Enum) is str-compatible.
    """
    return Column(String, nullable=False)


# ---------------------------------------------------------------------------
# Identity tables
# ---------------------------------------------------------------------------


class Persona(SQLModel, table=True):
    """A digital character. The top-level identity that everything else hangs off.

    Biographic facts (full_name / gender / birth_date / ...) are structured
    companions to the prose core_blocks: they answer "what is her timezone"
    or "when was she born" without re-reading the persona block. All are
    nullable — LLM extraction fills what it can, the user reviews / edits
    in the onboarding flow or the admin page. See `docs/en/configuration.md`
    for the per-field contract.
    """

    __tablename__ = "personas"

    id: str = Field(primary_key=True)
    display_name: str
    description: str | None = None
    avatar_path: str | None = None
    voice_config: str | None = Field(default=None)  # JSON string, v1.x

    # --- Biographic facts ---------------------------------------------------
    # Identity
    full_name: str | None = None
    gender: str | None = None  # 'female' | 'male' | 'non_binary'
    birth_date: date | None = None
    ethnicity: str | None = None

    # Culture & language
    nationality: str | None = None  # ISO 3166-1 alpha-2, e.g. 'CN'
    native_language: str | None = None  # BCP 47, e.g. 'zh-CN'
    locale_region: str | None = None  # free-form, e.g. 'northeast_china'

    # Education & occupation
    education_level: str | None = None  # high_school | bachelor | master | phd
    occupation: str | None = None
    occupation_field: str | None = None

    # Life
    location: str | None = None
    timezone: str | None = None  # IANA tz, e.g. 'Asia/Shanghai'
    relationship_status: str | None = None  # single | married | widowed | divorced
    life_stage: str | None = None  # student | working | retired | new_parent | between_jobs
    health_status: str | None = None  # healthy | chronic_illness | recovering | serious

    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )
    deleted_at: datetime | None = None


class User(SQLModel, table=True):
    """A human the persona interacts with. MVP: only one row with id='self'."""

    __tablename__ = "users"

    id: str = Field(primary_key=True)
    display_name: str
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    deleted_at: datetime | None = None


class ExternalIdentity(SQLModel, table=True):
    """Maps a transport-native identity to an internal user_id.

    Channels surface their own identity tokens (Discord snowflakes, phone
    handles, web "self"). The memory layer scopes everything by
    ``internal_user_id`` so retrieve / consolidate / core_blocks stay
    coherent across channels. This table is the bridge.

    MVP semantics: every external id resolves to ``"self"`` — the local
    daemon has one human owner. Future multi-user / group-chat work
    rebinds rows here without touching the rest of the schema.
    """

    __tablename__ = "external_identities"

    channel_id: str = Field(primary_key=True)
    external_id: str = Field(primary_key=True)
    internal_user_id: str = Field(foreign_key="users.id")
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )


# ---------------------------------------------------------------------------
# L1 · Core blocks
# ---------------------------------------------------------------------------


class CoreBlock(SQLModel, table=True):
    """A single L1 core block that is always injected into the prompt.

    Business rules (enforced at application layer, not DB):
      - persona/self/mood blocks have user_id = NULL (shared across users)
      - user/relationship blocks have user_id NOT NULL (per-user)
      - See `echovessel.core.types.SHARED_BLOCK_LABELS`.
    """

    __tablename__ = "core_blocks"
    __table_args__ = (
        UniqueConstraint(
            "persona_id", "user_id", "label", name="uq_core_block_persona_user_label"
        ),
        Index("idx_core_blocks_persona_user", "persona_id", "user_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    persona_id: str = Field(foreign_key="personas.id")
    user_id: str | None = Field(default=None, foreign_key="users.id")
    label: BlockLabel = Field(sa_column=_str_enum_column())
    content: str = ""
    char_count: int = 0
    char_limit: int = 5000
    version: int = 1
    last_edited_by: str = "system"  # 'user' | 'reflection' | 'system'
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )
    )
    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session(SQLModel, table=True):
    """A conversation session. Internal extraction unit — see architecture §3.4.

    Sharded by (persona_id, user_id, channel_id) — see docs/DISCUSSION.md
    2026-04-14 D6. Each channel's IDLE / MAX_LENGTH / LIFECYCLE fires
    independently because they're physical signals (Discord idle must not
    close an active iMessage session).

    Note: session sharding by channel is for SESSION LIFECYCLE ONLY. Once a
    session's L3 events are extracted they join the unified memory pool and
    retrieval NEVER filters by channel_id (D4 铁律).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_status_last", "status", "last_message_at"),
        Index(
            "idx_sessions_persona_user_channel_started",
            "persona_id",
            "user_id",
            "channel_id",
            "started_at",
        ),
        # Audit P1-5: at most one OPEN session per (persona, user, channel).
        # Without this, two concurrent ingests for the same triple can
        # both see no open session and both insert, leaving the DB with
        # duplicate OPEN rows. Partial (WHERE status='open') so closed
        # history rows can pile up freely.
        Index(
            "uq_sessions_one_open_per_channel",
            "persona_id",
            "user_id",
            "channel_id",
            unique=True,
            sqlite_where=text("status = 'open' AND deleted_at IS NULL"),
        ),
    )

    id: str = Field(primary_key=True)  # UUID
    persona_id: str = Field(foreign_key="personas.id")
    user_id: str = Field(foreign_key="users.id")
    channel_id: str  # NOT NULL · opaque string ('web', 'discord:g123', etc.)
    status: SessionStatus = Field(
        default=SessionStatus.OPEN, sa_column=_str_enum_column()
    )

    started_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    last_message_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    closed_at: datetime | None = None

    extracted: bool = False
    extracted_at: datetime | None = None
    # Resume point for partial consolidation: set True after the extraction
    # phase commits so a transient reflection failure can be retried without
    # re-running extraction (which would duplicate events). Invariant:
    # extracted=True implies extracted_events=True; never the reverse.
    extracted_events: bool = False
    extracted_events_at: datetime | None = None
    trivial: bool = False

    message_count: int = 0
    total_tokens: int = 0
    close_trigger: str | None = None  # 'idle' | 'max_length' | 'explicit' | 'lifecycle' | 'catchup'

    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# L2 · Recall (raw messages, ground truth)
# ---------------------------------------------------------------------------


class RecallMessage(SQLModel, table=True):
    """A single message, raw. The ground truth layer.

    Does NOT participate in default retrieval. Only queried on explicit user
    reference, offline re-extraction, or delete/export operations. See
    architecture §4.6 for L2's archival role.

    A separate FTS5 virtual table `recall_messages_fts` is created via raw DDL
    in `db.py` for full-text search. Keep that table in sync via triggers
    (also in `db.py`).
    """

    __tablename__ = "recall_messages"
    __table_args__ = (
        Index("idx_recall_session", "session_id", "created_at"),
        Index("idx_recall_persona_user_day", "persona_id", "user_id", "day"),
        Index("idx_recall_deleted", "deleted_at"),
        Index(
            "idx_recall_persona_user_created",
            "persona_id",
            "user_id",
            "created_at",
        ),
        # v0.3 · optional index for turn-grouped reads (pairs user burst →
        # persona reply without scanning all rows in a session)
        Index("idx_recall_turn", "turn_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id")
    persona_id: str = Field(foreign_key="personas.id")
    user_id: str = Field(foreign_key="users.id")
    # Redundantly stored here even though it's derivable via session_id —
    # avoids a join on every retrieve and leaves room for a "show my web
    # history" query without joining sessions. See DISCUSSION.md 2026-04-14.
    channel_id: str
    role: MessageRole = Field(sa_column=_str_enum_column())
    content: str
    token_count: int | None = None
    day: date
    # v0.3 · nullable · channel debounce produces a ULID per IncomingTurn
    # (channels spec v0.2 §2.3a). Legacy rows have NULL; new rows are
    # filled by the channel layer. Persona reply uses the same turn_id as
    # the user turn it answers, letting L2 readers reconstruct pairings.
    turn_id: str | None = None
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# L3 + L4 · Concept nodes (events + thoughts in the same table)
# ---------------------------------------------------------------------------


class ConceptNode(SQLModel, table=True):
    """L3 events and L4 thoughts, unified by NodeType discriminator.

    See architecture §4.8 for why these share a table (same data structure,
    same retrieval path, same scoring, same lifecycle — type is provenance
    only).

    A separate sqlite-vec virtual table `concept_nodes_vec` holds the
    embedding, keyed by `id`. Created via raw DDL in `db.py`.
    """

    __tablename__ = "concept_nodes"
    __table_args__ = (
        Index("idx_concept_persona_user_type", "persona_id", "user_id", "type"),
        Index("idx_concept_created", "created_at"),
        Index("idx_concept_impact", "emotional_impact"),
        Index("idx_concept_deleted", "deleted_at"),
        Index("idx_concept_source_session", "source_session_id"),
        # v0.3 · soft index on the turn the extractor tied this node to
        Index("idx_concept_source_turn", "source_turn_id"),
        # v0.3 · import-side lookup (dedup-check by file hash)
        Index("idx_concept_imported_from", "imported_from"),
        # v0.3 · mutual exclusion between session-sourced and import-sourced
        # provenance. One or the other may be non-NULL, not both.
        CheckConstraint(
            "imported_from IS NULL OR source_session_id IS NULL",
            name="ck_concept_nodes_source_mutex",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    persona_id: str = Field(foreign_key="personas.id")
    user_id: str = Field(foreign_key="users.id")

    type: NodeType = Field(sa_column=_str_enum_column())

    description: str

    # Scoring dimensions
    emotional_impact: int = Field(default=0, ge=-10, le=10)
    emotion_tags: list[str] = Field(default_factory=list, sa_type=JSON)
    relational_tags: list[str] = Field(default_factory=list, sa_type=JSON)

    # Retrieval bookkeeping
    access_count: int = 0
    last_accessed_at: datetime | None = None

    # Source (L2 pointer, only set for events)
    source_session_id: str | None = Field(default=None, foreign_key="sessions.id")
    source_deleted: bool = False
    # v0.3 · optional soft reference to the user turn the extractor said
    # this event/thought came from. `source_session_id` remains the
    # authoritative L2 anchor; `source_turn_id` is a soft provenance hint
    # emitted by the extraction prompt (review R2: extraction is still
    # per-session, not per-turn).
    source_turn_id: str | None = None
    # v0.3 · set when the row was produced by the import pipeline instead
    # of a conversation session. Value is a file hash (see import spec).
    # Mutually exclusive with `source_session_id` (CHECK constraint above).
    imported_from: str | None = None

    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    deleted_at: datetime | None = None


# ---------------------------------------------------------------------------
# L4 · Filling (provenance chain for thoughts)
# ---------------------------------------------------------------------------


class ConceptNodeFilling(SQLModel, table=True):
    """Links a thought (parent) to the evidence nodes that generated it (child).

    When a user deletes a child node but chooses to keep the thought, we
    mark `orphaned = True` rather than dropping the row. This preserves
    auditability of the forgetting-rights flow. See architecture §4.12.2.
    """

    __tablename__ = "concept_node_filling"
    __table_args__ = (
        UniqueConstraint(
            "parent_id", "child_id", name="uq_filling_parent_child"
        ),
        Index("idx_filling_parent", "parent_id"),
        Index("idx_filling_child_active", "child_id", "orphaned"),
    )

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int = Field(foreign_key="concept_nodes.id")
    child_id: int = Field(foreign_key="concept_nodes.id")
    orphaned: bool = False
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )
    orphaned_at: datetime | None = None


# ---------------------------------------------------------------------------
# L1 · Core block append-only audit log (v0.3)
# ---------------------------------------------------------------------------


class CoreBlockAppend(SQLModel, table=True):
    """Audit row for each append to an L1 core block.

    Written by the import pipeline whenever it appends content to an
    existing core block (persona / self / user / relationship). The live
    block state stays in `core_blocks.content`; this table is the
    provenance ledger so "undo last import" and auditing can work.

    `user_id` is **nullable** to match L1 core-block semantics: shared
    blocks (persona / self / mood) have `user_id = NULL`. The schema
    documentation notes a NOT NULL hint for this column, but applying
    that would make appending to persona_block impossible without a fake
    sentinel user_id, so this implementation relaxes the constraint to
    match L1's actual semantics. The architecture spec's CoreBlock model
    also treats user_id as optional on shared labels.

    This table is append-only; no soft delete column. To "revert" an
    import, higher layers issue a physical DELETE WHERE imported_from =
    <file_hash> against both this table and concept_nodes.
    """

    __tablename__ = "core_block_appends"
    __table_args__ = (
        Index(
            "idx_core_block_appends_persona_user_label",
            "persona_id",
            "user_id",
            "label",
            "created_at",
        ),
        Index(
            "idx_core_block_appends_created",
            "persona_id",
            "created_at",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    persona_id: str = Field(foreign_key="personas.id")
    user_id: str | None = Field(default=None, foreign_key="users.id")
    # Label string matches the BlockLabel enum (persona / self / user /
    # relationship). Relationship blocks encode the target person via the
    # sibling core_blocks row's uniqueness key, not via a dedicated column
    # here. Kept as plain TEXT in SQLite for simplicity.
    label: str
    content: str
    # JSON-encoded provenance object. Schema (v0.3):
    #   {
    #     "imported_from": "<file_hash>",
    #     "source_label":  "...",
    #     "chunk_index":   <int>,
    #     "prompt_round":  "<pipeline-name>",
    #     "notes":         "..."
    #   }
    # Stored as JSON so provenance keys can evolve without schema churn.
    provenance_json: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, server_default=func.now())
    )

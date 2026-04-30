"""Proactive · persisted SQLModel rows.

Three tables live here, all written by the proactive subsystem:

1. ``persona_profile`` — Layer 1 behaviour profile, generated once
   during onboarding by the LARGE-tier LLM.
2. ``proactive_state`` — single-float engagement signal per
   (persona, user) pair.
3. ``proactive_decisions`` — append-only audit row, one per fire-or-
   suppress attempt.

The layered import contract allows ``proactive → memory``, so other
proactive modules (engines / execution) import these classes from
here. The reverse direction (``memory → proactive``) is forbidden.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import JSON, Column, Index, String
from sqlmodel import Field, SQLModel

ProfileSource = Literal["llm_onboarding", "user_edited", "fallback_default"]


class PersonaProfile(SQLModel, table=True):
    """Layer 1 · per-persona behaviour profile.

    Generated once during onboarding by the LARGE-tier LLM, then
    editable via the admin "行为侧写" tab. The system does NOT
    auto-rewrite this row to avoid silent drift in tone.
    """

    __tablename__ = "persona_profile"

    persona_id: str = Field(primary_key=True)

    style_summary: str

    quiet_hours: list[int] = Field(
        default_factory=lambda: [23, 7],
        sa_column=Column(JSON, nullable=False),
    )

    forbidden_topics: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )

    voice_id: str | None = Field(default=None)

    profile_generated_at: datetime
    profile_source: str  # ProfileSource literal — stored as plain TEXT


class ProactiveState(SQLModel, table=True):
    """Single-float engagement signal per (persona, user) pair.

    Updated by the BA contingent reward loop. ``decay > reward`` is
    the literature-driven hard constraint (over-restraint < over-
    talking in cost). Initial value 0.7 — gives a new relationship a
    starting runway.
    """

    __tablename__ = "proactive_state"

    persona_id: str = Field(primary_key=True)
    user_id: str = Field(primary_key=True)
    engagement_score: float = 0.7
    last_updated: datetime


ProactiveAction = Literal["fire", "suppress"]
ProactiveTriggerType = Literal["follow_up", "thread_due", "high_emotional_event"]


class ProactiveDecision(SQLModel, table=True):
    """One row per attempt — fire or suppress.

    Replaces the v1 JSONL audit sink. Non-partial indexes live here
    via ``__table_args__``; the partial index over un-replied fires
    (``idx_decisions_pending_reply``) is emitted from
    :mod:`echovessel.memory.migrations`.

    Note the name collision with
    :class:`echovessel.proactive.core.base.ProactiveDecision`: that
    sibling is the in-memory value object the runtime scheduler
    builds; this class is the persisted SQLModel row. The two are
    deliberately not unified — the value type carries transient
    runtime fields (``rationale``, ``llm_latency_ms``) that the audit
    row does not need.
    """

    __tablename__ = "proactive_decisions"
    __table_args__ = (
        Index(
            "idx_decisions_persona_user_time",
            "persona_id",
            "user_id",
            "timestamp",
        ),
        Index("idx_decisions_persona_time", "persona_id", "timestamp"),
    )

    id: int | None = Field(default=None, primary_key=True)
    decision_id: str = Field(index=True)
    timestamp: datetime = Field(index=True)
    persona_id: str = Field(index=True)
    user_id: str = Field(index=True)

    # ProactiveTriggerType literal — stored as plain TEXT.
    # 'thread_due' / 'high_emotional_event' kept for v2 audit history compat.
    trigger_type: ProactiveTriggerType = Field(sa_column=Column(String, nullable=False))

    # v3 · legacy column kept for v2 audit history compat. Never written.
    thread_id: int | None = Field(default=None)
    source_event_id: int | None = Field(default=None, foreign_key="concept_nodes.id")

    # v3 · 'pre' | 'on' | 'post' | 'check_1' | 'check_N' | None
    phase: str | None = Field(default=None)

    action: str  # ProactiveAction literal
    suppress_reason: str | None = None

    message_text: str | None = None
    sent_message_id: int | None = Field(default=None, foreign_key="recall_messages.id")
    user_replied_at: datetime | None = None
    voice_used: bool | None = None
    voice_error: str | None = None

    gate_state_snapshot: str | None = None  # JSON-encoded payload

    created_at: datetime


# Sentinel value written into ``ProactiveDecision.user_replied_at`` by
# the engagement settlement loop to mark "fire whose 24h reply window
# expired without a user message". Lives next to the model so the admin
# route can scrub it at the API boundary without crossing the
# ``channels → proactive`` import-linter wall — both sides depend on
# this module.
NOT_REPLIED_SENTINEL: datetime = datetime.min

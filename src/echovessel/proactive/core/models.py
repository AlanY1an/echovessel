"""Proactive · per-persona behaviour profile + engagement state rows.

Two tables live here, both proactive-private:

1. ``persona_profile`` — Layer 1 behaviour profile, generated once
   during onboarding by the LARGE-tier LLM.
2. ``proactive_state`` — single-float engagement signal per
   (persona, user) pair.

The audit row ``ProactiveDecision`` lives in :mod:`echovessel.memory.models`
because both proactive (writer) and channels (admin reader) depend on
it, and those two sibling layers cannot import each other. Anchoring
``ProactiveDecision`` one layer below in memory lets both sides reach
it without crossing the wall.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import JSON, Column
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

"""ProactiveConfig Pydantic v2 model (spec §12).

Runtime instantiates this from the ``[proactive]`` TOML section via its
own config loader (Thread RT-round2). Proactive code imports this model
directly so tests and the factory can build instances without going
through runtime.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ProactiveConfig(BaseModel):
    """Proactive subsystem configuration.

    Every field has a default chosen to match spec §12.3 rationale.
    The defaults are the MVP-intended behaviour; overriding is only for
    power users / tests.
    """

    enabled: bool = True
    tick_interval_seconds: int = Field(default=60, ge=10, le=3600)

    # Quiet hours (local time, 24h)
    quiet_hours_start: int = Field(default=23, ge=0, le=23)
    quiet_hours_end: int = Field(default=7, ge=0, le=23)

    # Rate limit
    max_per_24h: int = Field(default=3, ge=0, le=100)

    # Cold-user detection
    cold_user_threshold: int = Field(default=2, ge=1, le=20)
    cold_user_response_window_hours: int = Field(default=6, ge=1, le=72)

    # Long silence (gentle nudge)
    long_silence_hours: int = Field(default=48, ge=1, le=720)

    # Queue cap (spec §2.5)
    max_events_in_queue: int = Field(default=64, ge=8, le=1024)

    # v0.2 · review Check 3 · field retained for spec §12 schema
    # compatibility but NO LONGER READ by the proactive code path. The
    # single source of truth for voice delivery is ``persona.voice_enabled``
    # (spec §6.2a + review Check 3). To disable voice ops-wide, inject
    # ``voice_service=None`` into the factory — the PersonaView's
    # ``voice_enabled`` property is the only other short-circuit.
    use_voice_when_available: bool = True

    # Audit sink (MVP only supports 'jsonl')
    audit_sink: Literal["jsonl", "memory_db"] = "jsonl"

    # Stop grace (spec §16.8)
    stop_grace_seconds: int = Field(default=10, ge=1, le=120)

    # Convenience: MVP single-persona default identity
    persona_id: str = "default"
    user_id: str = "self"

    @field_validator("audit_sink")
    @classmethod
    def _memory_db_not_mvp(cls, v: str) -> str:
        if v == "memory_db":
            raise ValueError(
                "audit_sink='memory_db' is v1.0 only; use 'jsonl'"
            )
        return v


__all__ = ["ProactiveConfig"]

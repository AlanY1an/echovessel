"""ProactiveConfig Pydantic v2 model.

Runtime instantiates this from the ``[proactive]`` TOML section via its
own config loader. Proactive code imports this model directly so tests
and the factory can build instances without going through runtime.

Layer-1 PROFILE knobs (``quiet_hours``, ``forbidden_topics``,
``style_summary``) live on the per-persona ``persona_profile`` row and
are edited via the admin "行为侧写" tab — they are deliberately *not*
config fields, because they are persona-specific behaviour rather than
deployment-wide policy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProactiveConfig(BaseModel):
    """Proactive subsystem configuration.

    Only the fields the v2 daemon actually reads are listed here. The
    defaults match the MVP-intended behaviour; overriding is for power
    users and tests.
    """

    # Master on/off switch.
    enabled: bool = True

    # Daily ceiling on fired proactive messages (rolling 24h window),
    # evaluated by the rate_limit gate.
    max_per_24h: int = Field(default=3, ge=0, le=100)

    # Hard cap on the in-memory event queue; excess events are dropped
    # oldest-first so the scheduler never OOMs under load.
    max_events_in_queue: int = Field(default=64, ge=8, le=1024)

    # Wait-time for in-flight tick on graceful shutdown.
    stop_grace_seconds: int = Field(default=10, ge=1, le=120)

    # MVP single-persona default identity. Multi-persona daemons would
    # build one ProactiveConfig per persona.
    persona_id: str = "default"
    user_id: str = "self"


__all__ = ["ProactiveConfig"]

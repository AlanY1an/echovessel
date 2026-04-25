"""Memory consolidation — close a session, extract events / thoughts, run reflection.

This sub-package contains the write-side of memory: the
``consolidate_session`` entry point and the phase-A/B/C/D/E/F state
machine that runs over each closed session, plus the per-session
trace recorder used by the dev console.

Internal layout:

- :mod:`.core`     — orchestration (``consolidate_session``) + extraction
                     / reflection DTOs + ``ExtractFn`` / ``ReflectFn`` /
                     ``EmbedFn`` callable shapes
- :mod:`.phase_a`  — trivial-skip gating: ``TRIVIAL_*`` thresholds,
                     ``STRONG_EMOTION_KEYWORDS``, :func:`is_trivial`
- :mod:`.phase_bce` — extraction-side helpers + reflection thresholds:
                     ``SHOCK_IMPACT_THRESHOLD`` / ``TIMER_REFLECTION_HOURS``
                     / ``REFLECTION_HARD_LIMIT_24H`` and the private
                     helpers consolidate_session calls into
- :mod:`.tracer`   — per-session dev-mode trace recorder

Public API is re-exported here so callers continue to use
``from echovessel.memory.consolidate import …`` without depending on
the internal split.
"""

from echovessel.memory.consolidate.core import (
    ConsolidateResult,
    EmbedFn,
    ExtractedEntity,
    ExtractedEntityClarification,
    ExtractedEvent,
    ExtractedSessionMoodSignal,
    ExtractedThought,
    ExtractFn,
    ExtractionResult,
    ReflectFn,
    consolidate_session,
)
from echovessel.memory.consolidate.phase_a import (
    TRIVIAL_MESSAGE_COUNT,
    TRIVIAL_TOKEN_COUNT,
    is_trivial,
)
from echovessel.memory.consolidate.phase_bce import (
    REFLECTION_HARD_LIMIT_24H,
    SHOCK_IMPACT_THRESHOLD,
    TIMER_REFLECTION_HOURS,
)
from echovessel.memory.consolidate.tracer import (
    ConsolidateTracer,
    NullConsolidateTracer,
    make_consolidate_tracer,
)

__all__ = [
    # Entry point
    "consolidate_session",
    # Trivial gating (Phase A)
    "TRIVIAL_MESSAGE_COUNT",
    "TRIVIAL_TOKEN_COUNT",
    "is_trivial",
    # Reflection thresholds
    "SHOCK_IMPACT_THRESHOLD",
    "TIMER_REFLECTION_HOURS",
    "REFLECTION_HARD_LIMIT_24H",
    # Extraction DTOs
    "ExtractedEvent",
    "ExtractedEntity",
    "ExtractedEntityClarification",
    "ExtractedSessionMoodSignal",
    "ExtractedThought",
    "ExtractionResult",
    # Result + callable shapes
    "ConsolidateResult",
    "ExtractFn",
    "ReflectFn",
    "EmbedFn",
    # Trace
    "ConsolidateTracer",
    "NullConsolidateTracer",
    "make_consolidate_tracer",
]

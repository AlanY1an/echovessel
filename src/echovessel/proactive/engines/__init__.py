"""Proactive decision engines — policy + generator.

policy decides whether to speak (returns Trigger / SkipReason).
generator turns a triggering snapshot into a draft message.
"""

from echovessel.proactive.engines.generator import (
    F10Violation,
    GenerationOutcome,
    MessageGenerator,
)
from echovessel.proactive.engines.policy import SHOCK_IMPACT, PolicyEngine

__all__ = [
    "F10Violation",
    "GenerationOutcome",
    "MessageGenerator",
    "PolicyEngine",
    "SHOCK_IMPACT",
]

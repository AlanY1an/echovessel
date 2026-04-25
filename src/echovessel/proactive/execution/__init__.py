"""Proactive runtime delivery — scheduler, queue, delivery, audit.

scheduler runs the tick loop and the event queue.
delivery routes a generated message to the right channel.
audit records every decision for the admin Cost / Trace tabs.
"""

from echovessel.proactive.execution.audit import JSONLAuditSink
from echovessel.proactive.execution.delivery import (
    DeliveryRouter,
    VoiceBudgetError,
    VoicePermanentError,
    VoiceTransientError,
)
from echovessel.proactive.execution.queue import DEFAULT_MAX_EVENTS, ProactiveEventQueue
from echovessel.proactive.execution.scheduler import DefaultScheduler

__all__ = [
    "DEFAULT_MAX_EVENTS",
    "DefaultScheduler",
    "DeliveryRouter",
    "JSONLAuditSink",
    "ProactiveEventQueue",
    "VoiceBudgetError",
    "VoicePermanentError",
    "VoiceTransientError",
]

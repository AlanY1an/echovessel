"""Proactive runtime delivery — scheduler, queue, delivery, audit.

scheduler runs the tick loop and the event queue.
delivery routes a generated message to the right channel.
audit records every decision into the ``proactive_decisions`` table
for the admin "主动消息历史" tab.
"""

from echovessel.proactive.execution.audit import SQLiteAuditSink
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
    "ProactiveEventQueue",
    "SQLiteAuditSink",
    "VoiceBudgetError",
    "VoicePermanentError",
    "VoiceTransientError",
]

"""Proactive core — Protocols, value types, config, errors.

Foundational types shared across the engines and execution layers.
"""

from echovessel.proactive.core.base import (
    CONFIG_VERSION,
    ActionType,
    AuditSink,
    ChannelProtocol,
    ChannelRegistryApi,
    DeliveryKind,
    EventType,
    MemoryApi,
    MemorySnapshot,
    OutgoingMessageKind,
    PersonaView,
    ProactiveDecision,
    ProactiveEvent,
    ProactiveFn,
    ProactiveMessage,
    ProactiveScheduler,
    SkipReason,
    TriggerReason,
    VoiceServiceProtocol,
)
from echovessel.proactive.core.config import ProactiveConfig
from echovessel.proactive.core.errors import (
    ProactiveError,
    ProactivePermanentError,
    ProactiveTransientError,
)

__all__ = [
    "CONFIG_VERSION",
    "ActionType",
    "AuditSink",
    "ChannelProtocol",
    "ChannelRegistryApi",
    "DeliveryKind",
    "EventType",
    "MemoryApi",
    "MemorySnapshot",
    "OutgoingMessageKind",
    "PersonaView",
    "ProactiveDecision",
    "ProactiveEvent",
    "ProactiveFn",
    "ProactiveMessage",
    "ProactiveScheduler",
    "SkipReason",
    "TriggerReason",
    "VoiceServiceProtocol",
    "ProactiveConfig",
    "ProactiveError",
    "ProactivePermanentError",
    "ProactiveTransientError",
]

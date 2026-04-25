"""Proactive subsystem — persona's autonomous initiative.

Decides when the persona speaks first, what it says, and which channel
carries the message. Runs as an asyncio background task inside the
runtime daemon.

Public API:

    from echovessel.proactive import (
        # Top-level
        ProactiveScheduler,
        build_proactive_scheduler,
        ProactiveConfig,
        # Data types
        ProactiveEvent,
        ProactiveDecision,
        ProactiveMessage,
        MemorySnapshot,
        # Enums
        EventType,
        TriggerReason,
        SkipReason,
        ActionType,
        # Injected Protocols
        MemoryApi,
        ChannelProtocol,
        ChannelRegistryApi,
        VoiceServiceProtocol,
        AuditSink,
        ProactiveFn,
        # Errors
        ProactiveError,
        ProactiveTransientError,
        ProactivePermanentError,
    )

Dependency layering (enforced by import-linter — see pyproject.toml):

    Layer 3: echovessel.proactive  (this module)  ←  echovessel.channels
    Layer 2: echovessel.memory  ←  echovessel.voice
    Layer 1: echovessel.core

Proactive may import:
    - echovessel.memory
    - echovessel.voice
    - echovessel.channels (Protocol only)
    - echovessel.core
Proactive may NOT import:
    - echovessel.runtime   (runtime → proactive, never the reverse)
    - echovessel.prompts   (prompts flow through runtime's make_proactive_fn)

Spec: docs/proactive/01-spec-v0.1.md
"""

from echovessel.proactive.core.base import (
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
from echovessel.proactive.engines.generator import (
    F10Violation,
    GenerationOutcome,
    MessageGenerator,
)
from echovessel.proactive.engines.policy import SHOCK_IMPACT, PolicyEngine
from echovessel.proactive.execution.audit import JSONLAuditSink
from echovessel.proactive.execution.delivery import (
    DeliveryRouter,
    VoiceBudgetError,
    VoicePermanentError,
    VoiceTransientError,
)
from echovessel.proactive.execution.queue import DEFAULT_MAX_EVENTS, ProactiveEventQueue
from echovessel.proactive.execution.scheduler import DefaultScheduler
from echovessel.proactive.factory import build_proactive_scheduler

__all__ = [
    # Top-level
    "ProactiveScheduler",
    "DefaultScheduler",
    "build_proactive_scheduler",
    "ProactiveConfig",
    # Data types
    "ProactiveEvent",
    "ProactiveDecision",
    "ProactiveMessage",
    "MemorySnapshot",
    "GenerationOutcome",
    # Enums
    "EventType",
    "TriggerReason",
    "SkipReason",
    "ActionType",
    "DeliveryKind",
    "OutgoingMessageKind",
    # Injected Protocols
    "MemoryApi",
    "ChannelProtocol",
    "ChannelRegistryApi",
    "VoiceServiceProtocol",
    "PersonaView",
    "AuditSink",
    "ProactiveFn",
    # Engines
    "PolicyEngine",
    "MessageGenerator",
    "DeliveryRouter",
    "ProactiveEventQueue",
    "JSONLAuditSink",
    # Constants
    "SHOCK_IMPACT",
    "DEFAULT_MAX_EVENTS",
    # Errors
    "ProactiveError",
    "ProactiveTransientError",
    "ProactivePermanentError",
    "F10Violation",
    # Voice errors (re-exported for tests and for runtime round 2 to
    # catch from the unified namespace)
    "VoiceTransientError",
    "VoicePermanentError",
    "VoiceBudgetError",
]

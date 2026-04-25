"""Error hierarchy for the proactive subsystem.

Mirrors the runtime/llm/voice error taxonomy: one abstract root, two
concrete subclasses for transient vs permanent failures. Proactive is
single-shot by design (§16) — it never retries — so ``ProactiveTransientError``
is informational, not a retry signal.
"""

from __future__ import annotations


class ProactiveError(Exception):
    """Root of all errors raised inside the proactive subsystem."""


class ProactiveTransientError(ProactiveError):
    """Recoverable failure. Proactive still does NOT retry automatically;
    the next tick will reevaluate from scratch. The class exists so callers
    can distinguish 'flaky I/O, will probably recover next tick' from
    'something is fundamentally misconfigured'."""


class ProactivePermanentError(ProactiveError):
    """Unrecoverable failure — config invalid, injected dependency missing,
    etc. Raised at construction or during start() when possible."""


__all__ = [
    "ProactiveError",
    "ProactiveTransientError",
    "ProactivePermanentError",
]

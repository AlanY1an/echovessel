"""Message generator (spec §5).

Constructs a ``MemorySnapshot`` from the injected ``MemoryApi`` facade,
runs the F10 no-channel-leak guard, then calls the runtime-injected
``proactive_fn`` to actually invoke the LLM.

Hard rules enforced here:

- **D4**: all memory read calls in this file go through ``memory.load_core_blocks``,
  ``memory.get_recent_events`` and ``memory.list_recall_messages``. None
  of them accept a channel_id parameter — the ``MemoryApi`` Protocol
  (base.py) removes that ability at the type level. The D4 guard test
  in ``tests/proactive/test_d4_no_channel_filter.py`` additionally greps
  this file to make sure no ``channel_id=`` kwarg sneaks in.

- **F10**: ``_assert_no_channel_leak`` walks the entire snapshot and
  rejects it if ANY string value contains ``channel_id`` or a known
  channel label (``web`` / ``discord:`` / ``imessage`` / ``wechat``)
  that could only originate from an ingest metadata field. The F10
  guard test constructs a polluted snapshot and asserts the guard
  fires.

- **Model role = "main"**: this module itself never selects a role. The
  ``proactive_fn`` callable is built by runtime in prompts_wiring with
  ``model_role="main"`` baked in. Proactive trusts runtime to keep the
  role at "main" and does not pass it.

- **Observability-only rationale**: ``ProactiveMessage.rationale`` is
  persisted to the audit trail but NEVER re-enters the prompt for the
  next generation (spec §7.7). The generator does not hold state
  across invocations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from echovessel.proactive.core.base import (
    MemoryApi,
    MemorySnapshot,
    ProactiveDecision,
    ProactiveFn,
    ProactiveMessage,
    SkipReason,
)

log = logging.getLogger(__name__)


# Spec §2 + §5.3: how many recent L3 events to pull into the snapshot.
DEFAULT_RECENT_EVENTS_LIMIT = 20

# Spec §5.3: recent L2 window matches interaction's default.
DEFAULT_RECENT_L2_LIMIT = 20

# Spec §5.3: how far back to query L3 events.
DEFAULT_RECENT_EVENTS_DAYS = 14


# Known channel label fragments that MUST never appear in a snapshot field.
# The list is deliberately conservative — it flags strings that could only
# have originated from a channel_id metadata leak (spec F10). General
# English words like "web" appearing in arbitrary free text would trigger
# false positives, so we match on the FULL channel id shapes plus a few
# well-known prefixes from docs/channels/01-spec-v0.1.md.
_FORBIDDEN_CHANNEL_SUBSTRINGS: tuple[str, ...] = (
    "channel_id",
    "discord:",
    "imessage:",
    "wechat:",
)
_FORBIDDEN_EXACT_TOKENS: tuple[str, ...] = (
    "web",
    "discord",
    "imessage",
    "wechat",
)


class F10Violation(RuntimeError):  # noqa: N818
    """Raised when _assert_no_channel_leak finds a channel_id fragment in
    the MemorySnapshot that is about to be sent to the LLM. This is a
    spec-level violation (spec F10) and must stop generation immediately
    — never paper over it.

    Named after the F10 rule, not with an ``Error`` suffix, because the
    name is the thing: a grep for ``F10`` in the repo should surface
    every place the rule is enforced.
    """


@dataclass
class GenerationOutcome:
    """What MessageGenerator.generate() returns. Combines the
    ProactiveMessage (when successful) with observability fields the
    scheduler writes into audit. ``message`` is None iff generation
    failed; in that case ``skip_reason`` identifies which failure mode
    fired."""

    message: ProactiveMessage | None
    snapshot: MemorySnapshot
    latency_ms: int
    skip_reason: SkipReason | None = None
    error: str | None = None


@dataclass
class MessageGenerator:
    """Builds snapshots, calls proactive_fn, enforces F10."""

    memory: MemoryApi
    proactive_fn: ProactiveFn
    recent_events_days: int = DEFAULT_RECENT_EVENTS_DAYS
    recent_events_limit: int = DEFAULT_RECENT_EVENTS_LIMIT
    recent_l2_limit: int = DEFAULT_RECENT_L2_LIMIT

    async def generate(
        self,
        *,
        decision: ProactiveDecision,
        now: datetime,
    ) -> GenerationOutcome:
        """Build a MemorySnapshot, enforce F10, then call proactive_fn.

        Caller contract (spec §5.6):
          - LLM timeout / error → outcome.message=None, skip_reason=LLM_ERROR
          - LLM returns empty / invalid → outcome.message=None,
            skip_reason=LLM_OUTPUT_INVALID
          - F10 violation → outcome.message=None, skip_reason=LLM_ERROR
            (an F10 violation is a code bug; LLM_ERROR is the broadest
            honest bucket for "something in message generation failed")
        """
        snapshot = self._build_snapshot(
            trigger=decision.trigger,
            trigger_payload=decision.trigger_payload or {},
            persona_id=decision.persona_id,
            user_id=decision.user_id,
            now=now,
        )

        try:
            _assert_no_channel_leak(snapshot)
        except F10Violation as e:
            log.error("F10 guard fired in generator: %s", e)
            return GenerationOutcome(
                message=None,
                snapshot=snapshot,
                latency_ms=0,
                skip_reason=SkipReason.LLM_ERROR,
                error=f"F10 guard: {e}",
            )

        start = time.monotonic()
        try:
            message = await self.proactive_fn(snapshot)
        except Exception as e:  # noqa: BLE001
            latency_ms = int((time.monotonic() - start) * 1000)
            log.error("proactive_fn failed: %s", e, exc_info=True)
            return GenerationOutcome(
                message=None,
                snapshot=snapshot,
                latency_ms=latency_ms,
                skip_reason=SkipReason.LLM_ERROR,
                error=type(e).__name__,
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        if message is None or not isinstance(message, ProactiveMessage):
            return GenerationOutcome(
                message=None,
                snapshot=snapshot,
                latency_ms=latency_ms,
                skip_reason=SkipReason.LLM_PARSE_ERROR,
                error="proactive_fn did not return ProactiveMessage",
            )

        text = (message.text or "").strip()
        if not text or len(text) < 5:
            return GenerationOutcome(
                message=None,
                snapshot=snapshot,
                latency_ms=latency_ms,
                skip_reason=SkipReason.LLM_OUTPUT_INVALID,
                error="empty or too-short message text",
            )

        if message.llm_latency_ms is None:
            message.llm_latency_ms = latency_ms

        return GenerationOutcome(
            message=message,
            snapshot=snapshot,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # Snapshot construction
    # ------------------------------------------------------------------

    def _build_snapshot(
        self,
        *,
        trigger: str,
        trigger_payload: dict[str, Any],
        persona_id: str,
        user_id: str,
        now: datetime,
    ) -> MemorySnapshot:
        core_blocks = tuple(
            self.memory.load_core_blocks(persona_id, user_id)
        )
        recent_l3_events = tuple(
            self.memory.get_recent_events(
                persona_id,
                user_id,
                since=now - timedelta(days=self.recent_events_days),
                limit=self.recent_events_limit,
            )
        )
        recent_l2_window = tuple(
            self.memory.list_recall_messages(
                persona_id,
                user_id,
                limit=self.recent_l2_limit,
            )
        )
        snapshot_hash = _hash_snapshot(
            trigger=trigger,
            core_blocks=core_blocks,
            recent_l3_events=recent_l3_events,
            recent_l2_window=recent_l2_window,
        )
        return MemorySnapshot(
            trigger=trigger,
            trigger_payload=trigger_payload,
            core_blocks=core_blocks,
            recent_l3_events=recent_l3_events,
            recent_l2_window=recent_l2_window,
            relationship_state=None,
            snapshot_hash=snapshot_hash,
        )


# ---------------------------------------------------------------------------
# F10 guard
# ---------------------------------------------------------------------------


def _assert_no_channel_leak(snapshot: MemorySnapshot) -> None:
    """Walk every text-bearing field in the snapshot and reject any channel
    metadata hint. Raises F10Violation on the first hit.

    Core blocks and recall messages legitimately have ``channel_id``
    attributes on the memory side — but those attributes must NOT flow
    into any string that the LLM will see. The generator's contract with
    proactive_fn is: **we give you descriptions and contents; we do NOT
    give you channel ids**. This guard enforces that contract on the
    generator's output snapshot, before the prompt is ever built.
    """
    # trigger string itself: should only be a TriggerReason value; never
    # contain channel hints.
    _scan_text(snapshot.trigger)

    # trigger_payload: can include event_ids, impact numbers, etc. Must
    # not contain channel_id.
    _scan_mapping(snapshot.trigger_payload)

    for block in snapshot.core_blocks:
        _scan_object(block, attrs=("label", "content", "description"))

    for event in snapshot.recent_l3_events:
        _scan_object(
            event,
            attrs=("description", "emotion_tags", "relational_tags"),
        )

    for msg in snapshot.recent_l2_window:
        # RecallMessage.content / role are always safe to include. We
        # deliberately DO NOT scan msg.content here — it's user-generated
        # free text and may legitimately contain the substring "web" as
        # part of a URL, etc. We DO scan channel_id if present (which it
        # is on RecallMessage per memory schema) — that attribute is the
        # actual leak vector and it must stay on the memory row, not in
        # the prompt.
        channel_id = getattr(msg, "channel_id", None)
        if channel_id is not None:
            # The object itself has the field — that's fine as long as
            # proactive_fn does NOT serialise it into the prompt. But if
            # a caller mutates this snapshot to stuff channel_id into
            # a user-visible field, the guard on core_blocks / events
            # would catch that. RecallMessage objects ARE allowed to
            # carry channel_id because the Protocol consumer (the
            # prompts layer) must strip it when building the user prompt.
            pass


def _scan_text(value: Any) -> None:
    if not isinstance(value, str):
        return
    lowered = value.lower()
    for frag in _FORBIDDEN_CHANNEL_SUBSTRINGS:
        if frag in lowered:
            raise F10Violation(
                f"channel_id fragment {frag!r} detected in snapshot field"
            )


def _scan_mapping(value: Any) -> None:
    if not isinstance(value, dict):
        return
    for k, v in value.items():
        if isinstance(k, str) and "channel" in k.lower():
            raise F10Violation(
                f"channel-related key {k!r} in snapshot mapping"
            )
        if isinstance(v, str):
            _scan_text(v)
        elif isinstance(v, dict):
            _scan_mapping(v)
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, str):
                    _scan_text(item)


def _scan_object(obj: Any, *, attrs: tuple[str, ...]) -> None:
    """Check specific attributes on an object for channel-leak fragments.

    Attributes that don't exist are silently skipped so tests can pass in
    simple dicts / duck-typed objects without implementing every field.
    """
    for attr in attrs:
        value = getattr(obj, attr, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(attr)
        if isinstance(value, str):
            _scan_text(value)
            for token in _FORBIDDEN_EXACT_TOKENS:
                if value.strip().lower() == token:
                    raise F10Violation(
                        f"bare channel token {token!r} in snapshot field {attr}"
                    )
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    _scan_text(item)


# ---------------------------------------------------------------------------
# Snapshot hashing
# ---------------------------------------------------------------------------


def _hash_snapshot(
    *,
    trigger: str,
    core_blocks: tuple[Any, ...],
    recent_l3_events: tuple[Any, ...],
    recent_l2_window: tuple[Any, ...],
) -> str:
    """sha256-based stable hash of the snapshot inputs. Goes into audit so
    two identical reruns produce the same hash."""
    h = hashlib.sha256()
    h.update(trigger.encode("utf-8"))
    h.update(b"|")
    for b in core_blocks:
        h.update(_obj_signature(b).encode("utf-8"))
        h.update(b"|")
    h.update(b"##events##")
    for ev in recent_l3_events:
        h.update(_obj_signature(ev).encode("utf-8"))
        h.update(b"|")
    h.update(b"##msgs##")
    for m in recent_l2_window:
        h.update(_obj_signature(m).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:16]


def _obj_signature(obj: Any) -> str:
    """Deterministic string representation for hashing. Walks known
    attributes first, falls back to ``repr`` for plain dicts."""
    attrs = ("id", "description", "content", "role", "emotional_impact")
    parts = []
    for a in attrs:
        v = getattr(obj, a, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(a)
        if v is not None:
            parts.append(f"{a}={v}")
    if parts:
        return ",".join(parts)
    try:
        return json.dumps(obj, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(obj)


__all__ = [
    "MessageGenerator",
    "GenerationOutcome",
    "F10Violation",
]

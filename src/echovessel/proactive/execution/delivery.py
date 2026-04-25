"""Delivery router (spec §6).

Two responsibilities:

1. **Pick a target channel** — reads recent L2 to find the user's most
   recent active channel, falls back to 'web', finally falls back to
   the first enabled channel that supports outgoing push.

2. **Prepare the voice artifact (v0.2)** — when ``persona.voice_enabled``
   is True AND a voice_service is injected AND the persona has a
   configured voice_id, call ``VoiceService.generate_voice`` (spec
   §4.7a facade), which internally uses the TTS provider and caches
   the mp3 to ``~/.echovessel/voice_cache/<message_id>.mp3``. Proactive
   does NOT call ``speak()`` directly — tracker hard constraint #4
   (preserves R3 layering).

Delivery inheritance rule (spec §6.2a + review Check 3):

    persona.voice_enabled == False  →  delivery = "text"
    persona.voice_enabled == True   →  delivery = "voice_neutral"

Proactive never picks prosody tone variants. Persona-selected delivery
is deferred to v1.0 along with the prosody variants (review R1).

Contract with the scheduler:

- ``pick_channel`` is pure (no I/O other than the channel_registry
  read + the memory L2 read).
- ``prepare_voice`` is async, takes an already-ingested message_id, and
  MUST NOT raise for voice-path failures; it returns a ``VoiceOutcome``
  describing success / graceful downgrade.
- ``pick_channel`` does NOT look at voice; voice decisions happen in
  ``prepare_voice`` so voice failures never affect channel selection.

D4 guard: the L2 read inside ``pick_channel`` calls
``memory.list_recall_messages(persona_id, user_id, limit=...)`` — no
``channel_id=`` kwarg. The entire algorithm classifies messages by their
``channel_id`` attribute as a local, scheduler-side operation; memory
stays unified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from echovessel.proactive.core.base import (
    ChannelProtocol,
    ChannelRegistryApi,
    MemoryApi,
    VoiceServiceProtocol,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Voice error import shim
# ---------------------------------------------------------------------------
#
# Thread VOICE-code may not have landed yet. The spec (§10.4) requires us
# to catch three specific error classes:
#
#     VoiceTransientError, VoicePermanentError, VoiceBudgetError
#
# We attempt to import them from echovessel.voice; if that import fails
# (voice module not yet implemented), we define local sentinel classes so
# the except clause still compiles and tests can still raise a matching
# exception via monkeypatching.
#
# When VOICE-code lands with real error classes in echovessel.voice, this
# shim picks them up automatically at import time and the local sentinels
# become unreachable.

try:  # pragma: no cover - environment-specific
    from echovessel.voice import (  # type: ignore[attr-defined]
        VoiceBudgetError,
        VoicePermanentError,
        VoiceTransientError,
    )
except ImportError:  # pragma: no cover - Voice code not yet landed

    class VoiceTransientError(Exception):  # type: ignore[no-redef]
        """Placeholder until echovessel.voice exposes the real class."""

    class VoicePermanentError(Exception):  # type: ignore[no-redef]
        """Placeholder until echovessel.voice exposes the real class."""

    class VoiceBudgetError(Exception):  # type: ignore[no-redef]
        """Placeholder until echovessel.voice exposes the real class."""


VOICE_ERRORS: tuple[type[Exception], ...] = (
    VoiceTransientError,
    VoicePermanentError,
    VoiceBudgetError,
)


# ---------------------------------------------------------------------------
# Delivery results
# ---------------------------------------------------------------------------


@dataclass
class VoiceOutcome:
    """Result of ``prepare_voice``.

    v0.2 fields (review R1 + Check 3 + M5):

    - ``delivery`` reflects the final OutgoingMessage delivery value
      populated from ``persona.voice_enabled``; it is "text" when voice
      is off or downgraded, "voice_neutral" when generate_voice
      succeeds.
    - ``voice_result`` is the opaque ``VoiceResult`` dataclass returned
      by ``VoiceService.generate_voice()``. It is stored as ``Any``
      here so proactive does not have to import the voice module's
      concrete type (keeps Layer 3 imports minimal).
    - ``voice_used`` is True iff generate_voice ran successfully AND
      returned a usable artifact.
    - ``voice_error`` carries the exception class name on graceful
      downgrade; None otherwise.
    """

    delivery: str  # "text" | "voice_neutral"
    voice_used: bool
    voice_result: Any | None = None
    voice_error: str | None = None


@dataclass
class ChannelPick:
    """Result of pick_channel. ``reason`` is included for audit clarity:
    why did we end up on this channel."""

    channel: ChannelProtocol | None
    reason: str
    candidates_considered: int = 0


# ---------------------------------------------------------------------------
# DeliveryRouter
# ---------------------------------------------------------------------------


@dataclass
class DeliveryRouter:
    memory: MemoryApi
    channel_registry: ChannelRegistryApi
    voice_service: VoiceServiceProtocol | None = None
    default_channel_name: str = "web"
    recent_l2_limit: int = 20

    # ------------------------------------------------------------------
    # Channel picking
    # ------------------------------------------------------------------

    def pick_channel(
        self,
        *,
        persona_id: str,
        user_id: str,
    ) -> ChannelPick:
        """Spec §6.1 algorithm. Returns a ChannelPick — when no channel
        can be found, ``channel`` is None and ``reason`` explains why."""

        enabled = self._pushable_enabled_channels()
        if not enabled:
            return ChannelPick(
                channel=None,
                reason="no_enabled_channel",
                candidates_considered=0,
            )

        # User's most recent active channels (newest-first, de-duplicated)
        recent_active_ids = self._recent_user_channel_ids(
            persona_id, user_id
        )

        for ch_id in recent_active_ids:
            for candidate in enabled:
                if _name_of(candidate) == ch_id:
                    return ChannelPick(
                        channel=candidate,
                        reason="recent_user_activity",
                        candidates_considered=len(enabled),
                    )

        # Fallback: default channel (MVP = 'web')
        for candidate in enabled:
            if _name_of(candidate) == self.default_channel_name:
                return ChannelPick(
                    channel=candidate,
                    reason="default_channel",
                    candidates_considered=len(enabled),
                )

        # Last resort: first enabled pushable
        return ChannelPick(
            channel=enabled[0],
            reason="first_pushable",
            candidates_considered=len(enabled),
        )

    # ------------------------------------------------------------------
    # Voice path (v0.2 — delivery inherits from persona.voice_enabled)
    # ------------------------------------------------------------------

    async def prepare_voice(
        self,
        *,
        text: str,
        message_id: int,
        persona_voice_enabled: bool,
        persona_voice_id: str | None,
    ) -> VoiceOutcome:
        """Voice-path decision and execution for a proactive message.

        Spec refs:
            - §6.2a main switch = persona.voice_enabled
            - §4.7a VoiceService.generate_voice facade (message_id-keyed cache)
            - §6.3 VoiceError graceful downgrade to text

        The decision fans out as follows:

            persona_voice_enabled == False
                → delivery="text"  (review Check 3 — persona said "no voice")
            voice_service is None
                → delivery="text"  (VOICE module not wired up or unavailable)
            persona_voice_id is None
                → delivery="text"  (persona has no cloned voice configured)
            generate_voice raises Voice*Error
                → delivery="text", voice_error=<class name>  (spec §6.3 downgrade)
            generate_voice succeeds
                → delivery="voice_neutral", voice_used=True

        NEVER raises. Voice failures always resolve to a text fallback
        so the caller can still publish the message.

        The method takes ``message_id`` because the v0.2 facade uses it
        as the idempotency key for its on-disk cache (spec §4.7a point
        1). The scheduler ingests the message first, reads the resulting
        L2 row id, then calls this method — maintaining the "先 ingest
        再 send" order invariant from spec §4.5 / §7.4 / §6.2b.
        """
        # 1. Persona toggle — the v0.2 single source of truth (spec §6.2a)
        if not persona_voice_enabled:
            return VoiceOutcome(delivery="text", voice_used=False)

        # 2. Capability: voice module wired?
        if self.voice_service is None:
            return VoiceOutcome(delivery="text", voice_used=False)

        # 3. Capability: persona has a voice id configured?
        if persona_voice_id is None or persona_voice_id == "":
            return VoiceOutcome(delivery="text", voice_used=False)

        # 4. Call the v0.2 facade. MVP tone_hint is always "neutral"
        #    (review R1 defers tender/whisper to v1.0).
        try:
            result = await self.voice_service.generate_voice(
                text=text,
                voice_id=persona_voice_id,
                message_id=message_id,
                tone_hint="neutral",
            )
        except VOICE_ERRORS as e:
            log.warning(
                "generate_voice failed, downgrading to text: %s",
                type(e).__name__,
            )
            return VoiceOutcome(
                delivery="text",
                voice_used=False,
                voice_error=type(e).__name__,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "generate_voice raised unexpected %s, downgrading to text",
                type(e).__name__,
            )
            return VoiceOutcome(
                delivery="text",
                voice_used=False,
                voice_error=type(e).__name__,
            )

        return VoiceOutcome(
            delivery="voice_neutral",
            voice_used=True,
            voice_result=result,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pushable_enabled_channels(self) -> list[ChannelProtocol]:
        """List enabled channels that support outgoing push.

        ``supports_outgoing_push`` is a capability flag that channels
        spec v0.1 does not yet define (see tracker §8.1 #5). We probe
        with ``getattr`` and default to True for backwards compat: the
        only MVP channel is Web, which trivially supports outgoing
        pushes.
        """
        enabled = self.channel_registry.list_enabled()
        return [
            c
            for c in enabled
            if getattr(c, "supports_outgoing_push", True)
        ]

    def _recent_user_channel_ids(
        self, persona_id: str, user_id: str
    ) -> list[str]:
        """Return channel_ids the user has posted on recently,
        newest-first, de-duplicated. Memory read is channel-unaware (D4)."""
        try:
            recent = self.memory.list_recall_messages(
                persona_id,
                user_id,
                limit=self.recent_l2_limit,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "delivery: recent L2 read failed (%s); skipping recent-activity hint",
                e,
            )
            return []

        seen: dict[str, None] = {}
        for msg in recent:
            role = getattr(msg, "role", None)
            role_value = getattr(role, "value", role)
            if role_value != "user":
                continue
            cid = getattr(msg, "channel_id", None)
            if cid is None:
                continue
            if cid not in seen:
                seen[cid] = None
        return list(seen.keys())


def _name_of(channel: ChannelProtocol) -> str:
    name = getattr(channel, "name", None)
    if name is None:
        name = getattr(channel, "channel_id", None)
    return name or ""


__all__ = [
    "DeliveryRouter",
    "ChannelPick",
    "VoiceOutcome",
    "VOICE_ERRORS",
    "VoiceTransientError",
    "VoicePermanentError",
    "VoiceBudgetError",
]

"""Hard-coded TTS cost estimates for `VoiceService.generate_voice()`.

This is the "方案 Z" implementation of review R3.4 (see voice spec
§4.7a · Cost estimation). It deliberately does NOT call any provider
billing API and does NOT read any config file — the whole point is to
give the user a rough order-of-magnitude hint without taking a
dependency on provider dashboards.

The authoritative bill lives on the provider's own dashboard. Web UI
surfaces values derived from this module MUST label them "估算值"
(spec §4.7a + Web channel spec §3.4).

Rates below are documented in the voice spec (docs/voice/01-spec-v0.1.md
§4.7a, dated 2026-04-16). Update both places together when re-checking
FishAudio pricing.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hard-coded per-character USD cost table
# ---------------------------------------------------------------------------


# Source: docs/voice/01-spec-v0.1.md §4.7a · Cost estimation (方案 Z).
# fish.audio TTS list price: $15 per 1M UTF-8 bytes → $0.000015/byte.
# We bill per `len(text)` (code points), which equals bytes for ASCII;
# CJK text is ~3 bytes/char, so the real spend can be up to ~3x this
# estimate — acceptable for an order-of-magnitude hint. Re-check against
# https://fish.audio/ pricing before shipping v1.0.
# Stub is always free. Unknown providers degrade to 0.0 with a warning.
COST_PER_CHAR_USD: dict[str, float] = {
    "fishaudio": 15.0 / 1_000_000,
    "stub": 0.0,
    # "elevenlabs": 0.0003,  # v1.0 reference value — not wired yet.
}


# Single source of truth for the startup disclosure string. The runtime
# may also display this at disclosure time, but VoiceService logs it at
# construction so every process run leaves a breadcrumb in the log.
COST_ESTIMATE_DISCLAIMER = (
    "voice cost estimate is approximate; authoritative billing is in "
    "the provider dashboard"
)


def estimate_tts_cost(provider: str, text: str) -> float:
    """Return the hard-coded USD cost estimate for synthesising `text`.

    Formula: `len(text) * COST_PER_CHAR_USD[provider]`.

    - `provider` is the opaque label returned by `TTSProvider.provider_name`.
    - Unknown providers log a one-time warning (per call-site) and return
      `0.0` so the caller can still assemble a `VoiceResult` without
      crashing. The warning path exists so a future provider drop-in is
      visible in logs instead of silently free.

    Returns `float` to match the spec §4.7a `VoiceResult.cost_usd` field
    type. Precision beyond a handful of significant digits is not
    meaningful because the rates are rough estimates to begin with.
    """
    rate = COST_PER_CHAR_USD.get(provider)
    if rate is None:
        log.warning(
            "voice pricing: unknown TTS provider %r — returning cost_usd=0.0. "
            "Add it to COST_PER_CHAR_USD to track estimates.",
            provider,
        )
        return 0.0
    return float(len(text)) * rate


__all__ = [
    "COST_PER_CHAR_USD",
    "COST_ESTIMATE_DISCLAIMER",
    "estimate_tts_cost",
]

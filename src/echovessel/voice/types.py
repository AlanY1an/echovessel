"""Voice module value objects — v0.2 additions.

Lives alongside `base.py` (which owns the Protocol + pre-existing value
types). This file intentionally hosts only the v0.2 `VoiceResult`
dataclass so base.py stays byte-for-byte identical to round1 and the
non-regression diff is trivial to eyeball.

Spec: docs/voice/01-spec-v0.1.md §4.7a (normative shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VoiceResult:
    """Return shape of `VoiceService.generate_voice()`.

    All fields are load-bearing — the Web channel serializes this
    dataclass into the `chat.message.voice_ready` SSE payload
    (spec §4.7a + Web channel spec §3.4).

    Fields (all required, see spec §4.7a):
        url:              e.g. "/api/chat/voice/<message_id>.mp3"
        cache_path:       absolute filesystem path to the cached mp3.
                          Same file the ``url`` serves over HTTP. Added
                          in Stage 7 so Discord can upload the file as
                          an attachment without re-downloading from a
                          relative URL.
        duration_seconds: best-effort; 0.0 if the provider stream did not
                          expose duration metadata.
        provider:         opaque provider label — "fishaudio" / "stub" /
                          future: "elevenlabs".
        cost_usd:         per-call hard-coded estimate (spec §4.7a
                          Cost estimation, 方案 Z). 0.0 on cache hit.
        cached:           True iff the result was served from the
                          on-disk generate cache without touching the
                          underlying TTS provider.
    """

    url: str
    cache_path: Path
    duration_seconds: float
    provider: str
    cost_usd: float
    cached: bool


__all__ = ["VoiceResult"]

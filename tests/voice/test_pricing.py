"""Tests for `echovessel.voice.pricing` — hard-coded TTS cost estimates."""

from __future__ import annotations

import pytest

from echovessel.voice.pricing import COST_PER_CHAR_USD, estimate_tts_cost

# fish.audio TTS list price: $15 per 1M UTF-8 bytes.
_FISHAUDIO_USD_PER_MILLION_BYTES = 15.0


def test_fishaudio_rate_matches_published_byte_price() -> None:
    assert COST_PER_CHAR_USD["fishaudio"] == pytest.approx(
        _FISHAUDIO_USD_PER_MILLION_BYTES / 1_000_000
    )


def test_fishaudio_estimate_order_of_magnitude() -> None:
    # 1000 ASCII chars == 1000 UTF-8 bytes → $0.015, i.e. cents not dollars.
    cost = estimate_tts_cost("fishaudio", "x" * 1000)
    assert cost == pytest.approx(0.015)
    assert cost < 0.1


def test_stub_is_free() -> None:
    assert estimate_tts_cost("stub", "anything at all") == 0.0


def test_unknown_provider_returns_zero_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="echovessel.voice.pricing"):
        cost = estimate_tts_cost("no-such-provider", "text")

    assert cost == 0.0
    assert any("no-such-provider" in record.message for record in caplog.records)

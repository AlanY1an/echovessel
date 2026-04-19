"""Tests for the per-conversation loop rate limiter."""

from __future__ import annotations

import time
from unittest.mock import patch

from echovessel.channels.imessage.rate_limiter import LoopRateLimiter


class TestBasicThreshold:
    def test_under_threshold_not_suppressed(self):
        lim = LoopRateLimiter(window_s=60.0, threshold=5)
        for _ in range(4):
            lim.record_drop("conv-1")
        assert not lim.is_suppressed("conv-1")

    def test_at_threshold_is_suppressed(self):
        lim = LoopRateLimiter(window_s=60.0, threshold=5)
        for _ in range(5):
            lim.record_drop("conv-1")
        assert lim.is_suppressed("conv-1")

    def test_above_threshold_stays_suppressed(self):
        lim = LoopRateLimiter(window_s=60.0, threshold=5)
        for _ in range(12):
            lim.record_drop("conv-1")
        assert lim.is_suppressed("conv-1")

    def test_unknown_conversation_not_suppressed(self):
        lim = LoopRateLimiter()
        assert not lim.is_suppressed("conv-missing")


class TestWindowSliding:
    def test_window_slides_old_events_away(self):
        """Events older than window must not count toward threshold."""
        lim = LoopRateLimiter(window_s=10.0, threshold=3)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            for _ in range(3):
                lim.record_drop("conv-1")
            assert lim.is_suppressed("conv-1")
        # 11 seconds later — all old events outside window.
        with patch.object(time, "monotonic", return_value=t0 + 11.0):
            assert not lim.is_suppressed("conv-1")

    def test_partial_window_slide(self):
        """Only events that fall off the left should stop counting."""
        lim = LoopRateLimiter(window_s=10.0, threshold=3)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            lim.record_drop("conv-1")  # at t0
            lim.record_drop("conv-1")  # at t0
        with patch.object(time, "monotonic", return_value=t0 + 5.0):
            lim.record_drop("conv-1")  # at t0+5
        # At t0+11, first two events expired (11 > 10) → only 1 event
        # remains, under the threshold of 3.
        with patch.object(time, "monotonic", return_value=t0 + 11.0):
            assert not lim.is_suppressed("conv-1")
        # At t0+12, need to record three more to re-trip.
        with patch.object(time, "monotonic", return_value=t0 + 12.0):
            lim.record_drop("conv-1")
            lim.record_drop("conv-1")
            assert lim.is_suppressed("conv-1")


class TestIsolation:
    def test_conversations_are_independent(self):
        """conv-A tripping the limit must NOT suppress conv-B."""
        lim = LoopRateLimiter(window_s=60.0, threshold=3)
        for _ in range(5):
            lim.record_drop("conv-A")
        lim.record_drop("conv-B")
        assert lim.is_suppressed("conv-A")
        assert not lim.is_suppressed("conv-B")


class TestHousekeeping:
    def test_reset_clears_conversation(self):
        lim = LoopRateLimiter(threshold=3)
        for _ in range(5):
            lim.record_drop("conv-1")
        assert lim.is_suppressed("conv-1")
        lim.reset("conv-1")
        assert not lim.is_suppressed("conv-1")

    def test_reset_unknown_conversation_is_noop(self):
        lim = LoopRateLimiter()
        lim.reset("never-seen")  # must not raise

    def test_empty_queue_entry_is_cleaned_up_on_check(self):
        """After the window slides, the dict entry should be removed."""
        lim = LoopRateLimiter(window_s=5.0, threshold=3)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            lim.record_drop("conv-1")
        assert "conv-1" in lim._events
        with patch.object(time, "monotonic", return_value=t0 + 10.0):
            assert not lim.is_suppressed("conv-1")
        assert "conv-1" not in lim._events

"""Tests for the dual-TTL echo cache."""

from __future__ import annotations

import time
from unittest.mock import patch

from echovessel.channels.imessage.echo_cache import EchoCache


class TestAddContains:
    def test_text_hit_without_id(self):
        cache = EchoCache(text_ttl_s=10.0)
        cache.add(text="hello")
        assert cache.contains(text="hello")

    def test_id_hit_without_matching_text(self):
        cache = EchoCache(id_ttl_s=10.0)
        cache.add(text="a", message_id="msg-1")
        # Different text, same id → still hit.
        assert cache.contains(text="b", message_id="msg-1")

    def test_text_with_id_both_hit(self):
        cache = EchoCache()
        cache.add(text="hello", message_id="msg-1")
        assert cache.contains(text="hello", message_id="msg-1")

    def test_miss_when_neither_matches(self):
        cache = EchoCache()
        cache.add(text="hello", message_id="msg-1")
        assert not cache.contains(text="different", message_id="msg-2")

    def test_empty_cache_never_hits(self):
        cache = EchoCache()
        assert not cache.contains(text="anything")

    def test_normalization_collapses_whitespace_differences(self):
        """Text window should match regardless of trivial whitespace drift."""
        cache = EchoCache()
        cache.add(text="hello world")
        assert cache.contains(text="hello   world")
        assert cache.contains(text="  hello\tworld  ")


class TestTTLs:
    def test_text_expires_after_text_ttl(self):
        """Text window elapses on its own schedule."""
        cache = EchoCache(text_ttl_s=5.0, id_ttl_s=60.0)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            cache.add(text="hello")
        # Within window
        with patch.object(time, "monotonic", return_value=t0 + 4.0):
            assert cache.contains(text="hello")
        # Past window
        with patch.object(time, "monotonic", return_value=t0 + 6.0):
            assert not cache.contains(text="hello")

    def test_id_expires_after_id_ttl(self):
        cache = EchoCache(text_ttl_s=4.0, id_ttl_s=60.0)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            cache.add(text="x", message_id="id-1")
        # Text expired, id still fresh → still a hit (via id).
        with patch.object(time, "monotonic", return_value=t0 + 10.0):
            assert cache.contains(text="different", message_id="id-1")
        # Both expired.
        with patch.object(time, "monotonic", return_value=t0 + 61.0):
            assert not cache.contains(text="different", message_id="id-1")

    def test_text_and_id_have_independent_clocks(self):
        """Adding a fresh text entry must not extend an older id entry."""
        cache = EchoCache(text_ttl_s=4.0, id_ttl_s=10.0)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            cache.add(text="a", message_id="id-A")
        with patch.object(time, "monotonic", return_value=t0 + 8.0):
            cache.add(text="b")  # no id, but bumps text window
        with patch.object(time, "monotonic", return_value=t0 + 11.0):
            # id-A is 11s old → expired; "b" is 3s old → still in text window.
            assert not cache.contains(text="different", message_id="id-A")
            assert cache.contains(text="b")


class TestEviction:
    def test_evict_removes_stale_entries(self):
        """Underlying dicts shouldn't grow unboundedly."""
        cache = EchoCache(text_ttl_s=1.0, id_ttl_s=1.0)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            cache.add(text="a", message_id="id-A")
            cache.add(text="b", message_id="id-B")
            cache.add(text="c", message_id="id-C")
        assert len(cache._text_expiry) == 3
        assert len(cache._id_expiry) == 3

        with patch.object(time, "monotonic", return_value=t0 + 5.0):
            # Any call runs eviction first.
            cache.contains(text="new")
        assert len(cache._text_expiry) == 0
        assert len(cache._id_expiry) == 0


class TestConcurrentAddScenarios:
    def test_repeated_add_refreshes_entry(self):
        """Sending the same text twice should extend the dedup window."""
        cache = EchoCache(text_ttl_s=4.0)
        t0 = 1000.0
        with patch.object(time, "monotonic", return_value=t0):
            cache.add(text="hello")
        with patch.object(time, "monotonic", return_value=t0 + 3.0):
            cache.add(text="hello")  # refresh
        # Without refresh, hit would have expired at t0+4. With refresh,
        # it lives until t0+7.
        with patch.object(time, "monotonic", return_value=t0 + 6.0):
            assert cache.contains(text="hello")

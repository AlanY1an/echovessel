"""Tests for handle normalization rules."""

from __future__ import annotations

import pytest

from echovessel.channels.imessage.handles import normalize_handle


class TestPhonePaths:
    def test_us_bare_digits_get_plus_one(self):
        assert normalize_handle("4155551234", region="US") == "+14155551234"

    def test_cn_bare_digits_get_plus_86(self):
        assert normalize_handle("13477161153", region="CN") == "+8613477161153"

    def test_plus_form_is_preserved(self):
        assert normalize_handle("+14155551234") == "+14155551234"

    def test_e164_passthrough_ignores_region(self):
        # Already E.164 — region hint must not re-prepend.
        assert normalize_handle("+8613477161153", region="US") == "+8613477161153"

    def test_strips_parentheses_spaces_and_dashes(self):
        assert normalize_handle("+1 (415) 555-1234") == "+14155551234"

    def test_strips_dots_and_slashes(self):
        assert normalize_handle("415.555.1234", region="US") == "+14155551234"
        assert normalize_handle("415/555/1234", region="US") == "+14155551234"

    def test_unknown_region_defaults_to_us(self):
        assert normalize_handle("4155551234", region="XX") == "+14155551234"


class TestEmailPaths:
    def test_lowercases_email(self):
        assert normalize_handle("Alice@Example.COM") == "alice@example.com"

    def test_strips_internal_spaces(self):
        assert normalize_handle("alice @ example.com") == "alice@example.com"

    def test_strips_whitespace_around_email(self):
        assert normalize_handle("  bob@host.io  ") == "bob@host.io"


class TestServicePrefixes:
    def test_strips_imessage_prefix(self):
        assert normalize_handle("imessage:+14155551234") == "+14155551234"

    def test_strips_sms_prefix(self):
        assert normalize_handle("sms:+14155551234") == "+14155551234"

    def test_strips_auto_prefix(self):
        assert normalize_handle("auto:+14155551234") == "+14155551234"

    def test_strips_rcs_prefix(self):
        assert normalize_handle("rcs:+14155551234") == "+14155551234"

    def test_prefix_case_insensitive(self):
        assert normalize_handle("iMessage:+14155551234") == "+14155551234"
        assert normalize_handle("SMS:+14155551234") == "+14155551234"

    def test_prefix_then_email(self):
        assert normalize_handle("imessage:Alice@Example.COM") == "alice@example.com"


class TestStructuredTargets:
    def test_chat_id_passes_through(self):
        assert normalize_handle("chat_id:42") == "chat_id:42"

    def test_chat_guid_passes_through(self):
        assert normalize_handle("chat_guid:iMessage;+;chat1") == "chat_guid:iMessage;+;chat1"

    def test_chat_identifier_passes_through(self):
        assert normalize_handle("chat_identifier:+14155551234") == "chat_identifier:+14155551234"

    def test_structured_prefix_lowercased_but_id_preserved(self):
        assert normalize_handle("CHAT_ID:42") == "chat_id:42"


class TestEdgeCases:
    @pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
    def test_empty_returns_empty(self, raw):
        assert normalize_handle(raw) == ""

    def test_service_prefix_only_returns_empty(self):
        assert normalize_handle("imessage:") == ""

    def test_malformed_non_email_non_phone_returns_whitespace_stripped(self):
        # We do not silently discard unparseable inputs — return the
        # whitespace-stripped form so allowlist matches can still work.
        # Non-ASCII / letter-bearing strings are NOT phone-like so the
        # phone cleanup (strips dashes etc.) must not fire.
        assert normalize_handle("weird-name") == "weird-name"
        assert normalize_handle("  foo  bar  ") == "foobar"

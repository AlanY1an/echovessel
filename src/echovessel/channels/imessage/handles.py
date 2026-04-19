"""Handle normalization for iMessage identifiers.

iMessage identifies peers by either phone number or email address, and
the raw form can arrive in many shapes:

- ``+1 (415) 555-1234``
- ``14155551234``
- ``imessage:+14155551234``
- ``sms:+14155551234``
- ``Alice@Example.COM``
- ``chat_id:42`` · already a structured target, no normalization needed

All of these must collapse to a canonical form so an allowlist match
and a conversation binding look up the same key regardless of how the
handle arrived from imsg. This is a direct port of the rules in
openclaw's ``extensions/imessage/src/targets.ts``.

Rules:

1. Strip a single-service or service-id prefix (``imessage:``, ``sms:``,
   ``auto:``, ``rcs:``). Case-insensitive.
2. Structured targets (``chat_id:…``, ``chat_guid:…``,
   ``chat_identifier:…``) pass through unchanged aside from lowercasing
   the prefix.
3. Whitespace is stripped for all non-structured inputs.
4. If the remainder contains ``@``, treat as email → lowercase.
5. Otherwise, treat as phone. Remove spaces / dashes / parentheses /
   slashes. If the result is all digits and does NOT start with ``+``,
   prepend ``+`` + default region code. If already starts with ``+``,
   leave as-is.

The default region is passed in by the caller (comes from channel
config · ``region = "US"`` → prefix ``+1`` when no explicit ``+``).
"""

from __future__ import annotations

import re

# Matches ``<prefix>:<rest>`` when <prefix> is a known iMessage service
# tag. Case-insensitive. Structured chat targets are handled separately.
_SERVICE_PREFIX_RE = re.compile(r"^(imessage|sms|auto|rcs):", re.IGNORECASE)

# Structured target prefixes — these remain prefixed after normalization
# so callers can still distinguish them from bare handles.
_STRUCTURED_PREFIXES = ("chat_id:", "chat_guid:", "chat_identifier:")

# Region → country code used when a digit-only phone is supplied without
# an explicit ``+``. Extend as needed; US is MVP default.
_REGION_CODES: dict[str, str] = {
    "US": "1",
    "CA": "1",
    "CN": "86",
    "GB": "44",
    "JP": "81",
}


def normalize_handle(raw: str, *, region: str = "US") -> str:
    """Return the canonical form of an iMessage handle.

    Examples:

        >>> normalize_handle("+1 (415) 555-1234")
        '+14155551234'
        >>> normalize_handle("4155551234", region="US")
        '+14155551234'
        >>> normalize_handle("imessage:Alice@Example.COM")
        'alice@example.com'
        >>> normalize_handle("chat_id:42")
        'chat_id:42'
    """
    if raw is None:
        return ""
    text = raw.strip()
    if not text:
        return ""

    # Structured target — just lowercase the prefix and keep the id.
    lower = text.lower()
    for prefix in _STRUCTURED_PREFIXES:
        if lower.startswith(prefix):
            return prefix + text[len(prefix) :].strip()

    # Service prefix — strip it, then normalize the remainder.
    match = _SERVICE_PREFIX_RE.match(text)
    if match is not None:
        text = text[match.end() :].strip()
        if not text:
            return ""

    # Email path.
    if "@" in text:
        return text.lower().replace(" ", "")

    # Phone path · only if the raw form is plausibly phone-shaped
    # (contains digits and no letters). Applying the phone cleanup to
    # arbitrary text would silently mangle malformed inputs.
    looks_like_phone = any(c.isdigit() for c in text) and not any(c.isalpha() for c in text)
    if looks_like_phone:
        cleaned = re.sub(r"[\s\-\(\)\.\/]", "", text)
        if not cleaned:
            return ""
        if cleaned.startswith("+"):
            digits = re.sub(r"\D", "", cleaned[1:])
            return "+" + digits if digits else ""
        if cleaned.isdigit():
            code = _REGION_CODES.get(region.upper(), _REGION_CODES["US"])
            return "+" + code + cleaned

    # Non-phone, non-email, non-structured — return the whitespace-
    # stripped form so allowlist matches still work on malformed inputs
    # rather than silently becoming empty.
    stripped = "".join(text.split())
    return stripped


__all__ = ["normalize_handle"]

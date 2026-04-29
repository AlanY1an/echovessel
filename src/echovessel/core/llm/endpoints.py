"""Endpoint classification helpers shared between runtime cost code
and admin route validation.

Lives in `core` because both `runtime` and `channels` need it; per the
layered architecture both layers may import downward into `core`.
"""

from __future__ import annotations

from urllib.parse import urlparse

LOCAL_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "::1"}
)


def is_local_base_url(base_url: str | None) -> bool:
    """Return True iff `base_url`'s hostname is a loopback/dev address.

    Used to short-circuit billing (no charge for local inference) and
    PATCH-time env-var validation (local endpoints don't require a key).
    """
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS

"""v0.7 · SESSION_IDLE_MINUTES default + config override."""

from datetime import datetime, timedelta

import pytest

from echovessel.memory.models import Session, SessionStatus
from echovessel.memory.sessions import (
    SESSION_IDLE_MINUTES,
    is_session_stale,
    set_session_idle_minutes,
)


def test_default_idle_is_10_minutes():
    """v3 default is 10 minutes (was 30 in v2)."""
    assert SESSION_IDLE_MINUTES == 10


def test_set_session_idle_minutes_overrides():
    """Runtime can override default via config."""
    original = SESSION_IDLE_MINUTES
    try:
        set_session_idle_minutes(5)

        s = Session(
            id="s1",
            persona_id="p1",
            user_id="u1",
            channel_id="web",
            status=SessionStatus.OPEN,
            last_message_at=datetime.now() - timedelta(minutes=6),
        )
        assert is_session_stale(s, datetime.now()) is True

        set_session_idle_minutes(60)
        assert is_session_stale(s, datetime.now()) is False
    finally:
        set_session_idle_minutes(original)


def test_set_session_idle_minutes_validates_range():
    """Must be 1..1440 (24h max)."""
    original = SESSION_IDLE_MINUTES
    try:
        with pytest.raises(ValueError):
            set_session_idle_minutes(0)
        with pytest.raises(ValueError):
            set_session_idle_minutes(1441)
    finally:
        set_session_idle_minutes(original)
